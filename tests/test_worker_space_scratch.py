"""Space before a push to a worker with a scratch disk (2026-11-27): the distfiles,
ccache and PKGDIR a push sends land on the scratch disk mounted over them, so their
bytes are compared to THAT disk's free space -- and everything else to the work disk's.

Same fake worker and helpers as ``tests/test_worker_space.py``; the fake ``df`` answers
for a second filesystem under the scratch paths once ``scratch_avail`` is set.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import remote, worker
from tests._fake_worker import GIB, SCRATCH_SUBPATHS, FakeWorker, mentions_size
from tests.test_worker_space import _GUARD, _fork, _real_transfers, _sparse


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


def test_the_fake_scratch_paths_are_the_ones_the_push_measures() -> None:
    assert SCRATCH_SUBPATHS == worker.SCRATCH_PATHS


def test_hostile_distfiles_that_fit_the_work_disk_but_not_the_scratch_disk_send_nothing(
    fw: FakeWorker,
) -> None:
    """The work disk has room to spare: only measuring the scratch disk refuses this."""
    _sparse(fw.host_cache / "distfiles" / "big-1.0.tar.gz", 12 * GIB)
    fw.set(df_avail=400 * GIB, scratch_avail=15 * GIB)
    with pytest.raises(remote.SyncError) as err:
        worker.push(fw.remote(), "v3", bwlimit=None)
    message = str(err.value)
    assert f"{worker.WORK}/cache/distfiles" in message, message
    assert mentions_size(message, 12 * GIB) and mentions_size(message, 15 * GIB), message
    assert _real_transfers(fw) == []  # not even the venv


def test_hostile_caches_that_fit_the_scratch_disk_one_by_one_but_not_together_send_nothing(
    fw: FakeWorker,
) -> None:
    """Distfiles and ccache are two mounts of ONE disk: their bytes add up."""
    _sparse(fw.host_cache / "distfiles" / "big-1.0.tar.gz", 4 * GIB)
    _sparse(fw.host_cache / "ccache" / "0" / "big.o", 4 * GIB)
    fw.set(df_avail=400 * GIB, scratch_avail=17 * GIB)  # 8 GiB + 10 GiB reserve > 17
    with pytest.raises(remote.SyncError) as err:
        worker.push(fw.remote(), "v3", bwlimit=None)
    assert mentions_size(str(err.value), 8 * GIB), str(err.value)
    assert _real_transfers(fw) == []


def test_a_fork_point_counts_against_the_work_disk_not_the_scratch_disk(fw: FakeWorker) -> None:
    gen = str(fw.config()["generation"])
    _sparse(fw.host_cache / "fork-points" / _fork(gen, "desktop"), 12 * GIB)
    fw.set(df_avail=15 * GIB, scratch_avail=400 * GIB)
    with pytest.raises(remote.SyncError) as err:
        worker.push(fw.remote(), "v3", bwlimit=None)
    message = str(err.value)
    assert f"{fw.name}:{worker.WORK}: " in message, message  # the work disk, not a scratch path
    assert mentions_size(message, 15 * GIB), message
    assert _real_transfers(fw) == []


def test_a_push_that_fits_both_disks_is_sent(fw: FakeWorker) -> None:
    gen = str(fw.config()["generation"])
    (fw.host_cache / "fork-points").mkdir(parents=True, exist_ok=True)
    (fw.host_cache / "fork-points" / _fork(gen, "minimal")).write_text("fp\n")
    (fw.host_cache / "distfiles").mkdir(parents=True, exist_ok=True)
    (fw.host_cache / "distfiles" / "small-1.0.tar.gz").write_text("src\n")
    fw.set(df_avail=400 * GIB, scratch_avail=400 * GIB)
    worker.push(fw.remote(), "v3", bwlimit=None)
    assert (fw.work / "cache" / "distfiles" / "small-1.0.tar.gz").is_file()
    assert (fw.work / "cache" / "fork-points" / _fork(gen, "minimal")).is_file()
