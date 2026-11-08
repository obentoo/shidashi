"""One round of the host's CI pool (story 034, task 4.3; R5.4, R6.1-R6.7, R6.10, R9.1, Q4,
Q9).

Contract (design.md, "shidashi/ci_pool.py + shidashi ci pool"; Error Handling Strategy):
``round(cfg, state, *, github, worker) -> RoundResult`` with ``state = PoolState()``
(``served``, ``auth_failures``) updated in place; ``github`` is ``ApiRequest -> JSON``
(``FakeGitHub``), ``worker`` has ``busy()``, ``results()`` and
``dispatch(job, repo, jitconfig)`` (``FakePoolWorker``, both in tests/_fake_ci_pool.py):

- a busy worker (a ``shidashi-ci-*`` unit active) -> nothing this round (R6.4); a worker
  that cannot be asked -> a WARN, nothing minted (R6.5);
- an idle worker: its results read first -- rc 1/3/4 is a WARN ``ci_job_failed`` naming
  the job and why --, then every served job forgotten: no runner minted before is alive,
  so a job still queued is minted again (R6.10);
- the oldest queued job of an allowlisted, PRIVATE repository carrying ``self-hosted`` and
  the label (any case) gets one JIT runner ``bentoo-worker-<job id>`` and one dispatch per
  round (R5.4, R6.1, R6.2); an unreachable worker leaves it unserved (R6.5); a dispatch
  error is one ERROR ``ci_dispatch`` event, the job unserved and the round over;
- every GitHub error is one ERROR event with endpoint and status (R6.6); 401/403 rounds
  are counted in ``state.auth_failures``, any other answer resets it (R6.7);
- ``ci_mint`` and ``ci_dispatch`` events carry ``job``, ``repo``, ``duration_ms``,
  ``result`` and never the JIT config (R9.1).
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests._fake_ci_pool import (
    DISPATCH_ERROR,
    LABEL,
    FakeGitHub,
    FakeJob,
    FakePoolWorker,
    level_of,
    pool_events,
    write_config,
)
from tests._pending import try_import

load_config: Any = try_import("shidashi.ci_pool", "load_config")
pool_round: Any = try_import("shidashi.ci_pool", "round")
PoolState: Any = try_import("shidashi.ci_pool", "PoolState")
WorkerUnreachable: Any = try_import("shidashi.ci_pool", "WorkerUnreachable")

REPO = "obentoo/private-app"
OTHER = "obentoo/private-lib"


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return load_config(write_config(tmp_path / "cfg"))


def round_once(cfg: Any, state: Any, github: FakeGitHub, worker_: FakePoolWorker) -> Any:
    return pool_round(cfg, state, github=github, worker=worker_)


def served(state: Any) -> set[str]:
    return {str(j) for j in state.served}


def test_round_mints_one_runner_for_a_labelled_queued_job_and_hands_it_to_the_worker(
    cfg: Any,
) -> None:
    """R6.1."""
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9001))
    round_once(cfg, state, github, workers)
    mints = github.mints()
    assert len(mints) == 1, github.calls
    assert mints[0].path == f"/repos/{REPO}/actions/runners/generate-jitconfig"
    assert mints[0].body["name"] == "bentoo-worker-9001"
    assert {"self-hosted", LABEL} <= set(mints[0].body["labels"])
    assert [(d.job, d.repo, d.jitconfig) for d in workers.dispatched] == [
        ("9001", REPO, github.minted[0])
    ]
    assert served(state) == {"9001"}


def test_round_a_queued_job_of_a_run_already_in_progress_is_served(cfg: Any) -> None:
    """Hostile split of R6.1: a workflow's SECOND job is queued while its run is
    ``in_progress`` (the first job ran); ``?status=queued`` on runs does not list that run,
    yet the job is a queued job of the repository."""
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9002), run_status="in_progress")
    round_once(cfg, state, github, workers)
    assert [d.job for d in workers.dispatched] == ["9002"], github.calls


@pytest.mark.parametrize(
    "labels",
    [
        # hostile collapse: near the label, or one of the two missing
        pytest.param(["self-hosted", "bentoo-worker-2"], id="longer-label"),
        pytest.param(["self-hosted", "bentoo"], id="shorter-label"),
        pytest.param(["self-hosted"], id="no-label"),
        pytest.param([LABEL], id="no-self-hosted"),
        pytest.param(["self-hosted", "linux", "x64"], id="generic"),
        pytest.param(["ubuntu-latest"], id="hosted"),
    ],
)
def test_round_a_job_without_both_labels_is_not_served(cfg: Any, labels: list[str]) -> None:
    """R6.2."""
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9003, labels=labels))
    round_once(cfg, state, github, workers)
    assert any(c.path.endswith("/jobs") for c in github.calls), "the jobs were never listed"
    assert github.mints() == [] and workers.dispatched == []


def test_round_labels_match_whatever_their_case_as_on_github(cfg: Any) -> None:
    """Hostile split of R6.2: GitHub matches runner labels case-insensitively, so a
    workflow's ``runs-on: [Self-Hosted, Bentoo-Worker]`` reaches this runner."""
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9004, labels=["Self-Hosted", "Bentoo-Worker"]))
    round_once(cfg, state, github, workers)
    assert [d.job for d in workers.dispatched] == ["9004"]


