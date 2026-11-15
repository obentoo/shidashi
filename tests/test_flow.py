"""Where the stage step kinds may stand in ``stages.steps``.

``stale-binpkgs`` (story 019, task 3.1): at most once, before ``emerge-stage``, and
the repository's flow places it there. ``perl-rebuild``: at most once before
``emerge-stage`` (after ``apply-config``) and at most once after it (before
``snapshot``), and the repository's flow has both.
"""

from typing import Any

import pytest

from shidashi import phases
from shidashi.flow import StagesFlow


def _steps() -> list[dict[str, Any]]:
    return [s.model_dump() for s in phases.stages_flow().steps]


def _validate(steps: list[dict[str, Any]]) -> StagesFlow:
    data = phases.stages_flow().model_dump()
    data["steps"] = steps
    return StagesFlow.model_validate(data)


def test_the_repository_flow_finds_stale_binpkgs_before_the_stage_emerge() -> None:
    kinds = [s["do"] for s in _steps()]
    assert kinds.count("stale-binpkgs") == 1, kinds
    assert kinds.index("stale-binpkgs") < kinds.index("emerge-stage")


def test_the_flow_refuses_stale_binpkgs_after_the_emerge() -> None:
    steps = [s for s in _steps() if s["do"] != "stale-binpkgs"]
    steps.append({"name": "stale", "do": "stale-binpkgs"})  # after the emerge
    with pytest.raises(ValueError, match="stale-binpkgs must come before emerge-stage"):
        _validate(steps)


def test_the_flow_refuses_stale_binpkgs_twice() -> None:
    steps = [s for s in _steps() if s["do"] != "stale-binpkgs"]
    at = [s["do"] for s in steps].index("emerge-stage")
    twice = [{"name": "stale", "do": "stale-binpkgs"}] * 2
    with pytest.raises(ValueError, match="`stale-binpkgs` at most once"):
        _validate([*steps[:at], *twice, *steps[at:]])


# --- perl-rebuild: once before the emerge, once between the emerge and the fork point ---

_PERL = {"name": "perl", "do": "perl-rebuild"}


def _with_perl(*, before: int = 0, after: int = 0) -> list[dict[str, Any]]:
    """The repository's steps with that many ``perl-rebuild`` right before and
    right after ``emerge-stage``, and none anywhere else."""
    steps = [s for s in _steps() if s["do"] != "perl-rebuild"]
    at = [s["do"] for s in steps].index("emerge-stage")
    return [*steps[:at], *[_PERL] * before, steps[at], *[_PERL] * after, *steps[at + 1 :]]


def test_the_repository_flow_repairs_perl_before_the_emerge_and_right_after_it() -> None:
    steps = _steps()
    kinds = [s["do"] for s in steps]
    first, second = (i for i, kind in enumerate(kinds) if kind == "perl-rebuild")
    assert kinds.index("apply-config") < first < kinds.index("emerge-stage")
    # right after the emerge: the later steps of the stage and its fork point see the repair
    assert second == kinds.index("emerge-stage") + 1
    assert second < kinds.index("snapshot")
    assert steps[first]["name"] != steps[second]["name"]


@pytest.mark.parametrize(("before", "after"), [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_the_flow_accepts_perl_rebuild_once_on_each_side_of_the_emerge(
    before: int, after: int
) -> None:
    kinds = [s.do for s in _validate(_with_perl(before=before, after=after)).steps]
    assert kinds.count("perl-rebuild") == before + after


@pytest.mark.parametrize(("before", "after"), [(2, 0), (0, 2), (2, 1), (1, 2)])
def test_the_flow_refuses_perl_rebuild_twice_on_one_side_of_the_emerge(
    before: int, after: int
) -> None:
    said = f"at most once after it, has {before} before and {after} after"
    with pytest.raises(ValueError, match=said):
        _validate(_with_perl(before=before, after=after))


@pytest.mark.parametrize("stage_step", ["snapshot", "check-binpkgs"])
def test_the_flow_refuses_a_perl_rebuild_the_fork_point_would_not_see(stage_step: str) -> None:
    steps = _with_perl(before=1)
    at = [s["do"] for s in steps].index(stage_step)
    steps.insert(at + 1, _PERL)  # after the fork point is written
    with pytest.raises(ValueError, match="perl-rebuild after emerge-stage must come before snap"):
        _validate(steps)


def test_the_flow_refuses_a_perl_rebuild_before_the_configuration_is_applied() -> None:
    steps = _with_perl(after=1)
    at = [s["do"] for s in steps].index("apply-config")
    steps.insert(at, _PERL)
    with pytest.raises(ValueError, match="before emerge-stage must come after apply-config"):
        _validate(steps)
