"""An ended image's row while the others still run (story 006, review of 6.5; R1.9).

The child writes ``done``/``failed`` to its status file when its assemble ends, and the
parent's row for it stops its clock there instead of naming the last step for as long
as the slowest image runs.
"""

from __future__ import annotations

import errno
import io
import json
import time
from pathlib import Path

import pytest
from rich.console import Console

from shidashi import cli, parallel


def _status(path: Path, step: str, since: float) -> None:
    path.write_text(json.dumps({"step": step, "since": since}) + "\n")


def test_the_child_writes_done_or_failed_when_its_assemble_ends(tmp_path: Path) -> None:
    ok, bad = tmp_path / "kde.status", tmp_path / "gnome.status"
    with cli._child_result(None, "kde", status_file=ok):
        pass
    with pytest.raises(RuntimeError), cli._child_result(None, "gnome", status_file=bad):
        raise RuntimeError("boom")
    assert json.loads(ok.read_text())["step"] == "done"
    assert json.loads(bad.read_text())["step"] == "failed"


def test_an_unwritable_status_file_does_not_change_the_outcome(tmp_path: Path) -> None:
    status = tmp_path / "missing-dir" / "kde.status"
    with cli._child_result(None, "kde", status_file=status) as record:
        pass
    assert record["exit_code"] == 0
    assert not status.exists()


def test_an_ended_row_stops_its_clock_while_another_image_runs(tmp_path: Path) -> None:
    start = time.time() - 600
    kde, gnome = tmp_path / "kde.status", tmp_path / "gnome.status"
    _status(kde, "done", start + 65)  # ended after 1:05
    _status(gnome, "install", start + 10)
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    with parallel.watch(
        {"kde": kde, "gnome": gnome}, console=console, started={"kde": start, "gnome": start}
    ):
        pass
    lines = out.getvalue().splitlines()
    assert any(line.startswith("kde  done  ") and "1:05" in line for line in lines), lines
    assert any(line.startswith("gnome  install  ") and "10:0" in line for line in lines), lines


def test_a_clock_stepped_back_never_shows_a_negative_time(tmp_path: Path) -> None:
    status = tmp_path / "kde.status"
    _status(status, "seed", time.time())
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    with parallel.watch({"kde": status}, console=console, started={"kde": time.time() + 3600}):
        pass
    assert "-" not in out.getvalue().split("seed", 1)[1], out.getvalue()


def test_a_step_name_with_markup_does_not_break_the_live_table(tmp_path: Path) -> None:
    status = tmp_path / "kde.status"
    _status(status, "[/x]", time.time())
    out = io.StringIO()
    console = Console(file=out, force_terminal=True, width=120, color_system=None)
    with parallel.watch({"kde": status}, console=console, started={"kde": time.time()}):
        pass
    assert "[/x]" in out.getvalue()


def test_an_interrupt_is_never_replaced_by_the_view(tmp_path: Path) -> None:
    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise OSError(errno.EIO, "the terminal is gone")  # SIGHUP: the tty closed

    status = tmp_path / "kde.status"
    console = Console(file=Broken(), force_terminal=False, width=200)
    with (
        pytest.raises(KeyboardInterrupt),
        parallel.watch({"kde": status}, console=console, started={"kde": time.time()}),
    ):
        _status(status, "seed", time.time())  # the final poll has a line to print
        raise KeyboardInterrupt
