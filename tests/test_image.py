"""Testes de shidashi.image — squashfs + live medium da ISO (OVERVIEW §7, Fase 1).

No idioma de tests/test_container.py: os construtores de argv e o ``grub.cfg`` são
**puros** (testados sem root nem ferramentas), o staging da árvore do ISO é I/O de
arquivo puro (tmp), e as execuções (``make_squashfs``/``build_iso``) são exercidas
com ``shutil.which`` e ``subprocess.run`` monkeypatchados — sem invocar
``mksquashfs``/``grub-mkrescue`` reais (host-gated).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

import shidashi.image as img
from shidashi.image import (
    VOLUME_ID,
    ImageError,
    _grub_cfg,
    _grub_mkrescue_argv,
    _mksquashfs_argv,
    _stage_iso_tree,
    build_iso,
    make_squashfs,
)

# --- construtores de argv (PUROS) --------------------------------------------


def test_mksquashfs_argv_form_and_defaults() -> None:
    argv = _mksquashfs_argv(Path("/r"), Path("/o.sq"), compression="zstd", level=19)
    assert argv == [
        "mksquashfs",
        "/r",
        "/o.sq",
        "-comp",
        "zstd",
        "-Xcompression-level",
        "19",
        "-noappend",
        "-no-progress",
        "-e",
        "boot",
        "proc",
        "sys",
        "dev",
        "run",
        "var/cache/binpkgs",
    ]


def test_mksquashfs_argv_excludes_boot_and_volatile_dirs() -> None:
    # boot (kernel+initramfs vão no boot/ da ISO, não na raiz live) + voláteis.
    argv = _mksquashfs_argv(Path("/r"), Path("/o.sq"), compression="zstd", level=19)
    sep = argv.index("-e")
    excludes = argv[sep + 1 :]
    assert "boot" in excludes
    for volatile in ("proc", "sys", "dev", "run"):
        assert volatile in excludes


def test_grub_mkrescue_argv_passes_volid_after_separator() -> None:
    argv = _grub_mkrescue_argv(Path("/iso"), Path("/out.iso"), volume_id="BENTOO")
    assert argv == ["grub-mkrescue", "-o", "/out.iso", "/iso", "--", "-volid", "BENTOO"]


def test_grub_cfg_references_liveos_and_volume() -> None:
    cfg = _grub_cfg(volume_id="BENTOO")
    # cmdline do dmsquash-live precisa do CDLABEL + rd.live.image e dos artefatos.
    assert "root=live:CDLABEL=BENTOO" in cfg
    assert "rd.live.image" in cfg
    assert "/boot/vmlinuz" in cfg
    assert "/boot/initramfs.img" in cfg
    assert "set timeout=10" in cfg  # default público do menu


# --- staging da árvore do ISO (I/O puro) -------------------------------------


def test_stage_iso_tree_lays_out_live_medium(tmp_path: Path) -> None:
    squashfs = tmp_path / "rootfs.squashfs"
    kernel = tmp_path / "vmlinuz-1.2.3"
    initramfs = tmp_path / "initramfs-1.2.3.img"
    for p, data in ((squashfs, b"SQ"), (kernel, b"K"), (initramfs, b"I")):
        p.write_bytes(data)
    iso_root = tmp_path / "iso"

    _stage_iso_tree(squashfs, kernel, initramfs, iso_root)

    assert (iso_root / "LiveOS" / "squashfs.img").read_bytes() == b"SQ"
    assert (iso_root / "boot" / "vmlinuz").read_bytes() == b"K"
    assert (iso_root / "boot" / "initramfs.img").read_bytes() == b"I"
    assert f"CDLABEL={VOLUME_ID}" in (iso_root / "boot" / "grub" / "grub.cfg").read_text()


# --- make_squashfs (execução monkeypatchada) ---------------------------------


def test_make_squashfs_invokes_tool_and_returns_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/mksquashfs")
    calls: list[list[str]] = []

    def record(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok()

    monkeypatch.setattr(subprocess, "run", record)
    out = tmp_path / "nested" / "rootfs.squashfs"

    result = make_squashfs(tmp_path / "rootfs", out)

    assert result == out
    assert out.parent.is_dir()  # diretório-pai criado
    assert calls == [_mksquashfs_argv(tmp_path / "rootfs", out, compression="zstd", level=19)]


def test_make_squashfs_missing_tool_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ImageError, match="mksquashfs"):
        make_squashfs(Path("/r"), Path("/o.sq"))


def test_make_squashfs_nonzero_wraps_in_image_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/mksquashfs")

    def boom(argv: list[str], **kw: object) -> None:
        raise subprocess.CalledProcessError(1, argv, stderr="disk full")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ImageError, match="mksquashfs"):
        make_squashfs(tmp_path / "r", tmp_path / "o.sq")


# --- build_iso (execução monkeypatchada) -------------------------------------


def test_build_iso_stages_tree_and_invokes_grub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/grub-mkrescue")
    squashfs = tmp_path / "r.squashfs"
    kernel = tmp_path / "vmlinuz-1"
    initramfs = tmp_path / "initramfs-1.img"
    for p in (squashfs, kernel, initramfs):
        p.write_bytes(b"x")
    out = tmp_path / "dist" / "bentoo.iso"

    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        # a árvore temporária do ISO deve existir no momento do grub-mkrescue.
        iso_root = Path(argv[3])
        assert (iso_root / "LiveOS" / "squashfs.img").is_file()
        seen.append(argv)
        return _ok()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = build_iso(squashfs, out, kernel=kernel, initramfs=initramfs)

    assert result == out
    assert out.parent.is_dir()
    assert len(seen) == 1 and seen[0][0] == "grub-mkrescue"
    assert seen[0][:3] == ["grub-mkrescue", "-o", str(out)]
    assert seen[0][-3:] == ["--", "-volid", VOLUME_ID]


def test_build_iso_missing_tool_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ImageError, match="grub-mkrescue"):
        build_iso(
            tmp_path / "r.sq", tmp_path / "o.iso", kernel=tmp_path / "k", initramfs=tmp_path / "i"
        )


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# NB: a execução REAL (mksquashfs/grub-mkrescue/xorriso de verdade) é host-gated
# (exige as ferramentas instaladas + um rootfs com kernel); fica para o smoke-test
# de boot da Fase 1 (QEMU), não para o unit off-host.
assert img is not None  # módulo importa sem acionar portage_api (R9.1)
