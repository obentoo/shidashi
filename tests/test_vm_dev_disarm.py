"""Whatever removes a dev session ends its ``--max-life`` watchdog -- and nothing else.

``vm stop``, the sweep of a later start and a start under the same name each remove or
replace a session; a watchdog left behind would wake at its deadline for a session that
is gone, or power off the one that took its name. These watchdogs are real processes
(the real module, kept in the foreground so the test can wait for them): what is checked
is that the right one receives SIGTERM, and that a process which is not the watchdog --
the caller itself, a stranger that inherited the pid -- never does.
"""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shidashi import vm, vm_dev, vm_dev_expiry
from shidashi.cli import app
from tests.test_vm_dev_cleanup import _dead_pid, _session
from tests.test_vm_dev_expiry import FakeWatchdogLauncher

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's session root, the one ``vm start --dev`` uses with no scratch named."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    root = tmp_path / "state" / "shidashi" / "vm"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def watchdogs() -> Generator[list[subprocess.Popen[bytes]]]:
    """The real watchdogs a test started, none of them left running after it."""
    running: list[subprocess.Popen[bytes]] = []
    yield running
    for child in running:
        child.kill()
        child.wait()


def _real_watchdog(
    directory: Path, running: list[subprocess.Popen[bytes]], *, hours: float = 1
) -> subprocess.Popen[bytes]:
    """A real watchdog of the session in ``directory``, in the foreground (ours to wait
    for), up: its pid is in the record."""

    def launch(session: Path, name: str, token: str) -> None:
        argv = [sys.executable, "-m", vm_dev_expiry.WATCHDOG_MODULE, "--foreground"]
        argv += ["--directory", str(session), "--name", name, "--token", token]
        env = {**os.environ, "PYTHONPATH": str(_REPO)}
        running.append(subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL))

    vm_dev_expiry.arm(directory, name=directory.name, hours=hours, launch=launch)
    deadline = time.monotonic() + 20
    while (expiry := vm_dev_expiry.read_expiry(directory)) is None or expiry.pid is None:
        assert time.monotonic() < deadline, "the watchdog never claimed its record"
        time.sleep(0.05)
    assert expiry.pid == running[-1].pid
    return running[-1]


def test_vm_stop_ends_the_watchdog_of_the_session_it_removes(
    state: Path, watchdogs: list[subprocess.Popen[bytes]]
) -> None:
    directory = _session(state, "dev", dev=True, pid=_dead_pid())
    watchdog = _real_watchdog(directory, watchdogs)
    result = CliRunner().invoke(app, ["vm", "stop", "--name", "dev"])
    assert result.exit_code == 0, result.output
    assert watchdog.wait(timeout=5) == -signal.SIGTERM
    assert not directory.exists()
    assert not vm_dev_expiry.expiry_log(directory).exists()  # a stop is not an expiry


def test_a_watchdog_started_by_a_caller_that_ignores_sigterm_is_still_ended(
    state: Path, watchdogs: list[subprocess.Popen[bytes]]
) -> None:
    """Hostile: an ignored signal is inherited -- the watchdog would never hear a stop."""
    directory = _session(state, "dev", dev=True, pid=_dead_pid())
    before = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        watchdog = _real_watchdog(directory, watchdogs)
    finally:
        signal.signal(signal.SIGTERM, before)
    vm_dev_expiry.disarm(directory)
    assert watchdog.wait(timeout=5) == -signal.SIGTERM


def test_the_sweep_ends_the_watchdog_of_a_dead_session(
    state: Path, watchdogs: list[subprocess.Popen[bytes]]
) -> None:
    dead = _session(state, "bentoo-build-1-1", dev=True, pid=_dead_pid())
    alive = _session(state, "bentoo-build-2-1", dev=True, pid=os.getpid())
    of_dead = _real_watchdog(dead, watchdogs)
    of_alive = _real_watchdog(alive, watchdogs)
    assert vm_dev.sweep_dead_dev_sessions(state, keep="bentoo-build-9-1") == ["bentoo-build-1-1"]
    assert of_dead.wait(timeout=5) == -signal.SIGTERM
    assert of_alive.poll() is None  # a session whose QEMU runs keeps its deadline
    assert vm_dev_expiry.read_expiry(alive) is not None


