"""Regression (review of 2026-10-08): a job's binhost is its commit's generation.

A job runs the commit it ships, with that commit's ``seeds/``. The push and the pull
used to work the generation out of the HOST checkout's pins instead, so a pin bump
committed (or left dirty) while a job ran made the pull look in a directory the job
never wrote: it reported "built no binpkg", released the lock and left the job's
binpkgs on the worker. The generation now travels in the owner lock.
"""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import ownership, worker
from tests._fake_worker import FakeWorker, seed_host_cache

_STAGE3 = """snapshot = "{snapshot}"
base_url = "https://distfiles.gentoo.org/releases/amd64/autobuilds"

[systemd]
filename = "stage3-amd64-systemd-{snapshot}.tar.xz"
sha512 = "{sha}"
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_pin(repo: Path, snapshot: str) -> str:
    seeds = repo / "seeds"
    seeds.mkdir(exist_ok=True)
    (seeds / "stage3.toml").write_text(_STAGE3.format(snapshot=snapshot, sha="0" * 128))
    _git(repo, "add", "seeds/stage3.toml")
    _git(repo, "commit", "-q", "-m", f"pin {snapshot}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    return r


def test_generation_at_reads_the_commits_pin_not_the_working_tree(repo: Path) -> None:
    old = _commit_pin(repo, "20260823T153057Z")
    _commit_pin(repo, "20261001T000000Z")
    # and an uncommitted bump on top: what --allow-dirty would leave behind
    (repo / "seeds" / "stage3.toml").write_text(
        _STAGE3.format(snapshot="20261005T000000Z", sha="0" * 128)
    )
    assert worker.generation_at(repo, old, "systemd") == "20260823T153057Z"
    assert worker.generation_at(repo, "HEAD", "systemd") == "20261001T000000Z"


def test_a_lock_written_before_the_generation_was_recorded_still_loads() -> None:
    owner = ownership.Owner.model_validate(
        {
            "arch": "v3",
            "worker": "w1",
            "job": "kde",
            "commit": "c" * 40,
            "since": "2026-10-05T14:02:31Z",
            "host_pid": None,
        }
    )
    assert owner.generation == ""


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    yield w
    w.close()


def test_the_pull_brings_the_jobs_generation_after_the_host_moved_on(
    fw: FakeWorker, tmp_path: Path
) -> None:
    seed_host_cache(fw)
    host_gen = str(fw.config()["generation"])
    job_gen = "20250101T000000Z"
    assert job_gen != host_gen  # the host checkout pins another generation by now
    fw.put(f"cache/binpkgs/v3/{job_gen}/app-misc/built-1.gpkg.tar", "binpkg\n")
    owner = ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker=fw.name,
            job="kde",
            commit="c" * 40,
            since="2026-10-05T14:02:31Z",
            host_pid=None,
            generation=job_gen,
        ),
    )

    got = worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results", owner=owner)

    assert got.binhost, got.binhost_reason
    pulled = fw.host_cache / "binpkgs" / "v3" / job_gen / "app-misc" / "built-1.gpkg.tar"
    assert pulled.is_file()
    assert not (
        fw.host_cache / "binpkgs" / "v3" / host_gen / "app-misc" / "built-1.gpkg.tar"
    ).exists()


def test_a_commit_without_seeds_falls_back_to_the_host_pin(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "README").write_text("no seeds here\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "no seeds")
    monkeypatch.setattr(worker, "generation", lambda init: f"host-{init}")
    assert worker.generation_at(repo, "HEAD", "systemd") == "host-systemd"
