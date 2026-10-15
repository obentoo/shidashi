"""UNIT tests of a resumed stepwise run keeping the earlier runs' diffs (story 004, 10.1).

The revalidation of 2026-10-14 found that ``run_phases_stepwise`` started every run with
no phase diffs and no prior atoms: a resume overwrote the saved diffs with its own
(R4.4) and never flagged a rebuild of a package an earlier run had built (R4.2).

Container and snapshot are faked; the state file is real, under ``tmp_path``.
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import phases, state
from shidashi.container import CommandResult
from shidashi.phases import CheckpointDecision, run_phases_stepwise
from shidashi.recipe import Phase, ResolvedRecipe

# the binpkg check of a shipped stage extracts the stage3's vdb: stubbed here
pytestmark = pytest.mark.usefixtures("no_stage3_vdb")


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(
            Phase(name="rebuild", emptytree=True),
            Phase(name="graphics", packages=("cat/pkg",)),
        ),
        portage_layers=("base", "arch/v3", "flavor/minimal", "init/systemd"),
    )


class _Container:
    """Every emerge rebuilds ``cat/pkg-1``, a package the earlier run built."""

    rootfs = Path("/live-rootfs")
    binds: tuple[Any, ...] = ()
    binds_rw: tuple[Any, ...] = ()

    def run(self, _argv: Any, **_k: Any) -> CommandResult:
        return CommandResult(0, "[ebuild   R    ] cat/pkg-1\n", "")

    def shell(self) -> None:
        raise AssertionError("no shell in these tests")


def _save(path: Path, completed: tuple[str, ...], diffs: tuple[state.PhaseDiff, ...]) -> None:
    recipe = _recipe()
    state.save_state(
        path,
        state.BuildState(
            arch=recipe.arch,
            flavor=recipe.flavor,
            init=recipe.init,
            snapshot="S",
            pins="P",
            recipe_hash=state.recipe_hash(recipe),
            seed_done=True,
            bootstrap_done=True,
            completed_phases=completed,
            phase_diffs=diffs,
        ),
    )


def _resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed: tuple[str, ...]) -> Any:
    monkeypatch.setattr(phases, "snapshot_fork_point", lambda *_a, **_k: Path("/snap.tar"))
    path = tmp_path / "state.json"
    run_phases_stepwise(
        _Container(),  # type: ignore[arg-type]
        _recipe(),
        emptytree=True,
        completed=completed,
        until=None,
        snapshot="S",
        pins="P",
        fork_points_dir=tmp_path / "fp",
        state_path=path,
        on_checkpoint=lambda *_a: CheckpointDecision.CONTINUE,
    )
    return state.load_state(path)


REBUILD = state.PhaseDiff(phase="rebuild", built=("cat/pkg-1",))


def test_a_resume_keeps_the_earlier_runs_diffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(tmp_path / "state.json", ("rebuild",), (REBUILD,))
    saved = _resume(tmp_path, monkeypatch, ("rebuild",))
    assert saved.completed_phases == ("rebuild", "graphics")
    assert tuple(d.phase for d in saved.phase_diffs) == ("rebuild", "graphics")


def test_a_resume_flags_a_rebuild_of_an_earlier_runs_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(tmp_path / "state.json", ("rebuild",), (REBUILD,))
    saved = _resume(tmp_path, monkeypatch, ("rebuild",))
    graphics = next(d for d in saved.phase_diffs if d.phase == "graphics")
    assert graphics.unexpected_rebuilds == ("cat/pkg-1",)


def test_a_saved_diff_of_a_phase_no_longer_completed_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a fallback restore trimmed `completed` to nothing: rebuild runs again
    _save(tmp_path / "state.json", ("rebuild",), (REBUILD,))
    saved = _resume(tmp_path, monkeypatch, ())
    assert tuple(d.phase for d in saved.phase_diffs) == ("rebuild", "graphics")
    assert saved.phase_diffs[0].unexpected_rebuilds == ()
