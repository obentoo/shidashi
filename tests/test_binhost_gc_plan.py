"""The GC plan and ebuild availability (story 029, task 1.4; R3.1, R3.2).

Contract from design.md "shidashi/binhost.py":

- ``gc_plan(refs, *, available, keep=2)``: candidates are refs whose CPV is not available;
  grouped by ``(cat/pkg, SLOT)``; a group's CPVs ranked by their NEWEST ``BUILD_TIME`` (no
  version comparison); every instance of the ``keep`` most recent unavailable CPVs is kept;
  every instance of an available CPV is kept regardless of ``keep``. Returns what to move.
- ``ebuild_available(repos)``: ``cat/pn-ver`` -> ``<repo>/<cat>/<pn>/<pn>-<ver>.ebuild``
  exists in ANY pinned repo.
"""

from pathlib import Path
from typing import Any

from tests._pending import try_import
from tests.test_binhost_refs import entry, index

parse_refs: Any = try_import("shidashi.binhost", "parse_refs")
gc_plan: Any = try_import("shidashi.binhost", "gc_plan")
ebuild_available: Any = try_import("shidashi.binhost", "ebuild_available")


def _plan(text: str, available: set[str], **kw: Any) -> set[tuple[str, int]]:
    refs = parse_refs(text)
    assert refs, "the fixture index parsed to nothing"
    return {(r.cpv, r.build_id) for r in gc_plan(refs, available=lambda c: c in available, **kw)}


# --- hostile halves first ----------------------------------------------------------------


def test_an_available_cpv_is_never_planned_whatever_its_build_ids_or_keep() -> None:
    """R3.2: a second BUILD_ID of an available CPV is never moved, not even at keep=0."""
    text = index(
        entry("dev-libs/foo-2.0", 1, build_time=100),
        entry("dev-libs/foo-2.0", 2, build_time=200),
        entry("dev-libs/foo-2.0", 3, build_time=300),
        entry("dev-libs/foo-1.0", 1, build_time=50),
    )
    assert _plan(text, {"dev-libs/foo-2.0"}, keep=0) == {("dev-libs/foo-1.0", 1)}


def test_an_available_cpv_takes_no_keep_slot() -> None:
    """Hostile half (wrongly collapse): the newest CPV is available; ranking it with the
    unavailable ones would spend a keep slot on it and move one CPV too many."""
    text = index(
        entry("dev-libs/foo-2.0", 1, build_time=400),  # available, newest
        entry("dev-libs/foo-1.2", 1, build_time=300),
        entry("dev-libs/foo-1.1", 1, build_time=200),
        entry("dev-libs/foo-1.0", 1, build_time=100),
    )
    assert _plan(text, {"dev-libs/foo-2.0"}) == {("dev-libs/foo-1.0", 1)}


def test_three_unavailable_cpvs_with_keep_two_move_exactly_one_with_all_its_instances() -> None:
    text = index(
        entry("dev-libs/foo-1.2", 1, build_time=300),
        entry("dev-libs/foo-1.1", 1, build_time=200),
        entry("dev-libs/foo-1.0", 1, build_time=100),
        entry("dev-libs/foo-1.0", 2, build_time=110),
    )
    assert _plan(text, set()) == {("dev-libs/foo-1.0", 1), ("dev-libs/foo-1.0", 2)}


def test_two_slots_of_one_package_are_ranked_apart() -> None:
    """Hostile half (wrongly collapse): python 3.11 is the only CPV of its slot; ranked with
    3.12's it would be the oldest and move, though its slot keeps every CPV it has."""
    text = index(
        entry("dev-lang/python-3.12.1", 1, slot="3.12", build_time=300),
        entry("dev-lang/python-3.12.0", 1, slot="3.12", build_time=200),
        entry("dev-lang/python-3.11.9", 1, slot="3.11", build_time=100),
    )
    assert _plan(text, set(), keep=1) == {("dev-lang/python-3.12.0", 1)}


def test_one_slot_with_two_subslots_is_ranked_together() -> None:
    """Hostile half (wrongly split): ``0/34`` and ``0/36`` are slot 0 -- a subslot bump
    must not open a second group that keeps ``keep`` more CPVs."""
    text = index(
        entry("dev-cpp/simdutf-9.2.1", 1, slot="0/36", build_time=300),
        entry("dev-cpp/simdutf-6.2.0", 1, slot="0/34", build_time=200),
        entry("dev-cpp/simdutf-6.1.0", 1, slot="0/34", build_time=100),
    )
    assert _plan(text, set(), keep=1) == {
        ("dev-cpp/simdutf-6.2.0", 1),
        ("dev-cpp/simdutf-6.1.0", 1),
    }


