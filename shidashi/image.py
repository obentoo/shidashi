"""Image — the live squashfs and the ISO's layout (OVERVIEW §7).

Command-line builders are **pure and inspectable** (testable without root); the
thin runners over them need the host tools and are exercised by host-gated tests.

* :func:`make_squashfs` packs the rootfs into a read-only squashfs, with a
  compression profile (:data:`COMPRESSION`) and the exclude list of
  ``variants/livecd.yaml`` (``squashfs_exclude``).
* :func:`build_iso` lays the medium out the way the major distributions do and
  makes a hybrid BIOS+UEFI ISO with ``grub-mkrescue`` (which drives xorriso)::

      .disk/info                  one line naming the medium      (Debian, Ubuntu)
      .disk/id                    the build's id                  (Gentoo)
      bentoo/                     version, build.json, packages.txt, world, SBOM
                                                                  (Arch, Fedora, openSUSE)
      LiveOS/squashfs.img         the root, dracut dmsquash-live's convention
      LiveOS/squashfs.img.sha512  its checksum                    (Arch)
      boot/vmlinuz, initramfs.img, grub/grub.cfg
"""

import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "COMPRESSION",
    "ImageError",
    "VOLUME_ID",
    "build_iso",
    "make_squashfs",
    "volume_id",
]

#: The default volume label; an image's own is :func:`volume_id`.
VOLUME_ID = "BENTOO"

# The medium's layout. The squashfs is the RAW root (not Fedora's nested
# LiveOS/rootfs.img): dmsquash-live accepts that when the root has /usr on top --
# every assembled rootfs does (usr-merged Gentoo). Otherwise it stops with
# "Failed to find a root filesystem" (seen by the smoke test, §7).
_LIVEOS_IMG = "LiveOS/squashfs.img"
_ISO_KERNEL = "boot/vmlinuz"
_ISO_INITRD = "boot/initramfs.img"

#: mksquashfs settings per profile, measured on a 4.6 GB sample of the kde rootfs
#: (2026-09-30), against zstd 19 with 128K blocks (the first ISO's):
#: zstd 19 + 1M blocks: -5.7%, same decompression speed -- the default, the
#: faster live session; xz + 1M + BCJ x86: -9.9%, 2.3x slower to decompress --
#: the optional smaller download (Arch's settings). zstd 22 gained nothing.
COMPRESSION: dict[str, tuple[str, ...]] = {
    "zstd": ("-comp", "zstd", "-Xcompression-level", "19", "-b", "1M"),
    "xz": ("-comp", "xz", "-b", "1M", "-Xbcj", "x86"),
}


class ImageError(Exception):
    """The squashfs or the ISO could not be made (OVERVIEW §7).

    Raised for a host tool that is missing and for a non-zero exit, always with
    a message that names the command and carries its stderr.
    """


def volume_id(flavor: str) -> str:
    """The ISO's volume label, per image: ``BENTOO_KDE``. Pure.

    ISO 9660 d-characters only (A-Z, 0-9, _), at most 32. Per flavor so that two
    Bentoo media plugged in at once do not answer to the same
    ``root=live:CDLABEL=``.
    """
    clean = "".join(c if c.isalnum() else "_" for c in flavor.upper())
    return f"{VOLUME_ID}_{clean}"[:32]


def _require_tool(tool: str) -> None:
    if shutil.which(tool) is None:
        raise ImageError(
            f"{tool!r} is missing on the host; the ISO needs squashfs-tools, grub and xorriso"
        )


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as err:
        raise ImageError(f"command failed ({argv[0]}): {' '.join(argv)}\n{err.stderr}") from err


