"""``shidashi release upload``: what is copied to the bucket, the public index, and which
releases the bucket keeps (story 033 R3-R5). The planners here are pure; the rclone calls
that act on their plans live beside them."""

import datetime
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from shidashi.release_sign import RELEASE_KEY_FILE, SUMS_FILE

#: The run bundle carries internal paths and build logs: it never leaves the host (R3.3).
_BUNDLE_SUFFIX = ".build.tar.zst"
#: Copied last: its presence in the bucket means the whole release is there (R3.2).
SUMS_SIGNATURE_FILE = f"{SUMS_FILE}.asc"
#: The public index, relative to the bucket root: R2 does not list objects to the public,
#: so this is the only way a reader finds a release.
INDEX_OBJECT = "releases/index.json"
#: The format version of index.json and latest.json (Q12): any other is refused.
INDEX_SCHEMA = 1
#: The bucket key of a release is its directory name. ``[0-9]``, not ``\d``, which also
#: matches non-ASCII digits: the date ends up in a ``purge`` path.
_RELEASE_DATE_RE = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}")
#: The entry fields that name an object of the entry's own release.
_ENTRY_PATH_FIELDS = ("sha256sums", "signature", "pubkey")


class UploadError(Exception):
    """An upload step refused or failed; the message names the file, object or step,
    never a credential (R3.5)."""


@dataclass(frozen=True)
class UploadPlan:
    """``data`` is copied first, ``seal`` after it: a reader who finds ``SHA256SUMS.asc``
    (the last object) finds every file it vouches for (R3.2)."""

    data: tuple[str, ...]
    seal: tuple[str, ...]


@dataclass(frozen=True)
class RetentionPlan:
    """The index entries ``kept``, to ``drop`` (purged from the bucket after ``index`` is
    written), and ``refused`` (beyond ``--keep`` but kept: no host copy and no ``--yes``);
    the three never overlap. ``index`` is the index without the ``drop`` entries."""

    kept: tuple[dict[str, Any], ...]
    drop: tuple[dict[str, Any], ...]
    refused: tuple[dict[str, Any], ...]
    index: dict[str, Any]


def _is_public_name(name: str) -> bool:
    # A name with "/" would leave releases/<date>/ in the bucket; a hidden one is
    # bookkeeping (locks, the signing stage), never a published file (R3.3).
    return "/" not in name and not name.startswith(".")


def upload_set(sums: Mapping[str, str], present: Collection[str]) -> UploadPlan:
    """The objects to copy for one release: ``sums`` is ``SHA256SUMS`` as name -> digest,
    ``present`` the names in the release directory. Raises :class:`UploadError` naming a
    listed name that is unsafe or missing, or a seal file that is missing. Pure."""
    on_disk = set(present)
    for name in sums:
        if not _is_public_name(name):
            raise UploadError(f"SHA256SUMS lists {name!r}: a hidden name or one with '/'")
        if name not in on_disk:
            # An advertised file the bucket lacks would fail every reader's check (R3.1).
            raise UploadError(f"SHA256SUMS lists {name!r}, which is not in the release")
    for name in (RELEASE_KEY_FILE, SUMS_FILE, SUMS_SIGNATURE_FILE):
        if name not in on_disk:
            raise UploadError(f"{name!r} is not in the release: it is not signed")
    data = sorted(
        (n for n in sums if not n.endswith((_BUNDLE_SUFFIX, ".asc")) and n != SUMS_FILE),
        # ISOs are the largest objects: last among the data, so a slow or failed
        # transfer is noticed after the small files are already up.
        key=lambda n: (n.endswith(".iso"), n),
    )
    signatures = sorted(
        n
        for n in on_disk
        if n.endswith(".asc")
        and _is_public_name(n)
        and n not in (RELEASE_KEY_FILE, SUMS_SIGNATURE_FILE)
    )
    # The key is listed in SHA256SUMS yet ends in .asc: it travels with the seal, so a
    # reader who finds a signature also finds the key that checks it.
    seal = (RELEASE_KEY_FILE, SUMS_FILE, *signatures, SUMS_SIGNATURE_FILE)
    return UploadPlan(data=tuple(data), seal=seal)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _refuse_index(reason: str) -> UploadError:
    return UploadError(f"{INDEX_OBJECT}: {reason}; left unchanged")


