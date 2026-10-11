"""The Wi-Fi passphrase read from stdin (story 021, 4.1 review).

A passphrase that is not UTF-8 crashed the command with a traceback quoting one of its
bytes; a line ending in CRLF was refused as "non-printable" with no hint of why.
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shidashi import cli, provision


def _stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    # as CPython opens stdin on POSIX: no newline translation, strict UTF-8
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", newline="\n")
    )


def test_a_crlf_line_is_read_without_its_carriage_return(monkeypatch: pytest.MonkeyPatch) -> None:
    _stdin(monkeypatch, b"pass word\r\n")
    assert cli._wifi_passphrase("Home") == "pass word"


def test_a_passphrase_that_is_not_utf8_is_refused_without_quoting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stdin(monkeypatch, b"caf\xe9secret\n")
    with pytest.raises(provision.ProvisionError) as caught:
        cli._wifi_passphrase("Home")
    assert "secret" not in str(caught.value) and "0xe9" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_command_refuses_it_with_an_error_line_and_no_traceback(tmp_path: Path) -> None:
    iso = tmp_path / "worker.iso"
    iso.write_bytes(b"not an iso")
    env = {
        **os.environ,
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "SHIDASHI_RUNS": str(tmp_path / "runs"),
        "HOME": str(tmp_path),
        "COLUMNS": "200",
    }
    argv = ["worker", "provision", "w1", "--iso", str(iso), "--wifi", "Home"]
    done = subprocess.run(
        [sys.executable, "-c", "from shidashi.cli import app; app(prog_name='shidashi')", *argv],
        input=b"caf\xe9secret\n",
        capture_output=True,
        env=env,
        check=False,
        timeout=60,
    )
    output = done.stdout + done.stderr
    assert done.returncode == 1, output
    assert b"error:" in output
    assert b"Traceback" not in output and b"secret" not in output and b"0xe9" not in output
