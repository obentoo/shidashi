"""``release.run_build`` with a real child process (story 030, task 4.1, test-after).

Kept apart from tests/test_release_core.py: that file is the pre-authored contract and its
assertions are frozen; these tests were written after the implementation, as 4.1 asks.
"""

import contextlib
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shidashi.build_outcome import ReleaseError
from shidashi.release import run_build

SIGNALLED_CHILD = """
import pathlib, signal, sys, time
marks = pathlib.Path(sys.argv[1])
def stopped(signum, frame):
    (marks / "signal").write_text(str(signum))
    sys.exit(0)
signal.signal(signal.SIGTERM, stopped)
signal.signal(signal.SIGINT, stopped)
(marks / "ready").write_text("ready")
time.sleep(60)
"""

#: The release side, in its own process so a signal sent to it never reaches pytest.
RELEASE_SIDE = """
import sys
from shidashi.release import run_build
try:
    code = run_build([sys.executable, "-c", sys.argv[1], sys.argv[2]])
except KeyboardInterrupt:
    sys.exit(42)
sys.exit(code)
"""


@pytest.mark.parametrize("code", [0, 7])
def test_run_build_returns_the_exit_code_of_a_real_child(code: int) -> None:
    """R2.4: the release reads how the child ended, whatever it was."""
    assert run_build([sys.executable, "-c", f"raise SystemExit({code})"]) == code


def test_run_build_of_a_command_that_cannot_start_raises_naming_it(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-python"
    with pytest.raises(ReleaseError) as err:
        run_build([str(missing), "-c", "pass"])
    assert str(missing) in str(err.value)


@pytest.mark.parametrize("fails", [False, True], ids=["ran", "not-started"])
def test_run_build_restores_the_previous_signal_handlers(tmp_path: Path, fails: bool) -> None:
    def mine(signum: int, frame: object) -> None:
        del signum, frame

    before = {s: signal.signal(s, mine) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        argv = [str(tmp_path / "absent")] if fails else [sys.executable, "-c", "pass"]
        with pytest.raises(ReleaseError) if fails else contextlib.nullcontext():
            run_build(argv)
        assert signal.getsignal(signal.SIGINT) is mine
        assert signal.getsignal(signal.SIGTERM) is mine
    finally:
        for s, old in before.items():
            signal.signal(s, old)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_run_build_sends_a_stop_signal_on_to_the_child_and_stops_the_release(
    tmp_path: Path, signum: signal.Signals
) -> None:
    """R9.1: a ``kill`` of the release pid alone reaches the running child build, and the
    release stops (``KeyboardInterrupt``) once the child has exited."""
    if signal.getsignal(signum) is signal.SIG_IGN:
        pytest.skip(f"{signum.name} is ignored here; the release keeps it ignored")
    release = subprocess.Popen(
        [sys.executable, "-P", "-c", RELEASE_SIDE, SIGNALLED_CHILD, str(tmp_path)],
        cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "ready").exists():
            assert release.poll() is None, "the release ended before its child was ready"
            assert time.monotonic() < deadline, "the child never became ready"
            time.sleep(0.05)
        release.send_signal(signum)
        assert release.wait(timeout=30) == 42
        assert (tmp_path / "signal").read_text() == str(int(signum))
    finally:
        if release.poll() is None:
            release.kill()
            release.wait()
