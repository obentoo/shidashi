"""Cut instances and classification (story 029, task 1.3; R1.2, R2.2-R2.5).

Contract from design.md "shidashi/binhost.py":

- ``cut_instances(refs, cuts)``: an instance of a cut's package whose ``USE`` has the cut's
  flag in the cut's state (``(flag in use) == cut.enable``);
- ``classify(refs, record, *, available)``: in any stage's finals -> FINAL; else in any
  stage's transients -> TRANSIENT; else ``not available(cpv)`` -> OLD; else UNRECORDED.
  Instances are ``(cpv, build_id)`` pairs, never a CPV alone (GOTCHA).

Refs come from an index (``parse_refs``), never by hand.
"""

from collections.abc import Iterable
from typing import Any

from shidashi.recipe import UseBreak
from tests._pending import try_import
from tests.test_binhost_refs import entry, index

parse_refs: Any = try_import("shidashi.binhost", "parse_refs")
cut_instances: Any = try_import("shidashi.binhost", "cut_instances")
classify: Any = try_import("shidashi.binhost", "classify")
Kind: Any = try_import("shidashi.binhost", "Kind")
PoolRecord: Any = try_import("shidashi.binhost", "PoolRecord")

FFMPEG = "media-video/ffmpeg-8.0"
NO_SDL = UseBreak(atom="media-video/ffmpeg", flag="sdl", enable=False)


def _keys(refs: Iterable[Any]) -> set[tuple[str, int]]:
    return {(r.cpv, r.build_id) for r in refs}


def _record(
    finals: dict[str, set[tuple[str, int]]], transients: dict[str, set[tuple[str, int]]]
) -> Any:
    return PoolRecord(
        finals={k: frozenset(v) for k, v in finals.items()},
        transients={k: frozenset(v) for k, v in transients.items()},
    )


def _kinds(refs: Any, record: Any, available: Any) -> dict[tuple[str, int], Any]:
    return {(r.cpv, r.build_id): k for r, k in classify(refs, record, available=available).items()}


# --- cut_instances ------------------------------------------------------------------------


def test_a_cut_off_flag_selects_the_instances_built_without_it() -> None:
    refs = parse_refs(
        index(
            entry(FFMPEG, 2, use="x264"),  # the cut build: no sdl
            entry(FFMPEG, 3, use="sdl x264"),  # the settled final
        )
    )
    assert _keys(cut_instances(refs, [NO_SDL])) == {(FFMPEG, 2)}


def test_a_cut_on_flag_selects_the_instances_built_with_it() -> None:
    refs = parse_refs(
        index(
            entry("dev-lang/python-3.14.7", 1, use="bluetooth ssl"),
            entry("dev-lang/python-3.14.7", 2, use="ssl"),
        )
    )
    cut = UseBreak(atom="dev-lang/python", flag="bluetooth", enable=True)
    assert _keys(cut_instances(refs, [cut])) == {("dev-lang/python-3.14.7", 1)}


def test_a_cut_selects_only_its_own_package() -> None:
    """Hostile half (wrongly collapse): ``media-video/ffmpeg-compat`` starts with the cut
    package's name and ``media-libs/libsdl2`` lacks the flag too -- neither is the cut
    package. A flag spelled ``sdl2`` is not ``sdl``: that ffmpeg build lacks ``sdl``."""
    refs = parse_refs(
        index(
            entry(FFMPEG, 2, use="sdl2 x264"),
            entry("media-video/ffmpeg-compat-1.0", 1, use="x264"),
            entry("media-libs/libsdl2-2.30.0", 1, use="x264"),
            entry("media-video/ffmpeg-7.1", 1, use="x264"),  # another version: still ffmpeg
        )
    )
    assert _keys(cut_instances(refs, [NO_SDL])) == {(FFMPEG, 2), ("media-video/ffmpeg-7.1", 1)}


