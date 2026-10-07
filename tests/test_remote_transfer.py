"""Story 010's transport helpers in shidashi/remote.py: rsync, tree shipping, streaming.

The argv builders are pure. The fidelity tests run the REAL rsync, git and tar
against a fake worker (tests/_fake_worker.py): a fake ``ssh`` that runs the remote
command locally under a temporary root. No network, no real worker.
"""

import io
import shlex
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import remote
from tests._fake_worker import FakeWorker, option_values


@pytest.fixture
def rem(tmp_path: Path) -> Any:
    return remote.Remote(
        name="bentoo-lab",
        address="192.0.2.10",
        key=tmp_path / "id_ed25519",
        known_hosts=tmp_path / "known_hosts",
    )


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    worker = FakeWorker.install(tmp_path / "fw", monkeypatch)
    yield worker
    worker.close()


def _rsh(argv: list[str]) -> str:
    values = option_values(argv, "-e", "--rsh")
    assert len(values) == 1, argv
    return values[0]


def _reaches_address(argv: list[str], spec: str, address: str) -> bool:
    """The worker side of an rsync reaches the registered ADDRESS: either the spec
    names it, or the ssh command sets HostName to it (the spec then names the pin)."""
    host = spec.split("@", 1)[1].split(":", 1)[0]
    return host == address or f"HostName={address}" in shlex.split(_rsh(argv))


# --- ssh_command (1.1) ---------------------------------------------------------------


def test_ssh_command_is_the_pinned_ssh_without_a_destination_or_command(rem: Any) -> None:
    cmd = remote.ssh_command(rem)
    assert cmd[0] == "ssh"
    # the pin, exactly as story 009's ssh_argv carries it
    assert "StrictHostKeyChecking=yes" in cmd
    assert "HostKeyAlias=bentoo-lab" in cmd
    assert f"UserKnownHostsFile={rem.known_hosts}" in cmd
    assert str(rem.key) in cmd
    assert not any(a.lower() == "stricthostkeychecking=no" for a in cmd)
    # rsync appends the host and its own command: neither may be in the prefix
    assert not any(a.startswith("root@") for a in cmd)
    # story 009's prefix, plus the HostName this story adds (a worker without DNS)
    i = cmd.index("HostName=192.0.2.10")
    assert cmd[i - 1] == "-o"
    rest = cmd[: i - 1] + cmd[i + 1 :]
    base = [a for a in remote.ssh_argv(rem, "true") if a not in ("true",)]
    assert rest == [a for a in base if not a.startswith(("root@", "bentoo-lab"))][: len(rest)]


def test_ssh_command_reaches_the_address_and_pins_the_name(rem: Any) -> None:
    cmd = remote.ssh_command(rem)
    assert "HostName=192.0.2.10" in cmd  # where ssh connects
    assert "HostKeyAlias=bentoo-lab" in cmd  # which key it accepts: unchanged


def test_ssh_command_honours_its_timeout(rem: Any) -> None:
    assert "ConnectTimeout=5" in remote.ssh_command(rem, timeout=5)
    assert "ConnectTimeout=10" in remote.ssh_command(rem)


# --- rsync_argv (1.1) ----------------------------------------------------------------


def test_rsync_argv_e_string_survives_a_key_path_with_spaces(tmp_path: Path) -> None:
    """GOTCHA: -e takes ONE string; joining with spaces would split this path."""
    spaced = remote.Remote(
        name="bentoo-lab",
        address="192.0.2.10",
        key=tmp_path / "my keys" / "id ed25519",
        known_hosts=tmp_path / "known hosts",
    )
    argv = remote.rsync_argv(spaced, ["/c/distfiles/"], "/mnt/work/cache/distfiles/", push=True)
    assert shlex.split(_rsh(argv)) == remote.ssh_command(spaced)


def test_rsync_argv_push_is_archive_resumable_and_never_deletes(rem: Any) -> None:
    argv = remote.rsync_argv(rem, ["/c/distfiles/", "/c/ccache/"], "/mnt/work/cache/", push=True)
    assert argv[0] == "rsync"
    assert "-aH" in argv or {"-a", "-H"} <= set(argv)
    assert "--numeric-ids" in argv
    assert "--partial" in argv  # R6.2: an interrupted push resumes
    assert not any(a.startswith("--delete") or a == "--remove-source-files" for a in argv)
    assert not option_values(argv, "--bwlimit")
    # sources in order, then the worker side
    assert argv[-3:-1] == ["/c/distfiles/", "/c/ccache/"]
    spec = argv[-1]
    assert spec.startswith("root@") and spec.endswith(":/mnt/work/cache/")
    assert _reaches_address(argv, spec, "192.0.2.10")
    rsh = shlex.split(_rsh(argv))
    assert "HostKeyAlias=bentoo-lab" in rsh and "StrictHostKeyChecking=yes" in rsh


