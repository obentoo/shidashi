"""The upload's pure planners (story 033, Task 2; R3.1-R3.3, R4.1-R4.4, R5.1-R5.3, Q12).

Contract from design.md "shidashi release upload" and "Data Models", and the signatures of
tasks.md 2.1-2.3, all in ``shidashi.release_upload``:

- ``upload_set(sums, present) -> UploadPlan(data, seal)``: ``sums`` is ``SHA256SUMS`` as
  name -> digest, ``present`` the names in the directory. ``data`` is every listed file but
  the run bundles (``*.build.tar.zst``); ``seal`` is ``SHA256SUMS`` and the signatures
  (every ``*.asc``, the exported key included), ``SHA256SUMS.asc`` last. A listed name with
  ``/``, a hidden one, or one not on disk is ``UploadError`` naming it.
- ``parse_index(raw)``, ``merge_index(current, entry)``, ``latest(index)``: ``index.json``
  schema 1, ``{"schema": 1, "updated": <UTC>, "releases": [entry, ...]}``, newest first;
  ``latest.json`` is ``{"schema": 1, "release": <newest entry>}``. Anything else read back
  is ``UploadError`` naming ``releases/index.json``. ``merge_index`` is called without a
  clock here: ``updated`` is checked to fall within the call.
- ``retention(index, keep, current, host_dates, yes) -> RetentionPlan(kept, drop,
  refused)``; ``kept``/``drop``/``refused`` may hold entries or dates.
"""

import datetime
import json
import secrets
from typing import Any

import pytest

from tests._pending import try_import

UploadError: Any = try_import("shidashi.release_upload", "UploadError")
upload_set: Any = try_import("shidashi.release_upload", "upload_set")
parse_index: Any = try_import("shidashi.release_upload", "parse_index")
merge_index: Any = try_import("shidashi.release_upload", "merge_index")
latest: Any = try_import("shidashi.release_upload", "latest")
retention: Any = try_import("shidashi.release_upload", "retention")

DAY = "2026.10.25"
INDEX = "releases/index.json"


# --- 2.1 upload_set (R3.1, R3.2, R3.3) ----------------------------------------------------------

ISOS = [f"bentoo-{DAY}-minimal-systemd-v3.iso", f"bentoo-{DAY}-kde-systemd-v3-xz.iso"]
#: The run bundles a build lists in SHA256SUMS (cli.py): one shares an ISO's stem.
BUNDLES = [
    f"bentoo-{DAY}-minimal-systemd-v3.build.tar.zst",
    "20261025T000003Z-a1b2c3.build.tar.zst",
]
LISTED_PUBLIC = [
    *ISOS,
    *(f"{iso}.DIGESTS" for iso in ISOS),
    f"bentoo-{DAY}-minimal-systemd-v3.iso.packages",
    f"bentoo-{DAY}-minimal-systemd-v3.iso.spdx.json",
    "latest-minimal-systemd-v3.txt",
    "release.json",
    "bentoo-release-key.asc",
]
SIGNATURES = [f"{iso}.DIGESTS.asc" for iso in ISOS]
#: On disk but never listed: bookkeeping and the signing stage.
HIDDEN = [".release.lock", ".SHA256SUMS.lock", ".outcome", ".signing"]


def release_sums(extra: dict[str, str] | None = None) -> dict[str, str]:
    names = [*LISTED_PUBLIC, *BUNDLES, *(extra or {})]
    return {name: secrets.token_hex(32) for name in names}


def release_present(sums: dict[str, str]) -> set[str]:
    return {*sums, "SHA256SUMS", "SHA256SUMS.asc", *SIGNATURES, *HIDDEN}


def plan_names(plan: Any) -> tuple[list[str], list[str]]:
    return list(plan.data), list(plan.seal)


def test_upload_set_sends_every_listed_file_but_the_run_bundles() -> None:
    """R3.1: every listed public file, ``SHA256SUMS``, every signature and the key."""
    sums = release_sums()
    data, seal = plan_names(upload_set(sums, release_present(sums)))
    expected = {*LISTED_PUBLIC, "SHA256SUMS", "SHA256SUMS.asc", *SIGNATURES}
    assert set(data) | set(seal) == expected
    assert not [n for n in data + seal if n.endswith(".build.tar.zst")]