@pytest.mark.parametrize("status", ["in_progress", "completed", "waiting"])
def test_round_a_job_that_is_not_queued_is_not_served(cfg: Any, status: str) -> None:
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9005, status=status))
    round_once(cfg, state, github, workers)
    assert any(c.path.endswith("/actions/runs") for c in github.calls), "no listing"
    assert github.mints() == [] and workers.dispatched == []


def test_round_does_not_mint_twice_while_the_runner_minted_for_the_job_may_live(
    cfg: Any,
) -> None:
    """R6.3: the job stays queued until the guest's runner picks it up -- and while the
    dispatched unit runs, the runner minted for it may still take it."""
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9006))
    round_once(cfg, state, github, workers)
    assert workers.is_busy, "the dispatch did not start the job's unit"
    round_once(cfg, state, github, workers)
    assert len(github.mints()) == 1
    assert [d.job for d in workers.dispatched] == ["9006"]


def test_round_an_idle_worker_forgets_every_served_job_so_a_still_queued_one_is_minted_again(
    cfg: Any,
) -> None:
    """R6.10, hostile split of R6.3: with at most one VM per worker, an idle worker means
    every runner minted before is gone -- its VM ended, failed, or its runner took another
    queued job. A job still queued is minted again, under the same runner name."""
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9030))
    round_once(cfg, state, github, workers)
    assert served(state) == {"9030"}
    workers.is_busy = False  # the VM ended, the job is still queued
    round_once(cfg, state, github, workers)
    # GitHub keeps the first attempt's runner registered: the second mint meets its 409,
    # the dead runner is unregistered and the name minted again (host proof 6.1)
    mints = github.mints()
    assert len(github.minted) == 2, github.calls
    assert {m.body["name"] for m in mints} == {"bentoo-worker-9030"}
    assert [d.job for d in workers.dispatched] == ["9030", "9030"]
    assert workers.dispatched[1].jitconfig == github.minted[1] != github.minted[0]


def test_round_forgets_a_served_job_once_it_leaves_the_queue(cfg: Any) -> None:
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    repo = github.repo(REPO, FakeJob(9007))
    round_once(cfg, state, github, workers)
    assert served(state) == {"9007"}
    repo.runs[0].jobs[0].status = "in_progress"
    repo.runs[0].status = "in_progress"
    workers.is_busy = False
    round_once(cfg, state, github, workers)
    assert served(state) == set()


def test_round_skips_while_the_worker_runs_a_ci_vm(cfg: Any) -> None:
    """R6.4."""
    github, workers, state = FakeGitHub(), FakePoolWorker(is_busy=True), PoolState()
    github.repo(REPO, FakeJob(9008))
    round_once(cfg, state, github, workers)
    assert workers.asked_busy >= 1
    assert github.mints() == [] and workers.dispatched == []


