"""`worker job`'s exit survives a hung-up terminal at the start and the pull (story 014, R4.11).

R4.11: ``shidashi worker job`` stopped by SIGTERM or SIGHUP leaves the remote job
running and exits 128 + signum. The CLI turns the signal into ``SystemExit(143)`` /
``SystemExit(129)`` raised wherever ``worker.job`` happens to be. When that is the
unit's start or the results' pull, ``worker.job`` prints how to resume (follow, pull,
unlock) on stderr before re-raising. After a SIGHUP the terminal is gone: that write
raises ``OSError`` (``EIO`` on a hung-up tty, ``EPIPE`` on a closed pipe), and it must
not replace the stop -- the lines are dropped and the very exception that stopped the
job propagates.

Hostile halves first, both converses of "the write's error is not the job's error":

- the write's ``OSError`` must not stand in for the stop (nor for any other error);
- the job's OWN ``OSError`` (a failed start or pull) must not be taken for the write's
  and dropped: with a dead terminal it still propagates, the same object.

Then the benign half: with a working stderr the resume lines are still printed.

``worker.job`` runs against tests/_fake_worker.py (the ``fw`` fixture of
tests/test_worker_job.py). The terminal dies inside the faked start or pull, right
before the stop is raised: earlier writes reach a live stderr.
"""

import errno
import io
import sys
from collections.abc import Callable
from typing import Any

import pytest

from shidashi import ownership, worker
from tests._fake_worker import FakeWorker
from tests.test_worker_job import FACTORY, _run_job, fw

__all__ = ["fw"]  # the fixture, reused

JOB = "fac-v3"
STOPS = pytest.mark.parametrize(
    "code", [143, 129], ids=["sigterm", "sighup"]
)  # 128 + SIGTERM, 128 + SIGHUP
DEATHS = pytest.mark.parametrize("dead_errno", [errno.EIO, errno.EPIPE], ids=["eio", "epipe"])


class HungUp(io.TextIOBase):
    """A terminal that is gone: every write raises ``dead_errno``. No descriptor, so it
    cannot be pointed at /dev/null; nothing is buffered, so a flush has nothing to send."""

    def __init__(self, dead_errno: int) -> None:
        self.dead_errno = dead_errno
        self.attempts = 0

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, _s: str) -> int:
        self.attempts += 1
        raise OSError(self.dead_errno, "the terminal is gone")

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


def _hang_up(monkeypatch: pytest.MonkeyPatch, dead_errno: int) -> HungUp:
    dead = HungUp(dead_errno)
    monkeypatch.setattr(sys, "stderr", dead)
    return dead


def _stopping_start(
    monkeypatch: pytest.MonkeyPatch, stop: BaseException, *, dead_errno: int | None
) -> list[HungUp]:
    """The unit starts, then (terminal gone, if ``dead_errno``) ``stop`` is raised."""
    real = worker._start_unit
    dead: list[HungUp] = []

    def start(*a: Any, **k: Any) -> None:
        real(*a, **k)
        if dead_errno is not None:
            dead.append(_hang_up(monkeypatch, dead_errno))
        raise stop

    monkeypatch.setattr(worker, "_start_unit", start)
    return dead


def _stopping_pull(
    monkeypatch: pytest.MonkeyPatch, stop: BaseException, *, dead_errno: int | None
) -> list[HungUp]:
    """The pull begins, then (terminal gone, if ``dead_errno``) ``stop`` is raised."""
    dead: list[HungUp] = []

    def pull(*_a: Any, **_k: Any) -> Any:
        if dead_errno is not None:
            dead.append(_hang_up(monkeypatch, dead_errno))
        raise stop

    monkeypatch.setattr(worker, "pull", pull)
    return dead


def _raised(call: Callable[[], Any]) -> BaseException:
    """What ``call`` raised -- any BaseException, so a substitute is seen, not crashed on."""
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the identity of what escaped is the test
        return exc
    raise AssertionError("worker.job returned instead of raising")