def _mksquashfs_argv(
    rootfs: Path,
    output: Path,
    *,
    compression: str = "zstd",
    exclude_file: Path | None = None,
    processors: int | None = None,
) -> list[str]:
    """The ``mksquashfs`` command line. Pure.

    ``-noappend``: never append to an existing image. ``-wildcards -ef``: the
    exclude list, where ``dir/*`` keeps the directory (a mount point) and drops
    its content. ``processors`` caps the threads (``None``: every CPU).
    """
    if compression not in COMPRESSION:
        raise ImageError(f"unknown compression {compression!r}; known: {', '.join(COMPRESSION)}")
    cap = ["-processors", str(processors)] if processors is not None else []
    exclude = ["-wildcards", "-ef", str(exclude_file)] if exclude_file is not None else []
    return [
        "mksquashfs", str(rootfs), str(output), *COMPRESSION[compression],
        "-noappend", "-no-progress", *cap, *exclude,
    ]


def make_squashfs(
    rootfs: Path,
    output: Path,
    *,
    compression: str = "zstd",
    exclude_file: Path | None = None,
    processors: int | None = None,
) -> Path:
    """Pack ``rootfs`` into ``output`` and return it. Needs ``mksquashfs``."""
    _require_tool("mksquashfs")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(_mksquashfs_argv(rootfs, output, compression=compression,
                          exclude_file=exclude_file, processors=processors))
    return output


def _linux(volume: str, extra: str = "") -> str:
    args = f"root=live:CDLABEL={volume} rd.live.image quiet splash {extra}".strip()
    return f"    linux /{_ISO_KERNEL} {args}\n    initrd /{_ISO_INITRD}\n"


#: The open-driver boot of an image that ships nvidia-drivers. That package's
#: modprobe.d blacklists nouveau, so by default the proprietary driver takes the
#: GPU -- and >=595 only drives Turing and newer, leaving Maxwell and Pascal with
#: no driver at all. This keeps the proprietary modules out and loads nouveau
#: explicitly in the initramfs (nouveau.ko is there; an explicit load ignores a
#: blacklist, which only stops alias autoloading).
OPEN_NVIDIA_ARGS = (
    "modprobe.blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm rd.driver.pre=nouveau"
)


def _grub_cfg(
    *,
    volume: str,
    title: str,
    text_target: str | None,
    open_nvidia: bool = False,
    timeout: int = 10,
) -> str:
    """The medium's boot menu. Pure.

    The entries the major distributions offer (Fedora, Ubuntu, Gentoo): the live
    session; safe graphics (``nomodeset``) for the GPU a driver cannot light up;
    copy to RAM (``rd.live.ram=1``) to free the USB stick; a text console
    (``text_target``, systemd's multi-user.target) to repair without a
    desktop; the UEFI firmware settings; reboot and power off. ``open_nvidia``
    adds the nouveau boot for NVIDIA GPUs the proprietary driver dropped
    (:data:`OPEN_NVIDIA_ARGS`).
    """
    entries = [
        (title, _linux(volume)),
        (f"{title} (safe graphics)", _linux(volume, "nomodeset")),
        *([(f"{title} (open NVIDIA driver)", _linux(volume, OPEN_NVIDIA_ARGS))]
          if open_nvidia else []),
        (f"{title} (copy to RAM)", _linux(volume, "rd.live.ram=1")),
    ]
    if text_target is not None:
        entries.append((f"{title} (text console)", _linux(volume, f"systemd.unit={text_target}")))
    lines = [f"set timeout={timeout}", "set default=0", ""]
    for name, body in entries:
        lines.append(f'menuentry "{name}" {{\n{body}}}')
    lines += [
        'if [ "${grub_platform}" = "efi" ]; then',
        '    menuentry "UEFI firmware settings" { fwsetup }',
        "fi",
        'menuentry "Reboot" { reboot }',
        'menuentry "Power off" { halt }',
    ]
    return "\n".join(lines) + "\n"


