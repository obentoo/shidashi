"""The slot rule through the ``stale-binpkgs`` step (``shidashi.stale_plan``; story 019).

The build host's case of 2026-11-15: ``dev-lang/python-3.14.10`` and ``-3.15.2``, built
against OpenSSL's old subslot and superseded in the tree, were never rebuilt, so they
never left the pool and ``dev-lang/python`` was excluded in every stage of every build --
the current ``python-3.15.2_p1`` compiled each time beside its fresh binpkg.

Two harnesses, imported: ``tests/test_phases_stale.py`` (the subslot pass; its emerge
rebuilds vte) and ``tests/test_phases_soname.py`` (the soname pass, with a plan and the
providers-first oneshot).
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from shidashi import phases
from shidashi.container import CommandResult
from tests import test_phases_soname as soname
from tests.test_phases_stale import (
    CURL,
    EXCLUDE_VTE,
    GNOME,
    SIMDUTF,
    TREE,
    VTE_SLOT,
    VTE_STALE,
    _binhost,
    _entry,
    _Factory,
    _quarantined,
    _recipe,
    _run,
)

OPENSSL = ">=dev-libs/openssl-1.1.1:0"
PY314, PY315 = "dev-lang/python:3.14", "dev-lang/python:3.15"
OLD_314 = _entry("dev-lang/python-3.14.10", 1, f"{OPENSSL}/3=", slot="3.14")
OLD_315 = _entry("dev-lang/python-3.15.2", 1, f"{OPENSSL}/3=", slot="3.15")
NEW_315 = _entry("dev-lang/python-3.15.2_p1", 6, f"{OPENSSL}/4=", slot="3.15")
SPICE = _entry("app-emulation/spice-0.16.0", 1, f"{OPENSSL}/3=")
#: the pinned tree: OpenSSL 4, and a newer python in each slot
PINNED = {
    **TREE,
    OPENSSL: ["dev-libs/openssl-4.0.1", "0/4"],
    PY314: ["dev-lang/python-3.14.10_p1", "3.14"],
    PY315: ["dev-lang/python-3.15.2_p1", "3.15"],
}


def _exclusions(argv: list[str]) -> list[str]:
    return [a for a in argv if a.startswith("--usepkg-exclude")]


def _asked(container: Any) -> list[str]:
    """Every ``best_visible`` query the stage's portageq was asked, in order."""
    return [r["argv"][-1] for r in container.tools("portageq") if r["argv"][0] == "best_visible"]


def test_the_old_pythons_move_aside_at_once_and_the_current_one_is_not_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured case. Under the per-package rule this emerge carried
    ``--usepkg-exclude=dev-lang/python`` and the two old binpkgs stayed in the pool."""
    pkgdir = _binhost(tmp_path, monkeypatch, OLD_314, OLD_315, NEW_315, CURL)
    container = _Factory(tmp_path, pkgdir, tree=PINNED)
    step = _run(tmp_path, container)

    (emerge,) = container.emerges
    assert emerge == phases.phase_emerge_argv(GNOME, _recipe(), emptytree=False)
    (pretend,) = container.pretends
    assert _exclusions(pretend) == []
    # gone BEFORE the emerge (and before its plan), the fresh one still there
    assert container.pool_at_emerge == [{"python-3.15.2_p1-6.gpkg.tar", "curl-8.16.0-1.gpkg.tar"}]
    assert sorted(p.name for p in _quarantined(tmp_path)) == [
        "python-3.14.10-1.gpkg.tar",
        "python-3.15.2-1.gpkg.tar",
    ]
    index = (pkgdir / "Packages").read_text()
    assert "python-3.15.2-1.gpkg.tar" not in index and "python-3.15.2_p1-6.gpkg.tar" in index
    # the trail alone tells what went, and what the tree picks instead
    assert step["excluded"] == []
    assert step["superseded"] == [
        {
            "cpv": "dev-lang/python-3.14.10",
            "build_id": 1,
            "slot": PY314,
            "tree_picks": "dev-lang/python-3.14.10_p1",
        },
        {
            "cpv": "dev-lang/python-3.15.2",
            "build_id": 1,
            "slot": PY315,
            "tree_picks": "dev-lang/python-3.15.2_p1",
        },
    ]
    assert len(step["quarantined"]) == 2
    assert {e["cpv"] for e in step["stale"]} == {
        "dev-lang/python-3.14.10",
        "dev-lang/python-3.15.2",
    }


def test_the_second_run_of_a_stage_excludes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop, broken. Run 1 moves the old pythons aside, compiles vte's slot and moves
    vte's stale binpkg aside once the fresh one exists; run 2 finds nothing stale."""
    pkgdir = _binhost(tmp_path, monkeypatch, OLD_314, OLD_315, NEW_315, VTE_STALE, CURL)
    first = _Factory(tmp_path / "one", pkgdir, tree=PINNED)
    step = _run(tmp_path / "one", first)
    (emerge,) = first.emerges
    assert _exclusions(emerge) == [EXCLUDE_VTE]  # python is not in it
    assert step["excluded"] == [VTE_SLOT] and len(step["superseded"]) == 2
    assert sorted(p.name for p in _quarantined(tmp_path)) == [
        "python-3.14.10-1.gpkg.tar",
        "python-3.15.2-1.gpkg.tar",
        "vte-0.84.1-1.gpkg.tar",  # settle_stale: after the rebuild wrote build 2
    ]

    second = _Factory(tmp_path / "two", pkgdir, tree=PINNED)
    again = _run(tmp_path / "two", second)
    assert second.emerges == [phases.phase_emerge_argv(GNOME, _recipe(), emptytree=False)]
    assert _exclusions(second.pretends[0]) == []
    assert again["stale"] == [] and again["excluded"] == [] and again["superseded"] == []
    assert again["quarantined"] == []
    assert len(_quarantined(tmp_path)) == 3  # nothing more moved
    assert second.tools("emaint") == []


