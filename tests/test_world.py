"""Unit tests of shidashi.world -- each image's flat package list (the world file)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from shidashi import config, world
from shidashi.cli import _world_recipes, app


def test_every_committed_world_file_matches_what_the_kits_compose() -> None:
    """The committed lists are the truth only while they are current: a kit
    changed without `shidashi world` fails here, not on an ISO."""
    for recipe in _world_recipes():
        path = world.world_file(recipe, config.variants_dir())
        assert path.is_file(), f"{path} missing: run `shidashi world`"
        assert path.read_text() == world.render(recipe), f"{path} stale: run `shidashi world`"


def test_the_world_does_not_depend_on_the_arch() -> None:
    """One file per image and init stands for every arch."""
    arches = config.available_names("arch")
    for target in config.target_names():
        lists = {world.render(config.load_recipe(a, target, "systemd")) for a in arches}
        assert len(lists) == 1, target


def test_kde_lists_its_display_manager_per_init_and_only_explicit_choices() -> None:
    systemd = world.read(world.world_file(config.load_recipe("v3", "kde", "systemd"),
                                          config.variants_dir()))
    openrc = world.read(world.world_file(config.load_recipe("v3", "kde", "openrc"),
                                         config.variants_dir()))
    assert "kde-plasma/plasma-login-manager" in systemd and "x11-misc/sddm" not in systemd
    assert "x11-misc/sddm" in openrc and "kde-plasma/plasma-login-manager" not in openrc
    assert list(systemd) == sorted(systemd)
    assert not any(a.startswith("@") for a in systemd)  # flattened, no set refs


def test_current_atoms_refuses_a_stale_file(tmp_path: Path) -> None:
    recipe = config.load_recipe("v3", "minimal", "systemd")
    copy = tmp_path / "minimal"
    copy.mkdir()
    real = world.world_file(recipe, config.variants_dir())
    (copy / real.name).write_text(real.read_text() + "app-misc/sneaked-in\n")
    with pytest.raises(world.StaleWorldError, match="shidashi world"):
        world.current_atoms(recipe, tmp_path)
    assert world.current_atoms(recipe, config.variants_dir()) == world.read(real)


def test_write_to_image_holds_only_atoms(tmp_path: Path) -> None:
    path = world.write_to_image(tmp_path, ("app-misc/a", "dev-libs/b"))
    assert path == tmp_path / "var/lib/portage/world"
    assert path.read_text() == "app-misc/a\ndev-libs/b\n"


def test_the_world_command_checks_and_names_stale_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    tree = tmp_path / "variants"
    shutil.copytree(config.variants_dir(), tree)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(tree))
    runner = CliRunner()
    assert runner.invoke(app, ["world", "--check"]).exit_code == 0
    stale = tree / "minimal/world.systemd"
    stale.write_text("# edited by hand\napp-misc/x\n")
    result = runner.invoke(app, ["world", "--check"])
    assert result.exit_code == 1 and "stale   variants/minimal/world.systemd" in result.output
    assert runner.invoke(app, ["world"]).exit_code == 0  # regenerates
    assert runner.invoke(app, ["world", "--check"]).exit_code == 0


def test_an_exclude_takes_the_package_out_of_the_world() -> None:
    from shidashi.resolve import world_atoms

    recipe = config.load_recipe("v3", "kde", "systemd")
    excluded = recipe.model_copy(update={"exclude": ("app-emulation/virtualbox",)})
    assert "app-emulation/virtualbox" in world_atoms(recipe)
    assert "app-emulation/virtualbox" not in world_atoms(excluded)


def test_an_exclude_that_matches_nothing_fails_with_the_reason() -> None:
    """A typo in exclude: used to be ignored -- the package it meant to take
    out stayed in the image, and nothing said so."""
    from shidashi.resolve import ResolveError, world_atoms

    recipe = config.load_recipe("v3", "minimal", "systemd")
    typo = recipe.model_copy(update={"exclude": ("app-emulation/virtualbx",)})
    with pytest.raises(ResolveError, match="virtualbx is in no set of the minimal chain"):
        world_atoms(typo)


def test_the_init_layer_excludes_for_its_own_images_only() -> None:
    """metalog and ntp are OpenRC's logger and clock: systemd images leave them
    out through init/systemd, and OpenRC images keep them."""
    from shidashi.resolve import world_atoms

    systemd = world_atoms(config.load_recipe("v3", "minimal", "systemd"))
    openrc = world_atoms(config.load_recipe("v3", "minimal", "openrc"))
    for atom in ("app-admin/metalog", "net-misc/ntp"):
        assert atom not in systemd and atom in openrc