@pytest.mark.parametrize(
    "private", [pytest.param(False, id="public"), pytest.param(None, id="unknown")]
)
def test_round_refuses_a_repository_that_is_not_private_with_a_warn_event(
    cfg: Any,
    private: bool | None,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R5.4: never a runner for it, a WARN event naming it -- and the private repository
    next to it in the allowlist is still served."""
    caplog.set_level(logging.DEBUG)
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9009), private=private)
    github.repo(OTHER, FakeJob(9010))
    round_once(cfg, state, github, workers)
    assert all(not m.path.startswith(f"/repos/{REPO}/") for m in github.mints())
    assert [(d.job, d.repo) for d in workers.dispatched] == [("9010", OTHER)]
    seen = pool_events(capfd.readouterr().err, caplog.records)
    warns = [e for e in seen if level_of(e) == "WARNING" and REPO in json.dumps(e)]
    assert warns, seen


def test_round_an_unreachable_worker_warns_and_the_job_is_retried_next_round(
    cfg: Any, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """R6.5: the minted config is dropped, the job not marked served."""
    caplog.set_level(logging.DEBUG)
    github, state = FakeGitHub(), PoolState()
    github.repo(REPO, FakeJob(9011))
    gone = FakePoolWorker()
    original = gone.dispatch

    def unreachable_dispatch(job: Any, repo: str, jitconfig: str) -> None:
        raise WorkerUnreachable("bentoo-lab (192.0.2.10) is unreachable")

    gone.dispatch = unreachable_dispatch  # type: ignore[method-assign]
    round_once(cfg, state, github, gone)
    assert served(state) == set()
    seen = pool_events(capfd.readouterr().err, caplog.records)
    assert any(level_of(e) == "WARNING" for e in seen), seen
    gone.dispatch = original  # type: ignore[method-assign]
    round_once(cfg, state, github, gone)
    assert [d.job for d in gone.dispatched] == ["9011"]
    assert gone.dispatched[0].jitconfig == github.minted[-1]


@pytest.mark.parametrize(
    "status",
    [pytest.param(500, id="500"), pytest.param(502, id="502"), pytest.param(None, id="timeout")],
)
def test_round_a_github_error_is_one_error_event_and_the_next_round_retries(
    cfg: Any,
    status: int | None,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R6.6: endpoint and status in ONE ERROR event; no exception escapes the round."""
    caplog.set_level(logging.DEBUG)
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9012))
    github.failures[("GET", "/actions/runs")] = status
    round_once(cfg, state, github, workers)
    seen = pool_events(capfd.readouterr().err, caplog.records)
    errors = [e for e in seen if level_of(e) == "ERROR"]
    assert len(errors) >= 1, seen
    assert any("/actions/runs" in json.dumps(e) for e in errors), errors
    if status is not None:
        assert any(e.get("status") == status for e in errors), errors
    github.failures.clear()
    round_once(cfg, state, github, workers)
    assert [d.job for d in workers.dispatched] == ["9012"]


def test_round_counts_consecutive_auth_failures_and_any_other_answer_resets_them(
    cfg: Any,
) -> None:
    """R6.7: 401 and 403 count; a round GitHub answers resets the count."""
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO)
    counts = []
    for status in (401, 403, None, 401):
        github.failures.clear()
        if status is not None:
            github.failures[("GET", "/actions/runs")] = status
        round_once(cfg, state, github, workers)
        counts.append(state.auth_failures)
    assert counts == [1, 2, 0, 1]


def test_round_hands_over_at_most_one_job_the_oldest_first(cfg: Any) -> None:
    """One VM per worker: one dispatch per round, the oldest queued job first."""
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(
        REPO,
        FakeJob(9014, created_at="2026-11-03T10:05:00Z"),
        FakeJob(9013, created_at="2026-11-03T10:00:00Z"),
    )
    round_once(cfg, state, github, workers)
    assert [d.job for d in workers.dispatched] == ["9013"]
    assert len(github.mints()) == 1


def test_round_events_carry_job_repo_duration_and_result_never_the_jit_secret(
    cfg: Any, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """R9.1, R5.2, Q9: the mint and the dispatch are events; the JIT config is in none."""
    caplog.set_level(logging.DEBUG)
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO, FakeJob(9015))
    round_once(cfg, state, github, workers)
    err = capfd.readouterr().err
    seen = pool_events(err, caplog.records)
    about = [e for e in seen if str(e.get("job")) == "9015" and e.get("repo") == REPO]
    assert {"ci_mint", "ci_dispatch"} <= {str(e.get("event")) for e in about}, seen
    for event in [e for e in about if e.get("event") in ("ci_mint", "ci_dispatch")]:
        assert isinstance(event.get("duration_ms"), int) and event.get("result"), event
    assert github.minted and github.minted[0] not in err + caplog.text


