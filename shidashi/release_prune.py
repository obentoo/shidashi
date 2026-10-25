"""Which release directories stay, move aside or go (story 032, R4). Pure.

A release directory is named ``YYYY.MM.DD`` after its start date (``release.release_dir``).
``plan`` reads names, never the disk: the command that applies it owns the I/O.
"""

import datetime
import re
from collections.abc import Iterable
from dataclasses import dataclass

#: The name of the directory pruned releases are moved into, beside the releases.
PRUNED_DIR = ".pruned"

_RELEASE_NAME = re.compile(r"\d{4}\.\d{2}\.\d{2}")


@dataclass(frozen=True)
class Plan:
    """``move``: root releases beyond the ``keep`` newest; ``purge``: ``.pruned`` entries
    beyond its ``keep`` newest; ``skipped``: every other name, left untouched. Newest first."""

    move: list[str]
    purge: list[str]
    skipped: list[str]


def _release_date(name: str) -> datetime.date | None:
    """The date ``name`` spells, or ``None`` when it is not a real ``YYYY.MM.DD`` date."""
    if not _RELEASE_NAME.fullmatch(name):
        return None
    year, month, day = (int(part) for part in name.split("."))
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _newest_first(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """``names`` that are release dates, newest first, and the rest, in the order given."""
    dated: list[tuple[datetime.date, str]] = []
    others: list[str] = []
    for name in names:
        when = _release_date(name)
        if when is None:
            others.append(name)
        else:
            dated.append((when, name))
    return [name for _, name in sorted(dated, reverse=True)], others


def plan(names: Iterable[str], keep: int, purge: Iterable[str]) -> Plan:
    """The prune of ``names`` (the output root's entries) keeping the ``keep`` newest.

    ``purge`` are the entries of ``<root>/.pruned``: those beyond its ``keep`` newest are
    purge candidates (R4.6), so the root and ``.pruned`` each hold ``keep`` releases. The
    ``.pruned`` directory itself is no release and is not reported. Raises ``ValueError``
    for ``keep < 1``: the newest release is never moved.
    """
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")
    releases, others = _newest_first(name for name in names if name != PRUNED_DIR)
    pruned, pruned_others = _newest_first(purge)
    return Plan(
        move=releases[keep:],
        purge=pruned[keep:],
        skipped=[*others, *(f"{PRUNED_DIR}/{name}" for name in pruned_others)],
    )