def test_a_version_with_nothing_visible_in_its_slot_moves_aside_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``app-emulation/spice-0.16.0``: removed from the tree, so nothing will rebuild it."""
    pkgdir = _binhost(tmp_path, monkeypatch, SPICE, CURL)
    container = _Factory(tmp_path, pkgdir, tree=PINNED)
    step = _run(tmp_path, container)

    assert container.emerges == [phases.phase_emerge_argv(GNOME, _recipe(), emptytree=False)]
    assert container.pool_at_emerge == [{"curl-8.16.0-1.gpkg.tar"}]
    assert step["excluded"] == []
    assert step["superseded"] == [
        {
            "cpv": "app-emulation/spice-0.16.0",
            "build_id": 1,
            "slot": "app-emulation/spice:0",
            "tree_picks": None,
        }
    ]


def test_the_tree_is_asked_only_about_the_slots_left_with_no_fresh_binpkg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second, small resolver run: the all-stale versions' slot atoms, plus one provider
    the first run resolved -- the witness that Portage still answers."""
    pkgdir = _binhost(tmp_path, monkeypatch, OLD_315, NEW_315, VTE_STALE, CURL)
    container = _Factory(tmp_path, pkgdir, tree=PINNED)
    _run(tmp_path, container)

    providers = sorted([OPENSSL, SIMDUTF])
    assert _asked(container) == [*providers, PY315, VTE_SLOT, providers[0]]
    assert container.resolver_runs == 2  # the soname pass adds no slot: no third run


def test_a_tree_that_stops_answering_fails_the_step_before_anything_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: the second run answers nothing at all. Taken at its word, every slot would
    read "nothing visible" and every binpkg asked about would move aside."""
    pkgdir = _binhost(tmp_path, monkeypatch, OLD_315, NEW_315, VTE_STALE, CURL)
    container = _Factory(tmp_path, pkgdir, tree=PINNED, later_trees=({},))
    with pytest.raises(phases.FactoryError) as err:
        phases.run_phase(container, _recipe(), GNOME, emptytree=False)  # type: ignore[arg-type]
    assert err.value.phase == "gnome:stale-binpkgs"
    assert "no longer resolves" in str(err.value)
    assert container.emerges == [] and _quarantined(tmp_path) == []
    assert (pkgdir / "dev-lang/python/python-3.15.2-1.gpkg.tar").is_file()


class _SlotQuestionFails(_Factory):
    """The 3.3 container, except the run that asks about vte's slot fails."""

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        if VTE_SLOT in list(argv):
            raise subprocess.CalledProcessError(1, argv, output="", stderr="portageq: boom\n")
        return super().run(argv, env=env, check=check)


def test_a_failed_slot_question_stops_the_stage_before_its_emerge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = _binhost(tmp_path, monkeypatch, VTE_STALE, CURL)
    container = _SlotQuestionFails(tmp_path, pkgdir)
    with pytest.raises(phases.FactoryError) as err:
        phases.run_phase(container, _recipe(), GNOME, emptytree=False)  # type: ignore[arg-type]
    assert err.value.phase == "gnome:stale-binpkgs" and "portageq: boom" in err.value.output
    assert container.emerges == [] and _quarantined(tmp_path) == []


# --- the soname pass: the three consumers, and a planned version the tree does not pick -----


