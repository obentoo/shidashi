"""``.github/workflows/weekly-release.yml`` (story 032, task 8.1; R7.1-R7.4, Q10).

Contract (story.md R7; design.md ".github/workflows/weekly-release.yml"): triggered only by
``workflow_dispatch``; top-level ``permissions: {}``; one job on ``[self-hosted,
bentoo-builder]`` with a ``timeout-minutes``; steps: the checkout (SHA-pinned,
``persist-credentials: false``), ``uv sync --frozen``, ``shidashi release v3 systemd
--unsigned --images minimal,kde,gnome`` into ``/mnt/out/releases``, and the release report
appended to ``$GITHUB_STEP_SUMMARY`` with ``if: always()``. Every action pinned by a commit
SHA; no ``github.event`` value inside a ``run:``.

``zizmor`` and ``actionlint`` are not installed (design.md "Tooling Decisions"): this test
stands in for the rules the story relies on. ``yaml.safe_load`` reads the key ``on`` as
``True`` (design.md GOTCHA), so both are looked up.
"""

import re
from pathlib import Path
from typing import Any

import yaml

from tests._fake_builder_guest import CHECKOUT

WORKFLOW = CHECKOUT / ".github" / "workflows" / "weekly-release.yml"
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def load_workflow(path: Path) -> dict[Any, Any]:
    assert path.is_file(), f"{path} is missing"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} is not a mapping"
    return data


def triggers_of(workflow: dict[Any, Any]) -> dict[str, Any]:
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {str(t): None for t in on}
    assert isinstance(on, dict), f"no triggers: {on!r}"
    return {str(k): v for k, v in on.items()}


def jobs_of(workflow: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"no jobs: {jobs!r}"
    return jobs


def steps_of(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, f"no steps: {job!r}"
    return steps


def the_job() -> dict[str, Any]:
    jobs = jobs_of(load_workflow(WORKFLOW))
    assert len(jobs) == 1, f"one job expected: {sorted(jobs)}"
    return next(iter(jobs.values()))


def index_of(steps: list[dict[str, Any]], *words: str) -> int:
    for i, step in enumerate(steps):
        run = " ".join(str(step.get("run", "")).split())
        if all(w in run for w in words):
            return i
    raise AssertionError(f"no step runs {words}: {steps}")


def test_weekly_release_is_triggered_by_workflow_dispatch_only() -> None:
    assert sorted(triggers_of(load_workflow(WORKFLOW))) == ["workflow_dispatch"]


def test_weekly_release_grants_no_token_permission_at_the_top() -> None:
    workflow = load_workflow(WORKFLOW)
    assert "permissions" in workflow and workflow["permissions"] == {}, workflow.get("permissions")


def test_weekly_release_job_runs_on_the_builder_runner_with_a_timeout() -> None:
    job = the_job()
    assert job.get("runs-on") == ["self-hosted", "bentoo-builder"], job.get("runs-on")
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int) and timeout > 0, timeout


def test_weekly_release_job_checks_out_without_credentials_and_pins_every_action() -> None:
    steps = steps_of(the_job())
    uses = [str(s["uses"]) for s in steps if "uses" in s]
    assert uses and all(PINNED_ACTION.match(u) for u in uses), uses
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1, steps
    assert checkouts[0].get("with", {}).get("persist-credentials") in (False, "false")


def test_weekly_release_job_releases_v3_systemd_unsigned_into_the_out_share() -> None:
    steps = steps_of(the_job())
    checkout = next(
        i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout@")
    )
    synced = index_of(steps, "uv sync", "--frozen")
    released = index_of(
        steps,
        "release v3 systemd",
        "--unsigned",
        "--images minimal,kde,gnome",
        "/mnt/out/releases",
    )
    assert checkout < synced < released, steps
    run = str(steps[released]["run"])
    assert "--key" not in run, "the weekly job must never sign"


def test_weekly_release_job_appends_the_report_to_the_summary_even_on_failure() -> None:
    steps = steps_of(the_job())
    released = index_of(steps, "release v3 systemd", "--unsigned")
    reported = index_of(steps, "release report", "--markdown", "GITHUB_STEP_SUMMARY")
    assert released < reported, steps
    assert "always()" in str(steps[reported].get("if", "")), steps[reported]


def test_weekly_release_expands_no_github_event_value_inside_run() -> None:
    for step in steps_of(the_job()):
        assert "github.event" not in str(step.get("run", "")), step
