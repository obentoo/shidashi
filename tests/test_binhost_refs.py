"""``binhost.parse_refs``: every ``Packages`` entry as a ``BinpkgRef`` (story 029, task 1.1).

Contract from design.md "shidashi/binhost.py": ``safe_path(root, rel)`` joins a PATH read
from an index or a list under ``root``, refusing an absolute PATH or a ``..`` component
(R3.11); ``BinpkgRef(cpv, build_id, path, slot, use,
build_time)``, built from the index and never by hand; the block split of
``binpkgs.parse_index`` -- the header has no ``CPV`` and is skipped, a malformed block is
skipped, never raised. ``USE`` is a set of flags, so a flag is a whole token, never a
substring of another one.

The index helpers here (``HEADER``, ``entry``, ``index``) are the fake ``Packages`` every
story-029 test builds, in the shape of tests/test_phases_stale.py.
"""

from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import

parse_refs: Any = try_import("shidashi.binhost", "parse_refs")
safe_path: Any = try_import("shidashi.binhost", "safe_path")
BinhostError: Any = try_import("shidashi.binhost", "BinhostError")

HEADER = "ARCH: amd64\nVERSION: 0\n"


def binpkg_path(cpv: str, build: int) -> str:
    """``cat/pn/pn-ver-BUILD.gpkg.tar``: where Portage puts a multi-instance binpkg."""
    category, pf = cpv.split("/")
    parts = pf.split("-")
    # the PN is everything before the first part that starts a version
    pn_end = next(i for i, p in enumerate(parts) if i > 0 and p[:1].isdigit())
    pn = "-".join(parts[:pn_end])
    return f"{category}/{pn}/{pf}-{build}.gpkg.tar"


def entry(
    cpv: str,
    build: int,
    *,
    slot: str = "0",
    use: str = "",
    build_time: int = 0,
    path: str | None = None,
    sha1: str = "",
) -> str:
    """One ``Packages`` block, as ``emaint binhost --fix`` writes it."""
    lines = [
        f"BUILD_ID: {build}",
        f"BUILD_TIME: {build_time}",
        f"CPV: {cpv}",
        f"MD5: {sha1[:32] or 'd41d8cd98f00b204e9800998ecf8427e'}",
        f"PATH: {path or binpkg_path(cpv, build)}",
        f"SHA1: {sha1 or 'da39a3ee5e6b4b0d3255bfef95601890afd80709'}",
        f"SLOT: {slot}",
        f"USE: {use}",
    ]
    return "\n".join(lines) + "\n"


def index(*entries: str) -> str:
    return "\n".join([HEADER, *entries])


def _by_key(refs: Any) -> dict[tuple[str, int], Any]:
    return {(r.cpv, r.build_id): r for r in refs}


def test_every_field_of_an_entry_reaches_its_ref() -> None:
    text = index(
        entry(
            "media-video/ffmpeg-8.0",
            3,
            slot="0/62",
            use="amd64 sdl x264",
            build_time=1760900000,
        )
    )
    refs = parse_refs(text)
    assert len(refs) == 1, refs
    ref = refs[0]
    assert ref.cpv == "media-video/ffmpeg-8.0"
    assert ref.build_id == 3
    assert ref.path == "media-video/ffmpeg/ffmpeg-8.0-3.gpkg.tar"
    assert ref.use == frozenset({"amd64", "sdl", "x264"})
    assert ref.build_time == 1760900000
    assert ref.slot.split("/")[0] == "0"


def test_two_instances_of_one_cpv_stay_two_refs() -> None:
    """Hostile half (wrongly collapse): a CPV names every instance; the BUILD_ID tells
    them apart (GOTCHA binpkg-multi-instance)."""
    text = index(
        entry("media-video/ffmpeg-8.0", 2, use="x264", build_time=100),
        entry("media-video/ffmpeg-8.0", 3, use="sdl x264", build_time=200),
    )
    refs = _by_key(parse_refs(text))
    assert set(refs) == {("media-video/ffmpeg-8.0", 2), ("media-video/ffmpeg-8.0", 3)}
    assert refs["media-video/ffmpeg-8.0", 2].path != refs["media-video/ffmpeg-8.0", 3].path
    assert "sdl" not in refs["media-video/ffmpeg-8.0", 2].use
    assert "sdl" in refs["media-video/ffmpeg-8.0", 3].use


def test_one_use_set_written_in_another_order_is_the_same_use() -> None:
    """Hostile half (wrongly split): the same flags in another order are one USE."""
    refs = parse_refs(
        index(
            entry("dev-libs/foo-1.0", 1, use="b a c"),
            entry("dev-libs/foo-1.0", 2, use="c  b a"),
        )
    )
    assert len(refs) == 2, refs
    a, b = refs
    assert a.use == b.use == frozenset({"a", "b", "c"})


def test_a_flag_is_a_whole_token_never_a_substring() -> None:
    refs = parse_refs(index(entry("media-libs/libsdl2-2.30.0", 1, use="sdl2 x264")))
    assert len(refs) == 1, refs
    assert "sdl" not in refs[0].use
    assert "sdl2" in refs[0].use


def test_an_entry_without_use_has_an_empty_use_set() -> None:
    block = entry("app-misc/plain-1", 1).replace("USE: \n", "")
    refs = parse_refs(index(block))
    assert len(refs) == 1, refs
    assert refs[0].use == frozenset()


def test_the_header_and_malformed_blocks_are_skipped_never_raised() -> None:
    no_cpv = entry("app-misc/a-1", 1).replace("CPV: app-misc/a-1\n", "")
    no_path = "BUILD_ID: 1\nCPV: app-misc/b-1\nSLOT: 0\n"
    bad_build = entry("app-misc/c-1", 1).replace("BUILD_ID: 1", "BUILD_ID: x")
    good = entry("app-misc/d-1", 4, build_time=7)
    refs = parse_refs(index(no_cpv, no_path, bad_build, good))
    assert [(r.cpv, r.build_id) for r in refs] == [("app-misc/d-1", 4)]
    assert parse_refs("") == [] and parse_refs(HEADER) == []


# --- safe_path (R3.11) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "/etc/passwd",
        "../outside/evil-1.gpkg.tar",
        "dev-libs/foo/../../../evil-1.gpkg.tar",
        "dev-libs/..",
        "..",
    ],
)
def test_safe_path_refuses_an_absolute_path_or_a_dotdot_component(tmp_path: Path, rel: str) -> None:
    """Hostile half: every spelling that leaves the root."""
    with pytest.raises(BinhostError) as err:
        safe_path(tmp_path, rel)
    assert rel in str(err.value)


def test_safe_path_joins_a_path_whose_names_only_contain_dots(tmp_path: Path) -> None:
    """Hostile half (wrongly refused): ``..`` inside a name is not a ``..`` component."""
    for rel in ("dev-libs/foo/foo-1.0-1.gpkg.tar", "app-misc/a..b/a..b-1-1.gpkg.tar", ".x/y"):
        assert safe_path(tmp_path, rel) == tmp_path / rel
