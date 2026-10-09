"""The ``stale-binpkgs`` stage step kind (story 019, task 3.1): at most once, before
``emerge-stage``, and the repository's flow places it there."""

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
