"""A child assemble unwinds on SIGTERM (story 006, R2.6; tech review of 6.2).

``build`` interrupts a child by SIGTERM to its process group. A boot test's QEMU is
daemonized out of that group, so only the child's own ``finally`` can stop it: SIGTERM
must unwind the run as an exit, not kill it outright.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from shidashi import cli


def test_sigterm_unwinds_the_block_as_exit_143_and_records_it(tmp_path: Path) -> None:
    result = tmp_path / "kde.json"
    unwound = []
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exited, cli._child_result(result, "kde"):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        finally:
            unwound.append(True)
    assert exited.value.code == 143
    assert unwound == [True]  # the finally of the run executed
    assert json.loads(result.read_text())["exit_code"] == 143
    assert signal.getsignal(signal.SIGTERM) is before  # handler restored


def test_an_interrupt_is_recorded_as_130(tmp_path: Path) -> None:
    result = tmp_path / "kde.json"
    with pytest.raises(KeyboardInterrupt), cli._child_result(result, "kde"):
        raise KeyboardInterrupt
    assert json.loads(result.read_text())["exit_code"] == 130


def test_a_failed_write_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "kde.json"
    target.mkdir()  # replace() onto a directory fails
    with pytest.raises(OSError):
        cli._write_result_file(
            target, {"image": "kde", "exit_code": 0, "run_id": None, "isos": [], "duration_s": 1}
        )
    assert [p.name for p in tmp_path.iterdir()] == ["kde.json"]


def test_a_second_sigterm_does_not_cut_the_cleanup(tmp_path: Path) -> None:
    """Review of 6.4: systemd and build may each send SIGTERM; the first one's unwinding
    (a boot test stopping its QEMU) must run to its end."""
    result = tmp_path / "kde.json"
    cleaned = []
    with pytest.raises(SystemExit) as exited, cli._child_result(result, "kde"):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        finally:
            os.kill(os.getpid(), signal.SIGTERM)  # the second one, mid-cleanup
            cleaned.append(True)
    assert exited.value.code == 143
    assert cleaned == [True]
    assert json.loads(result.read_text())["exit_code"] == 143
