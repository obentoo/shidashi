"""``build_outcome.recording`` when the build is stopped, and when its outcome cannot be
written (story 030, task 8.1; R3.4, R3.5).

Characterization tests: task 2.1 checked these paths with a throwaway script only, and
``tests/test_build_outcome_recording.py`` stays frozen. A stop is a ``KeyboardInterrupt``
(Ctrl-C) or the ``SystemExit(128 + signal)`` that ``cli._stopped`` turns SIGTERM/SIGHUP
into. A write fails for real: the outcome file's directory does not exist, so the
temporary file beside it cannot be created -- no monkeypatched ``write``.
"""

import json
import signal
from pathlib import Path
from typing import Any

import pytest
import typer

from shidashi import cli
from shidashi.build_outcome import ReleaseError, recording
from shidashi.phases import FactoryError

ARCH, INIT = "v3", "systemd"
MINIMAL = "bentoo-2026.10.25-minimal-systemd-v3.iso"


def outcome_of(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"no outcome file at {path}"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def unwritable(tmp_path: Path) -> Path:
    """An outcome path whose directory is missing: ``write`` raises ``ReleaseError``."""
    return tmp_path / "missing-dir" / "v3-systemd.json"


# --- a failed write never replaces the build's own failure (hostile half first) -----------


def test_a_failed_write_while_the_build_fails_re_raises_the_builds_own_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The build exits 3 (failed images) through ``_build_errors``; the outcome cannot be
    written. What comes out is that very ``typer.Exit`` -- never the ``Exit(1)`` of a
    failed write."""
    path = unwritable(tmp_path)
    build_failure = cli._ImagesFailed(3, ["kde: failed"], ["kde"])
    with (
        pytest.raises(typer.Exit) as raised,
        recording(path, arch=ARCH, init=INIT, output_dir=tmp_path),
        cli._build_errors(),
    ):
        raise build_failure
    assert raised.value.__cause__ is build_failure
    assert raised.value.exit_code == 3
    assert not path.exists()
    err = capsys.readouterr().err
    assert str(path) in err and "cannot write the build outcome" in err


def test_a_failed_write_during_a_phase_failure_keeps_the_phase_error_as_the_cause(
    tmp_path: Path,
) -> None:
    """Same exit code as a failed write (1): only the cause tells them apart."""
    path = unwritable(tmp_path)
    build_failure = FactoryError("emerge failed in phase world")
    with (
        pytest.raises(typer.Exit) as raised,
        recording(path, arch=ARCH, init=INIT, output_dir=tmp_path),
        cli._build_errors(),
    ):
        raise build_failure
    assert raised.value.__cause__ is build_failure
    assert not isinstance(raised.value.__cause__, ReleaseError)


def test_a_failed_write_during_a_ctrl_c_re_raises_the_keyboard_interrupt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = unwritable(tmp_path)
    stop = KeyboardInterrupt()
    with (
        pytest.raises(KeyboardInterrupt) as raised,
        recording(path, arch=ARCH, init=INIT, output_dir=tmp_path),
    ):
        raise stop
    assert raised.value is stop
    assert str(path) in capsys.readouterr().err


def test_a_failed_write_after_a_successful_build_prints_the_error_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asked to record and could not: the release would find no outcome anyway."""
    path = unwritable(tmp_path)
    (tmp_path / MINIMAL).write_bytes(b"iso")
    with (
        pytest.raises(typer.Exit) as raised,
        recording(path, arch=ARCH, init=INIT, output_dir=tmp_path),
    ):
        pass
    assert raised.value.exit_code == 1
    assert isinstance(raised.value.__cause__, ReleaseError)
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert str(raised.value.__cause__) in err
    assert str(path) in err and "cannot write the build outcome" in err


# --- a stopped build (R3.4) ----------------------------------------------------------------


def test_ctrl_c_records_an_error_with_exit_code_130_and_is_re_raised(tmp_path: Path) -> None:
    path = tmp_path / "v3-systemd.json"
    stop = KeyboardInterrupt()
    with (
        pytest.raises(KeyboardInterrupt) as raised,
        recording(path, arch=ARCH, init=INIT, output_dir=tmp_path),
    ):
        raise stop
    assert raised.value is stop
    outcome = outcome_of(path)
    assert (outcome["arch"], outcome["init"]) == (ARCH, INIT)
    assert outcome["status"] == "error"
    assert outcome["exit_code"] == 130
    assert outcome["isos"] == [] and outcome["toolchain"] is None


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
def test_a_signal_turned_into_system_exit_keeps_its_own_code(
    tmp_path: Path, sig: signal.Signals
) -> None:
    """``cli._stopped`` raises ``SystemExit(128 + sig)``: that code is recorded, and the
    same ``SystemExit`` leaves ``recording`` -- the process exits with it."""
    path = tmp_path / "v3-systemd.json"
    stop = SystemExit(128 + sig)
    with (
        pytest.raises(SystemExit) as raised,
        recording(path, arch=ARCH, init=INIT, output_dir=tmp_path),
    ):
        raise stop
    assert raised.value is stop
    assert raised.value.code == 128 + sig
    outcome = outcome_of(path)
    assert outcome["status"] == "error"
    assert outcome["exit_code"] == 128 + sig


def test_a_stop_records_nothing_without_an_outcome_file_and_is_re_raised(
    tmp_path: Path,
) -> None:
    """R3.5: without ``--outcome-file`` the stop is untouched and no file appears."""
    stop = SystemExit(128 + signal.SIGTERM)
    with (
        pytest.raises(SystemExit) as raised,
        recording(None, arch=ARCH, init=INIT, output_dir=tmp_path),
    ):
        raise stop
    assert raised.value is stop
    assert list(tmp_path.iterdir()) == []
