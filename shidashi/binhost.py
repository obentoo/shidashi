"""The build pool as classified binpkg instances, and the GC that follows from them (story 029).

The per-generation PKGDIR is the *build pool*: every instance the assembler may need,
transients included. Three dotfiles/rules keep it manageable without ever inferring after
the fact: the factory records which instance each shipped stage installed (finals) and
which instance a USE cut produced (transients) in ``.shidashi-pool.json``; the GC moves to
quarantine only instances whose ebuild is gone from the pinned trees; the moved PATHs are
listed in ``.shidashi-quarantine.json`` so a worker sync never brings them back.

Pure functions come first; the few I/O ones (records, ``ebuild_available``) say so.
"""

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from shidashi import binpkgs
from shidashi.recipe import UseBreak

RECORD_FILE = ".shidashi-pool.json"
QUARANTINE_FILE = ".shidashi-quarantine.json"
_SCHEMA_VERSION = 1

Pair = tuple[str, int]


class BinhostError(Exception):
    """A pool record, quarantine list, index PATH or plan the binhost refuses to act on."""


@dataclass(frozen=True)
class BinpkgRef:
    """One ``Packages`` entry: a ``(CPV, BUILD_ID)`` instance and what classifies it."""

    cpv: str
    build_id: int
    path: str
    slot: str
    use: frozenset[str]
    build_time: int


def parse_refs(index_text: str) -> list[BinpkgRef]:
    """One :class:`BinpkgRef` per entry of a ``Packages`` index. Pure.

    Split as :func:`shidashi.binpkgs.parse_index` splits it: the header block has no
    ``CPV`` and is skipped, as is any block missing ``CPV``, ``BUILD_ID`` or ``PATH``.
    """
    refs: list[BinpkgRef] = []
    for block in index_text.split("\n\n"):
        fields = binpkgs._fields(block)
        cpv, path, build = fields.get("CPV"), fields.get("PATH"), fields.get("BUILD_ID", "")
        if not cpv or not path or not build.isdigit():
            continue
        stamp = fields.get("BUILD_TIME", "")
        refs.append(
            BinpkgRef(
                cpv=cpv,
                build_id=int(build),
                path=path,
                slot=fields.get("SLOT") or "0",
                use=frozenset(fields.get("USE", "").split()),
                build_time=int(stamp) if stamp.isdigit() else 0,
            )
        )
    return refs


def _leaves_root(rel: str) -> bool:
    path = PurePosixPath(rel)
    return path.is_absolute() or ".." in path.parts


def safe_path(root: Path, rel: str) -> Path:
    """``root / rel``, refusing an absolute PATH or a ``..`` component (R3.11). Pure.

    Every PATH read from an index or a list goes through here: it later feeds a ``mv``,
    a copy or an rsync exclude. A name that merely *contains* ``..`` (``a..b``) is fine.
    """
    if _leaves_root(rel):
        raise BinhostError(f"PATH {rel!r} leaves the pool {root}")
    return root / rel


# --- pool record and quarantine list -----------------------------------------------------


@dataclass(frozen=True)
class PoolRecord:
    """What each shipped stage installed (finals) and what its cuts built (transients)."""

    finals: dict[str, frozenset[Pair]] = field(default_factory=dict)
    transients: dict[str, frozenset[Pair]] = field(default_factory=dict)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Replace ``path`` whole, through a sibling temporary file (R7.1)."""
    name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=f".{path.name}.", delete=False, encoding="utf-8"
        ) as tmp:
            name = tmp.name
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(name, path)
    except BaseException:
        if name:
            Path(name).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    """The file's object, ``None`` when absent; a damaged file raises, never reads as empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as err:
        raise BinhostError(f"cannot read {path}: {err}") from err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise BinhostError(f"{path} is not valid JSON ({err}); fix or remove it") from err
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        raise BinhostError(f"{path} is not a version-{_SCHEMA_VERSION} object; fix or remove it")
    return data


def _pairs(path: Path, raw: Any) -> frozenset[Pair]:
    if not isinstance(raw, list):
        raise BinhostError(f"{path}: a finals/transients value is not a list")
    pairs: set[Pair] = set()
    for item in raw:
        ok = isinstance(item, list) and len(item) == 2
        if not ok or not isinstance(item[0], str) or type(item[1]) is not int:
            raise BinhostError(f"{path}: {item!r} is not a [cpv, build_id] pair")
        pairs.add((item[0], item[1]))
    return frozenset(pairs)


def load_record(pkgdir: Path) -> PoolRecord:
    """The pool record of ``pkgdir``; absent is empty, a damaged file raises. I/O."""
    path = pkgdir / RECORD_FILE
    data = _read_json(path)
    if data is None:
        return PoolRecord()
    stages = data.get("stages")
    if not isinstance(stages, dict):
        raise BinhostError(f"{path}: 'stages' is not an object")
    finals: dict[str, frozenset[Pair]] = {}
    transients: dict[str, frozenset[Pair]] = {}
    for stage, body in stages.items():
        if not isinstance(body, dict):
            raise BinhostError(f"{path}: stage {stage!r} is not an object")
        finals[stage] = _pairs(path, body.get("finals", []))
        transients[stage] = _pairs(path, body.get("transients", []))
    return PoolRecord(finals=finals, transients=transients)


