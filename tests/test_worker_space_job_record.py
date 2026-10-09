"""The job's own record is exempt from the host's reserve (story 019, 5.4; author's
decision of 2026-10-09): a host already below 10 GiB free still brings a job's log,
rc and runs -- KiB -- while the reserve keeps guarding fork points, binpkgs and ISOs.

Same fake worker and helpers as ``tests/test_worker_space.py``.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import remote, worker
from tests._fake_worker import GIB, FakeWorker
from tests.test_worker_space import _GUARD, _fork, _host_free, _owner, _sparse


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    """The fake worker with the recipes committed and the big-file rsync guard."""
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    w.commit_recipes()
    guard = tmp_path / "guard-bin"
    guard.mkdir()
    (guard / "rsync").write_text(_GUARD.format(fake=w.base / "host-bin" / "rsync"))
    (guard / "rsync").chmod(0o755)
    monkeypatch.setenv("PATH", f"{guard}{os.pathsep}{os.environ['PATH']}")
    yield w
    w.close()


def _job_record(fw: FakeWorker, job: str) -> None:
    fw.put(f"out/jobs/{job}.log", "built\n")
    fw.put(f"out/jobs/{job}.rc", "0\n")


def test_a_host_below_the_reserve_still_brings_a_jobs_log_and_rc(
    fw: FakeWorker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _job_record(fw, "kde")
    _host_free(monkeypatch, 1 * GIB)  # 9 GiB short of the reserve
    pulled = worker.pull(fw.remote(), None, "kde", results=tmp_path / "results")
    assert (tmp_path / "results" / "kde.log").read_text() == "built\n"
    assert pulled.log == tmp_path / "results" / "kde.log"


def test_the_reserve_still_refuses_what_comes_beside_the_record(
    fw: FakeWorker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _job_record(fw, "kde")
    gen = str(fw.config()["generation"])
    _sparse(fw.work / "cache" / "fork-points" / _fork(gen, "desktop"), 3 * GIB)
    owner = _owner(fw, gen)
    _host_free(monkeypatch, 1 * GIB)
    with pytest.raises(remote.SyncError) as err:
        worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results", owner=owner)
    assert err.value.step == "space"
    assert not (tmp_path / "results" / "kde.log").exists()  # nothing copied: refused first