def test_upload_set_excludes_a_bundle_that_shares_an_iso_stem_but_keeps_the_iso() -> None:
    """R3.3, hostile half: the bundle named like the ISO goes; the ISO stays."""
    sums = release_sums()
    data, seal = plan_names(upload_set(sums, release_present(sums)))
    assert f"bentoo-{DAY}-minimal-systemd-v3.iso" in data
    assert f"bentoo-{DAY}-minimal-systemd-v3.build.tar.zst" not in data + seal


def test_upload_set_excludes_only_the_build_tar_zst_suffix() -> None:
    """R3.1, converse half: a name that merely contains ``.build.tar.zst`` is uploaded."""
    lookalike = "notes.build.tar.zst.txt"
    sums = release_sums({lookalike: ""})
    data, seal = plan_names(upload_set(sums, release_present(sums)))
    assert lookalike in data + seal
    assert BUNDLES[0] not in data + seal


def test_upload_set_seals_after_the_data_with_sha256sums_asc_last() -> None:
    """R3.2: data carries no signature and no SHA256SUMS; SHA256SUMS.asc closes the seal."""
    sums = release_sums()
    data, seal = plan_names(upload_set(sums, release_present(sums)))
    assert seal[-1:] == ["SHA256SUMS.asc"], seal
    assert "SHA256SUMS" in seal and "SHA256SUMS" not in data
    assert set(SIGNATURES) <= set(seal)
    assert not [n for n in data if n.endswith(".asc")], data


def test_upload_set_names_each_file_once() -> None:
    """R3.1: the key is listed in SHA256SUMS and ends in ``.asc``; it is still sent once."""
    sums = release_sums()
    data, seal = plan_names(upload_set(sums, release_present(sums)))
    assert len(data + seal) == len(set(data + seal)), data + seal
    assert "bentoo-release-key.asc" in seal


def test_upload_set_never_sends_a_hidden_file_present_on_disk() -> None:
    """R3.3: a hidden ``.asc`` on disk is not a signature to publish."""
    sums = release_sums()
    present = release_present(sums) | {".planted.asc", ".SHA256SUMS.asc.tmp"}
    try:
        data, seal = plan_names(upload_set(sums, present))
    except UploadError as err:
        assert ".planted.asc" in str(err) or ".SHA256SUMS.asc.tmp" in str(err), str(err)
        return
    assert not [n for n in data + seal if n.startswith(".")], data + seal
    assert set(SIGNATURES) <= set(seal), seal


@pytest.mark.parametrize("bad", ["sub/x.iso", "../escape.iso", ".hidden.iso"])
def test_upload_set_refuses_a_listed_name_with_a_slash_or_a_dot(bad: str) -> None:
    """R3.3: refused, naming it."""
    sums = release_sums({bad: ""})
    with pytest.raises(UploadError) as refused:
        upload_set(sums, release_present(sums))
    assert bad in str(refused.value)


def test_upload_set_refuses_a_listed_public_file_missing_from_disk() -> None:
    """R3.1: a listed ISO that is not there would advertise a file the bucket lacks."""
    sums = release_sums()
    present = release_present(sums) - {ISOS[1]}
    with pytest.raises(UploadError) as refused:
        upload_set(sums, present)
    assert ISOS[1] in str(refused.value)


# --- 2.2 the index (R4.1-R4.4, Q12) -------------------------------------------------------------


def entry(date: str, isos: list[str] | None = None) -> dict[str, Any]:
    return {
        "date": date,
        "isos": isos if isos is not None else [f"bentoo-{date}-minimal-systemd-v3.iso"],
        "sha256sums": f"releases/{date}/SHA256SUMS",
        "signature": f"releases/{date}/SHA256SUMS.asc",
        "pubkey": f"releases/{date}/bentoo-release-key.asc",
    }


def index_of(*dates: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "updated": "2026-10-18T10:00:00Z",
        "releases": [entry(d) for d in dates],
    }


def dates_of(index: dict[str, Any]) -> list[str]:
    return [e.get("date") for e in index.get("releases", [])]


def test_index_parse_of_no_index_is_an_empty_schema_1_index() -> None:
    """R4.1: a bucket without ``index.json`` starts an empty one."""
    empty = parse_index(None)
    assert empty.get("schema") == 1
    assert empty.get("releases") == []


