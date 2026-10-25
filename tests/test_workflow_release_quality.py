"""``.github/workflows/release.yml`` trimmed to ``quality`` (story 032, task 8.2; R7.4,
R7.5, R8.2, Q10).

Contract (story.md R7.4, R7.5; design.md ".github/workflows/weekly-release.yml (new) and
release.yml (trimmed)"): the gated ``weekly-release`` scaffold and the ``schedule`` trigger
are gone; the ``quality`` job stays -- its gates unchanged -- with a ``timeout-minutes``,
under a top-level ``permissions: contents: read``; every action pinned by a commit SHA, the
checkout without credentials, no ``github.event`` value inside a ``run:``. And no job a pull
request can trigger runs on a self-hosted runner: fork code must never reach the builder
(R8.2), in any workflow file.
"""

from typing import Any

from tests._fake_builder_guest import CHECKOUT
from tests.test_workflow_weekly_release import (
    PINNED_ACTION,
    jobs_of,
    load_workflow,
    steps_of,
    triggers_of,
)

WORKFLOWS = CHECKOUT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
QUALITY_GATES = (
    "uv sync --frozen --extra dev",
    "uv run --frozen ruff check .",
    "uv run --frozen ruff format --check .",
    "uv run --frozen mypy .",
    "uv run --frozen pytest",
)
FORK_TRIGGERS = {"pull_request", "pull_request_target", "push", "workflow_run", "issue_comment"}


def test_release_workflow_has_no_schedule_and_no_weekly_release_scaffold() -> None:
    workflow = load_workflow(RELEASE)
    assert "schedule" not in triggers_of(workflow)
    assert sorted(jobs_of(workflow)) == ["quality"]


def test_release_workflow_keeps_the_quality_gates_with_a_timeout() -> None:
    quality = jobs_of(load_workflow(RELEASE))["quality"]
    runs = [" ".join(str(s.get("run", "")).split()) for s in steps_of(quality)]
    for gate in QUALITY_GATES:
        assert gate in runs, f"the quality job lost {gate!r}: {runs}"
    timeout = quality.get("timeout-minutes")
    assert isinstance(timeout, int) and timeout > 0, timeout
    assert "pull_request" in triggers_of(load_workflow(RELEASE))


def test_release_workflow_reads_the_repository_only_at_the_top() -> None:
    workflow = load_workflow(RELEASE)
    assert workflow.get("permissions") == {"contents": "read"}, workflow.get("permissions")


def test_release_workflow_pins_every_action_and_checks_out_without_credentials() -> None:
    for name, job in jobs_of(load_workflow(RELEASE)).items():
        steps = steps_of(job)
        uses = [str(s["uses"]) for s in steps if "uses" in s]
        assert all(PINNED_ACTION.match(u) for u in uses), (name, uses)
        for step in steps:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") in (False, "false")
            assert "github.event" not in str(step.get("run", "")), step


def runs_on_of(job: dict[str, Any]) -> list[str]:
    target = job.get("runs-on", [])
    if isinstance(target, str):
        return [target]
    if isinstance(target, dict):
        return [str(v) for v in target.values()]
    return [str(t) for t in target]


def test_no_job_a_pull_request_can_trigger_runs_on_a_self_hosted_runner() -> None:
    """Hostile half of R8.2: the builder runner's label in any PR-triggered workflow is
    fork code on the builder."""
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert files
    for path in files:
        workflow = load_workflow(path)
        if not set(triggers_of(workflow)) & FORK_TRIGGERS:
            continue
        for name, job in jobs_of(workflow).items():
            labels = runs_on_of(job)
            assert "self-hosted" not in labels and "bentoo-builder" not in labels, (path, name)
