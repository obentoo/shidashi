"""UNIT (story 004 1.3) — build-state path helpers in shidashi.config.

Contract (design.md §config): ``state_dir()`` = ``cache_dir()/state`` (survives
the rootfs teardown; env-overridable PER CALL via ``SHIDASHI_CACHE``, same
pattern as the existing helpers); ``build_state_path(recipe)`` =
``state_dir()/<arch>-<flavor>-<init>.json`` (mirrors the rootfs/fork-point key).
Observable behavior only; we never depend on the host.

``state_dir``/``build_state_path`` are imported tolerantly (``getattr``):
while they do not exist, they go Red with ``AttributeError`` on use (expected Red).
"""

from pathlib import Path

import pytest

from shidashi import config
from shidashi.recipe import Phase, ResolvedRecipe


def _recipe(*, flavor: str = "minimal") -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor=flavor,
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(Phase(name="rebuild"),),
        portage_layers=("base", "arch/v3", "flavor/minimal", "init/systemd"),
    )


# --- state_dir --------------------------------------------------------------


def test_state_dir_under_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    assert config.state_dir() == tmp_path / "cache" / "state"


def test_state_dir_reads_cache_env_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "a"))
    assert config.state_dir() == tmp_path / "a" / "state"
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "b"))
    assert config.state_dir() == tmp_path / "b" / "state"


# --- build_state_path -------------------------------------------------------


def test_build_state_path_key_is_arch_flavor_init_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    path = config.build_state_path(_recipe(flavor="minimal"))
    assert path == tmp_path / "cache" / "state" / "v3-minimal-systemd.json"


def test_build_state_path_differs_per_flavor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    assert config.build_state_path(_recipe(flavor="minimal")) != config.build_state_path(
        _recipe(flavor="kde")
    )
