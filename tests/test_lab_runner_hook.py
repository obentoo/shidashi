"""``lab/vm/runner-hook.sh`` -- the job-started hook refuses every job but the weekly
dispatch on main (story 032, task 7.2; R6.4, R8.2).

Contract (story.md R6.4; design.md "lab/vm/builder.sh weekly + runner-hook.sh"): run as
``ACTIONS_RUNNER_HOOK_JOB_STARTED``, before any step of the job, the hook exits 0 only when
``GITHUB_REPOSITORY`` is ``obentoo/shidashi``, ``GITHUB_EVENT_NAME`` is
``workflow_dispatch``, ``GITHUB_REF`` is ``refs/heads/main`` and ``GITHUB_WORKFLOW_REF`` is
``obentoo/shidashi/.github/workflows/weekly-release.yml@refs/heads/main``; any other value,
or a missing one, exits 1 and names the variable that refused it.

Each value is compared whole: the hostile cases below are the near misses a prefix, a
pattern or an unquoted shell comparison would let through. Runs the real script under
``bash`` with a crafted environment (no bats).
"""

import os
import subprocess
from pathlib import Path

import pytest

from tests._fake_builder_guest import CHECKOUT, STANDARD_PATH

HOOK = CHECKOUT / "lab" / "vm" / "runner-hook.sh"
WORKFLOW_REF = "obentoo/shidashi/.github/workflows/weekly-release.yml@refs/heads/main"
ALLOWED = {
    "GITHUB_REPOSITORY": "obentoo/shidashi",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
}
#: What else a runner puts in a job's environment: none of it may change the verdict.
RUNNER_NOISE = {
    "GITHUB_ACTOR": "lucascs",
    "GITHUB_RUN_ID": "1234567890",
    "GITHUB_SHA": "f" * 40,
    "GITHUB_WORKFLOW": "weekly-release",
    "GITHUB_HEAD_REF": "",
    "GITHUB_BASE_REF": "",
    "RUNNER_NAME": "builder-20261101T010003Z",
    "RUNNER_OS": "Linux",
}


def run_hook(env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    assert HOOK.is_file(), f"{HOOK} is missing"
    return subprocess.run(
        ["bash", str(HOOK)],
        env={"PATH": os.environ.get("PATH", STANDARD_PATH), **env},
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_runner_hook_lets_the_weekly_dispatch_on_main_run(tmp_path: Path) -> None:
    done = run_hook(ALLOWED, cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_runner_hook_ignores_the_rest_of_the_job_environment(tmp_path: Path) -> None:
    """Converse half: the allowed job with everything else a runner sets still runs."""
    done = run_hook({**RUNNER_NOISE, **ALLOWED}, cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


HOOK_REFUSED = [
    ("GITHUB_REPOSITORY", "obentoo/shidashi-fork"),
    ("GITHUB_REPOSITORY", "attacker/shidashi"),
    ("GITHUB_REPOSITORY", "obentoo/shidashi2"),
    ("GITHUB_REPOSITORY", "obentoo/shidashi "),
    ("GITHUB_REPOSITORY", ""),
    ("GITHUB_EVENT_NAME", "pull_request"),
    ("GITHUB_EVENT_NAME", "pull_request_target"),
    ("GITHUB_EVENT_NAME", "push"),
    ("GITHUB_EVENT_NAME", "schedule"),
    ("GITHUB_EVENT_NAME", "workflow_run"),
    ("GITHUB_EVENT_NAME", "workflow_dispatch_x"),
    ("GITHUB_EVENT_NAME", "workflow_dispatch extra"),
    ("GITHUB_REF", "refs/heads/main-evil"),
    ("GITHUB_REF", "refs/heads/mainx"),
    ("GITHUB_REF", "refs/tags/main"),
    ("GITHUB_REF", "refs/pull/7/merge"),
    ("GITHUB_REF", "refs/heads/feature/refs/heads/main"),
    ("GITHUB_REF", "main"),
    ("GITHUB_WORKFLOW_REF", WORKFLOW_REF + "-evil"),
    ("GITHUB_WORKFLOW_REF", WORKFLOW_REF.replace("@refs/heads/main", "@refs/heads/dev")),
    ("GITHUB_WORKFLOW_REF", WORKFLOW_REF.replace("weekly-release.yml", "release.yml")),
    ("GITHUB_WORKFLOW_REF", WORKFLOW_REF.replace("weekly-release.yml", "weekly-release.yaml")),
    ("GITHUB_WORKFLOW_REF", WORKFLOW_REF.replace("obentoo/shidashi/", "attacker/shidashi/")),
    ("GITHUB_WORKFLOW_REF", "x" + WORKFLOW_REF),
    ("GITHUB_WORKFLOW_REF", ""),
]


@pytest.mark.parametrize(
    ("name", "value"), [pytest.param(n, v, id=f"{n}={v!r}") for n, v in HOOK_REFUSED]
)
def test_runner_hook_refuses_a_job_that_differs_in_one_value_naming_it(
    tmp_path: Path, name: str, value: str
) -> None:
    done = run_hook({**RUNNER_NOISE, **ALLOWED, name: value}, cwd=tmp_path)
    assert done.returncode == 1, done.stdout + done.stderr
    assert name in done.stdout + done.stderr


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_runner_hook_refuses_a_job_with_a_value_missing_naming_it(
    tmp_path: Path, name: str
) -> None:
    env = {k: v for k, v in ALLOWED.items() if k != name}
    done = run_hook(env, cwd=tmp_path)
    assert done.returncode == 1, done.stdout + done.stderr
    assert name in done.stdout + done.stderr
