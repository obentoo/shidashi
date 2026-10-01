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
    excluded = recipe.model_copy(
        update={"exclude": (*recipe.exclude, "kde-apps/konsole")}
    )
    assert "kde-apps/konsole" in world_atoms(recipe)
    assert "kde-apps/konsole" not in world_atoms(excluded)


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
    assert "    app-admin/metalog  (excluded by init/systemd)" in result.output
    assert "minimal/openrc" not in result.output
    assert world_file.read_text() == "# edited by hand\n"  # nothing written


def test_world_without_an_init_prints_every_init() -> None:
    result = CliRunner().invoke(app, ["world", "minimal"])
    assert result.exit_code == 0, result.output
    assert "minimal/systemd: " in result.output and "minimal/openrc: " in result.output
    # OpenRC keeps metalog: it is excluded only once, by the systemd image
    assert result.output.count("    app-admin/metalog  (excluded by init/systemd)") == 1


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
    assert "    app-admin/metalog  (excluded by init/systemd)" in result.output
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
    assert "    sys-fs/fuse  (excluded by base)" in result.output
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


def _variants_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import shutil

    tree = tmp_path / "variants"
    shutil.copytree(config.variants_dir(), tree)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(tree))
    return tree


def test_a_catalog_line_is_in_the_kit_but_in_no_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kits are the binhost's library: `#atom` keeps a line out of every
    image without an exclude."""
    from shidashi.resolve import kit_view, set_closure, world_atoms

    tree = _variants_copy(tmp_path, monkeypatch)
    kit = tree / "kits/core/shell"
    prose = "# app-misc/screen is prose: a space after #\n"
    kit.write_text(kit.read_text() + "#app-misc/tmux\n" + prose)
    recipe = config.load_recipe("v3", "minimal", "systemd")
    assert "app-misc/tmux" not in world_atoms(recipe)
    shell = next(k for k in kit_view(recipe) if k.name == "shell")
    assert "app-misc/tmux" in shell.catalog
    assert "app-misc/screen" not in shell.catalog  # a spaced comment is prose
    written = set_closure(recipe)["shell"]
    assert not any(ln.startswith("app-misc/tmux") for ln in written)
    assert written[0].startswith("# shidashi: catalog only (binhost, not this image): ")
    assert written[0].endswith(" app-misc/tmux")
    out = CliRunner().invoke(app, ["world", "minimal", "systemd"]).output
    assert "    app-misc/tmux  (catalog only)" in out


def test_a_catalog_ref_keeps_the_whole_kit_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shidashi.resolve import kit_view, world_atoms

    tree = _variants_copy(tmp_path, monkeypatch)
    kit = tree / "kits/core/base"
    kit.write_text(kit.read_text().replace("@archive\n", "#@archive\n"))
    archive = [ln for ln in (tree / "kits/core/archive").read_text().splitlines()
               if ln and not ln.startswith("#")]
    recipe = config.load_recipe("v3", "minimal", "systemd")
    assert "archive" not in {k.name for k in kit_view(recipe)}
    assert not set(archive) & set(world_atoms(recipe))


def test_excluding_a_catalog_atom_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shidashi.resolve import ResolveError, world_atoms

    tree = _variants_copy(tmp_path, monkeypatch)
    kit = tree / "kits/core/shell"
    kit.write_text(kit.read_text().replace("app-editors/vim\n", "#app-editors/vim\n"))
    recipe = config.load_recipe("v3", "minimal", "systemd")
    redundant = recipe.model_copy(update={"exclude": ("app-editors/vim",)})
    with pytest.raises(ResolveError, match="already catalog-only in kit 'shell'"):
        world_atoms(redundant)


def test_rust_is_catalog_only_and_images_ship_rust_bin() -> None:
    from shidashi.resolve import world_atoms

    for target in config.target_names():
        atoms = world_atoms(config.load_recipe("v3", target, "systemd"))
        assert "dev-lang/rust-bin" in atoms and "dev-lang/rust" not in atoms, target


def test_kits_check_validates_catalog_lines(tmp_path: Path) -> None:
    """A typo in a `#atom` line would otherwise pass: Portage never reads it."""
    from shidashi import kits

    lib = tmp_path / "kits" / "core"
    lib.mkdir(parents=True)
    (lib / "shell").write_text("# prose, skipped\n#app-misc/tmuxx\n#@nokit\n")
    repo = tmp_path / "gentoo"
    (repo / "app-misc" / "tmux").mkdir(parents=True)
    problems = kits.check(tmp_path / "kits", {"gentoo": repo})
    assert any("app-misc/tmuxx" in p for p in problems)
    assert any("@nokit names no kit" in p for p in problems)
    assert len(problems) == 2


def _with_include(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, text: str) -> Path:
    """A copy of variants/ where only ``stage`` has an include: -- ``text``."""
    tree = _variants_copy(tmp_path, monkeypatch)
    for recipe in (tree / "desktop/recipe.yaml", *tree.glob("flavor/*/recipe.yaml")):
        lines = recipe.read_text().splitlines()
        if "include:" in lines:  # drop the committed include: block
            start = lines.index("include:")
            end = start + 1
            while end < len(lines) and lines[end].startswith("  - "):
                end += 1
            recipe.write_text("\n".join(lines[:start] + lines[end:]) + "\n")
    path = tree / ("desktop" if stage == "desktop" else f"flavor/{stage}") / "recipe.yaml"
    path.write_text(path.read_text() + text)
    return tree


def test_an_include_puts_an_earlier_exclude_back_from_its_own_stage_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """minimal excludes bleachbit; kde includes it. Only kde's phase installs it,
    so the shared fork points of base, minimal and desktop never see it."""
    from shidashi.resolve import world_atoms

    _with_include(tmp_path, monkeypatch, "kde", "include:\n  - sys-apps/bleachbit\n")
    kde = config.load_recipe("v3", "kde", "systemd")
    assert kde.includes == {"include-kde": ("sys-apps/bleachbit",)}
    phases = {p.stage: p.sets for p in kde.phases if p.stage}
    assert "include-kde" in phases["kde"]
    assert not any("include-kde" in phases[s] for s in ("base", "minimal", "desktop"))
    assert "sys-apps/bleachbit" in world_atoms(kde)
    assert "sys-apps/bleachbit" not in world_atoms(config.load_recipe("v3", "gnome", "systemd"))
    assert "sys-apps/bleachbit" not in world_atoms(config.load_recipe("v3", "minimal", "systemd"))
    out = CliRunner().invoke(app, ["world", "kde", "systemd"]).output
    assert "\n@include-kde  (include:)\n  sys-apps/bleachbit\n" in out


def test_an_include_brings_a_catalog_only_atom_into_an_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shidashi.resolve import world_atoms

    _with_include(tmp_path, monkeypatch, "kde", "include:\n  - dev-lang/rust\n")
    assert "dev-lang/rust" in world_atoms(config.load_recipe("v3", "kde", "systemd"))


def test_an_include_of_something_never_taken_out_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shidashi.resolve import ResolveError, world_atoms

    _with_include(tmp_path, monkeypatch, "kde", "include:\n  - app-editors/vim\n")
    with pytest.raises(ResolveError, match="app-editors/vim .* is neither excluded"):
        world_atoms(config.load_recipe("v3", "kde", "systemd"))


def test_including_and_excluding_the_same_atom_is_a_contradiction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shidashi.recipe import RecipeChainError

    _with_include(tmp_path, monkeypatch, "desktop",
                  "include:\n  - sys-apps/bleachbit\nexclude:\n  - sys-apps/bleachbit\n")
    with pytest.raises(RecipeChainError, match="both includes and excludes sys-apps/bleachbit"):
        config.load_recipe("v3", "kde", "systemd")


def test_the_legend_names_who_excluded_and_who_put_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`(excluded)` alone read as "the later stages lost it" when a later stage
    had put it back: the legend names both layers."""
    _with_include(tmp_path, monkeypatch, "desktop", "include:\n  - sys-apps/bleachbit\n")
    kde = config.load_recipe("v3", "kde", "systemd")
    assert kde.exclude_origin["app-admin/metalog"] == "init/systemd"
    assert kde.exclude_origin["sys-apps/bleachbit"] == "minimal"
    out = CliRunner().invoke(app, ["world", "kde", "systemd"]).output
    assert "sys-apps/bleachbit  (excluded by minimal; included by desktop)" in out
    assert "app-admin/metalog  (excluded by init/systemd)" in out
    minimal = CliRunner().invoke(app, ["world", "minimal", "systemd"]).output
    assert "sys-apps/bleachbit  (excluded by minimal)\n" in minimal


def test_world_names_a_broken_exclude_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _variants_copy(tmp_path, monkeypatch)
    base = tree / "base/recipe.yaml"
    base.write_text(base.read_text().replace("exclude:\n", "exclude:\n  - app-misc/nothing-here\n"))
    result = CliRunner().invoke(app, ["world", "--check"])
    assert result.exit_code == 1
    assert "exclude: app-misc/nothing-here is in no set" in result.output.replace("\n", " ")
    assert "Traceback" not in result.output


def test_kits_check_names_a_glued_comment_that_is_no_atom(tmp_path: Path) -> None:
    """`#www-client-firefox` lost its `/`: a comment, so the catalog silently
    lacked firefox. It is a problem, not prose."""
    from shidashi import kits

    lib = tmp_path / "kits" / "internet"
    lib.mkdir(parents=True)
    (lib / "web").write_text("# prose\n#\n##\n#www-client-firefox\n#www-client/firefox\n")
    repo = tmp_path / "gentoo"
    (repo / "www-client" / "firefox").mkdir(parents=True)
    problems = kits.check(tmp_path / "kits", {"gentoo": repo})
    assert problems == [
        "web:4: #www-client-firefox is neither #category/package nor #@kit "
        "(prose needs a space after #)"
    ]
