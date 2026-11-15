"""``shidashi fork-points gc <arch>``: the real CLI over the lab of tests/test_binhost_cli_show.py.

The lab's pins give the current pin id; the restore points are files with the real name
shapes in ``<cache>/fork-points``. Only ``worker.job`` is faked, for a worker-owned arch,
the way tests/test_binhost_cli_gc.py fakes it.
"""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import Result

from shidashi import config, fork_point_gc, ownership, tree, workers
from shidashi import worker as worker_mod
from shidashi.cli import app
from shidashi.workers import WorkerEntry
from tests.test_binhost_cli_show import Lab, make_lab, runner

STAGE3 = "20260823T153057Z"
BUILD_KEY = "b392fdb91"
OLDEST = "p20260801.11111111"
PREVIOUS = "p20260815.22222222"


def fork_points(*args: str) -> Result:
    return runner.invoke(app, ["fork-points", *args])


def restore_point(pin: str, what: str, arch: str = "v3") -> str:
    return f"{arch}-systemd-{STAGE3}-{pin}-{BUILD_KEY}-{what}.tar"


class ForkLab:
    """The binhost lab plus its fork points directory, filled with every pin's tarballs."""

    def __init__(self, lab: Lab) -> None:
        self.lab = lab
        self.current = tree.load_pin_id(lab.seeds)
        self.dir = config.fork_points_dir()
        self.dir.mkdir(parents=True)
        self.by_pin = {
            pin: [restore_point(pin, what) for what in ("bootstrap", "desktop", "kde")]
            for pin in (OLDEST, PREVIOUS, self.current)
        }
        # never the gc's: another arch's old pin, a tarball being written, a lock
        self.foreign = [
            restore_point(OLDEST, "kde", arch="znver5"),
            f".{restore_point(self.current, 'gnome')}.tmp",
            "v3.owner.json",
        ]
        for name in [*(n for names in self.by_pin.values() for n in names), *self.foreign]:
            (self.dir / name).write_bytes(b"tar" * 100)

    def names(self) -> set[str]:
        return {p.name for p in self.dir.iterdir()}

    def every(self) -> set[str]:
        return {n for names in self.by_pin.values() for n in names} | set(self.foreign)

    def of(self, *pins: str) -> set[str]:
        return {n for pin in pins for n in self.by_pin[pin]}


@pytest.fixture
def fork_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ForkLab:
    return ForkLab(make_lab(tmp_path, monkeypatch))


def hold_v3_on_the_worker() -> None:
    ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker="bentoo-lab",
            job="build-v3",
            commit="a" * 40,
            since="2026-11-15T01:00:00Z",
            host_pid=None,
        ),
    )


def test_dry_run_prints_the_plan_and_deletes_nothing(fork_lab: ForkLab) -> None:
    result = fork_points("gc", "v3", "--dry-run")
    assert result.exit_code == 0, result.output
    planned = {line.split()[2] for line in result.output.splitlines() if line.startswith("would")}
    assert planned == fork_lab.of(OLDEST)
    assert f"kept pins: {fork_lab.current} (current), {PREVIOUS}" in result.output
    assert "3 fork points of v3 would be deleted" in result.output
    # 79.8 GiB of tarballs freed 33 GiB of the worker's zstd btrfs (2026-11-15)
    assert "apparent sizes" in result.output
    assert fork_lab.names() == fork_lab.every()


def test_dry_run_reads_the_plan_while_a_build_holds_the_arch(fork_lab: ForkLab) -> None:
    """It only lists, so it takes no lock: the plan can be read during a long build."""
    hold_v3_on_the_worker()
    result = fork_points("gc", "v3", "--dry-run")
    assert result.exit_code == 0, result.output
    holder = ownership.current("v3")
    assert holder is not None and holder.worker == "bentoo-lab"
    assert fork_lab.names() == fork_lab.every()


def test_gc_deletes_the_older_pins_and_keeps_the_current_and_the_newest_other(
    fork_lab: ForkLab,
) -> None:
    result = fork_points("gc", "v3")
    assert result.exit_code == 0, result.output
    assert fork_lab.names() == fork_lab.every() - fork_lab.of(OLDEST)
    deleted = {
        line.split()[1] for line in result.output.splitlines() if line.startswith("deleted ")
    }
    assert fork_lab.of(OLDEST) <= deleted
    assert "deleted 3 fork points of v3" in result.output
    assert "apparent sizes" in result.output
    assert ownership.current("v3") is None, "the lock outlived the gc"


def test_keep_zero_leaves_only_the_current_pin(fork_lab: ForkLab) -> None:
    result = fork_points("gc", "v3", "--keep", "0")
    assert result.exit_code == 0, result.output
    assert fork_lab.names() == fork_lab.every() - fork_lab.of(OLDEST, PREVIOUS)


