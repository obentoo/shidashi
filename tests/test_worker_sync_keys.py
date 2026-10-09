"""ADDITIONS to tests/test_worker_sync.py (story 019, tasks 5.2-5.3; R3.1, R3.2, R3.5):
a sync carries only the fork points restorable under the commit's keys, and says
how many it skipped. Self-contained so it runs alone; merge when materialized.

The fake checkout's HEAD holds this checkout's variants/ and seeds/ (what task 5.2
gives the fake worker), so the keys are this checkout's: computed here independently
(``load_pin_id`` + ``build_key``), never through the code under test.
"""

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from shidashi import config, ownership
from shidashi.cli import app
from shidashi.phases import build_key
from shidashi.tree import load_pin_id
from tests._fake_worker import FakeWorker

cli = CliRunner()


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    w.register()
    for name in ("variants", "seeds"):
        shutil.copytree(getattr(config, f"{name}_dir")(), w.repo / name)
    w._git("add", "-A")
    w._git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "the recipes")
    w.head = w._git("rev-parse", "HEAD")
    yield w
    w.close()


def _names(gen: str) -> tuple[list[str], list[str]]:
    """(restorable, orphans) fork-point names of v3 × systemd at this generation."""
    pins = load_pin_id(config.seeds_dir())
    key = f"{pins}-{build_key(config.load_recipe('v3', 'gnome', 'systemd'))}"
    keep = [
        f"v3-systemd-{gen}-{key}-desktop.tar",  # a stage fork point
        f"v3-gnome-systemd-{gen}-{key}-gnome.tar",  # a phase snapshot (with the flavor)
        f"v3-systemd-{gen}-{key}-bootstrap.tar",  # the bootstrap checkpoint
    ]
    orphans = [
        f"v3-systemd-{gen}-{pins}-desktop.tar",  # before the build key (2026-10-08)
        f"v3-systemd-{gen}-{pins}-b00000000-desktop.tar",  # another build key
        f"v3-systemd-{gen}-p20260101.00000000-{key.split('-', 1)[1]}-desktop.tar",  # pins
    ]
    return keep, orphans


def _skipped_line(out: str, count: int) -> bool:
    return any("skip" in ln.lower() and re.search(rf"\b{count}\b", ln) for ln in out.splitlines())


def _finished_job(fw: FakeWorker, gen: str, commit: str) -> None:
    """Job kde ended on the worker, holding v3's lock; every fork-point kind waits there."""
    keep, orphans = _names(gen)
    for name in [*keep, *orphans]:
        fw.put(f"cache/fork-points/{name}", "fp\n")
    # hostile third element: an in-progress temp of a CURRENT key is not a fork point
    fw.put(f"cache/fork-points/.{keep[0]}.tmp", "partial\n")
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n")
    fw.put("out/jobs/kde.log", "log\n")
    fw.put("out/jobs/kde.rc", "0\n")
    ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker=fw.name,
            job="kde",
            commit=commit,
            since="2026-10-09T10:00:00Z",
            generation=gen,
        ),
    )


def _pull() -> Result:
    return cli.invoke(app, ["worker", "sync", "pull", "bentoo-lab", "--arch", "v3", "--job", "kde"])


def test_pull_brings_only_fork_points_of_the_jobs_keys_and_says_how_many_it_skipped(
    fw: FakeWorker,
) -> None:
    gen = str(fw.config()["generation"])
    keep, orphans = _names(gen)
    _finished_job(fw, gen, fw.head)
    result = _pull()
    assert result.exit_code == 0, result.output
    pulled = sorted(p.name for p in (fw.host_cache / "fork-points").iterdir())
    assert pulled == sorted(keep)
    assert _skipped_line(result.output, len(orphans)), result.output
    # unchanged: the binpkgs still come and the lock is released
    assert (fw.host_cache / "binpkgs/v3" / gen / "app-misc/new-2.gpkg.tar").is_file()
    assert ownership.current("v3") is None


def test_push_sends_only_fork_points_of_the_shipped_commit_and_says_how_many_it_skipped(
    fw: FakeWorker,
) -> None:
    gen = str(fw.config()["generation"])
    keep, orphans = _names(gen)
    forks = fw.host_cache / "fork-points"
    forks.mkdir(parents=True, exist_ok=True)
    for name in [*keep, *orphans]:
        (forks / name).write_text("fp\n")
    result = cli.invoke(app, ["worker", "sync", "push", fw.name, "--arch", "v3"])
    assert result.exit_code == 0, result.output
    sent = sorted(p.name for p in (fw.work / "cache" / "fork-points").iterdir())
    assert sent == sorted(keep)
    assert _skipped_line(result.output, len(orphans)), result.output


def test_a_commit_the_host_lacks_copies_no_fork_point_warns_once_and_pulls_the_rest(
    fw: FakeWorker,
) -> None:
    """No keys, no fork point: everything else of the pull runs, and the lock goes."""
    gen = str(fw.config()["generation"])
    unknown = "d" * 40
    _finished_job(fw, gen, unknown)
    result = _pull()
    assert result.exit_code == 0, result.output
    forks = fw.host_cache / "fork-points"
    assert not forks.exists() or not any(forks.iterdir())
    warnings = [ln for ln in result.output.splitlines() if unknown[:12] in ln]
    assert len(warnings) == 1, result.output
    assert (fw.host_cache / "binpkgs/v3" / gen / "app-misc/new-2.gpkg.tar").is_file()
    assert ownership.current("v3") is None
