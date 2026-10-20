"""Move a GC plan's binpkgs aside (story 029, task 3.2; R3.4, R3.5, R3.7).

Split from ``binhost_cli`` so the command stays small. Nothing is deleted: a file goes to
``<cache>/quarantine/binpkgs/<arch>/<generation>/<category>/``, the layout
:func:`shidashi.phases.quarantine_binpkgs` uses for the assembler's own stale binpkgs.
"""

import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from shidashi import binhost, config


@dataclass
class QuarantineOutcome:
    """What a quarantine run moved, by index PATH, and why it stopped early, if it did."""

    moved: list[str] = field(default_factory=list)
    failure: str | None = None


def quarantine_dir(arch: str, pkgdir: Path) -> Path:
    """Where ``pkgdir``'s binpkgs of ``arch`` are set aside; the generation is its name."""
    return config.cache_dir() / "quarantine" / "binpkgs" / arch / pkgdir.name


def _free_target(root: Path, rel: str) -> Path:
    """``<root>/<category>/<file>``, serial-suffixed when an earlier quarantine holds it."""
    pure = PurePosixPath(rel)
    folder = root / pure.parts[0] if len(pure.parts) > 1 else root
    target = folder / pure.name
    serial = 1
    while target.exists():
        target = target.with_name(f"{pure.name}.{serial}")
        serial += 1
    return target


def quarantine_paths(pkgdir: Path, root: Path, paths: Iterable[str]) -> QuarantineOutcome:
    """Move each of ``paths`` out of ``pkgdir`` into ``root``; stop at the first failure.

    Every PATH goes through :func:`binhost.safe_path` here too, so no caller can move a
    file from outside the pool. A file already gone is skipped: an earlier run moved it
    and stopped before its reindex, which is what the caller's reindex repays.
    """
    outcome = QuarantineOutcome()
    for rel in paths:
        source = binhost.safe_path(pkgdir, rel)
        if not source.exists():
            continue
        target = _free_target(root, rel)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, target)
        except OSError as err:
            outcome.failure = f"cannot move {source} to {target}: {err.strerror or err}"
            break
        outcome.moved.append(rel)
    return outcome