def test_index_parse_reads_back_what_was_written_in_any_layout() -> None:
    """R4.1, converse half: the same index, pretty or compact, any key order, is read."""
    index = index_of("2026.10.18", "2026.10.11")
    compact = json.dumps(index, separators=(",", ":")).encode()
    pretty = json.dumps(dict(reversed(list(index.items()))), indent=2).encode()
    assert dates_of(parse_index(compact)) == ["2026.10.18", "2026.10.11"]
    assert dates_of(parse_index(pretty)) == ["2026.10.18", "2026.10.11"]


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("not JSON", b'{"schema": 1, "releases": ['),
        ("not UTF-8", b"\xff\xfe\x00"),
        ("not an object", b"[]"),
        ("another schema", b'{"schema": 2, "releases": []}'),
        ("no schema", b'{"releases": []}'),
        # equal to 1 in Python, but not the integer 1 the format names
        ("schema true", b'{"schema": true, "releases": []}'),
        ("schema as a string", b'{"schema": "1", "releases": []}'),
        ("releases not a list", b'{"schema": 1, "releases": {}}'),
    ],
)
def test_index_parse_refuses_what_is_not_schema_1_naming_it(label: str, raw: bytes) -> None:
    """R4.4, Q12: refused naming ``releases/index.json``."""
    with pytest.raises(UploadError) as refused:
        parse_index(raw)
    assert INDEX in str(refused.value), (label, str(refused.value))


def utc_now_floor() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def assert_updated_between(index: dict[str, Any], start: datetime.datetime) -> None:
    stamp = datetime.datetime.fromisoformat(str(index["updated"]).replace("Z", "+00:00"))
    assert stamp.utcoffset() == datetime.timedelta(0), index["updated"]
    assert start <= stamp <= datetime.datetime.now(datetime.UTC), index["updated"]


def test_index_merge_puts_the_new_release_first_then_the_earlier_newest_first() -> None:
    """R4.1: the uploaded release first, then every earlier one, newest first."""
    current = index_of("2026.10.11", "2026.10.18")  # oldest first: merge orders it
    before = json.dumps(current, sort_keys=True)
    start = utc_now_floor()
    merged = merge_index(current, entry(DAY))
    assert merged.get("schema") == 1
    assert dates_of(merged) == [DAY, "2026.10.18", "2026.10.11"]
    assert merged["releases"][0] == entry(DAY)
    assert_updated_between(merged, start)
    assert json.dumps(current, sort_keys=True) == before, "merge_index changed its input"


def test_index_merge_into_an_empty_index() -> None:
    """R4.1: the first release makes a one-entry index."""
    merged = merge_index(parse_index(None), entry(DAY))
    assert merged.get("releases") == [entry(DAY)]


def test_index_merge_replaces_an_entry_of_the_same_date() -> None:
    """R4.2: the same date uploaded again replaces its entry."""
    again = entry(DAY, [f"bentoo-{DAY}-gnome-systemd-v3.iso"])
    merged = merge_index(index_of(DAY, "2026.10.18"), again)
    assert dates_of(merged) == [DAY, "2026.10.18"]
    assert merged["releases"][0] == again


def test_index_merge_collapses_every_duplicate_of_the_date() -> None:
    """R4.2, hostile half: an index that already lists the date twice ends with it once."""
    current = index_of(DAY, "2026.10.18", DAY)
    merged = merge_index(current, entry(DAY))
    assert dates_of(merged) == [DAY, "2026.10.18"]


def test_index_merge_keeps_two_dates_apart_even_with_the_same_isos() -> None:
    """R4.2, converse half: entries are the same release by date, never by content."""
    same_isos = ["bentoo-minimal-systemd-v3.iso"]
    current = {"schema": 1, "updated": "x", "releases": [entry("2026.10.18", same_isos)]}
    merged = merge_index(current, entry(DAY, same_isos))
    assert dates_of(merged) == [DAY, "2026.10.18"]


def test_index_latest_names_only_the_newest_release() -> None:
    """R4.3: ``latest.json`` is the schema and the newest entry alone."""
    index = merge_index(index_of("2026.10.18", "2026.10.11"), entry(DAY))
    assert latest(index) == {"schema": 1, "release": entry(DAY)}


def test_index_latest_is_the_newest_date_after_an_older_release_is_uploaded_again() -> None:
    """R4.3, hostile half: a retry of last week's release is first in the index, yet
    ``latest.json`` still names this week's."""
    index = merge_index(index_of(DAY, "2026.10.18"), entry("2026.10.18"))
    assert latest(index).get("release", {}).get("date") == DAY


# --- 2.3 retention (R5.1-R5.3) --------------------------------------------------------------------

