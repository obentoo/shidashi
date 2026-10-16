"""image_specs and a trunk stage without a world file (story 014, found in 5.2's preparation).

Only images have a committed ``variants/.../world.<init>``; a stage such as ``desktop`` (the
trunk) never does, so ``world.current_atoms`` refuses it. Its package count then comes from
what its kits ask for (``resolve.world_atoms``); an IMAGE whose world file is missing or stale
still fails, naming it.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shidashi import assembler, config, resolve, schedule, world


@pytest.fixture
def recipes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def load_recipe(arch: str, target: str, init: str, **_k: Any) -> Any:
        return SimpleNamespace(arch=arch, flavor=target, init=init)

    def current_atoms(recipe: Any, _variants: Path) -> tuple[str, ...]:
        if recipe.flavor in ("desktop", "gnome"):
            raise world.StaleWorldError(f"{recipe.flavor}: no world file")
        return ("app-misc/a", "app-misc/b", "app-misc/c")

    monkeypatch.setattr(config, "load_recipe", load_recipe)
    monkeypatch.setattr(config, "variants_dir", lambda: tmp_path / "variants")
    monkeypatch.setattr(world, "current_atoms", current_atoms)
    monkeypatch.setattr(resolve, "world_atoms", lambda recipe: ("x/1",) * 9)
    monkeypatch.setattr(
        assembler, "trunk_stage", lambda recipe: "desktop" if recipe.flavor != "minimal" else None
    )


@pytest.mark.usefixtures("recipes")
def test_a_trunk_stage_without_a_world_file_counts_what_its_kits_ask_for(tmp_path: Path) -> None:
    specs = schedule.image_specs("v3", "systemd", ["kde"], runs_dir=tmp_path / "runs")
    kde = specs["v3-kde-systemd"]
    assert kde.packages == 3
    assert kde.trunk_key == "v3-desktop-systemd"
    assert kde.trunk_packages == 9


@pytest.mark.usefixtures("recipes")
def test_an_image_without_its_world_file_still_fails_naming_it(tmp_path: Path) -> None:
    with pytest.raises(schedule.ImageSpecError, match="gnome"):
        schedule.image_specs("v3", "systemd", ["gnome"], runs_dir=tmp_path / "runs")
