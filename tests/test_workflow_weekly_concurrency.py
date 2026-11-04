"""``.github/workflows/weekly-release.yml`` keeps a second dispatch waiting (story 032, task
12.4; R7.6).

Contract (story.md R7.6; design.md ".github/workflows/weekly-release.yml"): the workflow has a
top-level ``concurrency`` group ``weekly-release`` with ``cancel-in-progress: false``, so a
second dispatch waits for the running release instead of cancelling it. A job-level
``concurrency`` that cancels would undo it, so none may.
"""

from typing import Any

import yaml

from tests._fake_builder_guest import CHECKOUT

WORKFLOW = CHECKOUT / ".github" / "workflows" / "weekly-release.yml"


def load_workflow() -> dict[Any, Any]:
    assert WORKFLOW.is_file(), f"{WORKFLOW} is missing"
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW} is not a mapping"
    return data


def is_false(value: Any) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")


def test_weekly_release_has_the_weekly_release_concurrency_group() -> None:
    concurrency = load_workflow().get("concurrency")
    assert isinstance(concurrency, dict), f"no top-level concurrency mapping: {concurrency!r}"
    assert concurrency.get("group") == "weekly-release", concurrency


def test_weekly_release_concurrency_never_cancels_the_running_release() -> None:
    workflow = load_workflow()
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), f"no top-level concurrency mapping: {concurrency!r}"
    assert "cancel-in-progress" in concurrency, concurrency
    assert is_false(concurrency["cancel-in-progress"]), concurrency
    for name, job in (workflow.get("jobs") or {}).items():
        local = job.get("concurrency") if isinstance(job, dict) else None
        if isinstance(local, dict):
            assert is_false(local.get("cancel-in-progress", False)), (name, local)
