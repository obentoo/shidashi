"""A whole worker job against a local fake worker (story 010, task 7.2).

``shidashi worker job`` from the CLI: lock -> push -> ship -> unit -> rc -> index ->
pull -> release, with a host ``factory`` for the same arch attempted -- from a
separate process, as root's build would be -- while the job holds the lock.
"""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shidashi import config, ownership
from shidashi.cli import app
from tests._fake_worker import FakeWorker, fork_point_names, seed_host_cache

#: A host factory, as a separate process: the host check passes (as the test suite
#: arranges), and the Factory must never be reached while the arch is owned.
_HOST_FACTORY = (
    "from shidashi import cli, doctor\n"
    "doctor.require_build_host = lambda _w: None\n"
    "class Reached:\n"
    "    def __init__(self, *a, **k):\n"
    "        raise SystemExit('FACTORY REACHED')\n"
    "cli.Factory = Reached\n"
    "cli.app()\n"
)


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    w.register()
    w.commit_recipes()  # the job's commit: the sync resolves its fork-point keys there
    seed_host_cache(w)
    yield w
    w.close()


def test_a_factory_job_runs_end_to_end_and_owns_the_arch_meanwhile(
    fw: FakeWorker, tmp_path: Path
) -> None:
    gen = fw.config()["generation"]
    host_env = {**os.environ, "PATH": os.environ["PATH"].split(os.pathsep, 1)[1]}
    fw.set_job(
        during=[
            {
                "argv": [
                    sys.executable,
                    "-c",
                    _HOST_FACTORY,
                    "factory",
                    "v3",
                    "minimal",
                    "systemd",
                    "--no-download",
                ],
                "env": host_env,
                "cwd": str(tmp_path),
            }
        ]
    )
    result = CliRunner().invoke(
        app,
        [
            "worker",
            "job",
            fw.name,
            "fac-v3",
            "--results",
            str(tmp_path / "res"),
            "--",
            "factory",
            "v3",
            "minimal",
            "systemd",
        ],
    )
    assert result.exit_code == 0, result.output

    (inv,) = fw.job_invocations()
    # the lock was held while the job ran, and refused the host's own factory
    assert inv["host_locks"]["v3.owner.json"]["job"] == "fac-v3"
    (attempt,) = inv["during"]
    assert attempt["rc"] == 1, attempt["output"]
    assert "FACTORY REACHED" not in attempt["output"]
    for part in ("bentoo-lab", "fac-v3", "shidashi worker unlock v3"):
        assert part in attempt["output"]

    # the order: ship before the unit, the index after it, the lock around every transfer
    commands = fw.ssh_commands()
    ship = next(i for i, c in enumerate(commands) if "tar -x" in c)
    unit = next(
        i for i, c in enumerate(commands) if "systemd-run" in c and "shidashi-job-fac-v3" in c
    )
    index = next(i for i, c in enumerate(commands) if "emaint" in c)
    assert ship < unit < index
    rsyncs = fw.rsync_calls()
    assert rsyncs and all("v3.owner.json" in c["locks"] for c in rsyncs)
    assert (fw.work / "out" / "jobs" / "fac-v3.rc").read_text().strip() == "0"

    # the results are home, the index is the worker's, the lock is gone
    pkgdir = fw.host_cache / "binpkgs" / "v3" / gen
    assert (pkgdir / "app-misc" / "built-by-job-1.gpkg.tar").is_file()
    assert (pkgdir / "Packages").read_text() == (
        fw.work / "cache/binpkgs/v3" / gen / "Packages"
    ).read_text()
    # the job's fork point, named under its commit's keys (story 019, R3.1)
    fork_point = fork_point_names(gen, flavor="minimal", stage="minimal")["stage"]
    assert (fw.host_cache / "fork-points" / fork_point).is_file()
    assert (config.runs_dir() / "20261005T120000Z-f00d01" / "events.jsonl").is_file()
    assert ownership.current("v3") is None
    fw.assert_pinned()


def test_an_assemble_job_runs_end_to_end_without_the_lock(fw: FakeWorker, tmp_path: Path) -> None:
    gen = fw.config()["generation"]
    index = fw.host_cache / "binpkgs" / "v3" / gen / "Packages"
    before = index.read_text() if index.exists() else None
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/stray-1.gpkg.tar", "stray\n")
    result = CliRunner().invoke(
        app,
        [
            "worker",
            "job",
            fw.name,
            "kde",
            "--results",
            str(tmp_path / "res"),
            "--",
            "assemble",
            "v3",
            "kde",
            "systemd",
        ],
    )
    assert result.exit_code == 0, result.output
    (inv,) = fw.job_invocations()
    assert inv["host_locks"] == {}
    assert any(p.name == "bentoo-fake-kde-v3.iso" for p in (tmp_path / "res").rglob("*"))
    # an assemble only reads the binhost: the pull leaves it alone (contract C5)
    assert (index.read_text() if index.exists() else None) == before
    assert not (index.parent / "app-misc" / "stray-1.gpkg.tar").exists()


def test_the_readme_documents_using_a_worker() -> None:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    for command in (
        "shidashi worker status",
        "shidashi worker job",
        "shidashi worker logs",
        "shidashi worker sync",
        "shidashi worker unlock",
    ):
        assert command in readme, command