def test_a_session_kept_by_the_sweep_keeps_its_watchdog(
    state: Path, watchdogs: list[subprocess.Popen[bytes]]
) -> None:
    # the session being started is `keep`: the sweep is not what replaces it
    mine = _session(state, "dev", dev=True, pid=_dead_pid())
    watchdog = _real_watchdog(mine, watchdogs)
    assert vm_dev.sweep_dead_dev_sessions(state, keep="dev") == []
    assert watchdog.poll() is None


def test_disarm_spares_the_process_that_calls_it(
    state: Path, watchdogs: list[subprocess.Popen[bytes]]
) -> None:
    # the watchdog's own stop goes through discard_dev_session: it must not kill itself
    directory = _session(state, "dev", dev=True, pid=_dead_pid())
    watchdog = _real_watchdog(directory, watchdogs)
    vm_dev_expiry.disarm(directory, own_pid=watchdog.pid)
    time.sleep(0.3)
    assert watchdog.poll() is None
    assert vm_dev_expiry.read_expiry(directory) is not None  # and it keeps its record


def test_disarm_never_signals_a_pid_that_is_no_longer_the_watchdog(tmp_path: Path) -> None:
    """Hostile: the watchdog died and its pid now belongs to something else."""
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        launcher = FakeWatchdogLauncher(pid=bystander.pid)
        vm_dev_expiry.arm(tmp_path, name="dev", hours=1, launch=launcher)
        vm_dev_expiry.disarm(tmp_path)
        time.sleep(0.3)
        assert bystander.poll() is None
        assert vm_dev_expiry.read_expiry(tmp_path) is None  # the deadline goes all the same
    finally:
        bystander.kill()
        bystander.wait()


def test_disarm_never_signals_the_watchdog_of_another_session(
    state: Path, watchdogs: list[subprocess.Popen[bytes]]
) -> None:
    """Hostile: a record names the pid of a real watchdog -- of a session that is not it."""
    other = _real_watchdog(_session(state, "other", dev=True, pid=_dead_pid()), watchdogs)
    directory = _session(state, "dev", dev=True, pid=_dead_pid())
    vm_dev_expiry.arm(directory, name="dev", hours=1, launch=FakeWatchdogLauncher(pid=other.pid))
    vm_dev_expiry.disarm(directory)
    time.sleep(0.3)
    assert other.poll() is None


def test_disarm_of_a_session_without_a_deadline_is_nothing(tmp_path: Path) -> None:
    vm_dev_expiry.disarm(tmp_path)
    vm_dev_expiry.disarm(tmp_path / "removed")
    (tmp_path / "expiry.json").write_text("not json")
    vm_dev_expiry.disarm(tmp_path)
    assert not (tmp_path / "expiry.json").exists()


def test_the_real_launcher_leaves_a_detached_watchdog_a_stop_can_end(tmp_path: Path) -> None:
    # what must outlive `vm start` and an ssh session: a session leader with no file of
    # ours open -- and what `vm stop` must still find: its pid, in the record
    directory = tmp_path / "vm" / "dev"
    directory.mkdir(parents=True)
    vm_dev_expiry.arm(directory, name="dev", hours=1)
    expiry = vm_dev_expiry.read_expiry(directory)
    assert expiry is not None and expiry.pid is not None
    pid = expiry.pid
    try:
        assert os.getsid(pid) == pid != os.getsid(0)
        assert _parent_of(pid) != os.getpid()  # not our child: nothing of ours to reap
        for fd in (0, 1, 2):
            assert os.readlink(f"/proc/{pid}/fd/{fd}") == "/dev/null"
        assert os.readlink(f"/proc/{pid}/cwd") == "/"
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        assert vm_dev_expiry.WATCHDOG_MODULE.encode() in args and expiry.token.encode() in args
    finally:
        vm_dev_expiry.disarm(directory)
    assert _ended(pid)
    assert vm_dev_expiry.read_expiry(directory) is None


def test_the_real_launcher_says_why_a_watchdog_did_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an interpreter that cannot run the module: the start must fail, not go on unlimited
    monkeypatch.setattr(sys, "executable", "/bin/false")
    with pytest.raises(vm.VmError, match=r"watchdog did not start \(exit 1\)"):
        vm_dev_expiry.arm(tmp_path, name="dev", hours=1)
    assert vm_dev_expiry.read_expiry(tmp_path) is None


def _parent_of(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[1])


def _ended(pid: int) -> bool:
    """Whether ``pid`` is gone, or a zombie whoever adopted it has not collected yet."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] == "Z"
    except OSError:
        return True
