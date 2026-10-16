"""Unit tests of shidashi.schedule.image_specs -- every image of a build as an ImageSpec.

Contract (design.md v3, story 014, sub-task 3.2):
``image_specs(arch, init, images, *, runs_dir) -> dict[str, ImageSpec]`` keyed by
``<arch>-<image>-<init>``; each spec carries the target name ``image``.
- ``packages``: the length of ``packages.json`` of the latest successful assemble of the
  image on ANY node (the package set does not depend on the machine), else the length of
  its world, ``world.current_atoms(recipe, config.variants_dir())`` (R2.8).
- An image with a trunk (``assembler.trunk_stage(recipe)``): ``trunk_key`` is
  ``<arch>-<trunk>-<init>`` -- the KEY the checkpoints' claimants carry, compared with
  ``Node.trunks`` (R2.5); ``trunk_packages`` like ``packages``, for the trunk's key.
  An image without a trunk has ``trunk_key=None`` and ``trunk_packages=0``.
- Planning is read-only (v3): no install fingerprint, no tree fetch
  (``tree.pinned_repos``), no ``Assembler``, no seed pointer -- a fingerprint embeds the
  host's cache paths and never matches a worker's checkpoint.
- An image that cannot be resolved raises naming it.
The seams are module attributes: ``config.load_recipe``, ``config.variants_dir``,
``world.current_atoms``, ``assembler.trunk_stage``.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shidashi import assembler, config, phases, resolve, schedule, seed, tree, world

TRUNKS = {"kde": "desktop", "gnome": "desktop", "wm": "desktop"}


@dataclass
class Recipes:
    unknown: set[str] = field(default_factory=set)
    loaded: list[tuple[str, str, str]] = field(default_factory=list)
    worlds: list[str] = field(default_factory=list)
    variants: list[Path] = field(default_factory=list)


@pytest.fixture
def recipes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recipes:
    state = Recipes()
    variants = tmp_path / "variants"

    def load_recipe(arch: str, target: str, init: str, **_k: Any) -> Any:
        if target in state.unknown:
            raise config.UnknownAxisError("target", target, ["minimal", "kde"])
        state.loaded.append((arch, target, init))
        return SimpleNamespace(arch=arch, flavor=target, init=init, phases=(), sets=())

    def current_atoms(recipe: Any, variants_dir: Path) -> tuple[str, ...]:
        state.worlds.append(recipe.flavor)
        state.variants.append(variants_dir)
        count = 7 if recipe.flavor != "desktop" else 5
        return tuple(f"app-misc/{recipe.flavor}{n}" for n in range(count))

    def forbidden(name: str) -> Any:
        def call(*_a: Any, **_k: Any) -> Any:
            raise AssertionError(f"planning is read-only: {name} was called")

        return call

    monkeypatch.setattr(config, "load_recipe", load_recipe)
    monkeypatch.setattr(config, "variants_dir", lambda: variants)
    monkeypatch.setattr(assembler, "trunk_stage", lambda recipe: TRUNKS.get(recipe.flavor))
    monkeypatch.setattr(world, "current_atoms", current_atoms)
    # v3: none of these may run while planning (no fingerprint, no tree, no render)
    monkeypatch.setattr(assembler, "Assembler", forbidden("assembler.Assembler"))
    monkeypatch.setattr(tree, "pinned_repos", forbidden("tree.pinned_repos"))
    monkeypatch.setattr(tree, "ensure_tree", forbidden("tree.ensure_tree"))
    monkeypatch.setattr(seed, "load_pointer", forbidden("seed.load_pointer"))
    monkeypatch.setattr(seed, "fetch_stage3", forbidden("seed.fetch_stage3"))
    monkeypatch.setattr(phases, "image_cuts", forbidden("phases.image_cuts"))
    monkeypatch.setattr(resolve, "world_atoms", forbidden("resolve.world_atoms"))
    return state


def _run(
    runs: Path,
    run_id: str,
    *,
    image: str,
    packages: int,
    hostname: str,
    ok: bool = True,
    init: str = "systemd",
    arch: str = "v3",
) -> None:
    run = runs / run_id
    run.mkdir(parents=True)
    manifest = {
        "run": run_id,
        "command": "assemble",
        "argv": ["-c", "assemble", arch, image, init],
        "hostname": hostname,
        "status": "ok" if ok else "error",
        "started": f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}T10:00:00+00:00",
        "steps": [{"step": "install", "status": "ok" if ok else "error", "duration_s": 100.0}],
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "packages.json").write_text(json.dumps([{"atom": f"x/p{n}-1"} for n in range(packages)]))


def test_specs_are_keyed_by_arch_image_init_and_carry_the_target_name(
    recipes: Recipes, tmp_path: Path
) -> None:
    specs = schedule.image_specs("v3", "systemd", ["minimal", "kde"], runs_dir=tmp_path / "none")
    assert sorted(specs) == ["v3-kde-systemd", "v3-minimal-systemd"]
    assert specs["v3-kde-systemd"].image == "kde" and specs["v3-kde-systemd"].arch == "v3"
    assert specs["v3-kde-systemd"].key == "v3-kde-systemd"


def test_specs_take_the_package_count_of_the_latest_run_on_any_node(
    recipes: Recipes, tmp_path: Path
) -> None:
    runs = tmp_path / "runs"
    _run(runs, "20261003T100000Z-aaaaaa", image="kde", packages=1050, hostname="dojo")
    _run(runs, "20261004T100000Z-bbbbbb", image="kde", packages=1061, hostname="bentoo-lab")
    _run(runs, "20261005T100000Z-cccccc", image="kde", packages=9, hostname="dojo", ok=False)
    # hostile collapse: newer runs of kde on another init and another arch are other images
    _run(runs, "20261006T100000Z-dddddd", image="kde", packages=3, hostname="dojo", init="openrc")
    _run(runs, "20261006T110000Z-eeeeee", image="kde", packages=4, hostname="dojo", arch="znver5")
    specs = schedule.image_specs("v3", "systemd", ["kde"], runs_dir=runs)
    assert specs["v3-kde-systemd"].packages == 1061  # latest ok run, whichever node ran it


def test_specs_without_any_run_take_the_size_of_the_world(recipes: Recipes, tmp_path: Path) -> None:
    specs = schedule.image_specs("v3", "systemd", ["minimal"], runs_dir=tmp_path / "none")
    assert specs["v3-minimal-systemd"].packages == 7
    assert recipes.variants and all(v == tmp_path / "variants" for v in recipes.variants)


def test_specs_an_image_without_a_trunk_has_none(recipes: Recipes, tmp_path: Path) -> None:
    spec = schedule.image_specs("v3", "systemd", ["minimal"], runs_dir=tmp_path)[
        "v3-minimal-systemd"
    ]
    assert spec.trunk_key is None and spec.trunk_packages == 0


def test_specs_a_flavors_trunk_key_is_the_desktop_key(recipes: Recipes, tmp_path: Path) -> None:
    spec = schedule.image_specs("v3", "systemd", ["kde"], runs_dir=tmp_path)["v3-kde-systemd"]
    assert spec.trunk_key == "v3-desktop-systemd"  # what the trunk's checkpoint is claimed by
    assert spec.trunk_packages > 0
    assert not hasattr(spec, "trunk")  # no checkpoint id any more


def test_specs_flavors_sharing_a_trunk_share_one_key(recipes: Recipes, tmp_path: Path) -> None:
    # hostile split: three flavors, one trunk
    specs = schedule.image_specs("v3", "systemd", ["kde", "gnome", "wm"], runs_dir=tmp_path)
    assert {s.trunk_key for s in specs.values()} == {"v3-desktop-systemd"}


def test_specs_the_trunk_key_follows_the_arch_and_the_init(
    recipes: Recipes, tmp_path: Path
) -> None:
    # hostile collapse: the same trunk stage on another arch or init is another trunk
    z = schedule.image_specs("znver5", "systemd", ["kde"], runs_dir=tmp_path)
    o = schedule.image_specs("v3", "openrc", ["kde"], runs_dir=tmp_path)
    assert z["znver5-kde-systemd"].trunk_key == "znver5-desktop-systemd"
    assert o["v3-kde-openrc"].trunk_key == "v3-desktop-openrc"


def test_specs_never_fetch_a_tree_nor_build_an_assembler(recipes: Recipes, tmp_path: Path) -> None:
    # every forbidden seam raises AssertionError: reaching one fails this call
    specs = schedule.image_specs(
        "v3", "systemd", ["minimal", "kde", "gnome", "wm"], runs_dir=tmp_path
    )
    assert len(specs) == 4


def test_specs_an_image_that_cannot_be_resolved_is_named(recipes: Recipes, tmp_path: Path) -> None:
    recipes.unknown = {"plasma"}
    with pytest.raises(Exception, match="plasma"):
        schedule.image_specs("v3", "systemd", ["kde", "plasma"], runs_dir=tmp_path)
