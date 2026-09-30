"""Shared fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _runs_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's audit trails go to a temporary directory, never /var/log."""
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path_factory.mktemp("runs")))


@pytest.fixture
def no_stage3_vdb(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The binpkg check without a cached stage3: its vdb extraction is recorded,
    not run. Returns the destinations it was asked for."""
    from shidashi import phases

    extracted: list[Path] = []

    def _extract(_tarball: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        extracted.append(dest)

    monkeypatch.setattr(phases, "_seed_tarball", lambda _recipe: Path("/stage3.tar.xz"))
    monkeypatch.setattr(phases, "_extract_vdb", _extract)
    return extracted
