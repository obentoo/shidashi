"""Tests of `shidashi vm run` as bentoolkit drives it: --stream, stdin, --timeout 0, and
every way it is stopped -- its timeout, Ctrl+C, SIGTERM and SIGHUP.

bentoolkit ships a package with ``tar | shidashi vm run -- tar -x`` and follows a build
with ``vm run --stream``, so these run the real CLI in a child process: stdin, stdout and
the exit code cross real file descriptors. A fake ``ssh`` first on PATH stands in for
the guest -- it runs the remote command (ssh's last argument) with the local shell.
"""

import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from shidashi import vm, vm_exec

_REPO = Path(__file__).resolve().parents[1]
# sshd runs the remote command in a new session, so its shell leads its process group:
# setsid gives the stand-in the same shape (-w: wait, never fork into the background)
_FAKE_SSH = '#!/bin/sh\nfor last; do :; done\nexec setsid -w /bin/sh -c "$last"\n'
# The same guest behind a client that is a process of its own, as the real ssh is: a
# signal sent to the process group of `vm run` ends this client, never the guest's shell
_FAKE_SSH_CLIENT = '#!/bin/sh\nfor last; do :; done\nsetsid -w /bin/sh -c "$last" <&0 &\nwait $!\n'


@pytest.fixture
def guest(tmp_path: Path) -> Path:
    """A work dir holding a ``vm start``-shaped session named ``t``; returns the work dir."""
    work = tmp_path / "work"
    session = work / "scratch" / "vm" / "t"
    session.mkdir(parents=True)
    (session / "session.json").write_text(
        json.dumps({"iso": "/b.iso", "uefi": False, "cid": 61, "memory": "1G", "cpus": 1})
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(_FAKE_SSH)
    ssh.chmod(0o755)
    return work


def _vm_run(
    work: Path, *args: str, stdin: bytes = b"", timeout: float = 60
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env["PATH"] = f"{work.parent / 'bin'}:{env['PATH']}"
    env["PYTHONPATH"] = str(_REPO)
    argv = [sys.executable, "-c", "from shidashi.cli import app; app()", "vm", "run"]
    return subprocess.run(
        [*argv, "--work-dir", str(work), "--name", "t", *args],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def _tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize("mode", [[], ["--stream"]], ids=["buffered", "stream"])
def test_stdin_reaches_the_guest_command_byte_for_byte(guest: Path, mode: list[str]) -> None:
    # the S2 contract: a package directory piped into `tar -x` lands whole in the guest
    dest = guest.parent / "staged"
    dest.mkdir()
    payload = _tarball({"dev-libs/jsoncpp/jsoncpp-1.9.8.ebuild": b"EAPI=8\n" * 1000})
    done = _vm_run(guest, *mode, "--", f"tar -x -C {dest}", stdin=payload)
    assert done.returncode == 0, done.stderr
    assert (dest / "dev-libs/jsoncpp/jsoncpp-1.9.8.ebuild").read_bytes() == b"EAPI=8\n" * 1000


@pytest.mark.parametrize("mode", [[], ["--stream"]], ids=["buffered", "stream"])
def test_the_guest_exit_code_becomes_ours(guest: Path, mode: list[str]) -> None:
    done = _vm_run(guest, *mode, "--", "echo out; echo err >&2; exit 7")
    assert done.returncode == 7
    assert done.stdout == b"out\n" and done.stderr == b"err\n"


def test_stream_delivers_each_line_while_the_command_still_runs(guest: Path) -> None:
    # a build log is read as it is written: the first line must arrive before the end
    env = dict(os.environ)
    env["PATH"] = f"{guest.parent / 'bin'}:{env['PATH']}"
    env["PYTHONPATH"] = str(_REPO)
    argv = [sys.executable, "-c", "from shidashi.cli import app; app()", "vm", "run"]
    child = subprocess.Popen(
        [*argv, "--work-dir", str(guest), "--name", "t", "--stream", "--"]
        + ["echo first; sleep 5; echo second"],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    try:
        assert child.stdout is not None
        began = time.monotonic()
        first = child.stdout.readline()
        assert first == b"first\n"
        assert time.monotonic() - began < 4, "the first line waited for the command to end"
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("mode", [[], ["--stream"]], ids=["buffered", "stream"])
def test_timeout_zero_means_no_limit(guest: Path, mode: list[str]) -> None:
    done = _vm_run(guest, *mode, "--timeout", "0", "--", "sleep 2; echo done")
    assert done.returncode == 0, done.stderr
    assert done.stdout == b"done\n"


@pytest.mark.parametrize("mode", [[], ["--stream"]], ids=["buffered", "stream"])
def test_a_command_past_its_timeout_is_stopped_with_everything_it_started(
    guest: Path, mode: list[str]
) -> None:
    # a build is make, gcc and friends: the children must stop too, not only the shell
    pidfile = guest.parent / "child.pid"
    began = time.monotonic()
    done = _vm_run(guest, *mode, "--timeout", "1", "--", f"sleep 60 & echo $! > {pidfile}; wait")
    assert done.returncode == vm_exec.TIMEOUT_EXIT == 124
    assert time.monotonic() - began < 20, "the run waited for the guest command to end"
    assert b"timed out after 1 s" in done.stderr
    _gone(int(pidfile.read_text()))


def test_a_negative_timeout_is_refused(guest: Path) -> None:
    done = _vm_run(guest, "--timeout", "-1", "--", "true")
    assert done.returncode == 2  # click's usage error


def _guest_command_running(
    guest: Path, mode: list[str], *, own_group: bool = False
) -> tuple[subprocess.Popen[bytes], int]:
    """A ``vm run --timeout 0`` whose guest command sleeps: it, and the pid of that sleep."""
    pidfile = guest.parent / "guest.pid"
    env = dict(os.environ)
    env["PATH"] = f"{guest.parent / 'bin'}:{env['PATH']}"
    env["PYTHONPATH"] = str(_REPO)
    argv = [sys.executable, "-c", "from shidashi.cli import app; app()", "vm", "run"]
    child = subprocess.Popen(
        [*argv, "--work-dir", str(guest), "--name", "t", *mode, "--timeout", "0", "--"]
        + [f"sleep 60 & echo $! > {pidfile}; wait"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=own_group,
    )
    try:
        deadline = time.monotonic() + 20
        while not (pidfile.is_file() and pidfile.read_text().strip()):
            assert time.monotonic() < deadline, "the guest command never started"
            time.sleep(0.1)
        return child, int(pidfile.read_text())
    except BaseException:
        child.kill()
        child.wait()
        raise


@pytest.mark.parametrize("mode", [[], ["--stream"]], ids=["buffered", "stream"])
def test_ctrl_c_ends_the_guest_command_too(guest: Path, mode: list[str]) -> None:
    # a build followed with --stream and interrupted must not keep running behind us
    child, guest_pid = _guest_command_running(guest, mode)
    try:
        child.send_signal(signal.SIGINT)  # what Ctrl+C sends to the foreground process
        assert child.wait(timeout=10) != 0
        _gone(guest_pid)
    finally:
        child.kill()
        child.wait()


_STOPS = [(signal.SIGTERM, 143), (signal.SIGHUP, 129)]


def _skip_when_ignored_here(signum: signal.Signals) -> None:
    """A child inherits an ignored signal, and ``vm run`` keeps it ignored (``nohup``):
    under a test run that ignores ``signum`` there is no stop to observe."""
    if signal.getsignal(signum) is signal.SIG_IGN:
        pytest.skip(f"{signum.name} is ignored here; vm run keeps it ignored")


@pytest.mark.parametrize("mode", [[], ["--stream"]], ids=["buffered", "stream"])
@pytest.mark.parametrize(("signum", "status"), _STOPS, ids=["SIGTERM", "SIGHUP"])
def test_sigterm_and_sighup_end_the_guest_command_and_exit_128_plus_the_signal(
    guest: Path, mode: list[str], signum: signal.Signals, status: int
) -> None:
    # how a `vm run` on a worker ends when the relay's ssh goes: the default action of
    # both signals would end us where we stand, and the build would run on in the guest
    _skip_when_ignored_here(signum)
    child, guest_pid = _guest_command_running(guest, mode)
    try:
        child.send_signal(signum)
        assert child.wait(timeout=10) == status == 128 + signum
        _gone(guest_pid)
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("mode", [[], ["--stream"]], ids=["buffered", "stream"])
@pytest.mark.parametrize(("signum", "status"), _STOPS, ids=["SIGTERM", "SIGHUP"])
def test_a_signal_to_the_whole_process_group_still_ends_the_guest_command(
    guest: Path, mode: list[str], signum: signal.Signals, status: int
) -> None:
    """Hostile: the relay kills the group -- the local ssh dies with us, twice signalled."""
    _skip_when_ignored_here(signum)
    (guest.parent / "bin" / "ssh").write_text(_FAKE_SSH_CLIENT)
    child, guest_pid = _guest_command_running(guest, mode, own_group=True)
    try:
        os.killpg(child.pid, signum)
        os.killpg(child.pid, signum)  # a hangup is rarely one signal
        assert child.wait(timeout=10) == status
        _gone(guest_pid)
    finally:
        child.kill()
        child.wait()


def _gone(pid: int) -> None:
    """Fail unless ``pid`` ends within 5 s (and never leave it running)."""
    deadline = time.monotonic() + 5
    while _alive(pid):
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            pytest.fail(f"guest process {pid} outlived the stop")
        time.sleep(0.1)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is dead: its parent only has not collected it yet
    stat = Path(f"/proc/{pid}/stat")
    return stat.is_file() and stat.read_text().split(") ", 1)[1][0] != "Z"


def test_stream_records_the_command_and_its_exit_code_in_the_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shidashi import audit

    monkeypatch.setattr(audit, "repo_state", lambda: {})
    monkeypatch.setattr(vm, "ssh_argv", lambda _key, _cid, command, **_k: ["sh", "-c", command])
    session = vm.Session(vm.VmSpec(Path("/b.iso")), tmp_path)
    with audit.run(tmp_path / "runs", command="vm", argv=[]) as trail:
        code = vm_exec.stream(
            session, "exit 3", timeout=None, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )
    assert code == 3
    events = [
        e for e in audit.read_events(trail.path / "events.jsonl") if e["kind"] == "vm.command"
    ]
    assert events[0]["command"] == "exit 3"
    assert events[0]["exit_code"] == 3 and events[0]["stream"] is True
