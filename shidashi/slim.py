"""What the ``slim`` assemble step removes from an image (story 021).

A build oven boots from a live rootfs that carried the whole minimal image: kernel
sources, every GPU and sound driver with its firmware, documentation, headers. An
image whose ``system.yaml`` declares ``slim:`` keeps its keep list and loses the
rest. This module decides WHAT goes, on a tree it only reads; the assembler does
the removing inside the build container:

- :func:`world_text` and :func:`depclean_argv` -- the world depclean is left with;
- :func:`module_dirs_to_drop` -- the kernel-module subtrees the image never loads;
- :func:`firmware_wanted` and :func:`firmware_to_delete` -- the firmware no
  remaining module asks for;
- :func:`prune_paths` -- documentation, headers, the compiler's internals;
- :func:`missing_commands` -- the runtime contract, proved after the rest.

Every path of the image is resolved inside it: an absolute symlink names the
image's own file, never the build host's. No subprocess runs here.
"""

import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

#: Compression suffixes linux-firmware installs while ``modinfo`` names the bare file.
_FIRMWARE_SUFFIXES = (".zst", ".xz")
#: Symlinks followed before a chain counts as a loop (the kernel's own limit).
_MAX_HOPS = 40
#: A kernel module file, plain or compressed.
_MODULE_FILE = re.compile(r"\.ko(\.[a-z0-9]+)?$")


@dataclass(frozen=True)
class SlimReport:
    """What the step removed: names and paths relative to the image, sizes in bytes."""

    removed_packages: tuple[str, ...]
    dropped_module_dirs: tuple[str, ...]
    deleted_firmware_files: tuple[str, ...]
    pruned_files: tuple[str, ...]
    bytes_before: int
    bytes_after: int


def world_text(keep: Iterable[str]) -> str:
    """The new ``/var/lib/portage/world``: the keep list, one atom per line. Pure."""
    return "".join(f"{atom}\n" for atom in keep)


def depclean_argv() -> list[str]:
    """Depclean the whole world, build dependencies included. Names no atom: a named
    depclean removes only those atoms, not what the world no longer reaches."""
    return ["emerge", "--depclean", "--with-bdeps=n", "--quiet"]


def installed_packages(root: Path) -> set[str]:
    """The ``category/PF`` of every package in the image's vdb (``var/db/pkg``),
    skipping Portage's in-progress ``-MERGING-`` entries. Read-only."""
    vdb = root / "var" / "db" / "pkg"
    if not _is_real_dir(vdb):
        return set()
    return {
        f"{entry.parent.name}/{entry.name}"
        for entry in vdb.glob("*/*")
        if _is_real_dir(entry) and not entry.name.startswith("-")
    }


def kernel_modules(kver_root: Path) -> list[Path]:
    """Every module file (``*.ko``, ``*.ko.zst``, ``*.ko.xz`` ...) under ``kver_root``,
    sorted; symlinks are neither listed nor walked through (``build``/``source``
    point into the kernel sources). Read-only."""
    return sorted(
        path
        for path in _entries(kver_root)
        if not path.is_symlink() and _MODULE_FILE.search(path.name)
    )


def builtin_firmware(kver_root: Path) -> list[str]:
    """The firmware the kernel's BUILT-IN drivers declare: the ``<module>.firmware=``
    records of ``kver_root/modules.builtin.modinfo`` (NUL-separated). A built-in
    driver has no ``.ko`` for ``modinfo`` to read; no file is no record. Read-only."""
    index = kver_root / "modules.builtin.modinfo"
    if not index.is_file() or index.is_symlink():
        return []
    names: list[str] = []
    for record in index.read_bytes().split(b"\0"):
        key, sep, value = record.decode("utf-8", errors="replace").partition("=")
        if sep and key.endswith(".firmware") and value.strip():
            names.append(value.strip())
    return names


def module_dirs_to_drop(kver_root: Path, drop: Iterable[str]) -> list[Path]:
    """The subtrees of ``kver_root/kernel`` named by ``drop`` that exist. Read-only."""
    kernel = kver_root / "kernel"
    return [kernel / sub for sub in drop if _is_real_dir(kernel / sub)]


def unmatched_module_groups(kver_root: Path, drop: Iterable[str]) -> list[str]:
    """The entries of ``drop`` this kernel lacks -- reported, never an error: kernel
    layouts move between versions. Read-only."""
    kernel = kver_root / "kernel"
    return [sub for sub in drop if not _is_real_dir(kernel / sub)]


def firmware_wanted(modinfo_lines: Iterable[str]) -> set[str]:
    """The names ``modinfo -F firmware`` printed, one per line, blanks ignored. Pure."""
    return {name for line in modinfo_lines if (name := line.strip())}


