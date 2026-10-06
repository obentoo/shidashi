"""Unit tests of the stepwise state under the pins (story 016, task 3.3, R8.7, R6.12, R6.13).

``BuildState.pins`` (defaulted ``""``) records the pin id a state was saved
under; ``is_stale(..., pins=)`` declares any other pin id stale. The stepwise
runner persists the pins it ran under, so a resume under the same pins is not
stale.
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import phases, state
from shidashi.recipe import Phase, ResolvedRecipe
from shidashi.state import BuildState, is_stale, load_state

pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

NEW, OLD = "p20260928.3fa9c2d1", "p20260901.0a1b2c3d"


def _state(**over: Any) -> BuildState:
    fields: dict[str, Any] = {
        "arch": "v3",
        "flavor": "minimal",
        "init": "systemd",
        "snapshot": "S",
        "recipe_hash": "H",
    }
    fields.update(over)
    return BuildState(**fields)


@pytest.mark.parametrize("other", [OLD, "p20260928.3fa9c2d2", "p20260929.3fa9c2d1", ""])
def test_a_state_saved_under_another_pin_id_is_stale(other: str) -> None:
    """Hostile (wrong collapse): ids differing only in the hash or only in the date."""
    assert is_stale(_state(pins=NEW), snapshot="S", pins=other, recipe_hash="H") is True


def test_a_state_saved_under_the_same_pins_is_not_stale() -> None:
    """Hostile (wrong split), R6.12."""
    assert is_stale(_state(pins=NEW), snapshot="S", pins=NEW, recipe_hash="H") is False


def test_snapshot_and_recipe_still_make_a_state_stale() -> None:
    assert is_stale(_state(pins=NEW), snapshot="S2", pins=NEW, recipe_hash="H") is True
    assert is_stale(_state(pins=NEW), snapshot="S", pins=NEW, recipe_hash="H2") is True


def test_the_pins_default_to_empty() -> None:
    assert _state().pins == ""


def test_a_state_file_written_before_the_fix_loads_and_is_stale(tmp_path: Path) -> None:
    """R6.13 then R8.7: the pre-fix JSON (no pins) loads, and no real pin id matches it."""
    path = tmp_path / "v3-minimal-systemd.json"
    path.write_text(
        '{"arch":"v3","flavor":"minimal","init":"systemd","snapshot":"S","recipe_hash":"H",'
        '"seed_done":true,"seed_sha512":"","bootstrap_done":true,"completed_phases":["rebuild"],'
        '"accumulated_breaks":[],"phase_diffs":[]}',
        encoding="utf-8",
    )
    loaded = load_state(path)
    assert loaded is not None
    assert loaded.pins == ""
    assert is_stale(loaded, snapshot="S", pins=NEW, recipe_hash="H") is True


def test_the_stepwise_runner_persists_the_pins_it_ran_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise every resume under unchanged pins would be declared stale (R6.12)."""
    from shidashi.container import CommandResult

    class _Container:
        rootfs = tmp_path / "rootfs"

        def run(self, argv: Any, **_k: Any) -> Any:
            return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")

    monkeypatch.setattr(phases, "snapshot_fork_point", lambda _r, dest: dest)
    recipe = ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="p",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(Phase(name="rebuild", emptytree=True),),
        portage_layers=(),
    )
    path = tmp_path / "state.json"
    phases.run_phases_stepwise(
        _Container(),  # type: ignore[arg-type]
        recipe,
        emptytree=True,
        completed=(),
        until=None,
        snapshot="S",
        pins=NEW,
        fork_points_dir=tmp_path,
        state_path=path,
    )
    saved = load_state(path)
    assert saved is not None
    assert saved.pins == NEW
    assert saved.completed_phases == ("rebuild",)
    assert (
        state.is_stale(saved, snapshot="S", pins=NEW, recipe_hash=state.recipe_hash(recipe))
        is False
    )
