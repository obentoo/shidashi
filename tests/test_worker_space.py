"""Space before a worker sync (story 019, task 5.4; R3.3, R3.4, R3.6): a pull or a
push that would leave its destination with less than 10 GiB free copies nothing.

Needed bytes are what ``rsync --dry-run --stats`` would transfer: a file already on
the destination counts zero. Big files are SPARSE (their size is real, their blocks
are not), and a guard keeps every REAL rsync of these tests from copying them,
refusal or not; dry runs (the measurement) pass untouched. The host's free space is
``shutil.disk_usage``'s answer; the worker's is the fake ``df``'s.
"""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, ownership, remote, worker
from shidashi.phases import build_key
from shidashi.tree import load_pin_id
from tests._fake_worker import GIB, FakeWorker, mentions_size
from tests._pending import try_import

space_shortfall: Any = try_import("shidashi.worker", "space_shortfall")
RESERVE: Any = try_import("shidashi.worker", "RESERVE")

#: The guard in front of the fake rsync: a real client run gets ``--max-size``.
_GUARD = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    --server|--dry-run) exec "{fake}" "$@";;
    --*) ;;
    -*n*) exec "{fake}" "$@";;
  esac
done
exec "{fake}" --max-size=64M "$@"
"""


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    for name in ("variants", "seeds"):
        shutil.copytree(getattr(config, f"{name}_dir")(), w.repo / name)
    w._git("add", "-A")
    w._git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "the recipes")
    w.head = w._git("rev-parse", "HEAD")
    guard = tmp_path / "guard-bin"
    guard.mkdir()
    (guard / "rsync").write_text(_GUARD.format(fake=w.base / "host-bin" / "rsync"))
    (guard / "rsync").chmod(0o755)
    monkeypatch.setenv("PATH", f"{guard}{os.pathsep}{os.environ['PATH']}")
    yield w
    w.close()


def _fork(gen: str, stage: str, *, key: str | None = None) -> str:
    pins = load_pin_id(config.seeds_dir())
    bk = key or build_key(config.load_recipe("v3", "gnome", "systemd"))
    return f"v3-systemd-{gen}-{pins}-{bk}-{stage}.tar"


def _sparse(path: Path, size: int, *, mtime: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.truncate(size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _host_free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    usage = type(shutil.disk_usage("/"))

    def disk_usage(_path: Any) -> Any:
        return usage(20 * 1024 * GIB, 20 * 1024 * GIB - free, free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)


def _is_dry(argv: list[str]) -> bool:
    return "--dry-run" in argv or any(
        a.startswith("-") and not a.startswith("--") and "n" in a[1:] for a in argv
    )


def _real_transfers(fw: FakeWorker) -> list[list[str]]:
    return [c["argv"] for c in fw.rsync_calls() if not _is_dry(c["argv"])]


def _owner(fw: FakeWorker, gen: str) -> ownership.Owner:
    held = ownership.Owner(
        arch="v3",
        worker=fw.name,
        job="kde",
        commit=fw.head,
        since="2026-10-09T10:00:00Z",
        generation=gen,
    )
    return ownership.acquire("v3", held)


def test_the_reserve_is_10_gib_and_the_boundary_fits() -> None:
    assert RESERVE == 10 * GIB
    assert space_shortfall(5 * GIB, 15 * GIB, RESERVE) == 0  # exactly 10 GiB left
    assert space_shortfall(5 * GIB + 1, 15 * GIB, RESERVE) == 1


def test_hostile_a_pull_of_files_that_fit_one_by_one_but_not_together_copies_nothing(
    fw: FakeWorker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 GiB + 3 GiB against 15 GiB free - 10 GiB kept: each fits alone, not both."""
    gen = str(fw.config()["generation"])
    for stage in ("minimal", "desktop"):
        _sparse(fw.work / "cache" / "fork-points" / _fork(gen, stage), 3 * GIB)
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n")
    owner = _owner(fw, gen)
    _host_free(monkeypatch, 15 * GIB)
    with pytest.raises(remote.SyncError) as err:
        worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results", owner=owner)
    message = str(err.value)
    assert mentions_size(message, 6 * GIB) and mentions_size(message, 15 * GIB), message
    assert str(fw.host_cache) in message or str(tmp_path / "results") in message
    assert _real_transfers(fw) == []  # nothing copied: a dry run only measures
    forks = fw.host_cache / "fork-points"
    assert not forks.exists() or not any(forks.iterdir())
    assert ownership.current("v3") == owner  # the lock stays for a retry


def test_hostile_a_fork_point_already_on_the_host_counts_zero(
    fw: FakeWorker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 + 3 GiB on the worker, one already home (same size and mtime): 3 GiB to move."""
    gen = str(fw.config()["generation"])
    stamp = 1_760_000_000
    for stage in ("minimal", "desktop"):
        _sparse(fw.work / "cache" / "fork-points" / _fork(gen, stage), 3 * GIB, mtime=stamp)
    _sparse(fw.host_cache / "fork-points" / _fork(gen, "minimal"), 3 * GIB, mtime=stamp)
    owner = _owner(fw, gen)
    _host_free(monkeypatch, 15 * GIB)
    worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results", owner=owner)


def test_a_pull_that_fits_with_the_reserve_copies_as_before(
    fw: FakeWorker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = str(fw.config()["generation"])
    fw.put(f"cache/fork-points/{_fork(gen, 'desktop')}", b"f" * 1024 * 1024)
    owner = _owner(fw, gen)
    _host_free(monkeypatch, RESERVE + 64 * 1024 * 1024)
    worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results", owner=owner)
    assert (fw.host_cache / "fork-points" / _fork(gen, "desktop")).is_file()


def test_hostile_a_fork_point_the_push_skips_does_not_count_against_the_worker(
    fw: FakeWorker,
) -> None:
    """Only what is SENT counts: a 12 GiB orphan (another build key) stays home."""
    gen = str(fw.config()["generation"])
    forks = fw.host_cache / "fork-points"
    _sparse(forks / _fork(gen, "desktop", key="b00000000"), 12 * GIB)
    (forks / _fork(gen, "minimal")).write_text("fp\n")
    fw.set(df_avail=15 * GIB)
    worker.push(fw.remote(), "v3", bwlimit=None)
    assert (fw.work / "cache" / "fork-points" / _fork(gen, "minimal")).is_file()


def test_a_push_that_does_not_fit_on_the_work_disk_sends_nothing(fw: FakeWorker) -> None:
    gen = str(fw.config()["generation"])
    _sparse(fw.host_cache / "fork-points" / _fork(gen, "desktop"), 12 * GIB)
    fw.set(df_avail=15 * GIB)
    with pytest.raises(remote.SyncError) as err:
        worker.push(fw.remote(), "v3", bwlimit=None)
    message = str(err.value)
    assert fw.name in message
    assert mentions_size(message, 12 * GIB) and mentions_size(message, 15 * GIB), message
    assert _real_transfers(fw) == []  # not even the venv


def test_a_push_whose_free_space_cannot_be_read_sends_nothing(fw: FakeWorker) -> None:
    gen = str(fw.config()["generation"])
    (fw.host_cache / "fork-points").mkdir(parents=True, exist_ok=True)
    (fw.host_cache / "fork-points" / _fork(gen, "minimal")).write_text("fp\n")
    fw.fail(r"(^|[;&|\s])df\s", code=1, stderr="df: /mnt/work: Input/output error\n")
    with pytest.raises(remote.SyncError, match="Input/output error"):
        worker.push(fw.remote(), "v3", bwlimit=None)
    assert _real_transfers(fw) == []
