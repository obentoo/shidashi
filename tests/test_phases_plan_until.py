"""UNIT tests of ``plan_phase_run`` with ``until`` on a completed phase (story 004, 9.1).

The audit of 2026-10-14 found that ``--until X`` with X already completed planned every
phase after X: the completed phase was skipped before the stop was checked. Re-running the
same ``--until`` must plan nothing (R1.1/R1.4).
"""

from shidashi.phases import plan_phase_run
from shidashi.recipe import Phase, ResolvedRecipe


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(Phase(name="rebuild", emptytree=True), Phase(name="graphics"), Phase(name="apps")),
        portage_layers=("base", "arch/v3", "flavor/minimal", "init/systemd"),
    )


def _names(completed: tuple[str, ...], until: str | None) -> tuple[str, ...]:
    return tuple(p.name for p in plan_phase_run(_recipe(), completed=completed, until=until))


def test_until_a_completed_phase_plans_nothing() -> None:
    assert _names(("rebuild",), "rebuild") == ()


def test_until_a_completed_middle_phase_plans_nothing_after_it() -> None:
    assert _names(("rebuild", "graphics"), "graphics") == ()


def test_until_past_the_completed_phases_stops_at_it() -> None:
    assert _names(("rebuild",), "graphics") == ("graphics",)


def test_without_until_the_completed_phases_are_skipped() -> None:
    assert _names(("rebuild",), None) == ("graphics", "apps")
