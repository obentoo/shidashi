"""Story 007, sub-task 6.1 -- the stage4 carries no build-time secret.

The stage4 tarball is packed from the configured system, BEFORE the live layer
and ``finalize``: the removal there came too late for it, so every stage4 kept
net-dns/bind's /etc/bind/rndc.key. The ``stage4`` step now removes the same
named secrets first (on its record) and refuses to pack a rootfs that still
carries a path of the deny list -- naming each one, removing none of them, so
the next leak fails the build instead of shipping in a tarball.
"""

import os
from pathlib import Path
from typing import Any

import pytest

import shidashi.assembler as asm
from shidashi import audit, image, publish, toolbox, world
from shidashi.assembler import Assembler, AssemblerError
from tests.test_assembler import _FakeContainer, _FakeTools, _pointer, _recipe

_KEY = "etc/bind/rndc.key"
_CREDENTIAL = "var/lib/systemd/credential.secret"
_HOST_KEY = "etc/ssh/ssh_host_ed25519_key"


def _plant(rootfs: Path, *paths: str) -> None:
    for rel in paths:
        (rootfs / rel).parent.mkdir(parents=True, exist_ok=True)
        (rootfs / rel).write_text(f"generated at build time: {rel}\n")


class _Wired:
    """What the install leaves behind, and every ``make_stage4`` call seen."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.planted: tuple[str, ...] = ()
        #: per call: which planted paths were still in the rootfs it packed
        self.packed: list[dict[str, bool]] = []

    @property
    def rootfs(self) -> Path:
        return self.tmp_path / "scratch" / "assemble" / "znver5-kde-systemd"

    def assemble(self, *, stage4: bool) -> tuple[list[dict[str, Any]], AssemblerError]:
        """Run the assemble; the audit steps and the error it ended with (the
        fake rootfs fails verify-config later for its own gaps)."""
        out = self.tmp_path / "out"
        out.mkdir(exist_ok=True)
        with (
            audit.run(self.tmp_path / "runs", command="assemble", argv=[]) as trail,
            pytest.raises(AssemblerError) as caught,
        ):
            Assembler(_recipe(), self.tmp_path / "binhost").assemble(out, stage4=stage4)
        steps = audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))["steps"]
        return steps, caught.value


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Wired:
    """The assemble of tests/test_assembler.py with the real system config (the
    shipped variants/livecd.yaml deny list) and the real finalize and verify;
    apply_system plants what the install left; make_stage4 records, packs nothing."""
    state = _Wired(tmp_path)
    _FakeContainer.instances = []
    _FakeTools.instances = []
    tree = tmp_path / "pinned-gentoo"
    tree.mkdir()
    tar = tmp_path / "fork-points" / "toolbox.tar"
    tar.parent.mkdir(parents=True)
    tar.write_bytes(b"T")

    def apply_system(container: Any, _cfg: object, *, init: str, build: object = None) -> dict:
        del init, build
        _plant(container.rootfs, *state.planted)
        return {}

    def make_stage4(
        rootfs: Path, dest: Path, exclude_file: Path, *, threads: int | None = None
    ) -> Path:
        del exclude_file, threads
        state.packed.append({rel: (rootfs / rel).exists() for rel in state.planted})
        dest.write_bytes(b"stage4")
        return dest

    monkeypatch.setattr(asm, "pinned_repos", lambda **_k: {"gentoo": tree})
    monkeypatch.setattr(toolbox, "tarball_path", lambda recipe, **_k: tar)
    monkeypatch.setattr(toolbox, "ensure_rootfs", lambda tarball, rootfs: False)
    monkeypatch.setattr(toolbox, "Toolbox", _FakeTools)
    monkeypatch.setattr(world, "current_atoms", lambda recipe, variants_dir: ("app-misc/a",))
    monkeypatch.setattr(asm, "apply_system", apply_system)
    monkeypatch.setattr(asm, "apply_live", lambda _c, _cfg, *, init: {})
    monkeypatch.setattr(publish, "make_stage4", make_stage4)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: _pointer())
    monkeypatch.setattr(
        asm, "fetch_stage3", lambda pointer, *, cache_dir, download: tmp_path / "s.tar"
    )
    monkeypatch.setattr(
        asm,
        "extract_stage3",
        lambda _t, rootfs: (rootfs / "lib" / "modules" / "6.12.0").mkdir(parents=True),
    )
    monkeypatch.setattr(asm, "apply_portage", lambda rootfs, recipe, **_k: None)
    monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])
    monkeypatch.setattr(asm, "Container", _FakeContainer)
    monkeypatch.setattr(image, "make_squashfs", lambda *_a, **_k: pytest.fail("squashed"))
    return state


def _step(steps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((s for s in steps if s["step"] == name), None)


def test_another_build_time_secret_fails_the_assemble_before_the_stage4_is_packed(
    wired: _Wired,
) -> None:
    """Hostile half: the removal is rndc.key's alone. Every other deny-listed path
    is NOT removed: the assemble fails at the stage4, naming each one, and no
    tarball is packed -- not later at verify-config, after one already was."""
    wired.planted = (_KEY, _CREDENTIAL, _HOST_KEY)
    steps, error = wired.assemble(stage4=True)
    assert wired.packed == [], "make_stage4 ran on a rootfs carrying a build-time secret"
    assert _step(steps, "live") is None, "the assemble went past the stage4"
    assert _CREDENTIAL in str(error) and _HOST_KEY in str(error), str(error)
    assert "rndc.key" not in str(error)  # removed first, so not left to name
    assert (wired.rootfs / _CREDENTIAL).is_file()
    assert (wired.rootfs / _HOST_KEY).is_file()


def test_the_stage4_step_removes_the_key_left_by_the_install_before_packing(
    wired: _Wired,
) -> None:
    """The key the install left is gone by the time the tarball is packed, and the
    ``stage4`` step -- not only ``finalize``, which runs after it -- says so."""
    wired.planted = (_KEY,)
    steps, _ = wired.assemble(stage4=True)
    assert wired.packed == [{_KEY: False}], wired.packed
    stage4 = _step(steps, "stage4")
    assert stage4 is not None
    assert stage4.get("removed") == [f"/{_KEY}"], stage4


def test_without_the_stage4_nothing_changes(wired: _Wired) -> None:
    """Guard: no stage4 asked, no stage4 step and no tarball, whatever is planted."""
    wired.planted = (_KEY,)
    steps, _ = wired.assemble(stage4=False)
    assert wired.packed == []
    assert _step(steps, "stage4") is None
    assert _step(steps, "live") is not None  # the assemble went on as before


def test_a_clean_rootfs_packs_the_stage4_with_no_removed_field(wired: _Wired) -> None:
    """Guard: nothing to remove, nothing deny-listed -- the stage4 is packed and its
    step carries no ``removed:`` field (not even an empty one)."""
    steps, _ = wired.assemble(stage4=True)
    assert wired.packed == [{}]
    stage4 = _step(steps, "stage4")
    assert stage4 is not None
    assert "removed" not in stage4, stage4
    assert _step(steps, "live") is not None
