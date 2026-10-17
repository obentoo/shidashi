"""Story 028: the pull of an owned arch's job brings back its results, not its cache.

Real rsync through the fake ssh of tests/_fake_worker.py, as in test_worker_sync.py.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import audit, config, ownership, worker
from tests._fake_worker import FakeWorker, fork_point_names, seed_host_cache

FACTORY = ["factory", "v3", "kde", "systemd"]


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    w.commit_recipes()
    seed_host_cache(w)
    w.register(w.entry().model_copy(update={"cache_archs": ("v3",)}))
    yield w
    w.close()


@pytest.fixture
def gen(fw: FakeWorker) -> str:
    return str(fw.config()["generation"])


@pytest.fixture
def results(tmp_path: Path) -> Path:
    return tmp_path / "results"


@pytest.fixture
def owner(fw: FakeWorker) -> ownership.Owner:
    held = ownership.Owner(
        arch="v3", worker=fw.name, job="kde", commit=fw.head, since="2026-10-17T09:00:00Z"
    )
    return ownership.acquire("v3", held)


@pytest.fixture
def produced(fw: FakeWorker, gen: str) -> dict[str, Path]:
    put = fw.put
    v3 = fork_point_names(gen, flavor="kde", stage="minimal")
    return {
        "binpkg": put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n"),
        "fork_point": put(f"cache/fork-points/{v3['stage']}", "fp\n"),
        "distfile": put("cache/distfiles/new-1.0.tar.gz", "src\n"),
        "ccache": put("cache/ccache/1/new-entry", "cc\n"),
        "log": put("out/jobs/kde.log", "built\n"),
        "rc": put("out/jobs/kde.rc", "0\n"),
        "runs_list": put("out/jobs/kde.runs", "20261017T090000Z-abc123\n"),
        "run": put("out/runs/20261017T090000Z-abc123/events.jsonl", "{}\n"),
        "iso": put("out/iso/kde/bentoo-kde.iso", "ISO kde\n"),
    }


def _nothing_of_the_cache_came(fw: FakeWorker, gen: str) -> None:
    c = fw.host_cache
    assert not (c / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").exists()
    assert not (c / "distfiles" / "new-1.0.tar.gz").exists()
    assert not (c / "ccache" / "1" / "new-entry").exists()
    forks = c / "fork-points"
    assert not any("kde-minimal" in p.name for p in forks.glob("*")) if forks.exists() else True


def test_an_owned_pull_brings_the_jobs_results_only(
    fw: FakeWorker,
    produced: dict[str, Path],
    results: Path,
    owner: ownership.Owner,
    gen: str,
) -> None:
    pulled = worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner, owned=True)
    assert (results / "kde.log").read_text() == "built\n"
    assert (results / "kde.rc").is_file()
    assert (config.runs_dir() / "20261017T090000Z-abc123" / "events.jsonl").is_file()
    assert pulled.isos and pulled.isos[0].name == "bentoo-kde.iso"
    _nothing_of_the_cache_came(fw, gen)
    assert not pulled.binhost
    assert pulled.binhost_reason is not None and fw.name in pulled.binhost_reason
    assert not any("emaint" in c for c in fw.ssh_commands())  # no index to rebuild


def test_ownership_is_read_from_the_registry_when_not_given(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    _nothing_of_the_cache_came(fw, gen)


def test_owned_destinations_leave_out_the_caches_and_the_binhost(results: Path) -> None:
    dests = worker.pull_destinations(
        "v3", results=results, binhost_generation="20260823T153057Z", owned=True
    )
    assert set(dests) == {results, config.runs_dir(), results / "iso"}


def test_a_pull_of_an_owned_arch_without_a_job_is_refused_naming_the_worker(
    fw: FakeWorker, results: Path
) -> None:
    with pytest.raises(ValueError, match=fw.name):
        worker.pull(fw.remote(), "v3", None, results=results)


def test_an_owned_factory_job_releases_the_lock_and_leaves_its_binpkgs_on_the_worker(
    fw: FakeWorker, gen: str
) -> None:
    fw.set_job(lines=["fake-shidashi: emerging app-misc/built-by-job"])
    with audit.run(config.runs_dir(), command="worker-job", argv=["worker", "job"]):
        result = worker.job(
            fw.remote(),
            fw.entry(),
            "fac-v3",
            FACTORY,
            allow_dirty=False,
            follow=True,
            results=fw.base / "results" / "fac-v3",
        )
    assert result.exit_code == 0
    assert (fw.base / "results" / "fac-v3" / "fac-v3.log").is_file()
    host_pkg = fw.host_cache / "binpkgs" / "v3" / gen / "app-misc" / "built-by-job-1.gpkg.tar"
    assert not host_pkg.exists()
    assert (fw.work / "cache" / "binpkgs" / "v3" / gen / "app-misc").is_dir()
    assert ownership.current("v3") is None
