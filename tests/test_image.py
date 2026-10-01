"""Tests of shidashi.image -- the live squashfs and the ISO's layout (OVERVIEW §7).

The argv builders and grub.cfg are pure (no root, no tools); staging the ISO tree
is plain file I/O in tmp; the runners are exercised through a fake
:class:`~shidashi.toolbox.Tools` -- no real mksquashfs or grub-mkrescue.
"""

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from shidashi.image import (
    COMPRESSION,
    VOLUME_ID,
    ImageError,
    _grub_cfg,
    _grub_mkrescue_argv,
    _mksquashfs_argv,
    _stage_iso_tree,
    build_iso,
    make_squashfs,
    volume_id,
)

# --- argv builders (pure) --------------------------------------------------------


def test_mksquashfs_argv_default_is_zstd_19_with_1m_blocks() -> None:
    """The faster live session: 1M blocks gave -5.7% at the same speed (2026-09-30)."""
    argv = _mksquashfs_argv(Path("/r"), Path("/o.sq"))
    assert argv == [
        "mksquashfs",
        "/r",
        "/o.sq",
        "-comp",
        "zstd",
        "-Xcompression-level",
        "19",
        "-b",
        "1M",
        "-noappend",
        "-no-progress",
    ]


def test_mksquashfs_argv_xz_profile_is_archs_smaller_one() -> None:
    argv = _mksquashfs_argv(Path("/r"), Path("/o.sq"), compression="xz")
    assert argv[3:9] == ["-comp", "xz", "-b", "1M", "-Xbcj", "x86"]
    assert set(COMPRESSION) == {"zstd", "xz"}


def test_mksquashfs_argv_reads_the_exclude_list_with_wildcards() -> None:
    """``dir/*`` keeps the directory -- the mount points the first ISO dropped."""
    argv = _mksquashfs_argv(Path("/r"), Path("/o.sq"), exclude_file=Path("/x"), processors=4)
    assert argv[-5:] == ["-processors", "4", "-wildcards", "-ef", "/x"]


def test_mksquashfs_argv_refuses_an_unknown_profile() -> None:
    with pytest.raises(ImageError, match="lz4"):
        _mksquashfs_argv(Path("/r"), Path("/o.sq"), compression="lz4")


def test_the_repository_exclude_list_keeps_mount_points() -> None:
    from shidashi import config
    from shidashi.system import load_livecd

    patterns = load_livecd(config.variants_dir()).squashfs_exclude
    for mount in ("dev", "proc", "sys", "run", "tmp", "boot"):
        assert f"{mount}/*" in patterns and mount not in patterns
    assert "var/log/*.log" in patterns and "var/cache/binpkgs/*" in patterns


def test_grub_mkrescue_argv_passes_volid_after_separator() -> None:
    argv = _grub_mkrescue_argv(Path("/iso"), Path("/out.iso"), volume_id="BENTOO_KDE")
    assert argv == [
        "grub-mkrescue",
        "-o",
        "/out.iso",
        "-iso-level",
        "3",
        "/iso",
        "--",
        "-volid",
        "BENTOO_KDE",
    ]


def test_grub_mkrescue_argv_allows_files_over_4gib_before_the_tree() -> None:
    """The squashfs of a desktop image exceeds 4 GiB; after "--" the option would
    reach xorriso only once the tree is grafted, and it refuses the file (F78)."""
    argv = _grub_mkrescue_argv(Path("/iso"), Path("/out.iso"), volume_id="B")
    assert argv.index("-iso-level") < argv.index("/iso") < argv.index("--")


def test_volume_id_is_per_flavor_and_iso9660_safe() -> None:
    assert volume_id("kde") == "BENTOO_KDE"
    assert volume_id("wm-sway") == "BENTOO_WM_SWAY"
    assert len(volume_id("x" * 40)) == 32
    assert VOLUME_ID == "BENTOO"


def test_grub_cfg_offers_the_entries_the_major_distributions_do() -> None:
    cfg = _grub_cfg(volume="BENTOO_KDE", title="Bentoo KDE", text_target="multi-user.target")
    assert cfg.count("root=live:CDLABEL=BENTOO_KDE rd.live.image") == 4
    for entry in (
        "Bentoo KDE",
        "(safe graphics)",
        "(copy to RAM)",
        "(text console)",
        "UEFI firmware settings",
        "Reboot",
        "Power off",
    ):
        assert entry in cfg
    assert "nomodeset" in cfg and "rd.live.ram=1" in cfg
    assert "systemd.unit=multi-user.target" in cfg
    assert "initrd /boot/initramfs.img" in cfg


def test_grub_cfg_without_a_text_target_has_no_console_entry() -> None:
    cfg = _grub_cfg(volume="B", title="T", text_target=None)
    assert "(text console)" not in cfg


# --- the ISO tree (file I/O) -----------------------------------------------------