WEEKS = ["2026.10.25", "2026.10.18", "2026.10.11", "2026.10.04", "2026.09.27", "2026.09.20"]


def plan_dates(plan: Any) -> tuple[set[str], set[str], set[str]]:
    def dates(items: Any) -> set[str]:
        return {i["date"] if isinstance(i, dict) else str(i) for i in items}

    return dates(plan.kept), dates(plan.drop), dates(plan.refused)


def test_retention_drops_the_oldest_beyond_keep() -> None:
    """R5.1: six releases, keep 4, every one on the host: the two oldest go."""
    plan = retention(index_of(*WEEKS), 4, WEEKS[0], set(WEEKS), False)
    kept, drop, refused = plan_dates(plan)
    assert drop == {"2026.09.27", "2026.09.20"}
    assert kept == set(WEEKS[:4])
    assert refused == set()


def test_retention_drops_nothing_within_keep() -> None:
    """R5.1: four releases, keep 4: all stay."""
    plan = retention(index_of(*WEEKS[:4]), 4, WEEKS[0], set(WEEKS), False)
    kept, drop, _refused = plan_dates(plan)
    assert drop == set()
    assert kept == set(WEEKS[:4])


def test_retention_drops_the_oldest_by_date_whatever_the_entry_order() -> None:
    """R5.1: the oldest are the oldest dates, not the last entries."""
    shuffled = index_of("2026.10.25", "2026.10.04", "2026.10.18", "2026.09.27", "2026.10.11")
    plan = retention(shuffled, 3, "2026.10.25", set(WEEKS), False)
    kept, drop, _refused = plan_dates(plan)
    assert drop == {"2026.10.04", "2026.09.27"}
    assert kept == {"2026.10.25", "2026.10.18", "2026.10.11"}


def test_retention_never_drops_the_release_just_uploaded() -> None:
    """R5.2: an older release uploaded again is first in the index and older than the
    newest ``keep``: it stays."""
    current = "2026.09.20"
    index = index_of(current, "2026.10.25", "2026.10.18", "2026.10.11", "2026.10.04")
    plan = retention(index, 4, current, set(WEEKS), False)
    kept, drop, _refused = plan_dates(plan)
    assert current not in drop
    assert current in kept
    assert "2026.10.25" in kept


def test_retention_keep_1_keeps_only_the_current() -> None:
    """R5.1, R5.2."""
    kept, drop, _refused = plan_dates(
        retention(index_of(*WEEKS[:3]), 1, WEEKS[0], set(WEEKS), False)
    )
    assert kept == {WEEKS[0]}
    assert drop == set(WEEKS[1:3])


def test_retention_refuses_to_drop_a_release_the_host_has_no_copy_of() -> None:
    """R5.3: the oldest has no host copy: it is kept and reported, not dropped."""
    host = set(WEEKS) - {"2026.09.20"}
    plan = retention(index_of(*WEEKS), 4, WEEKS[0], host, False)
    kept, drop, refused = plan_dates(plan)
    assert drop == {"2026.09.27"}
    assert refused == {"2026.09.20"}
    assert "2026.09.20" not in drop
    assert set(WEEKS[:4]) <= kept


def test_retention_a_host_copy_of_another_date_does_not_count() -> None:
    """R5.3, hostile half: ``2026.09.21`` on the host is not a copy of ``2026.09.20``."""
    host = (set(WEEKS) - {"2026.09.20"}) | {"2026.09.21", "2026.09.2"}
    _kept, drop, refused = plan_dates(retention(index_of(*WEEKS), 4, WEEKS[0], host, False))
    assert "2026.09.20" in refused
    assert "2026.09.20" not in drop


def test_retention_yes_drops_a_release_without_a_host_copy() -> None:
    """R5.3: ``--yes`` opts in to an irreversible delete."""
    host = set(WEEKS) - {"2026.09.20"}
    _kept, drop, refused = plan_dates(retention(index_of(*WEEKS), 4, WEEKS[0], host, True))
    assert drop == {"2026.09.27", "2026.09.20"}
    assert refused == set()


@pytest.mark.parametrize("keep", [0, -1])
def test_retention_refuses_a_keep_below_1(keep: int) -> None:
    """R5.1: keeping nothing would delete the release just uploaded."""
    with pytest.raises(ValueError):
        retention(index_of(*WEEKS), keep, WEEKS[0], set(WEEKS), False)