def _check_entry(item: object, position: int) -> None:
    if not isinstance(item, dict):
        raise _refuse_index(f"release {position} is not an object")
    date = item.get("date")
    if not isinstance(date, str) or not _RELEASE_DATE_RE.fullmatch(date):
        raise _refuse_index(f"release {position} has no YYYY.MM.DD date")
    isos = item.get("isos")
    if not isinstance(isos, list) or not all(isinstance(i, str) for i in isos):
        raise _refuse_index(f"release {date}: isos is not a list of names")
    for field in _ENTRY_PATH_FIELDS:
        path = item.get(field)
        prefix, _, name = path.rpartition("/") if isinstance(path, str) else ("", "", "")
        # One plain name under its own date: an entry must not point at another release.
        if prefix != f"releases/{date}" or name in ("", ".", ".."):
            raise _refuse_index(f"release {date}: {field} is not under releases/{date}/")


def parse_index(raw: bytes | None) -> dict[str, Any]:
    """The bucket's ``index.json`` as ``rclone cat`` read it; ``None`` (no index yet) is an
    empty one. Raises :class:`UploadError` naming it unless it is UTF-8 JSON of schema 1
    whose entries each carry a release date and paths under that date (R4.4). Pure."""
    if raw is None:
        return {"schema": INDEX_SCHEMA, "releases": []}
    try:
        # Decoded here: json.loads(bytes) would also accept UTF-16 and UTF-32.
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as err:  # RecursionError: nesting too deep
        raise _refuse_index(f"not UTF-8 JSON ({err})") from err
    if not isinstance(doc, dict):
        raise _refuse_index("not a JSON object")
    # type(), not ==: True == 1 in Python, yet it is not the integer the format names.
    if type(doc.get("schema")) is not int or doc["schema"] != INDEX_SCHEMA:
        raise _refuse_index(f"schema is {doc.get('schema')!r}, not {INDEX_SCHEMA}")
    releases = doc.get("releases")
    if not isinstance(releases, list):
        raise _refuse_index("releases is not a list")
    for position, item in enumerate(releases, start=1):
        _check_entry(item, position)
    return doc


def _entry_date(entry: Mapping[str, Any]) -> str:
    return str(entry["date"])


def merge_index(
    current: Mapping[str, Any],
    entry: Mapping[str, Any],
    clock: Callable[[], datetime.datetime] = _utc_now,
) -> dict[str, Any]:
    """``current`` with ``entry`` added, replacing every entry of its date (R4.2), all
    newest date first (R4.1), ``updated`` stamped from ``clock``. ``current`` is left
    unchanged. Pure but for the default clock."""
    others = [dict(e) for e in current.get("releases", []) if e["date"] != entry["date"]]
    releases = sorted([*others, dict(entry)], key=_entry_date, reverse=True)
    stamp = clock().astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"schema": INDEX_SCHEMA, "updated": stamp, "releases": releases}


def latest(index: Mapping[str, Any]) -> dict[str, Any]:
    """``latest.json``: the entry with the newest date, whatever the entry order (R4.3).
    Raises :class:`UploadError` for an index with no release. Pure."""
    releases = index.get("releases", [])
    if not releases:
        raise UploadError(f"{INDEX_OBJECT} lists no release: no latest.json to write")
    return {"schema": INDEX_SCHEMA, "release": max(releases, key=_entry_date)}


def retention(
    index: Mapping[str, Any],
    keep: int,
    current: str,
    host_dates: Collection[str],
    yes: bool,
) -> RetentionPlan:
    """Which releases the bucket keeps: the ``keep`` newest dates (R5.1) and always
    ``current``, the release just uploaded (R5.2). One beyond them is dropped only when its
    date is in ``host_dates`` or ``yes`` (R5.3): a bucket delete is irreversible. Raises
    ``ValueError`` for ``keep < 1``. Pure."""
    if keep < 1:
        raise ValueError(f"--keep must be at least 1, not {keep}")
    newest_first = sorted(index.get("releases", []), key=_entry_date, reverse=True)
    kept: list[dict[str, Any]] = []
    drop: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for rank, item in enumerate(newest_first):
        date = _entry_date(item)
        # An older release uploaded again stays beside the newest `keep`: the bucket holds
        # keep + 1 until the next upload (decision 2026-10-24).
        if rank < keep or date == current:
            kept.append(item)
        elif date in host_dates or yes:
            drop.append(item)
        else:
            refused.append(item)
    remaining = sorted([*kept, *refused], key=_entry_date, reverse=True)
    return RetentionPlan(
        kept=tuple(kept),
        drop=tuple(drop),
        refused=tuple(refused),
        index={**index, "releases": remaining},
    )
