"""Path-resolution tests for the variants/ tree (shidashi.config).

INTEGRATION: builds a temporary variants/ tree in ``tmp_path`` and points the
code at it via the ``SHIDASHI_VARIANTS_DIR`` override. These tests check PATH
RESOLUTION, not YAML parsing -- hence the minimal files.
"""

from pathlib import Path

import pytest

from shidashi.config import (
    UnknownAxisError,
    available_names,
    axis_dir,
    base_path,
    catalyst_dir,
    catalyst_spec_dir,
    recipe_path,
    variants_dir,
)

# fixture names per axis (deliberately out of order to exercise the sort)
_FIXTURE = {
    "arch": ["znver5", "v3"],
    "flavor": ["minimal", "kde"],
    "init": ["systemd", "openrc"],
}


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build variants/ in tmp_path and export SHIDASHI_VARIANTS_DIR pointing at it."""
    root = tmp_path / "variants"
    for axis, names in _FIXTURE.items():
        for name in names:
            recipe = root / axis / name / "recipe.yaml"
            recipe.parent.mkdir(parents=True, exist_ok=True)
            recipe.write_text("", encoding="utf-8")
    base = root / "base" / "recipe.yaml"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("", encoding="utf-8")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    return root


# --- variants_dir honors the override ----------------------------------------


def test_variants_dir_honors_env_override(variants_tree: Path) -> None:
    assert variants_dir() == variants_tree


def test_variants_dir_reads_env_fresh_each_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(a))
    assert variants_dir() == a
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(b))
    assert variants_dir() == b


# --- axis_dir / recipe_path resolve every axis of the fixture ----------------


@pytest.mark.parametrize(
    ("axis", "name"),
    [(axis, name) for axis, names in _FIXTURE.items() for name in names],
)
def test_axis_dir_resolves_every_fixture(variants_tree: Path, axis: str, name: str) -> None:
    resolved = axis_dir(axis, name)
    assert resolved == variants_tree / axis / name
    assert resolved.is_dir()


@pytest.mark.parametrize(
    ("axis", "name"),
    [(axis, name) for axis, names in _FIXTURE.items() for name in names],
)
def test_recipe_path_resolves_every_fixture(variants_tree: Path, axis: str, name: str) -> None:
    resolved = recipe_path(axis, name)
    assert resolved == variants_tree / axis / name / "recipe.yaml"
    assert resolved.is_file()


# --- base_path ----------------------------------------------------------------


def test_base_path_points_at_base_yaml(variants_tree: Path) -> None:
    resolved = base_path()
    assert resolved == variants_tree / "base" / "recipe.yaml"
    assert resolved.is_file()


# --- available_names sorted ---------------------------------------------------


@pytest.mark.parametrize("axis", list(_FIXTURE))
def test_available_names_returns_sorted_fixture(variants_tree: Path, axis: str) -> None:
    assert available_names(axis) == sorted(_FIXTURE[axis])


def test_available_names_absent_axis_is_empty(variants_tree: Path) -> None:
    assert available_names("nonexistent") == []


# --- an unknown name raises UnknownAxisError with the available ones ---------


def test_axis_dir_unknown_name_raises_with_available(variants_tree: Path) -> None:
    with pytest.raises(UnknownAxisError) as excinfo:
        axis_dir("arch", "doesnotexist")
    err = excinfo.value
    assert err.axis == "arch"
    assert err.name == "doesnotexist"
    assert err.available == sorted(_FIXTURE["arch"])
    msg = str(err)
    assert "doesnotexist" in msg
    for name in _FIXTURE["arch"]:
        assert name in msg


def test_recipe_path_unknown_name_raises_with_available(variants_tree: Path) -> None:
    with pytest.raises(UnknownAxisError) as excinfo:
        recipe_path("flavor", "bogus")
    msg = str(excinfo.value)
    for name in _FIXTURE["flavor"]:
        assert name in msg


# --- catalyst paths (R3.5, story 005) ----------------------------------------


def test_catalyst_dir_is_arch_partitioned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # default under cache_dir()/catalyst/<arch>; partitioned per arch like pkgdir.
    monkeypatch.delenv("SHIDASHI_CATALYST_DIR", raising=False)
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path))
    assert catalyst_dir("znver5") == tmp_path / "catalyst" / "znver5"
    assert catalyst_dir("znver5") != catalyst_dir("v3")


def test_catalyst_dir_honors_dedicated_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_CATALYST_DIR", str(tmp_path / "cat"))
    assert catalyst_dir("znver5") == tmp_path / "cat" / "znver5"


def test_catalyst_spec_dir_under_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path))
    assert catalyst_spec_dir("znver5") == tmp_path / "catalyst" / "znver5"
