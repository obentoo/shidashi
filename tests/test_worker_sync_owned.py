"""Story 028: the push leaves an owned arch's cache on its worker.

Real rsync through the fake ssh of tests/_fake_worker.py, as in test_worker_sync.py.
"""

import fnmatch
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import config, worker
from tests._fake_worker import FakeWorker, seed_host_cache


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    w.commit_recipes()
    yield w
    w.close()


@pytest.fixture
def gen(fw: FakeWorker) -> str:
    return str(fw.config()["generation"])


@pytest.fixture
def host(fw: FakeWorker) -> dict[str, Path]:
    return seed_host_cache(fw)


def _own(fw: FakeWorker, *archs: str) -> None:
    fw.register(fw.entry().model_copy(update={"cache_archs": archs}))


def _cache_sources(plan: list[tuple[str, str]]) -> set[str]:
    return {h.rstrip("/") for h, _ in plan}


def test_an_owned_plan_names_only_the_pinned_inputs(gen: str, host: dict[str, Path]) -> None:
    sources = _cache_sources(worker.push_plan("v3", gen, owned=True))
    cache = config.cache_dir()
    assert str(cache / "trees") in sources
    assert str(cache / "repos") in sources
    assert any(fnmatch.fnmatch(str(host["stage3"]), s) for s in sources)
    for kept in (config.pkgdir("v3", gen), config.distdir(), config.ccache_dir()):
        assert str(kept) not in sources, kept
    assert not any(fnmatch.fnmatch(str(host["fork_point"]), s) for s in sources)


def test_an_unowned_plan_is_unchanged(gen: str, host: dict[str, Path]) -> None:
    assert worker.push_plan("v3", gen, owned=False) == worker.push_plan("v3", gen)


def test_pushing_an_owned_arch_sends_only_the_inputs(fw: FakeWorker, host: dict[str, Path]) -> None:
    _own(fw, "v3")
    worker.push(fw.remote(), "v3", bwlimit=None)
    w = fw.work / "cache"
    for key in ("tree", "repo", "stage3"):
        assert (w / host[key].relative_to(fw.host_cache)).is_file(), key
    for key in ("binpkg", "distfile", "ccache", "fork_point", "bootstrap"):
        assert not (w / host[key].relative_to(fw.host_cache)).exists(), key


def test_an_arch_owned_by_another_worker_is_pushed_as_today(
    fw: FakeWorker, host: dict[str, Path]
) -> None:
    other = fw.entry(name="elsewhere").model_copy(update={"cache_archs": ("v3",)})
    fw.register(fw.entry(), other)
    worker.push(fw.remote(), "v3", bwlimit=None)
    w = fw.work / "cache"
    assert (w / host["binpkg"].relative_to(fw.host_cache)).is_file()