def test_rsync_argv_pull_puts_the_worker_side_first(rem: Any) -> None:
    argv = remote.rsync_argv(rem, ["/mnt/work/out/iso/kde/"], "/home/u/results/", push=False)
    assert argv[-1] == "/home/u/results/"
    spec = argv[-2]
    assert spec.startswith("root@") and spec.endswith(":/mnt/work/out/iso/kde/")
    assert _reaches_address(argv, spec, "192.0.2.10")
    assert not any(a.startswith("--delete") for a in argv)


def test_rsync_argv_caps_the_rate_with_bwlimit(rem: Any) -> None:
    argv = remote.rsync_argv(rem, ["/c/x/"], "/mnt/work/cache/x/", push=True, bwlimit=500)
    assert option_values(argv, "--bwlimit") == ["500"]


def test_rsync_argv_passes_every_exclude_before_the_paths(rem: Any) -> None:
    argv = remote.rsync_argv(
        rem,
        ["/c/binpkgs/v3/g/"],
        "/mnt/work/cache/binpkgs/v3/g/",
        push=True,
        excludes=("Packages", "__editable__*"),
    )
    assert option_values(argv, "--exclude") == ["Packages", "__editable__*"]
    last_exclude = max(
        i
        for i, a in enumerate(argv)
        if a.startswith("--exclude") or (i > 0 and argv[i - 1] == "--exclude")
    )
    assert last_exclude < argv.index("/c/binpkgs/v3/g/")


