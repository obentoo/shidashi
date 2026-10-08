"""Unit tests of the assembler's toolbox lookup under the pins (story 016, task 3.6, R8.6).

Real ``toolbox.tarball_path`` and real pin files; the assemble is stopped at the
stage3 fetch, the first thing after the toolbox check.
"""

import os
from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import config, world
from shidashi.assembler import Assembler, AssemblerError
from shidashi.phases import build_key
from shidashi.recipe import ResolvedRecipe
from tests.test_factory_pins import SNAP, current_pins, pointer, write_seeds

OLD = "p20260901.0a1b2c3d"


class _Reached(Exception):
    """The assemble went past the toolbox check."""


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="znver5",
        flavor="kde",
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-O2",
        goamd64="v4",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(),
        portage_layers=(),
    )


BK = build_key(_recipe())


def _wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, str]:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    seeds = write_seeds(tmp_path / "seeds")
    monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(seeds))
    monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: pointer())
    monkeypatch.setattr(asm, "pinned_repos", lambda **_k: {"gentoo": tmp_path / "tree"})
    monkeypatch.setattr(world, "current_atoms", lambda recipe, variants_dir: ("app-misc/a",))

    def reached(*_a: object, **_k: object) -> Path:
        raise _Reached

    monkeypatch.setattr(asm, "fetch_stage3", reached)
    fps = config.fork_points_dir()
    fps.mkdir(parents=True)
    return fps, current_pins(seeds)


def test_an_older_pins_or_pre_fix_toolbox_is_not_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fps, pins = _wire(monkeypatch, tmp_path)
    (fps / f"znver5-systemd-{SNAP}-{OLD}-{BK}-toolbox.tar").write_bytes(b"T")
    (fps / f"znver5-systemd-{SNAP}-toolbox.tar").write_bytes(b"T")
    with pytest.raises(AssemblerError) as err:
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out")
    assert f"znver5-systemd-{SNAP}-{pins}-{BK}-toolbox.tar" in str(err.value)
    assert "shidashi factory znver5 toolbox systemd" in str(err.value)


def test_the_current_pins_toolbox_lets_the_assemble_proceed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fps, pins = _wire(monkeypatch, tmp_path)
    (fps / f"znver5-systemd-{SNAP}-{pins}-{BK}-toolbox.tar").write_bytes(b"T")
    with pytest.raises(_Reached):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out")
