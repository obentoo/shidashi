"""UNIT tests of the stepwise resume falling back to an older snapshot (story 004, 9.2).

The audit of 2026-10-14 found that when the newest completed phase has no snapshot on
disk, the resume restored an older phase's snapshot but kept every phase in
``completed`` — so the phases after the restored one were skipped on a rootfs that never
got their work. The resume must go on from the phase after the restored one (R5.2).
"""

from pathlib import Path

import pytest

from shidashi import factory, phases
from shidashi.recipe import Phase, ResolvedRecipe
from shidashi.seed import Stage3Pointer

SNAP = "20260823T153057Z"
PINS = "p20260928.60cfe9d6"


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(
            Phase(name="base", stage="base", emptytree=True),
            Phase(name="minimal", stage="minimal", ships=True),
            Phase(name="extra", stage="extra"),
        ),
        portage_layers=(),
        stages=("base", "minimal", "extra"),
    )


def _pointer() -> Stage3Pointer:
    return Stage3Pointer(
        init="systemd",
        base_url="https://distfiles.gentoo.org/x",
        snapshot=SNAP,
        filename=f"stage3-amd64-nomultilib-systemd-{SNAP}.tar.xz",
        sha512="0" * 128,
    )


def _snapshot(fps: Path, phase: str) -> Path:
    path = phases.phase_snapshot_path(
        _recipe(), snapshot=SNAP, pins=PINS, phase=phase, fork_points_dir=fps
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"tar")
    return path


def _resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed: tuple[str, ...]
) -> tuple[object, list[Path]]:
    restored: list[Path] = []
    monkeypatch.setattr(factory, "restore_fork_point", lambda path, _rootfs: restored.append(path))
    result = factory.Factory(_recipe(), tmp_path / "pkgdir")._seed_or_restore_stepwise(
        _recipe(),
        tmp_path / "rootfs",
        _pointer(),
        snapshot=SNAP,
        pins=PINS,
        recipe_hash="H",
        fork_points_dir=tmp_path / "fp",
        state_path=tmp_path / "state.json",
        completed=completed,
        seed_done=True,
        interactive=False,
        download=False,
        on_checkpoint=None,
    )
    return result, restored


def test_completed_through_keeps_the_phases_up_to_the_given_one() -> None:
    got = phases.completed_through(_recipe(), ("minimal", "base", "extra"), "minimal")
    assert got == ("base", "minimal")


def test_fallback_to_an_older_snapshot_trims_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _snapshot(tmp_path / "fp", "base")
    result, restored = _resume(tmp_path, monkeypatch, ("base", "minimal"))
    assert restored == [base]
    assert result == (False, ("base",))


def test_resume_from_the_newest_snapshot_keeps_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot(tmp_path / "fp", "base")
    minimal = _snapshot(tmp_path / "fp", "minimal")
    result, restored = _resume(tmp_path, monkeypatch, ("base", "minimal"))
    assert restored == [minimal]
    assert result == (False, ("base", "minimal"))