def _same(got: BaseException, want: BaseException) -> None:
    assert got is want, f"{want!r} was replaced by {got!r} (context: {got.__context__!r})"


# --- hostile half 1: the dead terminal's OSError does not replace the stop -----------------


@STOPS
@DEATHS
def test_a_stop_during_the_start_with_a_dead_terminal_propagates_unchanged(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch, code: int, dead_errno: int
) -> None:
    fw.set_job(hold_until_release=True)
    stop = SystemExit(code)
    dead = _stopping_start(monkeypatch, stop, dead_errno=dead_errno)
    got = _raised(lambda: _run_job(fw, JOB, FACTORY))
    assert dead and dead[0].attempts >= 1, "the resume lines were never attempted"
    _same(got, stop)
    assert isinstance(got, SystemExit) and got.code == code
    assert ownership.current("v3") is not None  # the unit may run: the lock stays


@STOPS
@DEATHS
def test_a_stop_during_the_pull_with_a_dead_terminal_propagates_unchanged(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch, code: int, dead_errno: int
) -> None:
    stop = SystemExit(code)
    dead = _stopping_pull(monkeypatch, stop, dead_errno=dead_errno)
    got = _raised(lambda: _run_job(fw, JOB, FACTORY))
    assert dead and dead[0].attempts >= 1, "the retry lines were never attempted"
    _same(got, stop)
    assert isinstance(got, SystemExit) and got.code == code
    assert ownership.current("v3") is not None  # not pulled: the lock stays


def test_a_failed_start_with_a_dead_terminal_keeps_its_own_error(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fw.set_job(hold_until_release=True)
    failure = RuntimeError("the start broke")
    _stopping_start(monkeypatch, failure, dead_errno=errno.EIO)
    _same(_raised(lambda: _run_job(fw, JOB, FACTORY)), failure)


# --- hostile half 2: the job's own OSError is not the dead terminal's ----------------------


def test_an_oserror_of_the_start_itself_still_propagates_with_a_dead_terminal(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fw.set_job(hold_until_release=True)
    own = OSError(errno.ENOSPC, "No space left on device")
    _stopping_start(monkeypatch, own, dead_errno=errno.EIO)
    _same(_raised(lambda: _run_job(fw, JOB, FACTORY)), own)


def test_an_oserror_of_the_pull_itself_still_propagates_with_a_dead_terminal(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = OSError(errno.ENOSPC, "No space left on device")
    _stopping_pull(monkeypatch, own, dead_errno=errno.EPIPE)
    _same(_raised(lambda: _run_job(fw, JOB, FACTORY)), own)
    assert ownership.current("v3") is not None


# --- benign half: a working stderr still gets the resume lines ------------------------------


@STOPS
def test_a_stop_during_the_start_with_a_working_terminal_prints_how_to_resume(
    fw: FakeWorker,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    code: int,
) -> None:
    fw.set_job(hold_until_release=True)
    stop = SystemExit(code)
    _stopping_start(monkeypatch, stop, dead_errno=None)
    _same(_raised(lambda: _run_job(fw, JOB, FACTORY)), stop)
    err = capfd.readouterr().err
    assert f"shidashi worker logs bentoo-lab {JOB} -f" in err, err
    assert f"shidashi worker sync pull bentoo-lab --arch v3 --job {JOB}" in err, err
    assert "shidashi worker unlock v3" in err, err


@STOPS
def test_a_stop_during_the_pull_with_a_working_terminal_prints_the_retry_and_unlock(
    fw: FakeWorker,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    code: int,
) -> None:
    stop = SystemExit(code)
    _stopping_pull(monkeypatch, stop, dead_errno=None)
    _same(_raised(lambda: _run_job(fw, JOB, FACTORY)), stop)
    err = capfd.readouterr().err
    assert f"shidashi worker sync pull bentoo-lab --arch v3 --job {JOB}" in err, err
    assert "shidashi worker unlock v3" in err, err