def test_the_plan_the_providers_first_emerge_and_the_stage_emerge_get_the_slot_atom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nodejs's slot is excluded by the subslot rule: the ``--pretend`` plan, the
    providers-first oneshot and the stage emerge all carry the slot atom, none the package."""
    nodejs = soname._entry(soname.NODEJS_CPV, 1, rdepend=f"{soname.ICU}/77=")
    pkgdir = soname._binhost(
        tmp_path, monkeypatch, soname.SIMDUTF_OLD, soname.VTE_STALE, nodejs, soname.CURL
    )
    tree = {
        **soname.TREE,
        soname.ICU: ["dev-libs/icu-78.1", "0/78"],
        soname.NODEJS_SLOT: [soname.NODEJS_CPV, "0"],
    }
    container = soname._Factory(
        tmp_path,
        pkgdir,
        plan=[soname.P_SIMDUTF_EBUILD, soname.P_VTE_1],
        tree=tree,
        installs=soname.SIMDUTF_INSTALLED,
    )
    step = soname._run(tmp_path, container)

    nodejs_atom = f"--usepkg-exclude={soname.NODEJS_SLOT}"
    (pretend,) = container.pretends()
    (oneshot,) = container.oneshots()
    (stage,) = container.stage_emerges()
    assert _exclusions(pretend) == [nodejs_atom]
    assert _exclusions(oneshot) == [nodejs_atom]
    # the stage emerge adds what the soname pass found: vte's slot
    assert _exclusions(stage) == [nodejs_atom, soname.EXCLUDE_VTE]
    assert step["excluded"] == [soname.NODEJS_SLOT, soname.VTE_SLOT]


def test_a_planned_stale_version_the_tree_does_not_pick_moves_now_and_its_slot_is_compiled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan merges vte-0.84.1 (stale by soname) while the tree picks vte-0.84.2 in the
    slot: the stale binpkg moves aside before the stage emerge, and the slot is still
    excluded -- whatever binpkg the emerge would take there instead was never judged."""
    pkgdir = soname._binhost(
        tmp_path, monkeypatch, soname.SIMDUTF_BIN, soname.VTE_STALE, soname.CURL
    )
    tree = {**soname.TREE, soname.VTE_SLOT: ["x11-libs/vte-0.84.2", "2.91"]}
    container = soname._Factory(
        tmp_path,
        pkgdir,
        plan=[soname.P_SIMDUTF_BIN, soname.P_VTE_1],
        tree=tree,
        installs=soname.SIMDUTF_INSTALLED,
    )
    step = soname._run(tmp_path, container)

    (stage,) = container.stage_emerges()
    assert _exclusions(stage) == [soname.EXCLUDE_VTE]
    assert container.stale_file_at_stage == [False]  # moved before the stage emerge
    assert step["excluded"] == [soname.VTE_SLOT]
    assert step["superseded"] == [
        {
            "cpv": "x11-libs/vte-0.84.1",
            "build_id": 1,
            "slot": soname.VTE_SLOT,
            "tree_picks": "x11-libs/vte-0.84.2",
        }
    ]


class _SonameSlotQuestionFails(soname._Factory):
    """The 3.4 container, except the run that asks about vte's slot fails."""

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        if soname.VTE_SLOT in list(argv):
            raise subprocess.CalledProcessError(1, argv, output="", stderr="portageq: boom\n")
        return super().run(argv, env=env, check=check)


def test_a_failed_slot_question_of_the_soname_pass_stops_the_stage_before_its_emerge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vte is stale by soname only, so its slot is asked after the plan: that run failing
    is the factory's error too, with the stage's phase."""
    pkgdir = soname._binhost(
        tmp_path, monkeypatch, soname.SIMDUTF_BIN, soname.VTE_STALE, soname.CURL
    )
    container = _SonameSlotQuestionFails(
        tmp_path, pkgdir, plan=[soname.P_SIMDUTF_BIN, soname.P_VTE_1]
    )
    with pytest.raises(phases.FactoryError) as err:
        phases.run_phase(container, soname._recipe(), soname.GNOME, emptytree=False)  # type: ignore[arg-type]
    assert err.value.phase == "gnome:stale-binpkgs" and "portageq: boom" in err.value.output
    assert container.stage_emerges() == [] and soname._quarantined(tmp_path) == []


def test_the_soname_pass_asks_about_a_version_made_all_stale_by_both_rules_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build 3 is stale by subslot, and the index still names it though a run that stopped
    before its reindex already moved the file; build 4, the planned one, is stale by
    soname. Neither rule alone leaves vte-0.84.1 without a fresh binpkg -- together they
    do, so its slot is asked about with BOTH sets of findings in hand."""
    by_subslot = soname._entry("x11-libs/vte-0.84.1", 3, slot="2.91", rdepend=f"{SIMDUTF}/33=")
    by_soname = soname._entry(
        "x11-libs/vte-0.84.1",
        4,
        slot="2.91",
        rdepend=f"{SIMDUTF}/34=",
        requires="x86_64: libc.so.6 libsimdutf.so.34",
    )
    pkgdir = soname._binhost(tmp_path, monkeypatch, soname.SIMDUTF_BIN, by_subslot, by_soname)
    (pkgdir / "x11-libs/vte/vte-0.84.1-3.gpkg.tar").unlink()  # the interrupted run's move
    tree = {**soname.TREE, soname.VTE_SLOT: ["x11-libs/vte-0.84.2", "2.91"]}
    planned = '[binary   N    ] x11-libs/vte-0.84.1-4:2.91::gentoo  USE="-vala" 0 KiB'
    container = soname._Factory(
        tmp_path,
        pkgdir,
        plan=[soname.P_SIMDUTF_BIN, planned],
        tree=tree,
        installs=soname.SIMDUTF_INSTALLED,
    )
    step = soname._run(tmp_path, container)

    assert soname.VTE_SLOT in _asked(container)
    assert [(e["build_id"], e["tree_picks"]) for e in step["superseded"]] == [
        (3, "x11-libs/vte-0.84.2"),
        (4, "x11-libs/vte-0.84.2"),
    ]
    assert step["excluded"] == [soname.VTE_SLOT]