def test_rsync_argv_moves_files_with_the_real_rsync_through_the_pinned_ssh(
    fw: FakeWorker, tmp_path: Path
) -> None:
    """Fidelity: the argv works with the real rsync, both ways, and only through
    the pinned transport to the worker's address."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a file").write_text("A")
    (fw.work / "cache" / "x").mkdir(parents=True)
    push = remote.rsync_argv(fw.remote(), [f"{src}/"], "/mnt/work/cache/x/", push=True)
    done = subprocess.run(push, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (fw.work / "cache" / "x" / "a file").read_text() == "A"
    back = tmp_path / "back"
    pull = remote.rsync_argv(fw.remote(), ["/mnt/work/cache/x/"], f"{back}/", push=False)
    done = subprocess.run(pull, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (back / "a file").read_text() == "A"
    fw.assert_pinned()
    assert {c["target"] for c in fw.calls("ssh")} == {fw.address}


# --- put_tree (1.1) ------------------------------------------------------------------


def test_put_tree_ships_the_commit_not_the_working_tree(fw: FakeWorker) -> None:
    fw.dirty()
    dest = f"/mnt/work/src/{fw.head}"
    remote.put_tree(fw.remote(), fw.head, dest, repo=fw.repo, runner=subprocess.Popen)
    shipped = fw.work / "src" / fw.head
    assert (shipped / "pyproject.toml").is_file()
    assert (shipped / "shidashi" / "cli.py").read_text() == "# committed\n"  # not the edit
    assert not (shipped / ".env").exists()  # untracked: never shipped (Q8)
    fw.assert_pinned()


def test_put_tree_keeps_a_hostile_destination_one_path(fw: FakeWorker) -> None:
    dest = "/mnt/work/src/a b;$(touch PWNED)"
    remote.put_tree(fw.remote(), fw.head, dest, repo=fw.repo, runner=subprocess.Popen)
    assert (fw.work / "src" / "a b;$(touch PWNED)" / "pyproject.toml").is_file()
    assert not list(fw.base.rglob("PWNED"))
    assert not list(Path.cwd().glob("PWNED"))


def test_put_tree_raises_sync_error_when_the_remote_tar_fails(fw: FakeWorker) -> None:
    fw.fail(r"tar\b", code=2, stderr="tar: /mnt/work: Read-only file system\n")
    with pytest.raises(remote.SyncError) as err:
        remote.put_tree(
            fw.remote(), fw.head, "/mnt/work/src/x", repo=fw.repo, runner=subprocess.Popen
        )
    assert "Read-only file system" in str(err.value)


def test_put_tree_raises_sync_error_when_git_archive_fails(fw: FakeWorker) -> None:
    with pytest.raises(remote.SyncError):
        remote.put_tree(
            fw.remote(), "0" * 40, "/mnt/work/src/x", repo=fw.repo, runner=subprocess.Popen
        )


# --- stream (1.2) --------------------------------------------------------------------


def test_stream_sends_every_line_to_the_sink_and_returns_the_exit_code(fw: FakeWorker) -> None:
    lines: list[str] = []
    code = remote.stream(
        fw.remote(),
        "printf 'one\\ntwo\\n'; echo oops >&2; exit 3",
        sink=lines.append,
    )
    assert code == 3
    got = [line.rstrip("\n") for line in lines]
    # stdout keeps its order; where stderr lands between its lines is not ordered
    # (two pipes, here and on a real ssh)
    assert [line for line in got if line != "oops"][:2] == ["one", "two"]
    assert "oops" in got  # stderr is merged, not lost
    fw.assert_pinned()


def test_stream_delivers_each_line_as_it_comes(fw: FakeWorker) -> None:
    seen: list[tuple[float, str]] = []
    remote.stream(
        fw.remote(),
        "echo first; sleep 2; echo second",
        sink=lambda line: seen.append((time.monotonic(), line.rstrip("\n"))),
    )
    assert [text for _, text in seen] == ["first", "second"]
    assert seen[1][0] - seen[0][0] > 1.0  # not buffered until the end


class _FakeProc:
    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        text = any(kwargs.get(k) for k in ("text", "universal_newlines", "encoding"))
        data = "line 1\nline 2\nline 3\n"
        self.stdout: Any = io.StringIO(data) if text else io.BytesIO(data.encode())
        self.stderr = None
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = -15 if (self.terminated or self.killed) else 0
        return self.returncode

    def communicate(self, *_a: Any, **_k: Any) -> tuple[Any, Any]:
        return (self.stdout.read(), None)

    def __enter__(self) -> _FakeProc:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.wait()


def test_stream_on_keyboard_interrupt_stops_the_local_ssh_and_reraises(rem: Any) -> None:
    procs: list[_FakeProc] = []

    def runner(argv: list[str], **kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv, **kwargs)
        procs.append(proc)
        return proc

    def sink(_line: str) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        remote.stream(rem, "tail -F /mnt/work/out/jobs/x.log", sink=sink, runner=runner)
    (proc,) = procs
    assert proc.terminated or proc.killed
    # through the pinned transport, the command as the last argument
    assert proc.argv[0] == "ssh" and proc.argv[-1] == "tail -F /mnt/work/out/jobs/x.log"
    assert "HostKeyAlias=bentoo-lab" in proc.argv


# --- stream regressions (tech review of 1.2) -----------------------------------------


def test_stream_survives_bytes_that_are_not_utf8(fw: FakeWorker) -> None:
    lines: list[str] = []
    code = remote.stream(fw.remote(), r"printf 'ok\n\xff latin1 caf\xe9\nend\n'", sink=lines.append)
    assert code == 0
    got = [line.rstrip("\n") for line in lines]
    assert got[0] == "ok" and got[-1] == "end"
    assert "�" in got[1]  # replaced, not fatal


def test_stream_never_forwards_the_callers_stdin(rem: Any) -> None:
    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> _FakeProc:
        seen.update(kwargs)
        return _FakeProc(argv, **kwargs)

    remote.stream(rem, "true", sink=lambda _line: None, runner=runner)
    assert seen["stdin"] is subprocess.DEVNULL


def test_stream_stops_the_local_ssh_when_the_sink_fails(rem: Any) -> None:
    procs: list[_FakeProc] = []

    def runner(argv: list[str], **kwargs: Any) -> _FakeProc:
        procs.append(_FakeProc(argv, **kwargs))
        return procs[-1]

    def sink(_line: str) -> None:
        raise OSError("sink broke")

    with pytest.raises(OSError, match="sink broke"):
        remote.stream(rem, "true", sink=sink, runner=runner)
    assert procs[0].terminated or procs[0].killed
