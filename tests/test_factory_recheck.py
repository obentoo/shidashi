"""Unit tests of the factory's per-phase re-check (story 016, task 4.3).

``_generation_recheck(pkgdir, rootfs, recipe)`` is an audited ``generation``
step carrying the phase (R4.5) that recomputes the real fingerprint of the
rootfs and checks it against the PKGDIR (R4.1, R4.2, R5.3). Both builds hand it
to their runner, after the start check (R6.6); a refused re-check fails the
build and keeps the rootfs (R6.9). Every phase run leaves one ``generation``
step: their count is the phases run plus one (the start check).
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, factory
from shidashi import phases as phases_mod
from shidashi.generation import (
    FINGERPRINT_FILE,
    GenerationMismatchError,
    check_or_record,
    fingerprint,
)
from shidashi.phases import FactoryError, PhaseResult
from shidashi.recipe import Phase
from tests.test_factory_pins import SNAP, Wired, recipe, toolchain, wire


def _generation_steps(trail: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    events = audit.read_events(trail.path / "events.jsonl")

    def last(e: dict[str, Any]) -> str:
        return str(e["step"]).rsplit("/", 1)[-1]

    starts = [e for e in events if e["kind"] == "step.start" and last(e) == "generation"]
    ends = [e for e in events if e["kind"] == "step.end" and last(e) == "generation"]
    return list(zip(starts, ends, strict=True))


def _rootfs(tmp_path: Path, gcc: str) -> Path:
    rootfs = tmp_path / "rootfs"
    toolchain(rootfs, gcc)
    return rootfs


# --- the closure ------------------------------------------------------------------------------


def test_the_recheck_accepts_the_pilots_patch_release_as_an_audited_step(tmp_path: Path) -> None:
    rootfs = _rootfs(tmp_path, "16.2.0")
    pkgdir = tmp_path / "pkgdir"
    check_or_record(pkgdir, fingerprint(rootfs, recipe()))
    toolchain(rootfs, "16.2.1_p20260926")
    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        factory._generation_recheck(pkgdir, rootfs, recipe())("base")
    [(start, end)] = _generation_steps(trail)
    assert start.get("after") == "base" or end.get("after") == "base"
    assert end["status"] == "ok"


def test_a_gcc_major_crossing_fails_the_recheck_naming_the_phase(tmp_path: Path) -> None:
    rootfs = _rootfs(tmp_path, "16.2.0")
    pkgdir = tmp_path / "pkgdir"
    check_or_record(pkgdir, fingerprint(rootfs, recipe()))
    before = (pkgdir / FINGERPRINT_FILE).read_bytes()
    toolchain(rootfs, "17.1.0")
    with (
        audit.run(tmp_path / "runs", command="factory", argv=[]) as trail,
        pytest.raises(GenerationMismatchError) as err,
    ):
        factory._generation_recheck(pkgdir, rootfs, recipe())("base")
    assert err.value.after_phase == "base"
    assert "'base'" in str(err.value)
    [(_start, end)] = _generation_steps(trail)
    assert end["status"] == "error"
    assert (pkgdir / FINGERPRINT_FILE).read_bytes() == before


def test_an_unreadable_fingerprint_propagates_from_the_recheck(tmp_path: Path) -> None:
    rootfs = _rootfs(tmp_path, "16.2.0")
    pkgdir = tmp_path / "pkgdir"
    pkgdir.mkdir()
    (pkgdir / FINGERPRINT_FILE).write_text("{", encoding="utf-8")
    with pytest.raises(FactoryError) as err:
        factory._generation_recheck(pkgdir, rootfs, recipe())("base")
    assert err.value.phase == "generation"
    assert FINGERPRINT_FILE in str(err.value)


# --- wired into both builds ----------------------------------------------------------------------


def _phases_that_upgrade(crossing: str | None) -> Any:
    """A fake runner: phase "base" keeps gcc 16, phase "flavor" moves it to ``crossing``."""

    def runner(w: Wired, kwargs: dict[str, Any]) -> tuple[Any, ...]:
        assert (w.pkgdir / FINGERPRINT_FILE).is_file()  # R6.6: the start check ran first
        hook = kwargs["on_phase_emerged"]
        toolchain(w.rootfs, "16.2.1_p20260926")
        hook("base")
        if crossing is not None:
            toolchain(w.rootfs, crossing)
        hook("flavor")
        return ()

    return runner


@pytest.mark.parametrize("method", ["build", "build_stepwise"])
def test_each_build_rechecks_after_every_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str
) -> None:
    w = wire(monkeypatch, tmp_path, runner=_phases_that_upgrade(None))
    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        getattr(factory.Factory(recipe(), w.pkgdir), method)(download=False)
    steps = _generation_steps(trail)
    # counted by the last path segment (design §4): the start check + one per phase run.
    # wire() fakes run_bootstrap, so its own `bootstrap/generation` step cannot occur here.
    assert not any(start["step"].startswith("bootstrap") for start, _end in steps)
    assert len(steps) == 3
    assert all(end["status"] == "ok" for _s, end in steps)
    afters = [start.get("after") or end.get("after") for start, end in steps]
    assert afters == [None, "base", "flavor"]


@pytest.mark.parametrize("method", ["build", "build_stepwise"])
def test_a_crossing_phase_fails_the_build_and_keeps_the_rootfs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str
) -> None:
    w = wire(monkeypatch, tmp_path, runner=_phases_that_upgrade("17.1.0"))
    with pytest.raises(GenerationMismatchError) as err:
        getattr(factory.Factory(recipe(), w.pkgdir), method)(download=False)
    assert err.value.after_phase == "flavor"
    assert err.value.successor == w.pkgdir.parent / f"{w.pkgdir.name}-gcc17"
    assert w.rootfs.is_dir()  # R6.9


# --- the update re-checks after its emerge (R7.8, R7.9) --------------------------------------


def test_an_update_whose_emerge_crosses_the_gcc_major_writes_no_fork_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    w = wire(monkeypatch, tmp_path)
    image = tmp_path / "image"
    toolchain(image, "16.2.0")
    w.fork_points.mkdir(parents=True, exist_ok=True)
    # the pre-fix key: found by the lookup both before and after task 3.5 (its last resort)
    phases_mod.snapshot_fork_point(image, w.fork_points / f"v3-systemd-{SNAP}-kde.tar")
    check_or_record(w.pkgdir, fingerprint(image, recipe()))
    monkeypatch.setattr(factory, "attach_packages", lambda *_a, **_k: None)

    def run_update(container: Any, _recipe: Any, **_k: Any) -> PhaseResult:
        toolchain(container.rootfs, "17.1.0")  # @preserved-rebuild pulled a new gcc
        return PhaseResult(phase=Phase(name="update", stage="kde"), built_atoms=(), snapshot=None)

    monkeypatch.setattr(factory, "run_update", run_update)
    with (
        audit.run(tmp_path / "runs", command="factory", argv=[]) as trail,
        pytest.raises(GenerationMismatchError) as err,
    ):
        factory.Factory(recipe(), w.pkgdir).update(download=False)
    assert err.value.after_phase == "update"
    assert "'update'" in str(err.value)
    assert w.snapshots == []  # R7.9: no fork point of the updated image
    afters = [start.get("after") or end.get("after") for start, end in _generation_steps(trail)]
    assert "update" in afters
    assert w.rootfs.is_dir()