def test_gc_deletes_under_this_host_s_owner_lock(
    fork_lab: ForkLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[Any] = []
    real = fork_point_gc.delete_fork_points

    def recording(files: Any) -> Any:
        held.append(ownership.current("v3"))
        return real(files)

    monkeypatch.setattr(fork_point_gc, "delete_fork_points", recording)
    result = fork_points("gc", "v3")
    assert result.exit_code == 0, result.output
    assert len(held) == 1 and held[0] is not None and held[0].worker.startswith("host:"), held
    assert ownership.current("v3") is None


def test_a_held_lock_refuses_naming_the_holder_and_deletes_nothing(fork_lab: ForkLab) -> None:
    hold_v3_on_the_worker()
    result = fork_points("gc", "v3")
    assert result.exit_code == 1
    assert "bentoo-lab" in result.output and "build-v3" in result.output
    assert fork_lab.names() == fork_lab.every()
    holder = ownership.current("v3")
    assert holder is not None and holder.worker == "bentoo-lab"  # never taken over


def test_a_running_pull_refuses_and_deletes_nothing(fork_lab: ForkLab) -> None:
    """A pull copies fork points into this directory without the owner lock."""
    marker = ownership.locks_dir() / "v3.pull"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = fork_points("gc", "v3")
    assert result.exit_code == 1
    assert str(os.getpid()) in result.output
    assert fork_lab.names() == fork_lab.every()


def test_a_negative_keep_is_refused_and_deletes_nothing(fork_lab: ForkLab) -> None:
    result = fork_points("gc", "v3", "--keep", "-1")
    assert result.exit_code != 0
    assert fork_lab.names() == fork_lab.every()


def test_unreadable_pins_refuse_before_anything_is_deleted(fork_lab: ForkLab) -> None:
    (fork_lab.lab.seeds / "gentoo.toml").unlink()
    result = fork_points("gc", "v3")
    assert result.exit_code == 1
    assert "gentoo.toml" in result.output
    assert fork_lab.names() == fork_lab.every()


def test_a_missing_fork_points_directory_deletes_nothing_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_lab(tmp_path, monkeypatch)
    result = fork_points("gc", "v3")
    assert result.exit_code == 0, result.output
    assert "deleted 0 fork points of v3" in result.output


# --- a worker-owned arch ---------------------------------------------------------------------


@pytest.fixture
def v3_owned_by_worker(
    fork_lab: ForkLab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """bentoo-lab owns v3's cache (story 028); ``worker.job`` is recorded, never run."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = config.workers_dir() / "workers.json"
    path.parent.mkdir(parents=True)
    workers.save_registry(
        path,
        {
            "bentoo-lab": WorkerEntry(
                name="bentoo-lab",
                address="192.0.2.10",
                host_key="ssh-ed25519 AAAA",
                host_key_fingerprint="SHA256:x",
                paired_at="2026-10-17T00:00:00Z",
                cache_archs=("v3",),
            )
        },
    )
    jobs: list[dict[str, Any]] = []

    def job(_remote: Any, entry: Any, name: str, args: Sequence[str], **kw: Any) -> Any:
        jobs.append({"worker": entry.name, "job": name, "args": list(args), **kw})
        code = 0 if "--dry-run" in args else 3
        return worker_mod.JobResult(
            exit_code=code, log=None, isos=(), run_ids=(), duration_s=0.0, followed=True
        )

    monkeypatch.setattr(worker_mod, "job", job)
    return jobs


def test_gc_of_a_worker_owned_arch_runs_as_a_job_on_that_worker(
    fork_lab: ForkLab, v3_owned_by_worker: list[dict[str, Any]]
) -> None:
    """The same gc, its options forwarded, on the owner: nothing here is deleted and no
    lock is taken on this host."""
    result = fork_points("gc", "v3", "--keep", "3", "--dry-run")
    assert result.exit_code == 0, result.output
    assert len(v3_owned_by_worker) == 1, (v3_owned_by_worker, result.output)
    job = v3_owned_by_worker[0]
    assert job["worker"] == "bentoo-lab" and job["job"] == "fork-points-gc-v3"
    assert job["args"] == ["fork-points", "gc", "v3", "--keep", "3", "--dry-run"]
    assert fork_lab.names() == fork_lab.every()
    assert ownership.current("v3") is None


def test_the_worker_job_s_failure_is_the_command_s_failure(
    fork_lab: ForkLab, v3_owned_by_worker: list[dict[str, Any]]
) -> None:
    result = fork_points("gc", "v3")
    assert len(v3_owned_by_worker) == 1, (v3_owned_by_worker, result.output)
    assert v3_owned_by_worker[0]["args"] == ["fork-points", "gc", "v3", "--keep", "1"]
    assert result.exit_code != 0
    assert fork_lab.names() == fork_lab.every()


def test_an_arch_the_worker_does_not_own_is_collected_here(
    fork_lab: ForkLab, v3_owned_by_worker: list[dict[str, Any]]
) -> None:
    """Hostile half: owning v3 says nothing about znver5."""
    result = fork_points("gc", "znver5", "--keep", "0")
    assert result.exit_code == 0, result.output
    assert v3_owned_by_worker == []
    assert fork_lab.names() == fork_lab.every() - {restore_point(OLDEST, "kde", arch="znver5")}