def firmware_to_delete(
    firmware_root: Path, wanted: set[str], keep_globs: Iterable[str]
) -> list[Path]:
    """The files and symlinks under ``firmware_root`` (the image's ``usr/lib/firmware``)
    that no remaining module asks for and no ``keep_globs`` pattern keeps. Read-only.

    A name matches with its ``.zst``/``.xz`` suffix stripped; a wanted name holding
    ``*``, ``?`` or ``[`` is a pattern, as ``modinfo`` prints for ath11k or brcmfmac.
    A kept symlink keeps every link and file its chain reaches inside the image (a
    directory: everything under it); a symlink nobody asks for goes, even when the
    file it names stays.
    """
    if firmware_root.parts[-3:] != ("usr", "lib", "firmware"):
        raise ValueError(f"{firmware_root} is not an image's usr/lib/firmware")
    image_root = firmware_root.parents[2]
    literals = {name for name in wanted if not _is_pattern(name)}
    patterns = [name for name in wanted if _is_pattern(name)]
    globs = tuple(keep_globs)
    entries = list(_entries(firmware_root))
    kept: set[Path] = set()
    for path in entries:
        rel = path.relative_to(firmware_root).as_posix()
        bare = _bare(rel)
        if (
            bare in literals
            or any(fnmatchcase(bare, p) for p in patterns)
            or any(fnmatchcase(rel, g) or fnmatchcase(bare, g) for g in globs)
        ):
            kept.add(path)
            if path.is_symlink():
                kept.update(reached(image_root, _in_image(image_root, path)))
    # a wanted name may cross a directory link no entry's own path shows
    fw_rel = firmware_root.relative_to(image_root).as_posix()
    for name in literals:
        for suffix in ("", *_FIRMWARE_SUFFIXES):
            kept.update(reached(image_root, f"{fw_rel}/{name}{suffix}"))
    kept_dirs = [p for p in kept if _is_real_dir(p)]
    return [
        path
        for path in entries
        if path not in kept and not any(path.is_relative_to(d) for d in kept_dirs)
    ]


def prune_paths(root: Path, globs: Iterable[str]) -> list[Path]:
    """The files and symlinks under ``root`` matching a glob relative to it, where
    ``**`` spans any number of directories and ``*`` stays within one. Never walks
    through a symlink. Read-only."""
    patterns = [tuple(g.split("/")) for g in globs]
    pruned: list[Path] = []
    for path in _entries(root):
        parts = path.relative_to(root).parts
        if any(_glob_match(p, parts) for p in patterns):
            pruned.append(path)
    return pruned


def missing_commands(root: Path, commands: Iterable[str]) -> list[str]:
    """The ``commands`` that do not resolve, inside ``root``, to an executable regular
    file: absent, dangling, a loop, a directory, or without an execute bit. Read-only."""
    missing: list[str] = []
    for command in commands:
        resolved = resolve_in(root, command)
        if resolved is None or not resolved.is_file() or not resolved.stat().st_mode & 0o111:
            missing.append(command)
    return missing


def resolve_in(root: Path, path: str) -> Path | None:
    """``path`` as the image rooted at ``root`` sees it, every symlink followed inside
    the image (an absolute target re-rooted, ``..`` stopping at the root); ``None``
    when a component is missing or the chain loops. Read-only."""
    return _walk(root, path)[0]


def _walk(root: Path, path: str) -> tuple[Path | None, set[Path]]:
    """:func:`resolve_in`'s result, and every symlink crossed on the way, each named
    under its real parent directory -- the path an image walk lists it by."""
    pending = _segments(path)
    resolved: list[str] = []
    crossed: set[Path] = set()
    hops = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink():
            crossed.add(candidate)
            hops += 1
            if hops > _MAX_HOPS:
                return None, crossed
            target = os.readlink(candidate)
            if target.startswith("/"):
                resolved = []
            pending = _segments(target) + pending
        elif candidate.exists():
            resolved.append(part)
        else:
            return None, crossed
    return root.joinpath(*resolved), crossed


def reached(root: Path, path: str) -> set[Path]:
    """The links crossed resolving ``path`` in the image, and where it lands. Read-only."""
    final, crossed = _walk(root, path)
    return crossed if final is None else crossed | {final}


def _in_image(root: Path, path: Path) -> str:
    return "/" + path.relative_to(root).as_posix()


def _is_pattern(name: str) -> bool:
    return any(c in name for c in "*?[")


def _segments(path: str) -> list[str]:
    return [part for part in path.split("/") if part not in ("", ".")]


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _entries(top: Path) -> Iterator[Path]:
    """Every file and symlink under ``top``; a symlink to a directory is an entry,
    never walked through."""
    for dirpath, dirnames, filenames in os.walk(top):
        base = Path(dirpath)
        yield from (base / name for name in dirnames if (base / name).is_symlink())
        yield from (base / name for name in filenames)


def _bare(name: str) -> str:
    for suffix in _FIRMWARE_SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def _glob_match(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """Whether path ``parts`` match glob ``pattern``, segment by segment."""
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_glob_match(rest, parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], head) and _glob_match(rest, parts[1:])
