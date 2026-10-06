"""Story 007, sub-task 3.1 -- the live layer removes /etc/bind/rndc.key, on the record.

net-dns/bind's pkg_postinst writes the key during the install, so every copy of an
ISO carried the same one. The live layer removes it after the last container
command and before verify-config (a resumed assemble passes through it too), and
says so in its step's audit fields. The removal is NAMED, not "whatever the deny
list matches": any other build-time secret still reaches verify-config and fails
the build, so the next leak is caught instead of hidden.
"""

import os
from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import audit, image, system, toolbox, world
from shidashi.assembler import Assembler, AssemblerError
from tests.test_assembler import _FakeContainer, _FakeTools, _pointer, _recipe
from tests.test_system import _Image, _image, _kde

_KEY = "etc/bind/rndc.key"


def _plant(rootfs: Path, *paths: str) -> None:
    for rel in paths:
        (rootfs / rel).parent.mkdir(parents=True, exist_ok=True)
        (rootfs / rel).write_text(f"generated at build time: {rel}\n")


def _live_layer(tmp_path: Path, *planted: str) -> tuple[Path, system.SystemConfig, list[object]]:
    """Configure a live image whose install left ``planted`` behind, as the
    assembler orders it: system, live, the build's resolv.conf, finalize.
    Returns the rootfs, its configuration and every step's audit fields."""
    cfg = _kde()
    img = _Image(_image(tmp_path))
    _plant(img.rootfs, *planted)
    records: list[object] = [system.apply_system(img, cfg, init="systemd")]
    records.append(system.apply_live(img, cfg, init="systemd", hasher=lambda p: "$6$salt$h"))
    (img.rootfs / "etc/resolv.conf").write_text("nameserver 8.8.8.8\n")
    records.append(system.finalize(img.rootfs, cfg, init="systemd"))
    return img.rootfs, cfg, records


def test_the_live_layer_removes_the_rndc_key_and_records_it(tmp_path: Path) -> None:
    rootfs, cfg, records = _live_layer(tmp_path, _KEY, "etc/bind/rndc.conf", "etc/bind/named.conf")
    key = rootfs / _KEY
    assert not key.exists() and not key.is_symlink()
    assert any(_KEY in repr(r) for r in records), records  # in the step's audit fields
    assert system.verify(rootfs, cfg, init="systemd", live=True) == []


def test_the_rndc_keys_public_neighbors_stay(tmp_path: Path) -> None:
    """Hostile half: removing "rndc.key" must not take bind's configuration with it."""
    rootfs, _, _ = _live_layer(tmp_path, _KEY, "etc/bind/rndc.conf", "etc/bind/named.conf")
    assert (rootfs / "etc/bind/rndc.conf").is_file()
    assert (rootfs / "etc/bind/named.conf").is_file()


def test_another_build_time_secret_is_not_removed_and_fails_verify(tmp_path: Path) -> None:
    """Hostile half: the removal is rndc.key's alone. A generic "delete what the
    deny list matches" would hide the next leak; verify must still name it."""
    other = "var/lib/systemd/credential.secret"
    rootfs, cfg, records = _live_layer(tmp_path, _KEY, other)
    assert (rootfs / other).is_file()
    assert not any(other in repr(r) for r in records)
    problems = system.verify(rootfs, cfg, init="systemd", live=True)
    assert len(problems) == 1 and other in problems[0]


def test_an_image_without_the_key_is_left_as_it_was(tmp_path: Path) -> None:
    """Unchanged: an image without net-dns/bind gains no /etc/bind and passes."""
    rootfs, cfg, _ = _live_layer(tmp_path)
    assert not (rootfs / "etc/bind").exists()
    assert system.verify(rootfs, cfg, init="systemd", live=True) == []


# --- in the assembler: after the last container command, before verify-config ---------


class _KeyWritingContainer(_FakeContainer):
    """The last container command (dracut) leaves the key behind, as a merge would."""

    def run(self, argv: list[str], **kw: object) -> object:
        if argv and argv[0] == "dracut":
            _plant(self.rootfs, _KEY)
        return super().run(argv, **kw)


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The assemble of tests/test_assembler.py with the real finalize and verify;
    apply_system/apply_live stubbed (they run before the last container command)."""
    _FakeContainer.instances = []
    _FakeTools.instances = []
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
    monkeypatch.setattr(
        asm,
        "extract_stage3",
        lambda _t, rootfs: (rootfs / "lib" / "modules" / "6.12.0").mkdir(parents=True),
    )
    monkeypatch.setattr(asm, "apply_portage", lambda rootfs, recipe, **_k: None)
    monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])
    monkeypatch.setattr(asm, "Container", _KeyWritingContainer)
    monkeypatch.setattr(image, "make_squashfs", lambda *_a, **_k: pytest.fail("squashed"))


def test_the_assembler_removes_a_key_left_by_the_last_container_command(
    tmp_path: Path, wired: None
) -> None:
    """The fake rootfs fails verify-config for its own gaps (no hostname, no
    user) and is kept: what matters is the key is gone from it, on the record of
    a step that is not verify-config (whose problems would name a key LEFT)."""
    with (
        audit.run(tmp_path / "runs", command="assemble", argv=[]) as trail,
        pytest.raises(AssemblerError) as caught,
    ):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")
    steps = audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))["steps"]
    rootfs = tmp_path / "scratch" / "assemble" / "znver5-kde-systemd"
    assert rootfs.is_dir()
    assert not (rootfs / _KEY).exists() and not (rootfs / _KEY).is_symlink()
    recorded = [s["step"] for s in steps if s["step"] != "verify-config" and _KEY in repr(s)]
    assert recorded, "no step records the removal"
    assert "rndc.key" not in str(caught.value)
