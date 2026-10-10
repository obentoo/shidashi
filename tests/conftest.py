"""Shared fixtures."""

import os
from pathlib import Path

import pytest

# Typer decides at IMPORT time to force colored help when it sees one of these
# (a CI runner sets GITHUB_ACTIONS): ANSI codes then split "--format" and the
# help tests fail on the runner only (found by `act -j quality`). Cleared here,
# before any test module imports typer.
for _var in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_var, None)


@pytest.fixture(autouse=True)
def _runs_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's audit trails go to a temporary directory, never /var/log."""
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path_factory.mktemp("runs")))


@pytest.fixture(autouse=True)
def _checkpoints_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assemble's checkpoints stay off unless a test hands in a backend: on a
    btrfs /tmp the real ``btrfs`` command would otherwise run, without root."""
    from shidashi import checkpoint

    monkeypatch.setattr(checkpoint, "filesystem_type", lambda _path: "tmpfs")


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


@pytest.fixture(autouse=True)
def _build_host_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's host check passes: tests describe hosts through doctor.checks'
    probes (tests/test_doctor.py), never through the machine they run on --
    a CI runner has no systemd-nspawn."""
    from shidashi import doctor

    monkeypatch.setattr(doctor, "require_build_host", lambda _work_dir: None)


@pytest.fixture(autouse=True)
def _locks_in_tmp(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that leaves SHIDASHI_CACHE unset takes its owner locks in a temporary
    directory, never in the real /var/cache/shidashi/locks. A test that sets it (at any
    point) gets the real ``<cache>/locks`` under its own cache."""
    from shidashi import ownership

    real = ownership.locks_dir
    spare = tmp_path_factory.mktemp("locks")

    def _locks_dir() -> Path:
        return real() if os.environ.get("SHIDASHI_CACHE") else spare

    monkeypatch.setattr(ownership, "locks_dir", _locks_dir)


@pytest.fixture(autouse=True)
def _no_mdns_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No worker answers the mDNS lookup of a worker command (story 020) unless a test
    says otherwise: no test sends a multicast packet or waits the real 5 s. Patched as
    ``remote.find`` -- ``mdns.find`` itself stays real for its own tests."""
    from shidashi import remote

    monkeypatch.setattr(remote, "find", lambda _name, **_kw: None)