def _grub_mkrescue_argv(iso_root: Path, output: Path, *, volume_id: str) -> list[str]:
    """The ``grub-mkrescue`` command line (hybrid BIOS+UEFI). Pure.

    ``-iso-level 3`` lets a file exceed 4 GiB -- the squashfs of a desktop image
    does (8.26 GB for kde). It must come BEFORE ``iso_root``: grub-mkrescue
    passes it to ``xorriso -as mkisofs`` ahead of the tree, while everything after
    ``--`` reaches xorriso's native mode only after the files are grafted, too
    late (measured 2026-09-30 with a 4200 MiB file, F78). ``-volid`` sets the label
    the kernel command line looks for.
    """
    return [
        "grub-mkrescue", "-o", str(output), "-iso-level", "3", str(iso_root),
        "--", "-volid", volume_id,
    ]


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _stage_iso_tree(
    iso_root: Path,
    *,
    squashfs: Path,
    kernel: Path,
    initramfs: Path,
    volume: str,
    title: str,
    build_id: str,
    text_target: str | None,
    extra: Mapping[str, str | Path],
    open_nvidia: bool = False,
) -> None:
    """Lay the medium out in ``iso_root`` (see the module's docstring). I/O only.

    ``extra`` maps a path inside the ISO to its text or to a file to copy.
    Files are copied with reflinks where the filesystem has them (btrfs), so an
    8 GB squashfs costs no time and no space.
    """
    (iso_root / "LiveOS").mkdir(parents=True, exist_ok=True)
    (iso_root / "boot" / "grub").mkdir(parents=True, exist_ok=True)
    (iso_root / ".disk").mkdir(exist_ok=True)
    _copy(squashfs, iso_root / _LIVEOS_IMG)
    (iso_root / f"{_LIVEOS_IMG}.sha512").write_text(
        f"{_sha512(squashfs)}  squashfs.img\n", encoding="utf-8"
    )
    _copy(kernel, iso_root / _ISO_KERNEL)
    _copy(initramfs, iso_root / _ISO_INITRD)
    (iso_root / "boot" / "grub" / "grub.cfg").write_text(
        _grub_cfg(volume=volume, title=title, text_target=text_target, open_nvidia=open_nvidia),
        encoding="utf-8",
    )
    (iso_root / ".disk" / "info").write_text(f"{title} ({build_id})\n", encoding="utf-8")
    (iso_root / ".disk" / "id").write_text(f"{build_id}\n", encoding="utf-8")
    for rel, content in extra.items():
        dest = iso_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Path):
            _copy(content, dest)
        else:
            dest.write_text(content, encoding="utf-8")


def _copy(src: Path, dest: Path) -> None:
    """``cp --reflink=auto``: instant on btrfs, a plain copy elsewhere."""
    try:
        subprocess.run(["cp", "--reflink=auto", "--", str(src), str(dest)], check=True,
                       capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        shutil.copy2(src, dest)


def build_iso(
    squashfs: Path,
    output: Path,
    *,
    kernel: Path,
    initramfs: Path,
    volume: str = VOLUME_ID,
    title: str = "Bentoo",
    build_id: str = "",
    text_target: str | None = None,
    open_nvidia: bool = False,
    extra: Mapping[str, str | Path] | None = None,
) -> Path:
    """Make the hybrid live ISO ``output`` and return it. Needs ``grub-mkrescue``.

    The medium's tree is staged beside ``output`` -- on the same filesystem, so
    the squashfs is reflinked, not copied into /tmp (a tmpfs: the first ISOs
    copied 8 GB into RAM there).
    """
    _require_tool("grub-mkrescue")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".shidashi-iso-", dir=output.parent) as tmp:
        iso_root = Path(tmp)
        _stage_iso_tree(
            iso_root, squashfs=squashfs, kernel=kernel, initramfs=initramfs, volume=volume,
            title=title, build_id=build_id, text_target=text_target,
            open_nvidia=open_nvidia, extra=extra or {},
        )
        _run(_grub_mkrescue_argv(iso_root, output, volume_id=volume))
    return output
