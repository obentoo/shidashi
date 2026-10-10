"""``shidashi worker provision`` warns when a replace left the previous identity behind
(story 020, task 1.4; the ``Provisioned.leftover`` follow-up of task 1.3's review).

The previous identity holds the old private key: the operator must be told where it is.
"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import provision
from shidashi.cli import app


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("COLUMNS", "300")
    return tmp_path / "xdg"


def _fake(leftover: Path | None) -> Any:
    def run(name: str, iso: Path, **_kw: object) -> provision.Provisioned:
        return provision.Provisioned(
            name=name, fingerprint="SHA256:abc", iso_path=iso, leftover=leftover
        )

    return run


def test_a_leftover_identity_is_named_in_a_warning(
    tmp_path: Path, xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso = tmp_path / "w.iso"
    iso.write_bytes(b"")
    left = tmp_path / ".lab.old-1234"
    monkeypatch.setattr(provision, "provision", _fake(left))
    result = CliRunner().invoke(app, ["worker", "provision", "lab", "--iso", str(iso)])
    out = result.stdout + (result.stderr or "")
    assert result.exit_code == 0, out
    assert "warning:" in out
    assert str(left) in "".join(out.split())
    assert "private key" in out


def test_no_leftover_no_warning(tmp_path: Path, xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    iso = tmp_path / "w.iso"
    iso.write_bytes(b"")
    monkeypatch.setattr(provision, "provision", _fake(None))
    result = CliRunner().invoke(app, ["worker", "provision", "lab", "--iso", str(iso)])
    assert result.exit_code == 0
    assert "warning:" not in result.stdout + (result.stderr or "")