def save_record(pkgdir: Path, record: PoolRecord) -> None:
    """Write the pool record atomically. I/O."""
    stages = {
        stage: {
            "finals": sorted([cpv, build] for cpv, build in record.finals.get(stage, ())),
            "transients": sorted([cpv, build] for cpv, build in record.transients.get(stage, ())),
        }
        for stage in sorted(set(record.finals) | set(record.transients))
    }
    _write_json(pkgdir / RECORD_FILE, {"version": _SCHEMA_VERSION, "stages": stages})


def merge_stage(
    record: PoolRecord, stage: str, finals: Iterable[Pair], transients: Iterable[Pair]
) -> PoolRecord:
    """``record`` with ``stage``'s sets replaced (a rebuilt stage supersedes its past). Pure."""
    return PoolRecord(
        finals={**record.finals, stage: frozenset(finals)},
        transients={**record.transients, stage: frozenset(transients)},
    )


def load_quarantined(pkgdir: Path) -> frozenset[str]:
    """The PATHs a GC moved aside; absent is empty, a damaged or unsafe list raises. I/O."""
    path = pkgdir / QUARANTINE_FILE
    data = _read_json(path)
    if data is None:
        return frozenset()
    paths = data.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise BinhostError(f"{path}: 'paths' is not a list of strings")
    for entry in paths:
        if _leaves_root(entry):
            raise BinhostError(f"{path}: entry {entry!r} leaves the pool")
    return frozenset(paths)


def add_quarantined(pkgdir: Path, paths: Iterable[str]) -> None:
    """Union ``paths`` into the quarantine list, atomically. I/O."""
    added = list(paths)
    for entry in added:
        if _leaves_root(entry):
            raise BinhostError(f"refusing to list {entry!r}: it leaves the pool {pkgdir}")
    merged = load_quarantined(pkgdir) | frozenset(added)
    _write_json(pkgdir / QUARANTINE_FILE, {"version": _SCHEMA_VERSION, "paths": sorted(merged)})


# --- classification ----------------------------------------------------------------------


class Kind(StrEnum):
    FINAL = "final"
    TRANSIENT = "transient"
    OLD = "old"
    UNRECORDED = "unrecorded"


def cut_instances(refs: Sequence[BinpkgRef], cuts: Sequence[UseBreak]) -> list[BinpkgRef]:
    """The instances a USE cut produced: its package, with its flag in the cut's state. Pure."""
    wanted = [(binpkgs.package_of(cut.atom), cut.flag, cut.enable) for cut in cuts]
    return [
        ref
        for ref in refs
        if any(
            binpkgs.package_of(ref.cpv) == package and (flag in ref.use) == enable
            for package, flag, enable in wanted
        )
    ]


def classify(
    refs: Sequence[BinpkgRef], record: PoolRecord, *, available: Callable[[str], bool]
) -> dict[BinpkgRef, Kind]:
    """One :class:`Kind` per instance, FINAL before TRANSIENT before OLD. Pure.

    Matched on ``(cpv, build_id)``, never the CPV alone: two builds of one CPV differ.
    """
    finals = frozenset().union(*record.finals.values())
    transients = frozenset().union(*record.transients.values())
    kinds: dict[BinpkgRef, Kind] = {}
    for ref in refs:
        pair = (ref.cpv, ref.build_id)
        if pair in finals:
            kinds[ref] = Kind.FINAL
        elif pair in transients:
            kinds[ref] = Kind.TRANSIENT
        elif not available(ref.cpv):
            kinds[ref] = Kind.OLD
        else:
            kinds[ref] = Kind.UNRECORDED
    return kinds


# --- the GC plan -------------------------------------------------------------------------


def gc_plan(
    refs: Sequence[BinpkgRef], *, available: Callable[[str], bool], keep: int = 2
) -> list[BinpkgRef]:
    """The instances to quarantine: unavailable CPVs beyond the ``keep`` newest. Pure.

    An instance whose ebuild is still in a pinned tree is never planned, and takes no keep
    slot. Groups are ``(cat/pkg, SLOT)`` with the subslot cut off; a group's CPVs are ranked
    by their newest ``BUILD_TIME`` -- Portage's version order is not available here.
    """
    if keep < 0:
        raise BinhostError(f"--keep must not be negative, got {keep}")
    groups: dict[tuple[str, str], dict[str, int]] = {}
    gone = [ref for ref in refs if not available(ref.cpv)]
    for ref in gone:
        newest = groups.setdefault((binpkgs.package_of(ref.cpv), ref.slot.split("/")[0]), {})
        newest[ref.cpv] = max(newest.get(ref.cpv, 0), ref.build_time)
    kept = {
        (group, cpv)
        for group, newest in groups.items()
        for cpv in sorted(newest, key=lambda c: (-newest[c], c))[:keep]
    }
    planned = [
        ref
        for ref in gone
        if ((binpkgs.package_of(ref.cpv), ref.slot.split("/")[0]), ref.cpv) not in kept
    ]
    return sorted(planned, key=lambda ref: ref.path)


def ebuild_available(repos: Sequence[Path]) -> Callable[[str], bool]:
    """A cached predicate: is ``cat/pn-ver`` an ebuild in any of ``repos``? I/O.

    A CPV that does not parse is reported available, so it is never moved.
    """
    cache: dict[str, bool] = {}

    def available(cpv: str) -> bool:
        if cpv not in cache:
            category, _, pf = cpv.partition("/")
            pn = binpkgs._VERSION.sub("", pf)
            if not category or not pf or pn == pf:
                cache[cpv] = True
            else:
                cache[cpv] = any(
                    (repo / category / pn / f"{pf}.ebuild").is_file() for repo in repos
                )
        return cache[cpv]

    return available
