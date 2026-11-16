"""``process_state``: a process or group that ended counts as gone, reaped or not.

The zombie case is made on purpose: a child this test does not wait for yet. It is what
every ended orphan stays forever under an init that never reaps (2026-11-16, ``act``)."""

import os
import signal
import subprocess
import time
from pathlib import Path

from shidashi import process_state


def _until_zombie(pid: int) -> None:
    deadline = time.monotonic() + 10
    while not Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].startswith("Z"):
        assert time.monotonic() < deadline, f"{pid} never ended"
        time.sleep(0.02)


def test_a_running_process_is_running() -> None:
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert process_state.process_running(child.pid)
    finally:
        child.kill()
        child.wait()


def test_an_ended_process_is_not_running_reaped_or_not() -> None:
    child = subprocess.Popen(["true"])
    _until_zombie(child.pid)
    os.kill(child.pid, 0)  # no ProcessLookupError: what the old check took for alive
    assert not process_state.process_running(child.pid)
    child.wait()
    assert not process_state.process_running(child.pid)


def test_a_process_of_another_user_is_running() -> None:
    """PID 1 always exists; as an unprivileged user kill(1, 0) is refused, not absent."""
    assert process_state.process_running(1)


def test_a_group_is_running_until_only_ended_members_are_left() -> None:
    leader = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert process_state.group_running(leader.pid)
        os.killpg(leader.pid, signal.SIGTERM)
        _until_zombie(leader.pid)
        assert not process_state.group_running(leader.pid)
    finally:
        leader.kill()
        leader.wait()


def test_a_group_nobody_is_in_is_not_running() -> None:
    child = subprocess.Popen(["true"], start_new_session=True)
    child.wait()
    assert not process_state.group_running(child.pid)
