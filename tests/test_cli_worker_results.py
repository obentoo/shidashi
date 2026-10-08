"""``shidashi worker sync pull --results`` and resume commands that carry it
(story 010, task 8.1; R6.12, R6.13).

A job's results land where the job said, whether it was followed or resumed later.
Against a fake worker (tests/_fake_worker.py). Every test name contains ``results``
for the task's ``-k results`` selection.
"""

import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from shidashi.cli import app
from tests._fake_worker import FakeWorker, seed_host_cache

cli = CliRunner()


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    w.register()
    yield w
    w.close()


@pytest.fixture
def cached(fw: FakeWorker) -> dict[str, Path]:
    return seed_host_cache(fw)


def _invoke(args: list[str], capfd: pytest.CaptureFixture[str]) -> tuple[Result, str]:
    result = cli.invoke(app, ["worker", *args])
    out = capfd.readouterr()
    return result, result.output + out.out + out.err


def _no_traceback(result: Result) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception


def _finished_job_on_the_worker(fw: FakeWorker, job: str) -> None:
    """A job that has ended on the worker: its log, rc and ISO wait in out/."""
    fw.put(f"out/jobs/{job}.log", f"log of {job}\n")
    fw.put(f"out/jobs/{job}.rc", "0\n")
    fw.put(f"out/iso/{job}/bentoo-kde.iso", "ISO\n")


def _has_results(d: Path, job: str) -> bool:
    return (
        (d / f"{job}.log").is_file()
        and (d / f"{job}.rc").is_file()
        and (d / "iso" / "bentoo-kde.iso").is_file()
    )


def _pull_commands(out: str) -> list[list[str]]:
    """Every printed ``shidashi worker sync pull`` command, split as a shell would."""
    commands = []
    for line in out.splitlines():
        at = line.find("shidashi worker sync pull")
        if at >= 0:
            commands.append(shlex.split(line[at:]))
    return commands


def _results_values(argv: list[str]) -> list[str]:
    values = []
    for i, arg in enumerate(argv):
        if arg == "--results" and i + 1 < len(argv):
            values.append(argv[i + 1])
        elif arg.startswith("--results="):
            values.append(arg.split("=", 1)[1])
    return values


# =====================================================================================
# sync pull --results (R6.12)
# =====================================================================================


def test_sync_pull_job_with_results_lands_in_that_dir_and_without_it_in_the_default(
    fw: FakeWorker, cached: dict[str, Path], tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    default = tmp_path / "worker-results" / fw.name / "kde"
    chosen = tmp_path / "elsewhere" / "kde-results"
    _finished_job_on_the_worker(fw, "kde")

    # hostile half first: a DIR was named, so nothing may land in the default
    result, out = _invoke(
        ["sync", "pull", fw.name, "--arch", "v3", "--job", "kde", "--results", str(chosen)],
        capfd,
    )
    assert result.exit_code == 0, out
    assert _has_results(chosen, "kde"), sorted(str(p) for p in tmp_path.rglob("kde*"))
    assert not default.exists(), "results also went to the default directory"

    # the converse: no DIR named, so they land in ./worker-results/<worker>/<job>
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "kde"], capfd)
    assert result.exit_code == 0, out
    assert _has_results(default, "kde")


def test_sync_pull_results_without_job_exits_2_before_any_transfer(
    fw: FakeWorker, cached: dict[str, Path], tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A caches-only pull writes no job results: --results without --job is refused."""
    result, out = _invoke(
        ["sync", "pull", fw.name, "--arch", "v3", "--results", str(tmp_path / "res")], capfd
    )
    assert result.exit_code == 2, out
    assert "No such option" not in out  # the option exists; it is refused for a reason
    assert "--job" in out  # and the refusal says what it needs
    _no_traceback(result)
    assert fw.ssh_commands() == [] and fw.rsync_calls() == []


# =====================================================================================
# resume commands carry --results (R6.13)
# =====================================================================================


def test_job_no_follow_resume_command_carries_results_only_when_it_was_given(
    fw: FakeWorker, cached: dict[str, Path], tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # hostile half first: started without --results, no printed pull may invent one
    result, out = _invoke(
        ["job", fw.name, "plain", "--no-follow", "--", "assemble", "v3", "kde", "systemd"],
        capfd,
    )
    assert result.exit_code == 0, out
    pulls = _pull_commands(out)
    assert pulls, out
    assert all(_results_values(argv) == [] for argv in pulls), pulls
    # one job at a time per worker: let the first one end before the second starts
    assert fw.wait_for(lambda: (fw.work / "out" / "jobs" / "plain.rc").is_file())
    fw.deactivate_unit("shidashi-job-plain")

    # the converse: started with --results DIR, the printed pull brings it back to DIR
    chosen = tmp_path / "placed-results"
    result, out = _invoke(
        [
            "job",
            fw.name,
            "placed",
            "--results",
            str(chosen),
            "--no-follow",
            "--",
            "assemble",
            "v3",
            "kde",
            "systemd",
        ],
        capfd,
    )
    assert result.exit_code == 0, out
    pulls = _pull_commands(out)
    assert pulls, out
    for argv in pulls:
        assert "placed" in argv, argv
        assert [Path(v).absolute() for v in _results_values(argv)] == [chosen], argv


def test_job_failed_pull_retry_command_carries_results(
    fw: FakeWorker, cached: dict[str, Path], tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A followed job whose pull fails prints a retry that pulls into the same DIR."""
    fw.fail(r"rsync --server --sender", code=12, stderr="connection reset\n")
    chosen = tmp_path / "kde-results"
    result, out = _invoke(
        [
            "job",
            fw.name,
            "kde",
            "--results",
            str(chosen),
            "--",
            "assemble",
            "v3",
            "kde",
            "systemd",
        ],
        capfd,
    )
    assert result.exit_code != 0, out
    _no_traceback(result)
    pulls = _pull_commands(out)
    assert pulls, out
    for argv in pulls:
        assert [Path(v).absolute() for v in _results_values(argv)] == [chosen], argv


def test_job_resume_command_keeps_a_results_dir_with_a_space_whole(
    fw: FakeWorker, cached: dict[str, Path], tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The printed command is pasted into a shell: a DIR with a space must come back
    as that one DIR, not split into two arguments."""
    chosen = tmp_path / "my results"
    result, out = _invoke(
        [
            "job",
            fw.name,
            "spaced",
            "--results",
            str(chosen),
            "--no-follow",
            "--",
            "assemble",
            "v3",
            "kde",
            "systemd",
        ],
        capfd,
    )
    assert result.exit_code == 0, out
    pulls = _pull_commands(out)
    assert pulls, out
    for argv in pulls:
        assert [Path(v).absolute() for v in _results_values(argv)] == [chosen], argv