def test_two_packages_are_ranked_apart() -> None:
    text = index(
        entry("dev-libs/foo-1.1", 1, build_time=300),
        entry("dev-libs/foo-1.0", 1, build_time=200),
        entry("dev-libs/bar-1.0", 1, build_time=100),
    )
    assert _plan(text, set(), keep=1) == {("dev-libs/foo-1.0", 1)}


def test_cpvs_are_ranked_by_build_time_not_by_version() -> None:
    """GOTCHA version order: 1.0 rebuilt last is the most recent; 2.0 is older."""
    text = index(
        entry("dev-libs/foo-2.0", 1, build_time=100),
        entry("dev-libs/foo-1.0", 1, build_time=500),
    )
    assert _plan(text, set(), keep=1) == {("dev-libs/foo-2.0", 1)}


def test_a_cpv_ranks_by_its_newest_instance() -> None:
    """1.2 has an old instance (50) and a new one (310): it is newer than 1.1 (200)."""
    text = index(
        entry("dev-libs/foo-1.2", 1, build_time=50),
        entry("dev-libs/foo-1.2", 2, build_time=310),
        entry("dev-libs/foo-1.1", 1, build_time=200),
    )
    assert _plan(text, set(), keep=1) == {("dev-libs/foo-1.1", 1)}


def test_within_keep_nothing_is_planned() -> None:
    text = index(
        entry("dev-libs/foo-1.1", 1, build_time=200),
        entry("dev-libs/foo-1.0", 1, build_time=100),
        entry("dev-libs/foo-2.0", 1, build_time=300),
    )
    refs = parse_refs(text)
    assert len(refs) == 3
    assert gc_plan(refs, available=lambda c: c == "dev-libs/foo-2.0") == []
    planned = gc_plan(refs, available=lambda c: c == "dev-libs/foo-2.0", keep=0)
    assert {(r.cpv, r.build_id) for r in planned} == {
        ("dev-libs/foo-1.1", 1),
        ("dev-libs/foo-1.0", 1),
    }


# --- ebuild_available ---------------------------------------------------------------------


def _ebuild(repo: Path, cat: str, pn: str, pf: str) -> None:
    path = repo / cat / pn / f"{pf}.ebuild"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("EAPI=8\n", encoding="utf-8")


def test_an_ebuild_in_any_pinned_repo_makes_its_cpv_available(tmp_path: Path) -> None:
    gentoo, overlay = tmp_path / "gentoo", tmp_path / "bentoo"
    _ebuild(gentoo, "dev-libs", "foo", "foo-2.0")
    _ebuild(overlay, "bentoo-base", "thing", "thing-1.0")
    available = ebuild_available([gentoo, overlay])
    assert available("dev-libs/foo-2.0")
    assert available("bentoo-base/thing-1.0")


def test_only_the_exact_version_of_the_exact_package_is_available(tmp_path: Path) -> None:
    """Hostile half (wrongly collapse): another version of the package, or the same
    ``pn-ver`` in another category, does not make this CPV available."""
    gentoo = tmp_path / "gentoo"
    _ebuild(gentoo, "dev-libs", "foo", "foo-2.0")
    _ebuild(gentoo, "dev-python", "foo", "foo-1.0")
    available = ebuild_available([gentoo])
    assert available("dev-libs/foo-2.0")
    assert not available("dev-libs/foo-1.0")
    assert not available("dev-libs/foo-2.0-r1")


def test_revisions_and_hyphenated_names_find_their_ebuild(tmp_path: Path) -> None:
    """Hostile half (wrongly split): ``-r1`` is part of the version, and a hyphen followed
    by a digit inside the name is part of the name."""
    gentoo = tmp_path / "gentoo"
    _ebuild(gentoo, "app-misc", "plain", "plain-1-r1")
    _ebuild(gentoo, "gui-libs", "gtk-4-layer-shell", "gtk-4-layer-shell-1.0.4")
    _ebuild(gentoo, "dev-lang", "python", "python-3.14.7_p1")
    available = ebuild_available([gentoo])
    assert available("app-misc/plain-1-r1")
    assert available("gui-libs/gtk-4-layer-shell-1.0.4")
    assert available("dev-lang/python-3.14.7_p1")
    assert not available("app-misc/plain-1")
