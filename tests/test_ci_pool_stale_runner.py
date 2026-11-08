"""A runner left registered by a failed attempt does not block the job (story 034, 6.1).

Found in the host proof on 2026-11-08: the first attempt's VM died in the prepare boot,
its JIT runner ``bentoo-worker-<job>`` stayed registered (offline), and every later mint
of that name was GitHub's 409 "Already exists" -- the queued job was never served again.

Contract: a round mints only when the worker is idle, so a runner already holding the
name is a dead attempt's (R6.10). On a 409 the round lists the repository's runners by
that name (``GET .../actions/runners?name=``), deletes each one that is ``offline`` and
not ``busy`` (``DELETE .../actions/runners/<id>``) with a WARN ``ci_runner_stale``, then
mints once more. A runner that is online or busy is never deleted; the round is then
``mint-failed``, as it is when the delete or the second mint fails.
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import ci_pool
from tests._fake_ci_pool import FakeGitHub, FakeJob, FakePoolWorker, pool_events, write_config

REPO = "obentoo/private-app"
JOB = 9101
NAME = f"bentoo-worker-{JOB}"


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return ci_pool.load_config(write_config(tmp_path / "cfg"))


def _stale(github: FakeGitHub, *, status: str = "offline", busy: bool = False) -> None:
    github.repo(REPO, FakeJob(JOB)).runners.append(
        {"id": 77, "name": NAME, "status": status, "busy": busy}
    )


def _deletes(github: FakeGitHub) -> list[str]:
    return [c.path for c in github.calls if c.method == "DELETE"]


@pytest.mark.parametrize(("status", "busy"), [("online", False), ("offline", True)])
def test_a_live_runner_holding_the_name_is_never_deleted(cfg: Any, status: str, busy: bool) -> None:
    github, workers = FakeGitHub(), FakePoolWorker()
    _stale(github, status=status, busy=busy)
    result = ci_pool.round(cfg, ci_pool.PoolState(), github=github, worker=workers)
    assert result.outcome == "mint-failed"
    assert _deletes(github) == []
    assert workers.dispatched == []


def test_a_failed_delete_ends_the_round_mint_failed(cfg: Any) -> None:
    github, workers = FakeGitHub(), FakePoolWorker()
    _stale(github)
    github.failures[("DELETE", "/actions/runners/77")] = 500
    result = ci_pool.round(cfg, ci_pool.PoolState(), github=github, worker=workers)
    assert result.outcome == "mint-failed"
    assert workers.dispatched == []


def test_the_mint_is_retried_once_only(cfg: Any) -> None:
    github, workers = FakeGitHub(), FakePoolWorker()
    _stale(github, status="online")
    ci_pool.round(cfg, ci_pool.PoolState(), github=github, worker=workers)
    assert len(github.mints()) <= 2


def test_a_dead_attempts_runner_is_deleted_and_the_job_served(
    cfg: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    github, workers = FakeGitHub(), FakePoolWorker()
    _stale(github)
    result = ci_pool.round(cfg, ci_pool.PoolState(), github=github, worker=workers)
    assert result == ci_pool.RoundResult("dispatched", str(JOB))
    assert _deletes(github) == [f"/repos/{REPO}/actions/runners/77"]
    listing = [c for c in github.calls if c.method == "GET" and c.path.endswith("/runners")]
    assert [c.query.get("name") for c in listing] == [[NAME]]
    assert [d.job for d in workers.dispatched] == [str(JOB)]
    stale = [
        e for e in pool_events(capsys.readouterr().err, []) if e.get("event") == "ci_runner_stale"
    ]
    assert stale and stale[0]["level"] == "WARN" and stale[0]["runner"] == 77
