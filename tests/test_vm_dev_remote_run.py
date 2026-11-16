"""``vm run`` and ``vm stop`` for a dev session on a build worker, as bentoolkit calls them.

The real CLI runs in a child process, as in tests/test_vm_exec.py: stdin, stdout, the
exit code and the signals cross real file descriptors and real processes. The worker
is a ``FakeWorkerShell`` whose ssh ends like the real client -- a signal that ends it
leaves the worker's side running -- so a stop only reaches the guest when the relay
carries it there itself.

Two shipped Shidashis stand in for the worker's: the fake one where only the hop matters
(stdin, exit codes, which tree runs), and this checkout's own ``vm run`` where the
contract is the worker's to keep -- a timeout, and a stop that ends the guest command.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import process_state, vm_dev_record
from tests._fake_worker_shell import SHIPPED, FakeWorkerShell

_REPO = Path(__file__).resolve().parents[1]
_APP = [sys.executable, "-c", "from shidashi.cli import app; app()"]


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorkerShell]:
    """A worker with session ``dev`` on it, and the host's record of that session."""
    shell = FakeWorkerShell.install(tmp_path, monkeypatch)
    shell.shipped_tree()
    shell.guest_session("dev")
    vm_dev_record.write_record(
        vm_dev_record.WorkerSession("dev", shell.name, SHIPPED, "2026-11-16T00:00:00Z")
    )
    yield shell
    shell.close()


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(_REPO)}


def _cli(*args: str, stdin: bytes = b"", timeout: float = 90) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*_APP, *args], input=stdin, capture_output=True, env=_env(), timeout=timeout, check=False
    )