def test_round_an_unreachable_worker_on_the_busy_probe_warns_and_mints_nothing(
    cfg: Any, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """R6.5: a worker that cannot say whether it is busy gets no runner -- a minted JIT
    config would only expire unused."""
    caplog.set_level(logging.DEBUG)
    github, workers, state = FakeGitHub(), FakePoolWorker(unreachable=True), PoolState()
    github.repo(REPO, FakeJob(9031))
    round_once(cfg, state, github, workers)
    assert workers.asked_busy >= 1
    assert github.mints() == [] and workers.dispatched == []
    seen = pool_events(capfd.readouterr().err, caplog.records)
    assert any(level_of(e) == "WARNING" for e in seen), seen
    workers.unreachable = False
    round_once(cfg, state, github, workers)
    assert [d.job for d in workers.dispatched] == ["9031"]


def test_round_a_dispatch_error_is_one_error_event_the_job_stays_unserved_and_the_round_ends(
    cfg: Any, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The worker answered and refused: one ERROR ``ci_dispatch`` event with its stderr
    tail; no second runner minted that round; the job retried next round."""
    caplog.set_level(logging.DEBUG)
    github, workers, state = FakeGitHub(), FakePoolWorker(dispatch_fails=True), PoolState()
    github.repo(REPO, FakeJob(9032, created_at="2026-11-03T10:00:00Z"))
    github.repo(OTHER, FakeJob(9033, created_at="2026-11-03T10:05:00Z"))
    round_once(cfg, state, github, workers)
    assert len(github.mints()) == 1, "the round went on after a failed dispatch"
    assert served(state) == set()
    seen = pool_events(capfd.readouterr().err, caplog.records)
    errors = [e for e in seen if e.get("event") == "ci_dispatch" and level_of(e) == "ERROR"]
    assert len(errors) == 1, seen
    assert str(errors[0].get("job")) == "9032" and errors[0].get("repo") == REPO
    assert DISPATCH_ERROR in json.dumps(errors[0]), errors[0]
    assert github.minted[0] not in json.dumps(seen)
    workers.dispatch_fails = False
    round_once(cfg, state, github, workers)
    assert [d.job for d in workers.dispatched] == ["9032"]


@pytest.mark.parametrize(
    ("rc", "reason"),
    [
        pytest.param(1, "fail", id="failed"),
        pytest.param(3, "disk", id="low-disk"),
        pytest.param(4, "time", id="timeout"),
    ],
)
def test_round_an_idle_worker_reports_a_failed_job_from_its_results(
    cfg: Any,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    rc: int,
    reason: str,
) -> None:
    """The worker's ``results/<job>.json`` (``run_job``): rc 1/3/4 is one WARN
    ``ci_job_failed`` naming the job and why -- the job stays queued on GitHub otherwise
    unexplained. Read once."""
    caplog.set_level(logging.DEBUG)
    github, workers, state = FakeGitHub(), FakePoolWorker(), PoolState()
    github.repo(REPO)
    done = {"job": "9034", "repo": REPO, "rc": rc, "ended": "2026-11-03T10:30:00Z"}
    workers.waiting_results = [done, {**done, "job": "9035", "rc": 0}]
    round_once(cfg, state, github, workers)
    assert workers.asked_results >= 1, "the idle worker's results were never read"
    seen = pool_events(capfd.readouterr().err, caplog.records)
    failed = [e for e in seen if e.get("event") == "ci_job_failed"]
    assert len(failed) == 1, seen
    assert level_of(failed[0]) == "WARNING", failed
    assert str(failed[0].get("job")) == "9034", failed
    assert reason in json.dumps(failed[0]).lower(), failed
    caplog.clear()
    round_once(cfg, state, github, workers)
    again = pool_events(capfd.readouterr().err, caplog.records)
    assert [e for e in again if e.get("event") == "ci_job_failed"] == [], "reported twice"