def test_stage_iso_tree_lays_out_the_medium_and_its_metadata(tmp_path: Path) -> None:
    squashfs, kernel, initramfs = (tmp_path / n for n in ("r.sq", "vmlinuz", "initrd"))
    squashfs.write_bytes(b"SQ")
    kernel.write_bytes(b"K")
    initramfs.write_bytes(b"I")
    sbom = tmp_path / "sbom.json"
    sbom.write_text("{}")
    root = tmp_path / "iso"
    _stage_iso_tree(
        root,
        squashfs=squashfs,
        kernel=kernel,
        initramfs=initramfs,
        volume="BENTOO_KDE",
        title="Bentoo KDE",
        build_id="20260930T0100Z-abc",
        text_target=None,
        extra={"bentoo/version": "bentoo-x\n", "bentoo/sbom.spdx.json": sbom},
    )
    assert (root / "LiveOS/squashfs.img").read_bytes() == b"SQ"
    assert (root / "LiveOS/squashfs.img.sha512").read_text() == (
        f"{hashlib.sha512(b'SQ').hexdigest()}  squashfs.img\n"
    )
    assert (root / "boot/vmlinuz").read_bytes() == b"K"
    assert (root / "boot/initramfs.img").read_bytes() == b"I"
    assert "BENTOO_KDE" in (root / "boot/grub/grub.cfg").read_text()
    assert (root / ".disk/info").read_text() == "Bentoo KDE (20260930T0100Z-abc)\n"
    assert (root / ".disk/id").read_text() == "20260930T0100Z-abc\n"
    assert (root / "bentoo/version").read_text() == "bentoo-x\n"
    assert (root / "bentoo/sbom.spdx.json").read_text() == "{}"


# --- runners (through a fake Tools) ----------------------------------------------


class _Tools:
    """Records each command; the tools see ``/work/<path relative to root>``."""

    def __init__(self, root: Path, *, fail: str | None = None) -> None:
        self.root = root
        self.fail = fail
        self.calls: list[list[str]] = []

    def path(self, host: Path) -> Path:
        return Path("/work") / host.relative_to(self.root)

    def run(self, argv: Sequence[str]) -> str:
        if self.fail is not None:
            raise subprocess.CalledProcessError(1, list(argv), output="", stderr=self.fail)
        self.calls.append(list(argv))
        return ""


def test_make_squashfs_runs_in_the_tools_with_their_paths(tmp_path: Path) -> None:
    tools = _Tools(tmp_path)
    out = tmp_path / "nested" / "rootfs.squashfs"
    exclude = tmp_path / "exclude"
    assert (
        make_squashfs(tmp_path / "rootfs", out, tools=tools, compression="xz", exclude_file=exclude)
        == out
    )
    assert out.parent.is_dir()  # created on the host side
    assert tools.calls == [
        _mksquashfs_argv(
            Path("/work/rootfs"),
            Path("/work/nested/rootfs.squashfs"),
            compression="xz",
            exclude_file=Path("/work/exclude"),
        )
    ]


def test_make_squashfs_nonzero_wraps_in_image_error(tmp_path: Path) -> None:
    with pytest.raises(ImageError, match="disk full"):
        make_squashfs(tmp_path / "r", tmp_path / "o.sq", tools=_Tools(tmp_path, fail="disk full"))


def test_build_iso_stages_beside_the_output_not_in_tmp(tmp_path: Path) -> None:
    """/tmp is a tmpfs here: the first ISOs copied the 8 GB squashfs into RAM."""
    squashfs, kernel, initramfs = (tmp_path / n for n in ("r.sq", "vmlinuz-1", "initramfs-1"))
    for p in (squashfs, kernel, initramfs):
        p.write_bytes(b"x")
    out = tmp_path / "dist" / "bentoo.iso"

    class _Staged(_Tools):
        def run(self, argv: Sequence[str]) -> str:
            iso_root = tmp_path / Path(argv[argv.index("--") - 1]).relative_to("/work")
            assert (iso_root / "LiveOS" / "squashfs.img").is_file()
            assert iso_root.parent == out.parent  # the same filesystem as the ISO
            return super().run(argv)

    tools = _Staged(tmp_path)
    result = build_iso(
        squashfs, out, tools=tools, kernel=kernel, initramfs=initramfs, volume="BENTOO_KDE"
    )
    assert result == out
    assert tools.calls[0][:3] == ["grub-mkrescue", "-o", "/work/dist/bentoo.iso"]
    assert tools.calls[0][-3:] == ["--", "-volid", "BENTOO_KDE"]
    assert not any(p.name.startswith(".shidashi-iso-") for p in out.parent.iterdir())


def test_grub_cfg_offers_the_open_nvidia_driver_only_when_asked() -> None:
    """An image with nvidia-drivers blacklists nouveau; the entry brings it back
    for the GPUs the proprietary driver dropped."""
    from shidashi.image import OPEN_NVIDIA_ARGS

    without = _grub_cfg(volume="B", title="T", text_target=None)
    assert "open NVIDIA driver" not in without and "nouveau" not in without
    cfg = _grub_cfg(volume="B", title="T", text_target=None, open_nvidia=True)
    assert 'menuentry "T (open NVIDIA driver)"' in cfg
    assert "modprobe.blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm" in OPEN_NVIDIA_ARGS
    assert "rd.driver.pre=nouveau" in OPEN_NVIDIA_ARGS
    assert cfg.index("(safe graphics)") < cfg.index("(open NVIDIA driver)")
    assert cfg.startswith("set timeout=10\nset default=0")  # the proprietary boot stays first
