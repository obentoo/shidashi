"""Defects the tech review of group 4 found in the host pool (story 034, 4.2-4.4).

Source: .draft/reports/4.x-tech-review-python-github-ssh.md. Each test pins one finding:

1. the runs listing must be the two filtered ones (``status=queued``, ``status=in_progress``):
   the unfiltered newest-100 listing passes ``send``'s 1 MiB cap once a repository has
   ~85 runs, and never sees a queued job in an older run;
2. a job is served only when the minted runner carries every one of its labels -- GitHub
   never gives ``[self-hosted, bentoo-worker, gpu]`` to a runner without ``gpu``, and the
   VM would idle until its timeout holding the one CI slot;
3. the tree is shipped on every dispatch: ``put_tree`` unpacks in place, so a ship cut
   short leaves a ``src/<commit>`` a presence test would take for whole;
4. a ``ci.toml`` or state file that is not UTF-8 is refused or set aside, never a crash.
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import ci_pool, ci_pool_worker, remote, worker
from tests._fake_ci_pool import LABEL, FakeGitHub, FakeJob, FakePoolWorker, write_config

REPO = "obentoo/private-app"


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return ci_pool.load_config(write_config(tmp_path / "cfg"))


def test_runs_are_listed_filtered_by_queued_and_in_progress(cfg: Any) -> None:
    github, state = FakeGitHub(), ci_pool.PoolState()
    github.repo(REPO, FakeJob(9001))
    ci_pool.round(cfg, state, github=github, worker=FakePoolWorker())
    listings = [c for c in github.calls if c.path == f"/repos/{REPO}/actions/runs"]
    assert sorted(c.query.get("status", [""])[0] for c in listings) == ["in_progress", "queued"]


@pytest.mark.parametrize(
    "labels",
    [
        ["self-hosted", LABEL, "gpu"],
        ["self-hosted", LABEL, "arm64"],
        ["self-hosted", LABEL, "windows"],
    ],
)
def test_a_job_wanting_a_label_the_runner_lacks_is_not_served(cfg: Any, labels: list[str]) -> None:
    github, workers, state = FakeGitHub(), FakePoolWorker(), ci_pool.PoolState()
    github.repo(REPO, FakeJob(9002, labels=labels))
    ci_pool.round(cfg, state, github=github, worker=workers)
    assert github.mints() == []
    assert workers.dispatched == []


def test_a_job_naming_the_runners_own_labels_is_served(cfg: Any) -> None:
    github, workers, state = FakeGitHub(), FakePoolWorker(), ci_pool.PoolState()
    github.repo(REPO, FakeJob(9003, labels=["self-hosted", "Linux", "X64", LABEL]))
    ci_pool.round(cfg, state, github=github, worker=workers)
    assert [d.job for d in workers.dispatched] == ["9003"]


def test_dispatch_ships_the_tree_even_when_its_directory_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped: list[str] = []
    monkeypatch.setattr(worker, "_push_runtime", lambda *_a, **_k: 0)
    monkeypatch.setattr(remote, "put_tree", lambda _r, commit, _dest, **_k: shipped.append(commit))
    monkeypatch.setattr(ci_pool_worker, "source_commit", lambda _source: "a" * 40)

    def run(_remote: Any, command: str, **_kw: Any) -> remote.RemoteResult:
        return remote.RemoteResult(command, exit_code=0, stdout="", stderr="", duration_s=0.0)

    monkeypatch.setattr(remote, "run", run)
    target = remote.Remote(
        name="w", address="192.0.2.1", key=tmp_path / "k", known_hosts=tmp_path / "kh"
    )
    ssh = ci_pool_worker.SshWorker(target, tmp_path, ci_pool.PoolVm())
    ssh.dispatch("9001", REPO, "jit")
    assert shipped == ["a" * 40]


def test_a_state_file_that_is_not_utf8_is_set_aside(tmp_path: Path) -> None:
    path = tmp_path / "ci-pool.json"
    path.write_bytes(b"\xff\xfe not json")
    state = ci_pool.load_state(path, now=lambda: 7.0)
    assert state.served == set() and state.auth_failures == 0
    assert not path.exists()
    assert (tmp_path / "ci-pool.json.corrupt-7").is_file()


def test_a_config_that_is_not_utf8_is_refused_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "ci.toml"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ci_pool.PoolConfigError) as refused:
        ci_pool.load_config(path)
    assert str(path) in str(refused.value)
