"""The pool record and the quarantine list files (story 029, task 1.2; R1.3, R7.1-R7.4).

Contract from design.md "shidashi/binhost.py" and "Data Models":
``.shidashi-pool.json`` = ``{"version": 1, "stages": {stage: {"finals": [[cpv, id]],
"transients": [[cpv, id]]}}}`` read as ``PoolRecord(finals, transients)`` keyed by stage;
``.shidashi-quarantine.json`` = ``{"version": 1, "paths": [...]}``. Both written through a
temporary file in the same directory renamed over the target; a missing file is empty; an
unparsable or wrong-shaped file raises ``BinhostError`` naming the file and is never
overwritten.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import

BinhostError: Any = try_import("shidashi.binhost", "BinhostError")
PoolRecord: Any = try_import("shidashi.binhost", "PoolRecord")
load_record: Any = try_import("shidashi.binhost", "load_record")
save_record: Any = try_import("shidashi.binhost", "save_record")
merge_stage: Any = try_import("shidashi.binhost", "merge_stage")
load_quarantined: Any = try_import("shidashi.binhost", "load_quarantined")
add_quarantined: Any = try_import("shidashi.binhost", "add_quarantined")

RECORD = ".shidashi-pool.json"
QUARANTINE = ".shidashi-quarantine.json"
FFMPEG = "media-video/ffmpeg-8.0"


def _record(**stages: tuple[set[tuple[str, int]], set[tuple[str, int]]]) -> Any:
    return PoolRecord(
        finals={s: frozenset(f) for s, (f, _t) in stages.items()},
        transients={s: frozenset(t) for s, (_f, t) in stages.items()},
    )


# --- pool record ------------------------------------------------------------------------


def test_a_saved_record_loads_back_pair_for_pair(tmp_path: Path) -> None:
    """Hostile half (wrongly collapse): two BUILD_IDs of one CPV are two instances, and a
    BUILD_ID stays an int -- ``(cpv, 3)`` never comes back as ``(cpv, "3")``."""
    record = _record(
        minimal=({(FFMPEG, 3), (FFMPEG, 4)}, set()),
        kde=({("net-misc/curl-8.16.0", 1)}, {(FFMPEG, 2)}),
    )
    save_record(tmp_path, record)
    loaded = load_record(tmp_path)
    assert loaded.finals.get("minimal") == frozenset({(FFMPEG, 3), (FFMPEG, 4)})
    assert loaded.finals.get("kde") == frozenset({("net-misc/curl-8.16.0", 1)})
    assert loaded.transients.get("kde") == frozenset({(FFMPEG, 2)})
    assert not loaded.transients.get("minimal")


def test_the_record_file_has_the_documented_shape(tmp_path: Path) -> None:
    save_record(tmp_path, _record(kde=({(FFMPEG, 3)}, {(FFMPEG, 2)})))
    path = tmp_path / RECORD
    assert path.is_file(), sorted(p.name for p in tmp_path.iterdir())
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["stages"]["kde"]["finals"] == [[FFMPEG, 3]]
    assert data["stages"]["kde"]["transients"] == [[FFMPEG, 2]]


def test_a_record_in_the_documented_shape_loads(tmp_path: Path) -> None:
    """The file is the contract between the factory and every reader (Data Models)."""
    (tmp_path / RECORD).write_text(
        json.dumps(
            {
                "version": 1,
                "stages": {"kde": {"finals": [[FFMPEG, 3]], "transients": [[FFMPEG, 2]]}},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_record(tmp_path)
    assert loaded.finals == {"kde": frozenset({(FFMPEG, 3)})}
    assert loaded.transients == {"kde": frozenset({(FFMPEG, 2)})}


def test_a_missing_record_is_an_empty_record(tmp_path: Path) -> None:
    loaded = load_record(tmp_path)
    assert not any(loaded.finals.values()) and not any(loaded.transients.values())
    assert not (tmp_path / RECORD).exists()  # reading never creates it
    # the empty record is a real PoolRecord a merge can start from
    merged = merge_stage(loaded, "minimal", [(FFMPEG, 3)], [])
    assert merged.finals.get("minimal") == frozenset({(FFMPEG, 3)})


def test_merging_a_rebuilt_stage_replaces_only_that_stage() -> None:
    """R1.3. Hostile half: the rebuilt stage's old finals are GONE (replaced, not merged
    into), and the other stage keeps exactly what it had."""
    before = _record(
        minimal=({(FFMPEG, 3), ("sys-apps/old-1", 1)}, {(FFMPEG, 2)}),
        kde=({("net-misc/curl-8.16.0", 1)}, {(FFMPEG, 2)}),
    )
    after = merge_stage(before, "minimal", [(FFMPEG, 5)], [])
    assert after.finals.get("minimal") == frozenset({(FFMPEG, 5)})
    assert not after.transients.get("minimal")
    assert after.finals.get("kde") == frozenset({("net-misc/curl-8.16.0", 1)})
    assert after.transients.get("kde") == frozenset({(FFMPEG, 2)})
    # pure: the record handed in is unchanged
    assert before.finals.get("minimal") == frozenset({(FFMPEG, 3), ("sys-apps/old-1", 1)})


def test_merging_a_new_stage_adds_it() -> None:
    before = _record(kde=({("net-misc/curl-8.16.0", 1)}, set()))
    after = merge_stage(before, "gnome", [(FFMPEG, 3)], [(FFMPEG, 2)])
    assert after.finals.get("gnome") == frozenset({(FFMPEG, 3)})
    assert after.transients.get("gnome") == frozenset({(FFMPEG, 2)})
    assert after.finals.get("kde") == frozenset({("net-misc/curl-8.16.0", 1)})


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"stages": {}}),  # no version
        json.dumps({"version": 2, "stages": {}}),  # an unknown version
        json.dumps({"version": 1}),  # no stages
        json.dumps({"version": 1, "stages": []}),
        json.dumps({"version": 1, "stages": {"kde": {"finals": [[FFMPEG, "3"]]}}}),
        json.dumps({"version": 1, "stages": {"kde": {"finals": [FFMPEG]}}}),
        json.dumps({"version": 1, "stages": {"kde": {"finals": {"x": 1}}}}),
    ],
    ids=[
        "unparsable",
        "a-list",
        "no-version",
        "version-2",
        "no-stages",
        "stages-list",
        "build-id-string",
        "pair-not-a-list",
        "finals-a-mapping",
    ],
)
def test_a_damaged_record_stops_naming_the_file_and_stays_byte_identical(
    tmp_path: Path, content: str
) -> None:
    """R7.3, R7.4: noticed, never silently replaced by an empty record."""
    path = tmp_path / RECORD
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(BinhostError) as err:
        load_record(tmp_path)
    assert RECORD in str(err.value)
    assert path.read_bytes() == before


def test_saving_writes_a_sibling_temporary_file_then_renames_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7.1: the target is only ever replaced whole, from the same directory."""
    renames: list[tuple[Path, Path]] = []
    real = os.replace

    def spy(src: Any, dst: Any, *a: Any, **kw: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    save_record(tmp_path, _record(kde=({(FFMPEG, 3)}, set())))
    onto = [(s, d) for s, d in renames if d == tmp_path / RECORD]
    assert onto, renames
    src, _dst = onto[-1]
    assert src.parent == tmp_path and src != tmp_path / RECORD
    assert sorted(p.name for p in tmp_path.iterdir()) == [RECORD]  # no temporary left


def test_a_save_that_dies_before_the_rename_leaves_the_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7.1 seen from the reader: a crash mid-write never leaves a half record."""
    save_record(tmp_path, _record(kde=({(FFMPEG, 3)}, set())))
    path = tmp_path / RECORD
    assert path.is_file()
    before = path.read_bytes()

    def crash(*_a: Any, **_k: Any) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", crash)
    monkeypatch.setattr(os, "rename", crash)
    with pytest.raises(OSError):
        save_record(tmp_path, _record(kde=({(FFMPEG, 9)}, set())))
    assert path.read_bytes() == before


# --- quarantine list ----------------------------------------------------------------------


def test_a_missing_quarantine_list_is_empty(tmp_path: Path) -> None:
    assert load_quarantined(tmp_path) == frozenset()
    add_quarantined(tmp_path, ["dev-libs/foo/foo-1.0-1.gpkg.tar"])
    assert load_quarantined(tmp_path) == frozenset({"dev-libs/foo/foo-1.0-1.gpkg.tar"})


def test_adding_paths_keeps_what_was_listed_and_lists_a_path_once(tmp_path: Path) -> None:
    """Hostile halves: an append never drops an earlier entry (wrongly replace), the same
    PATH twice is one entry, and two instances of one CPV are two PATHs."""
    first = "dev-libs/foo/foo-1.0-1.gpkg.tar"
    second = "dev-libs/foo/foo-1.0-2.gpkg.tar"
    add_quarantined(tmp_path, [first])
    add_quarantined(tmp_path, [second, first])
    assert load_quarantined(tmp_path) == frozenset({first, second})
    data = json.loads((tmp_path / QUARANTINE).read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert sorted(data["paths"]) == [first, second]


def test_a_quarantine_list_in_the_documented_shape_loads(tmp_path: Path) -> None:
    (tmp_path / QUARANTINE).write_text(
        json.dumps({"version": 1, "paths": ["media-video/ffmpeg/ffmpeg-7.1-1.gpkg.tar"]}),
        encoding="utf-8",
    )
    assert load_quarantined(tmp_path) == frozenset({"media-video/ffmpeg/ffmpeg-7.1-1.gpkg.tar"})


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"paths": []}),
        json.dumps({"version": 2, "paths": []}),
        json.dumps({"version": 1}),
        json.dumps({"version": 1, "paths": "a/b-1.gpkg.tar"}),
        json.dumps({"version": 1, "paths": [1, 2]}),
        json.dumps({"version": 1, "paths": ["dev-libs/foo/foo-1.0-1.gpkg.tar", "/etc/passwd"]}),
        json.dumps({"version": 1, "paths": ["../outside/evil-1.gpkg.tar"]}),
        json.dumps({"version": 1, "paths": ["dev-libs/foo/../../../evil-1.gpkg.tar"]}),
    ],
    ids=[
        "unparsable",
        "a-list",
        "no-version",
        "version-2",
        "no-paths",
        "paths-a-string",
        "paths-not-strings",
        "an-absolute-entry",
        "a-leading-dotdot",
        "a-dotdot-inside",
    ],
)
def test_a_damaged_quarantine_list_stops_and_an_append_never_replaces_it(
    tmp_path: Path, content: str
) -> None:
    """R7.3, R7.4, R4.4 (an entry that leaves the pool is a wrong shape: the list feeds a
    ``mv`` script and rsync excludes) -- the hostile half is the APPEND: it must not start
    a fresh list over a file it could not read."""
    path = tmp_path / QUARANTINE
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(BinhostError) as err:
        load_quarantined(tmp_path)
    assert QUARANTINE in str(err.value)
    with pytest.raises(BinhostError):
        add_quarantined(tmp_path, ["dev-libs/foo/foo-1.0-1.gpkg.tar"])
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "../outside/evil-1.gpkg.tar", "dev-libs/../../evil-1.gpkg.tar"]
)
def test_appending_a_path_that_leaves_the_pool_is_refused_and_writes_nothing(
    tmp_path: Path, bad: str
) -> None:
    """R4.4: never written, so never read back by a worker's ``mv`` or an exclude. A name
    with ``..`` inside it is not a ``..`` component and is accepted (wrongly-refused half)."""
    add_quarantined(tmp_path, ["dev-libs/foo/foo-1.0-1.gpkg.tar"])
    path = tmp_path / QUARANTINE
    assert path.is_file()
    before = path.read_bytes()
    with pytest.raises(BinhostError) as err:
        add_quarantined(tmp_path, ["dev-libs/foo/foo-1.1-1.gpkg.tar", bad])
    assert bad in str(err.value)
    assert path.read_bytes() == before
    add_quarantined(tmp_path, ["app-misc/a..b/a..b-1-1.gpkg.tar"])
    assert "app-misc/a..b/a..b-1-1.gpkg.tar" in load_quarantined(tmp_path)


def test_the_quarantine_list_is_written_through_a_sibling_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renames: list[tuple[Path, Path]] = []
    real = os.replace

    def spy(src: Any, dst: Any, *a: Any, **kw: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    add_quarantined(tmp_path, ["dev-libs/foo/foo-1.0-1.gpkg.tar"])
    onto = [(s, d) for s, d in renames if d == tmp_path / QUARANTINE]
    assert onto, renames
    assert onto[-1][0].parent == tmp_path and onto[-1][0] != tmp_path / QUARANTINE
    assert sorted(p.name for p in tmp_path.iterdir()) == [QUARANTINE]
