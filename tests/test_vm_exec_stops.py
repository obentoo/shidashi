"""``vm run`` stopped by a signal, seen from inside the process that runs it.

tests/test_vm_exec.py sends SIGTERM and SIGHUP to a real ``vm run`` and looks at its exit
status and at the guest. What a child process cannot show is checked here, with
``vm_exec`` called in this process: the handlers it finds are the ones it leaves, a
``nohup`` is respected, and a stop that comes in two signals -- or lands while another
stop is being carried out -- still ends the guest command. The guest is a local shell
in a session of its own, as sshd would start it; the signals are real, sent to pytest.
"""

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from shidashi import stop_signals, vm, vm_exec
from tests.test_vm_exec import _gone


@dataclass
class LocalGuest:
    """A session whose ssh is a local shell, and where its command leaves a pid."""

    session: vm.Session
    sleeper: Path

    @property
    def command(self) -> str:
        """A guest command that starts a child, says its pid, and never ends."""
        return f"sleep 60 & echo $! > {self.sleeper}; wait"

    def started(self) -> int:
        """The pid of the command's child, once it is there."""
        deadline = time.monotonic() + 20
        while not (self.sleeper.is_file() and self.sleeper.read_text().strip()):
            assert time.monotonic() < deadline, "the guest command never started"
            time.sleep(0.05)
        return int(self.sleeper.read_text())

    def run(self, *, captured: bool = False, timeout: float | None = None) -> None:
        if captured:
            vm_exec.run_captured(self.session, self.command, timeout=timeout)
        else:
            vm_exec.stream(
                self.session,
                self.command,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )


def _found(signum: int, frame: object) -> None:
    """The handler this test "found" installed: what must be there again afterwards."""
    del signum, frame


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[LocalGuest]:
    """A ``LocalGuest``, with SIGTERM and SIGHUP on ``_found`` and Ctrl+C raising here
    (a test run in the background starts with SIGINT ignored); all back afterwards."""

    def local_shell(_key: Path, _cid: int, command: str, **_k: object) -> list[str]:
        return ["setsid", "-w", "sh", "-c", command]

    monkeypatch.setattr(vm, "ssh_argv", local_shell)
    monkeypatch.setattr(vm_exec, "_pidfile", lambda: str(tmp_path / "run.pid"))
    before = {sig: signal.signal(sig, _found) for sig in stop_signals.STOP_SIGNALS}
    before[signal.SIGINT] = signal.signal(signal.SIGINT, signal.default_int_handler)
    yield LocalGuest(vm.Session(vm.VmSpec(Path("/b.iso")), tmp_path), tmp_path / "sleeper.pid")
    for sig, handler in before.items():
        signal.signal(sig, handler)


def _signalled_once_started(
    guest: LocalGuest, signum: signal.Signals, look: Callable[[], None] = lambda: None
) -> threading.Thread:
    """A thread that sends this process ``signum`` once the guest command runs."""

    def send() -> None:
        guest.started()
        look()
        os.kill(os.getpid(), signum)

    thread = threading.Thread(target=send)
    thread.start()
    return thread


def _both() -> list[object]:
    return [signal.getsignal(sig) for sig in stop_signals.STOP_SIGNALS]


@pytest.mark.parametrize("captured", [False, True], ids=["stream", "buffered"])
@pytest.mark.parametrize("signum", stop_signals.STOP_SIGNALS, ids=["SIGTERM", "SIGHUP"])
def test_a_stopped_run_restores_the_handlers_it_found(
    local: LocalGuest, signum: signal.Signals, captured: bool
) -> None:
    during: list[object] = []
    thread = _signalled_once_started(local, signum, lambda: during.extend(_both()))
    try:
        with pytest.raises(SystemExit) as stopped:
            local.run(captured=captured)
    finally:
        thread.join()
    assert stopped.value.code == 128 + signum
    assert during == [stop_signals.exit_on_stop] * 2  # while the guest command ran
    assert _both() == [_found, _found]
    _gone(local.started())


def test_a_run_that_ends_on_its_own_restores_the_handlers_too(local: LocalGuest) -> None:
    code = vm_exec.stream(
        local.session, "exit 3", timeout=None, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL
    )
    assert code == 3 and _both() == [_found, _found]
    assert vm_exec.run_captured(local.session, "exit 4", timeout=None).exit_code == 4
    assert _both() == [_found, _found]


def test_a_hangup_right_after_ctrl_c_does_not_cut_the_stop_short(
    local: LocalGuest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: Ctrl+C through ``ssh -t``, then the hangup of the session it ends --
    the second signal lands while the guest command is being stopped."""
    stop_the_guest = vm_exec._stop_guest

    def hung_up_first(session: vm.Session, pidfile: str) -> None:
        os.kill(os.getpid(), signal.SIGHUP)
        stop_the_guest(session, pidfile)

    monkeypatch.setattr(vm_exec, "_stop_guest", hung_up_first)
    thread = _signalled_once_started(local, signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):  # the first stop is the one reported
            local.run()
    finally:
        thread.join()
    _gone(local.started())
    assert _both() == [_found, _found]


@pytest.mark.parametrize("captured", [False, True], ids=["stream", "buffered"])
def test_a_stop_landing_while_the_timeout_is_carried_out_still_stops_the_guest(
    local: LocalGuest, monkeypatch: pytest.MonkeyPatch, captured: bool
) -> None:
    """Hostile: the caller gives up at the very moment the command times out -- the
    signal interrupts the timeout's own stop of the guest command."""
    stop_the_guest = vm_exec._stop_guest
    stops: list[str] = []

    def terminated_first(session: vm.Session, pidfile: str) -> None:
        stops.append(pidfile)
        if len(stops) == 1:
            os.kill(os.getpid(), signal.SIGTERM)  # raises here: this stop never happens
        stop_the_guest(session, pidfile)

    monkeypatch.setattr(vm_exec, "_stop_guest", terminated_first)
    with pytest.raises(SystemExit) as stopped:
        local.run(captured=captured, timeout=1)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert len(stops) == 2  # the one that was cut, then the stop's own
    _gone(local.started())
    assert _both() == [_found, _found]


def test_a_hangup_ignored_on_purpose_stays_ignored_while_a_command_runs(
    local: LocalGuest,
) -> None:
    # `nohup shidashi vm run …` asked to outlive the terminal: the guest command runs on
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    # the command hangs us up itself: ignored, it ends on its own and its code is ours
    code = vm_exec.stream(
        local.session,
        f"kill -s HUP {os.getpid()}; exit 5",
        timeout=None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    assert code == 5
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) is _found
