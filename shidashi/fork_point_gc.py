"""Which restore points a gc deletes and which it keeps (``shidashi fork-points gc``).

Every restore point's name carries the pin id it was built from -- the stage fork points,
the bootstrap checkpoint, the per-phase snapshots and the update's images alike
(``<arch>-...-<pin id>-<build key>-<what>.tar``, OVERVIEW §6) -- and a build under a new
pin never restores one of another pin (D7). Nothing else ever deleted them: on 2026-11-15
the v3 worker's disk was at 90% with the 19 tarballs of two superseded pins on it, ~140 GiB
apparent; deleting the older pin's 11 (79.8 GiB apparent) freed 33 GiB of its
zstd-compressed btrfs.

The gc keeps the current pin's and those of the ``keep`` newest other pins: the update
restores the newest older pin's image (:func:`shidashi.factory.update_source`), and a
reverted re-pin restores its own pin's again. The plan reads names only; listing the
directory and deleting are the two I/O functions.
"""

import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: The end of every restore point's name. Neither the pin id (:func:`shidashi.tree.pin_id`)
#: nor the build key (:func:`shidashi.phases.build_key`) contains ``-``, so the name parses
#: from its end whatever init, flavor or stage3 snapshot comes before them.
_FORK_POINT_TAIL = r"-(?P<pin>p(?P<date>\d{8})\.[0-9a-f]{8})-b[0-9a-f]{8}-[^/]+\.tar"


@dataclass(frozen=True)
class ForkPointFile:
    """One restore point of an arch: its path, the pin it was built from and its apparent
    size (``st_size``: a compressed filesystem holds less)."""

    path: Path
    pin: str
    pin_date: str
    size: int


@dataclass(frozen=True)
class ForkPointGcPlan:
    """``delete`` and ``keep`` split an arch's restore points; ``kept_pins`` is the current
    pin first, then the other pins kept, newest first."""

    kept_pins: tuple[str, ...]
    delete: tuple[ForkPointFile, ...]
    keep: tuple[ForkPointFile, ...]


def fork_point_pin(name: str, arch: str) -> tuple[str, str] | None:
    """``(pin id, pin date)`` of the restore point of ``arch`` called ``name``; ``None`` for
    any other name: another arch's, a tarball being written (``.<name>.tmp``), any file that
    is no restore point. Pure."""
    match = re.fullmatch(re.escape(arch) + r"-.+" + _FORK_POINT_TAIL, name)
    return (match["pin"], match["date"]) if match else None


def scan_fork_points(directory: Path, arch: str) -> list[ForkPointFile]:
    """The restore points of ``arch`` in ``directory``, by name; none when it is missing. I/O.

    Regular files only: a link or a directory never passes for a restore point, so it is
    never deleted.
    """
    if not directory.is_dir():
        return []
    found: list[ForkPointFile] = []
    for path in sorted(directory.iterdir()):
        parsed = fork_point_pin(path.name, arch)
        if parsed is None:
            continue
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            found.append(ForkPointFile(path, parsed[0], parsed[1], info.st_size))
    return found


def plan_fork_point_gc(
    files: Iterable[ForkPointFile], *, current_pin: str, keep: int
) -> ForkPointGcPlan:
    """Keep ``current_pin``'s restore points and those of the ``keep`` newest other pins. Pure.

    Newest is the pin date, then the pin id: two pins of one ``::gentoo`` date differ only
    by an overlay commit, and either order serves as long as it is always the same. A pin
    newer than the current one counts too: it is the re-pin a revert left behind, the
    first one a build needs again once the revert is undone. Raises ``ValueError`` for
    ``keep < 0``.
    """
    if keep < 0:
        raise ValueError(f"--keep must be 0 or more, not {keep}")
    listed = list(files)
    others = sorted({(f.pin_date, f.pin) for f in listed if f.pin != current_pin}, reverse=True)
    kept_pins = (current_pin, *(pin for _, pin in others[:keep]))
    return ForkPointGcPlan(
        kept_pins=kept_pins,
        delete=tuple(f for f in listed if f.pin not in kept_pins),
        keep=tuple(f for f in listed if f.pin in kept_pins),
    )


def delete_fork_points(files: Iterable[ForkPointFile]) -> tuple[list[ForkPointFile], list[str]]:
    """Delete ``files``; ``(deleted, failures)``, each failure naming its path. I/O.

    Goes on past a failure: every file is independent, and stopping would leave space
    unreclaimed for no safety gained. A file already gone counts as deleted.
    """
    deleted: list[ForkPointFile] = []
    failures: list[str] = []
    for item in files:
        try:
            item.path.unlink(missing_ok=True)
        except OSError as err:
            failures.append(f"cannot delete {item.path}: {err.strerror or err}")
        else:
            deleted.append(item)
    return deleted, failures