def _gone(pid: int, timeout: float = 15.0) -> None:
    """Fail unless process ``pid`` ends within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z"):
            return  # ended, not yet reaped by whoever adopted it
        time.sleep(0.05)
    raise AssertionError(f"process {pid} is still running")


def _wait_file(path: Path, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while not (path.is_file() and path.read_text().strip()):
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)
    return path.read_text().strip()


# --- the hop: what goes in comes out --------------------------------------------------------


@pytest.mark.parametrize("mode", [[], ["--stream"]], ids=["buffered", "stream"])
def test_stdin_reaches_the_command_on_the_worker_byte_for_byte(
    lab: FakeWorkerShell, tmp_path: Path, mode: list[str]
) -> None:
    payload = bytes(range(256)) * 400  # every byte value: no terminal, nothing translated
    dest = tmp_path / "landed"
    done = _cli(
        "vm",
        "run",
        "--name",
        "dev",
        *mode,
        "--timeout",
        "0",
        "--",
        f"cat > {dest}; wc -c < {dest}",
        stdin=payload,
    )
    assert done.returncode == 0, done.stderr
    assert dest.read_bytes() == payload
    assert done.stdout.strip() == b"102400"


def test_the_commands_exit_code_and_both_its_streams_are_ours(lab: FakeWorkerShell) -> None:
    done = _cli("vm", "run", "--name", "dev", "--stream", "--", "echo out; echo err >&2; exit 7")
    assert done.returncode == 7
    assert done.stdout == b"out\n" and done.stderr == b"err\n"


def test_the_command_crosses_two_shells_as_the_line_it_was_given(lab: FakeWorkerShell) -> None:
    # bentoolkit hands one quoted line: `cd DIR && exec env -i 'K=v w' argv...`
    line = "cd /tmp && exec env -i 'GREETING=hello  world' sh -c 'echo \"$GREETING\" $0' \"it's\""
    done = _cli("vm", "run", "--name", "dev", "--stream", "--timeout", "0", "--", line)
    assert done.returncode == 0, done.stderr
    assert done.stdout == b"hello  world it's\n"


def test_the_worker_runs_the_tree_recorded_with_the_session(lab: FakeWorkerShell) -> None:
    _cli("vm", "run", "--name", "dev", "--stream", "--timeout", "0", "--", "true")
    (call,) = lab.shidashi_calls()
    assert call["cwd"] == f"{lab.work}/src/{SHIPPED}"
    assert call["args"] == [
        "vm",
        "run",
        "--name",
        "dev",
        "--timeout",
        "0",
        "--stream",
        "--",
        "true",
    ]
    assert len(lab.worker_commands()) == 1 and lab.rsync_calls() == []  # nothing is shipped


def test_the_default_timeout_and_mode_are_handed_on_unchanged(lab: FakeWorkerShell) -> None:
    _cli("vm", "run", "--name", "dev", "--", "true")
    assert lab.shidashi_calls()[0]["args"] == [
        "vm",
        "run",
        "--name",
        "dev",
        "--timeout",
        "600",
        "--",
        "true",
    ]


def test_output_arrives_while_the_command_still_runs(lab: FakeWorkerShell) -> None:
    child = subprocess.Popen(
        [*_APP, "vm", "run", "--name", "dev", "--stream", "--timeout", "0", "--"]
        + ["echo first; sleep 5; echo second"],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=_env(),
    )
    try:
        assert child.stdout is not None
        began = time.monotonic()
        assert child.stdout.readline() == b"first\n"
        assert time.monotonic() - began < 4.5, "the first line waited for the command to end"
        assert child.stdout.read() == b"second\n"
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()
        child.wait()


def test_a_worker_that_cannot_be_reached_is_sshs_own_255(lab: FakeWorkerShell) -> None:
    lab.unreachable()
    done = _cli("vm", "run", "--name", "dev", "--stream", "--timeout", "0", "--", "true")
    assert done.returncode == 255
    assert b"No route to host" in done.stderr and lab.shidashi_calls() == []


def test_a_session_the_worker_lost_is_said_in_words(lab: FakeWorkerShell) -> None:
    (lab.sessions / "dev" / "session.json").unlink()
    (lab.sessions / "dev").rmdir()
    done = _cli("vm", "run", "--name", "dev", "--stream", "--timeout", "0", "--", "true")
    assert done.returncode == 1
    assert b"VM session dev is no longer on this worker" in done.stderr
    assert b"shidashi vm stop --name dev" in done.stderr
    assert b"No such file" not in done.stderr and lab.shidashi_calls() == []


def test_a_worker_that_was_unpaired_is_an_error_not_a_local_vm(lab: FakeWorkerShell) -> None:
    (Path(os.environ["XDG_DATA_HOME"]) / "shidashi" / "worker" / "workers.json").write_text("{}")
    done = _cli("vm", "run", "--name", "dev", "--", "true")
    assert done.returncode == 1 and b"no paired worker named 'bentoo-lab'" in done.stderr
    assert lab.ssh_calls() == []


def test_a_record_cut_short_is_an_error_not_a_local_vm(lab: FakeWorkerShell) -> None:
    vm_dev_record.record_path("dev").write_text('{"name": "dev", "wor')
    for command in (["vm", "run", "--name", "dev", "--", "true"], ["vm", "stop", "--name", "dev"]):
        done = _cli(*command)
        assert done.returncode == 1, command
        assert b"does not read as the record of a session on a worker" in done.stderr
    assert lab.ssh_calls() == []


def test_a_session_without_a_record_is_a_vm_on_this_host(lab: FakeWorkerShell) -> None:
    done = _cli("vm", "run", "--name", "elsewhere", "--", "true")
    assert done.returncode == 1 and b"no VM session named 'elsewhere'" in done.stderr
    assert lab.ssh_calls() == []


# --- the worker's own vm run: timeout and stop ----------------------------------------------


@pytest.fixture
def real(lab: FakeWorkerShell) -> FakeWorkerShell:
    """The same worker, with this checkout's Shidashi shipped to it."""
    lab.ship_real_shidashi()
    return lab


def test_the_timeout_is_the_workers_and_its_124_comes_through(
    real: FakeWorkerShell, tmp_path: Path
) -> None:
    sleeper = tmp_path / "sleeper.pid"
    began = time.monotonic()
    done = _cli(
        "vm",
        "run",
        "--name",
        "dev",
        "--stream",
        "--timeout",
        "2",
        "--",
        f"sleep 60 & echo $! > {sleeper}; wait",
    )
    assert done.returncode == 124
    assert b"timed out after 2 s" in done.stderr
    assert time.monotonic() - began < 30
    _gone(int(sleeper.read_text()))