def test_several_cuts_each_select_their_own_instances() -> None:
    refs = parse_refs(
        index(
            entry(FFMPEG, 2, use="x264"),
            entry("media-video/pipewire-1.4.9", 1, use="sound-server"),
            entry("media-video/pipewire-1.4.9", 2, use="ffmpeg sound-server"),
        )
    )
    cuts = [NO_SDL, UseBreak(atom="media-video/pipewire", flag="ffmpeg", enable=False)]
    assert _keys(cut_instances(refs, cuts)) == {(FFMPEG, 2), ("media-video/pipewire-1.4.9", 1)}


# --- classify -----------------------------------------------------------------------------


def test_an_instance_final_for_one_stage_and_transient_for_another_is_final() -> None:
    """R2.2, hostile half first: ``ffmpeg[-sdl]`` is kde's cut and minimal's final (GOTCHA
    cut_instances) -- TRANSIENT here would drop minimal's ffmpeg from the publish pool."""
    refs = parse_refs(index(entry(FFMPEG, 3, use="x264")))
    record = _record(
        finals={"minimal": {(FFMPEG, 3)}, "kde": set()},
        transients={"kde": {(FFMPEG, 3)}, "minimal": set()},
    )
    assert _kinds(refs, record, lambda _cpv: True) == {(FFMPEG, 3): Kind.FINAL}


def test_a_recorded_instance_is_matched_by_cpv_and_build_id_never_cpv_alone() -> None:
    """Hostile half (wrongly collapse): build 3 is the final; builds 2 and 4 of the same
    CPV are not -- 2 is a recorded transient, 4 nobody recorded."""
    refs = parse_refs(
        index(
            entry(FFMPEG, 2, use="x264"),
            entry(FFMPEG, 3, use="sdl x264"),
            entry(FFMPEG, 4, use="sdl x264"),
        )
    )
    record = _record(finals={"minimal": {(FFMPEG, 3)}}, transients={"kde": {(FFMPEG, 2)}})
    assert _kinds(refs, record, lambda _cpv: True) == {
        (FFMPEG, 2): Kind.TRANSIENT,
        (FFMPEG, 3): Kind.FINAL,
        (FFMPEG, 4): Kind.UNRECORDED,
    }


def test_a_recorded_instance_is_final_or_transient_whatever_its_ebuild() -> None:
    """A recorded final whose ebuild left the tree is still FINAL: the record wins."""
    refs = parse_refs(index(entry(FFMPEG, 3), entry(FFMPEG, 2)))
    record = _record(finals={"minimal": {(FFMPEG, 3)}}, transients={"kde": {(FFMPEG, 2)}})
    assert _kinds(refs, record, lambda _cpv: False) == {
        (FFMPEG, 3): Kind.FINAL,
        (FFMPEG, 2): Kind.TRANSIENT,
    }


def test_an_unrecorded_instance_is_old_without_its_ebuild_and_unrecorded_with_it() -> None:
    """R2.4, R2.5."""
    refs = parse_refs(index(entry("dev-libs/foo-1.0", 1), entry("dev-libs/foo-2.0", 1)))
    record = _record(finals={}, transients={})
    assert _kinds(refs, record, lambda cpv: cpv == "dev-libs/foo-2.0") == {
        ("dev-libs/foo-1.0", 1): Kind.OLD,
        ("dev-libs/foo-2.0", 1): Kind.UNRECORDED,
    }


def test_every_instance_gets_exactly_one_kind() -> None:
    """R2.1: one kind per instance of the index, none left out."""
    refs = parse_refs(
        index(
            entry(FFMPEG, 2),
            entry(FFMPEG, 3),
            entry("dev-libs/foo-1.0", 1),
            entry("dev-libs/foo-2.0", 1),
            entry("dev-libs/foo-2.0", 2),
        )
    )
    record = _record(finals={"kde": {(FFMPEG, 3)}}, transients={"kde": {(FFMPEG, 2)}})
    kinds = classify(refs, record, available=lambda cpv: cpv != "dev-libs/foo-1.0")
    assert len(refs) == 5
    assert set(kinds) == set(refs)
    assert sorted(kinds.values()) == sorted(
        [Kind.TRANSIENT, Kind.FINAL, Kind.OLD, Kind.UNRECORDED, Kind.UNRECORDED]
    )
