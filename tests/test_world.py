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


def test_world_with_an_image_prints_its_kits_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`shidashi world kde systemd` is a read-only view: every kit with what it
    keeps, the atoms exclude: takes out marked, and the world files untouched."""
    import shutil

    tree = tmp_path / "variants"
    shutil.copytree(config.variants_dir(), tree)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(tree))
    world_file = tree / "minimal/world.systemd"
    world_file.write_text("# edited by hand\n")
    result = CliRunner().invoke(app, ["world", "minimal", "systemd"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("minimal/systemd: ")
    assert "\n@base\n  @boot\n    sys-kernel/dracut" in result.output
    assert "    app-admin/metalog  (excluded)" in result.output
    assert "minimal/openrc" not in result.output
    assert world_file.read_text() == "# edited by hand\n"  # nothing written


def test_world_without_an_init_prints_every_init() -> None:
    result = CliRunner().invoke(app, ["world", "minimal"])
    assert result.exit_code == 0, result.output
    assert "minimal/systemd: " in result.output and "minimal/openrc: " in result.output
    # OpenRC keeps metalog: it is excluded only once, by the systemd image
    assert result.output.count("    app-admin/metalog  (excluded)") == 1


def test_world_names_an_unknown_image() -> None:
    result = CliRunner().invoke(app, ["world", "kdee"])
    assert result.exit_code == 1 and "unknown image kdee" in result.output


def test_kit_view_atoms_match_the_world() -> None:
    """The printed view and the world file come from the same walk."""
    from shidashi.resolve import kit_view, world_atoms

    recipe = config.load_recipe("v3", "kde", "systemd")
    printed = {a for kit in kit_view(recipe) for a in kit.atoms}
    assert tuple(sorted(printed)) == world_atoms(recipe)


def test_world_shows_the_base_alone() -> None:
    """The trunk every image grows from. init/systemd excludes ntp, which comes
    in a minimal kit: for the base alone that exclude is simply not there yet."""
    result = CliRunner().invoke(app, ["world", "base", "systemd"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("base/systemd: ")
    assert "    app-admin/metalog  (excluded)" in result.output
    assert "net-misc/ntp" not in result.output
    assert "@kde" not in result.output and "@extra-system" not in result.output


def test_the_base_is_still_not_an_image_to_build() -> None:
    with pytest.raises(config.UnknownAxisError):
        config.load_recipe("v3", "base", "systemd")


def test_kits_are_listed_under_the_set_that_includes_them() -> None:
    from shidashi.resolve import kit_view

    names = [k.name for k in kit_view(config.load_recipe("v3", "kde", "systemd"))]
    assert names[:3] == ["base", "boot", "fs"]


def test_world_shows_the_desktop_stage() -> None:
    """desktop is a stage every graphical image grows from, not an image itself."""
    result = CliRunner().invoke(app, ["world", "desktop", "systemd"])
    assert result.exit_code == 0, result.output
    assert "(base -> minimal -> desktop)" in result.output.splitlines()[0]
    assert "    sys-fs/fuse  (excluded)" in result.output
    assert "@kde" not in result.output
    with pytest.raises(config.UnknownAxisError):
        config.load_recipe("v3", "desktop", "systemd")


def test_a_malformed_recipe_names_its_file_and_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `-` under exclude: used to end in a pydantic traceback that
    named neither the file nor the line."""
    import shutil

    tree = tmp_path / "variants"
    shutil.copytree(config.variants_dir(), tree)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(tree))
    recipe = tree / "desktop/recipe.yaml"
    recipe.write_text(recipe.read_text() + "\nexclude:\n  -\n")
    result = CliRunner().invoke(app, ["world", "desktop", "systemd"])
    output = result.output.replace("\n", "")  # the console wraps long paths
    assert result.exit_code == 1
    assert "desktop/recipe.yaml: exclude.0: Input should be a valid string" in output


def test_world_prints_kits_as_a_tree() -> None:
    """An aggregator holds no atoms of its own: its kits print one level deeper,
    not as siblings that make it look empty."""
    out = CliRunner().invoke(app, ["world", "minimal", "systemd"]).output
    assert "\n@extra-system\n  @net-tools\n    net-dns/bind" in out
    assert "->" not in out.split("\n", 1)[1]  # no arrows below the header