def test_the_real_vm_run_passes_stdin_and_exit_codes_through_both_hops(
    real: FakeWorkerShell, tmp_path: Path
) -> None:
    dest = tmp_path / "x"
    done = _cli(
        "vm",
        "run",
        "--name",
        "dev",
        "--stream",
        "--timeout",
        "0",
        "--",
        f"cat > {dest}; cat {dest}; exit 7",
        stdin=b"hello",
    )
    assert (done.returncode, done.stdout) == (7, b"hello")
    hops = [dest for dest, _command in real.ssh_calls()]
    assert hops == ["root@192.0.2.10", "root@vsock/61"]  # the host's, then the worker's


def test_the_workers_side_never_relays_again_whatever_its_own_state_says(
    real: FakeWorkerShell,
) -> None:
    """Hostile: the worker's own state directory holds a record under the same name
    (it was once used as a host). Its ``vm run`` must run the command, not send it on:
    a record pointing back here would send it round and round."""
    state = real.root / "root" / ".local" / "state" / "shidashi" / "vm" / "dev"
    state.mkdir(parents=True)
    hostile = vm_dev_record.WorkerSession("dev", "elsewhere", SHIPPED, "2026-11-16T00:00:00Z")
    (state / "worker.json").write_text(json.dumps(hostile.__dict__))
    done = _cli("vm", "run", "--name", "dev", "--stream", "--timeout", "0", "--", "echo ran")
    assert (done.returncode, done.stdout) == (0, b"ran\n"), done.stderr
    stopped = _cli("vm", "stop", "--name", "dev")
    assert b"no paired worker" not in stopped.stderr


