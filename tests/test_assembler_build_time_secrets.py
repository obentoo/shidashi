"""Story 007, sub-task 2.2 -- a build-time secret fails the assemble before the squashfs.

The assemble runs in full with Container/seed/portage/image monkeypatched, as in
tests/test_assembler.py, but with the REAL verify(): the deny list must reach it
through the assembler's own configuration (variants/livecd.yaml), and the
verify-config step must fail naming the path, never packing the squashfs.

The secret planted is ``var/lib/systemd/credential.secret`` (the story's initial
deny list): unlike rndc.key, the live layer does not remove it, so it can only be
stopped by verify-config.
"""

import os
from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import image, toolbox, world
from shidashi.assembler import Assembler, AssemblerError
from tests.test_assembler import _FakeContainer, _FakeTools, _pointer, _recipe

_SECRET = "var/lib/systemd/credential.secret"


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Path]]:
    """The assemble of tests/test_assembler.py, with the real verify(); returns
    what was squashed and what the stage3 extraction planted."""
    _FakeContainer.instances = []
    _FakeTools.instances = []
    planted: list[Path] = []
    squashed: list[Path] = []
    tree = tmp_path / "pinned-gentoo"
    tree.mkdir()
    tar = tmp_path / "fork-points" / "toolbox.tar"
    tar.parent.mkdir(parents=True)
    tar.write_bytes(b"T")
    monkeypatch.setattr(asm, "pinned_repos", lambda **_k: {"gentoo": tree})
    monkeypatch.setattr(toolbox, "tarball_path", lambda recipe, **_k: tar)
    monkeypatch.setattr(toolbox, "ensure_rootfs", lambda tarball, rootfs: False)
    monkeypatch.setattr(toolbox, "Toolbox", _FakeTools)
    monkeypatch.setattr(world, "current_atoms", lambda recipe, variants_dir: ("app-misc/a",))
    monkeypatch.setattr(asm, "apply_system", lambda _c, _cfg, *, init, build=None: {})
    monkeypatch.setattr(asm, "apply_live", lambda _c, _cfg, *, init: {})
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: _pointer())
    monkeypatch.setattr(
        asm, "fetch_stage3", lambda pointer, *, cache_dir, download: tmp_path / "s.tar"
    )

    def fake_extract(_tarball: Path, rootfs: Path) -> None:
        (rootfs / "lib" / "modules" / "6.12.0").mkdir(parents=True)
        for rel in planted:
            (rootfs / rel).parent.mkdir(parents=True, exist_ok=True)
            (rootfs / rel).write_text("generated at build time\n")

    def make_squashfs(_rootfs: Path, output: Path, **_k: object) -> Path:
        squashed.append(output)
        return output

    monkeypatch.setattr(asm, "extract_stage3", fake_extract)
    monkeypatch.setattr(asm, "apply_portage", lambda rootfs, recipe, **_k: None)
    monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])
    monkeypatch.setattr(asm, "Container", _FakeContainer)
    monkeypatch.setattr(image, "make_squashfs", make_squashfs)
    return {"planted": planted, "squashed": squashed}


def _assemble(tmp_path: Path) -> AssemblerError:
    with pytest.raises(AssemblerError) as caught:
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")
    return caught.value


def test_a_build_time_secret_fails_verify_config_naming_the_path(
    tmp_path: Path, wired: dict[str, list[Path]]
) -> None:
    wired["planted"].append(Path(_SECRET))
    error = _assemble(tmp_path)
    assert "configuration did not apply" in str(error)  # the verify-config step
    assert _SECRET in str(error)
    assert wired["squashed"] == []  # never packed
    rootfs = tmp_path / "scratch" / "assemble" / "znver5-kde-systemd"
    assert (rootfs / _SECRET).is_file()  # kept for debugging, not cleaned away


def test_without_a_secret_the_failure_names_none(
    tmp_path: Path, wired: dict[str, list[Path]]
) -> None:
    """Hostile half: the fake rootfs fails verify for its own gaps (no hostname,
    no user); a secret check that fired on its own would name the path anyway."""
    error = _assemble(tmp_path)
    assert _SECRET not in str(error)
    assert wired["squashed"] == []
