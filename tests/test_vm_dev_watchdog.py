"""Tests of shidashi.vm_dev_watchdog -- what a dev session's watchdog does, and when.

It waits for the deadline, ends the session as ``vm stop`` does, and leaves one line in
the expiry log. The waiting is driven by a ``SteppingClock`` -- its ``sleep`` moves the
clock instead of the test -- and the stop by a ``FakeSessionEnd``. Then the real thing:
the module run as a process against a session directory whose QEMU is a stand-in
process, to prove the chain down to the removed directory and the powered-off "VM".
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shidashi import vm, vm_dev_expiry, vm_dev_watchdog
from tests.test_vm_dev_cleanup import _dead_pid, _session

_REPO = Path(__file__).resolve().parents[1]
_NOON = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)


class SteppingClock:
    """A wall clock that a ``sleep`` moves forward; every sleep is kept."""

    def __init__(self, now: datetime = _NOON) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class FakeSessionEnd:
    """Stands in for ``vm stop``: fails with each of ``errors`` in turn, then removes
    the directory. Keeps when it was called."""

    def __init__(self, clock: SteppingClock, *errors: Exception) -> None:
        self.clock = clock
        self.errors = list(errors)
        self.called_at: list[datetime] = []

    def __call__(self, directory: Path) -> None:
        self.called_at.append(self.clock.now)
        if self.errors:
            raise self.errors.pop(0)
        shutil.rmtree(directory)


def _armed(tmp_path: Path, *, hours: float = 0.02, now: datetime = _NOON) -> tuple[Path, str]:
    """A session directory with a deadline ``hours`` past ``now``; it and its token."""
    directory = tmp_path / "vm" / "dev"
    directory.mkdir(parents=True)
    vm_dev_expiry.arm(directory, name="dev", hours=hours, clock=lambda: now, launch=_no_launch)
    expiry = vm_dev_expiry.read_expiry(directory)
    assert expiry is not None
    return directory, expiry.token


def _no_launch(_directory: Path, _name: str, _token: str) -> None:
    """The watchdog is the test itself."""


def _log_lines(directory: Path) -> list[dict[str, object]]:
    log = vm_dev_expiry.expiry_log(directory)
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _watch(directory: Path, token: str, clock: SteppingClock, end: FakeSessionEnd) -> str:
    return vm_dev_watchdog.watch(
        directory, name="dev", token=token, clock=clock, sleep=clock.sleep, end_session=end
    )


# --- the wait ------------------------------------------------------------------------------


def test_it_waits_for_the_deadline_then_ends_the_session_once(tmp_path: Path) -> None:
    directory, token = _armed(tmp_path)  # 0.02 h: 72 s
    clock = SteppingClock()
    end = FakeSessionEnd(clock)
    assert _watch(directory, token, clock, end) == "removed"
    assert clock.slept == [30.0, 30.0, 12.0]  # a look at the session every 30 s
    assert end.called_at == [_NOON + timedelta(seconds=72)]  # never before the deadline
    assert _log_lines(directory) == [
        {
            "event": "vm.dev.expiry",
            "time": "2026-11-15T12:01:12Z",
            "session": "dev",
            "directory": str(directory),
            "deadline": "2026-11-15T12:01:12Z",
            "outcome": "removed",
            "attempts": 1,
            "detail": "the VM was powered off and the session's directory removed",
        }
    ]


def test_a_deadline_already_past_ends_the_session_at_once(tmp_path: Path) -> None:
    directory, token = _armed(tmp_path, now=_NOON - timedelta(hours=3))
    clock = SteppingClock()
    end = FakeSessionEnd(clock)
    assert _watch(directory, token, clock, end) == "removed"
    assert clock.slept == [] and end.called_at == [_NOON]


def test_a_clock_set_forward_is_noticed_at_the_next_look(tmp_path: Path) -> None:
    # the deadline is wall clock: a host that slept through it must not wait 2 h more
    directory, token = _armed(tmp_path, hours=2)
    clock = SteppingClock()
    end = FakeSessionEnd(clock)

    def sleep_through_a_suspend(seconds: float) -> None:
        clock.sleep(seconds)
        clock.now += timedelta(hours=5)

    outcome = vm_dev_watchdog.watch(
        directory,
        name="dev",
        token=token,
        clock=clock,
        sleep=sleep_through_a_suspend,
        end_session=end,
    )
    assert outcome == "removed" and clock.slept == [30.0]


# --- a session that is gone ----------------------------------------------------------------


def test_a_session_removed_before_its_deadline_is_left_alone_in_silence(tmp_path: Path) -> None:
    directory, token = _armed(tmp_path, hours=1)
    clock = SteppingClock()
    end = FakeSessionEnd(clock)

    def removed_meanwhile(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.slept) == 2:
            shutil.rmtree(directory)  # a `vm stop` whose signal never reached us

    outcome = vm_dev_watchdog.watch(
        directory, name="dev", token=token, clock=clock, sleep=removed_meanwhile, end_session=end
    )
    assert outcome == "gone"
    assert clock.slept == [30.0, 30.0]  # gone at the next look, not at the deadline
    assert end.called_at == [] and _log_lines(directory) == []


def test_a_session_started_again_under_the_name_is_not_this_watchdog_s(tmp_path: Path) -> None:
    """Hostile: the deadline of the dead session falls while its successor runs."""
    directory, token = _armed(tmp_path)
    clock = SteppingClock()
    end = FakeSessionEnd(clock)
    # the new start: another token, a deadline that is also past
    vm_dev_expiry.arm(directory, name="dev", hours=0.001, clock=clock, launch=_no_launch)
    clock.now += timedelta(hours=1)
    assert _watch(directory, token, clock, end) == "gone"
    assert end.called_at == [] and directory.is_dir() and _log_lines(directory) == []


def test_a_session_without_a_record_is_gone_from_the_start(tmp_path: Path) -> None:
    clock = SteppingClock()
    end = FakeSessionEnd(clock)
    assert _watch(tmp_path / "vm" / "removed", "token", clock, end) == "gone"
    assert clock.slept == [] and end.called_at == []


# --- a session that will not end -------------------------------------------------------------


def test_a_session_that_will_not_end_is_said_and_tried_again(tmp_path: Path) -> None:
    directory, token = _armed(tmp_path, now=_NOON - timedelta(hours=1))
    clock = SteppingClock()
    stuck = vm.VmError("QEMU (pid 7) is still running: the directory was kept")
    end = FakeSessionEnd(clock, stuck, vm.VmError(str(stuck)))
    assert _watch(directory, token, clock, end) == "removed"
    assert clock.slept == [60.0, 60.0]
    assert len(end.called_at) == 3
    failed, removed = _log_lines(directory)  # the same failure twice is said once
    assert failed["outcome"] == "failed" and failed["attempts"] == 1
    assert failed["error"] == str(stuck)
    assert failed["time"] == "2026-11-15T12:00:00Z"
    assert "trying again every 60 s" in str(failed["detail"])
    assert removed["outcome"] == "removed" and removed["attempts"] == 3
    assert removed["time"] == "2026-11-15T12:02:00Z"


def test_another_failure_is_said_too(tmp_path: Path) -> None:
    directory, token = _armed(tmp_path, now=_NOON - timedelta(hours=1))
    clock = SteppingClock()
    end = FakeSessionEnd(clock, vm.VmError("QEMU is still running"), PermissionError("denied"))
    assert _watch(directory, token, clock, end) == "removed"
    assert [(line["outcome"], line.get("error")) for line in _log_lines(directory)] == [
        ("failed", "QEMU is still running"),
        ("failed", "denied"),
        ("removed", None),
    ]


def test_a_stuck_session_removed_by_someone_else_ends_the_retries(tmp_path: Path) -> None:
    directory, token = _armed(tmp_path, now=_NOON - timedelta(hours=1))
    clock = SteppingClock()
    end = FakeSessionEnd(clock, *[vm.VmError("QEMU is still running")] * 50)

    def removed_meanwhile(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.slept) == 3:
            shutil.rmtree(directory)

    outcome = vm_dev_watchdog.watch(
        directory, name="dev", token=token, clock=clock, sleep=removed_meanwhile, end_session=end
    )
    assert outcome == "gone" and len(end.called_at) == 3
    assert [line["outcome"] for line in _log_lines(directory)] == ["failed"]


def test_a_stop_that_fails_because_the_session_went_under_it_is_no_news(tmp_path: Path) -> None:
    # a `vm stop` and the deadline at the same second: the loser finds nothing to remove
    directory, token = _armed(tmp_path, now=_NOON - timedelta(hours=1))
    clock = SteppingClock()

    def raced(session: Path) -> None:
        shutil.rmtree(session)
        raise FileNotFoundError(2, "No such file or directory", str(session))

    outcome = vm_dev_watchdog.watch(
        directory, name="dev", token=token, clock=clock, sleep=clock.sleep, end_session=raced
    )
    assert outcome == "gone" and clock.slept == [] and _log_lines(directory) == []


def test_a_log_that_cannot_be_written_does_not_keep_the_vm_running(tmp_path: Path) -> None:
    directory, token = _armed(tmp_path, now=_NOON - timedelta(hours=1))
    vm_dev_expiry.expiry_log(directory).mkdir()  # the log's place is taken
    clock = SteppingClock()
    end = FakeSessionEnd(clock, vm.VmError("QEMU is still running"))
    assert _watch(directory, token, clock, end) == "removed"
    assert len(end.called_at) == 2 and not directory.exists()


# --- the real thing ------------------------------------------------------------------------


def _orphan(*argv: str) -> int:
    """The pid of ``argv`` running as nobody's child here, as a daemonized QEMU is: once
    it ends it is gone, with no zombie of ours left to pass for a live process."""
    done = subprocess.run(
        ["sh", "-c", '"$@" >/dev/null 2>&1 & echo $!', "sh", *argv],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(done.stdout)


def _watchdog_argv(directory: Path, token: str, *more: str) -> list[str]:
    argv = [sys.executable, "-m", vm_dev_expiry.WATCHDOG_MODULE, *more]
    return [*argv, "--directory", str(directory), "--name", directory.name, "--token", token]


def _two_hours_ago() -> datetime:
    return datetime.now(UTC) - timedelta(hours=2)


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while Path(f"/proc/{pid}").exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def test_the_real_watchdog_powers_the_vm_off_removes_the_session_and_says_so(
    tmp_path: Path,
) -> None:
    qemu = _orphan("sleep", "300")  # what stands in for the VM: it ends on SIGTERM
    try:
        directory = _session(tmp_path / "vm", "w-dev", dev=True, pid=qemu)
        vm_dev_expiry.arm(directory, name="w-dev", hours=1, clock=_two_hours_ago, launch=_no_launch)
        expiry = vm_dev_expiry.read_expiry(directory)
        assert expiry is not None
        done = subprocess.run(
            _watchdog_argv(directory, expiry.token, "--foreground"),
            env={**os.environ, "PYTHONPATH": str(_REPO)},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        # exit 0, not -SIGTERM: its own stop went through disarm and did not kill it
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
        assert _gone_within(qemu, 5), "the VM outlived its session"
        assert not directory.exists()
        (line,) = _log_lines(directory)
        assert line["session"] == "w-dev" and line["outcome"] == "removed"
        assert line["deadline"] == vm_dev_expiry.utc_stamp(expiry.deadline)
    finally:
        if Path(f"/proc/{qemu}").exists():
            os.kill(qemu, signal.SIGKILL)


def test_the_real_watchdog_detached_ends_a_session_at_its_deadline(tmp_path: Path) -> None:
    # the whole path of `vm start --dev --max-life`: the real launcher, a real deadline
    directory = _session(tmp_path / "vm", "w-dev", dev=True, pid=_dead_pid())
    deadline = vm_dev_expiry.arm(directory, name="w-dev", hours=4 / 3600)  # 4 s from now
    expiry = vm_dev_expiry.read_expiry(directory)
    assert expiry is not None and expiry.pid is not None
    try:
        assert directory.is_dir()  # not before the deadline
        assert _gone_within(expiry.pid, 20), "the watchdog never ended"
        assert not directory.exists()
        (line,) = _log_lines(directory)
        assert line["outcome"] == "removed"
        assert line["deadline"] == vm_dev_expiry.utc_stamp(deadline)
        assert str(line["time"]) >= str(line["deadline"])
    finally:
        vm_dev_expiry.disarm(directory)


def test_a_real_watchdog_started_for_a_session_that_went_exits_quietly(tmp_path: Path) -> None:
    directory = _session(tmp_path / "vm", "w-dev", dev=True, pid=_dead_pid())
    vm_dev_expiry.arm(directory, name="w-dev", hours=1, launch=_no_launch)
    done = subprocess.run(
        _watchdog_argv(directory, "the-token-of-another-start", "--foreground"),
        env={**os.environ, "PYTHONPATH": str(_REPO)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert directory.is_dir() and _log_lines(directory) == []
    recorded = vm_dev_expiry.read_expiry(directory)
    assert recorded is not None and recorded.pid is None  # nothing was claimed


def test_a_watchdog_that_ends_on_an_error_says_so_in_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # its stderr is /dev/null: a traceback there would be a VM left running in silence
    directory, token = _armed(tmp_path, hours=1, now=datetime.now(UTC))

    def broken(*_a: object, **_k: object) -> str:
        raise RuntimeError("a bug")

    monkeypatch.setattr(vm_dev_watchdog, "watch", broken)
    argv = ["--foreground", "--directory", str(directory), "--name", "dev", "--token", token]
    before = signal.getsignal(signal.SIGTERM)  # main() resets it: it is pytest's here
    try:
        assert vm_dev_watchdog.main(argv) == 1
    finally:
        signal.signal(signal.SIGTERM, before)
    (line,) = _log_lines(directory)
    assert line["outcome"] == "crashed" and line["error"] == "RuntimeError: a bug"
    assert line["session"] == "dev"


def test_the_launcher_waits_until_the_watchdog_has_claimed_its_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `vm start` must not go on, nor a `vm stop` come, before the record names the
    # watchdog: here the side of the fork that prints the pid, the other side held back
    reading, writing = os.pipe()
    monkeypatch.setattr(vm_dev_watchdog, "_detach", lambda: (4242, reading))
    ended: list[int] = []
    argv = ["--directory", str(tmp_path), "--name", "dev", "--token", "t"]
    launcher = threading.Thread(target=lambda: ended.append(vm_dev_watchdog.main(argv)))
    launcher.start()
    try:
        launcher.join(timeout=0.5)
        assert launcher.is_alive() and ended == []  # nothing claimed yet: it waits
        assert capsys.readouterr().out == ""
    finally:
        os.close(writing)  # what the process that stays does once its pid is recorded
        launcher.join(timeout=10)
        os.close(reading)
    assert ended == [0]
    assert capsys.readouterr().out == "4242\n"


def test_ending_a_session_is_what_vm_stop_does(tmp_path: Path) -> None:
    qemu = _orphan("sleep", "300")
    try:
        directory = _session(tmp_path / "vm", "w-dev", dev=True, pid=qemu)
        vm_dev_watchdog.end_dev_session(directory)
        assert not directory.exists() and _gone_within(qemu, 5)
    finally:
        if Path(f"/proc/{qemu}").exists():
            os.kill(qemu, signal.SIGKILL)
    with pytest.raises(vm.VmError, match="no readable session.json"):
        vm_dev_watchdog.end_dev_session(directory)