@pytest.mark.parametrize(
    ("signum", "mode"),
    [(signal.SIGTERM, ["--stream"]), (signal.SIGHUP, ["--stream"]), (signal.SIGTERM, [])],
    ids=["SIGTERM", "SIGHUP", "SIGTERM-buffered"],
)
def test_a_signal_to_the_hosts_vm_run_stops_the_command_in_the_guest(
    real: FakeWorkerShell, tmp_path: Path, signum: signal.Signals, mode: list[str]
) -> None:
    """What bentoolkit's cancel must mean: exit 128 + signum, and within seconds neither
    the build in the guest nor the worker's ``vm run`` is left."""
    sleeper = tmp_path / "sleeper.pid"
    child = subprocess.Popen(
        [*_APP, "vm", "run", "--name", "dev", *mode, "--timeout", "0", "--"]
        + [f"sleep 300 & echo $! > {sleeper}; wait"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    try:
        guest_command = int(_wait_file(sleeper))
        (pidfile,) = (real.sessions / "dev").glob("relay-run-*.pid")
        worker_group = int(pidfile.read_text())
        began = time.monotonic()
        child.send_signal(signum)
        assert child.wait(timeout=60) == 128 + signum
        took = time.monotonic() - began
    finally:
        child.kill()
        child.wait()
    assert took < 30
    # all of it already over when the host's vm run returned: nothing to wait for
    _gone(guest_command, timeout=1.0)
    assert not process_state.group_running(worker_group)
    assert not pidfile.exists()


def test_a_stop_the_worker_does_not_answer_is_said(real: FakeWorkerShell, tmp_path: Path) -> None:
    sleeper = tmp_path / "sleeper.pid"
    # a file, not a pipe: what is left on the "worker" keeps a pipe's other end open
    said = tmp_path / "stderr"
    with said.open("wb") as stderr:
        child = subprocess.Popen(
            [*_APP, "vm", "run", "--name", "dev", "--stream", "--timeout", "0", "--"]
            + [f"sleep 300 & echo $! > {sleeper}; wait"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            env=_env(),
        )
        try:
            guest_command = int(_wait_file(sleeper))
            real.unreachable()  # the network went: the stop cannot be carried there
            child.send_signal(signal.SIGTERM)
            assert child.wait(timeout=60) == 143
        finally:
            child.kill()
            child.wait()
            os.kill(int(_wait_file(sleeper)), signal.SIGKILL)  # ends what was left there
    assert b"bentoo-lab did not answer the stop" in said.read_bytes()
    assert guest_command > 0


# --- vm stop --------------------------------------------------------------------------------


def test_vm_stop_ends_the_session_there_and_forgets_it_here(lab: FakeWorkerShell) -> None:
    done = _cli("vm", "stop", "--name", "dev")
    assert done.returncode == 0, done.stderr
    assert done.stdout == b"" and done.stderr == b""
    assert not (lab.sessions / "dev").exists()
    assert vm_dev_record.read_record("dev") is None
    # the record is gone: a second stop is a local one, of a session that is not there
    again = _cli("vm", "stop", "--name", "dev")
    assert again.returncode == 1 and b"no VM session named 'dev'" in again.stderr


def test_vm_stop_of_a_session_that_expired_says_so_and_exits_0(lab: FakeWorkerShell) -> None:
    vm_dev_record.write_record(
        vm_dev_record.WorkerSession(
            "dev", lab.name, SHIPPED, "2026-11-01T00:00:00Z", "2026-11-01T12:00:00Z"
        )
    )
    (lab.sessions / "dev" / "session.json").unlink()
    (lab.sessions / "dev").rmdir()
    done = _cli("vm", "stop", "--name", "dev")
    assert done.returncode == 0
    assert done.stderr == (
        b"VM session dev was no longer on bentoo-lab (its maximum life ended at "
        b"2026-11-01T12:00:00Z): forgotten\n"
    )
    assert vm_dev_record.read_record("dev") is None


def test_vm_stop_of_a_session_gone_before_its_deadline_names_the_other_causes(
    lab: FakeWorkerShell,
) -> None:
    vm_dev_record.write_record(
        vm_dev_record.WorkerSession(
            "dev", lab.name, SHIPPED, "2026-11-01T00:00:00Z", "2999-01-01T00:00:00Z"
        )
    )
    (lab.sessions / "dev" / "session.json").unlink()
    done = _cli("vm", "stop", "--name", "dev")
    assert done.returncode == 0
    assert b"the worker restarted, or a job took its place" in done.stderr


def test_vm_stop_keeps_the_record_when_the_worker_does_not_answer(lab: FakeWorkerShell) -> None:
    lab.unreachable()
    done = _cli("vm", "stop", "--name", "dev")
    assert done.returncode == 1
    assert b"is unreachable" in done.stderr
    assert b"session dev is still recorded on bentoo-lab" in done.stderr
    assert vm_dev_record.read_record("dev") is not None
    lab.unreachable(False)
    assert _cli("vm", "stop", "--name", "dev").returncode == 0


def test_vm_stop_keeps_the_record_when_the_stop_fails_there(lab: FakeWorkerShell) -> None:
    lab.plan("stop", exit=1, stderr="error: QEMU (pid 7) is still running: /s was kept\n")
    done = _cli("vm", "stop", "--name", "dev")
    assert done.returncode == 1 and b"QEMU (pid 7) is still running" in done.stderr
    assert vm_dev_record.read_record("dev") is not None


def test_a_stop_is_an_audited_run_and_a_run_is_not(lab: FakeWorkerShell) -> None:
    runs = Path(os.environ["SHIDASHI_RUNS"])
    _cli("vm", "run", "--name", "dev", "--", "true")
    assert not runs.exists() or list(runs.iterdir()) == []
    _cli("vm", "stop", "--name", "dev")
    (run,) = runs.iterdir()
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    assert events[0]["command"] == "vm-dev-worker-stop"
    (stopped,) = [e for e in events if e["kind"] == "vm.dev.worker.stop"]
    assert (stopped["worker"], stopped["session"], stopped["outcome"]) == (
        "bentoo-lab",
        "dev",
        "stopped",
    )
    assert (events[-1]["kind"], events[-1]["status"]) == ("run.end", "ok")
