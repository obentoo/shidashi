"""The five failure paths Q4 found untested (story 010, task 10.1).

Against a fake worker (tests/_fake_worker.py), like tests/test_cli_worker_jobs.py and
tests/test_worker_job.py; the fixtures are copied here, not imported. No production
change: each test pins a path that already exists.
"""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import audit, config, ownership, worker
from shidashi.cli import app
from shidashi.remote import SyncError
from tests._fake_worker import FakeWorker, seed_host_cache

cli = CliRunner()


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    w.register()
    yield w
    w.close()


def _invoke(args: list[str], capfd: pytest.CaptureFixture[str]) -> tuple[Result, str]:
    result = cli.invoke(app, ["worker", *args])
    out = capfd.readouterr()
    return result, result.output + out.out + out.err


def _no_traceback(result: Result) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception


def _lock(job: str) -> Any:
    return ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker="bentoo-lab",
            job=job,
            commit="a" * 40,
            since="2026-10-05T14:02:31Z",
            host_pid=None,
        ),
    )


# --- R5.4: a failed `sync pull --job` keeps the holder's lock ----------------------


def test_sync_pull_job_failure_keeps_the_lock_and_prints_retry_and_unlock(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    seed_host_cache(fw)
    held = _lock("fac-v3")
    fw.put("out/jobs/fac-v3.rc", "0\n")  # it has ended: rc present, unit inactive
    fw.fail(r"rsync --server --sender", code=12, stderr="connection reset\n")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code != 0, out
    _no_traceback(result)
    assert ownership.current("v3") == held  # kept (R5.4)
    assert "run this pull again" in out
    assert "shidashi worker unlock v3" in out


# --- R7.1: logs -f of a stopped unit, logs of a job with no log --------------------


def test_logs_follow_of_a_unit_that_never_writes_its_rc_exits_1_after_the_grace(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(worker, "LOGS_GRACE", 1)
    fw.put("out/jobs/kde.log", "only line\n")  # the unit is not active, no rc ever
    t0 = time.monotonic()
    result, out = _invoke(["logs", fw.name, "kde", "-f"], capfd)
    assert result.exit_code == 1, out
    assert time.monotonic() - t0 < 20
    _no_traceback(result)
    assert "only line" in out
    assert "shidashi-job-kde is not running" in out and "wrote no exit code" in out


def test_logs_of_a_job_with_no_log_exits_1(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    result, out = _invoke(["logs", fw.name, "ghost"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "no log of job ghost" in out


# --- R1.5: status NAME of an unreachable worker -----------------------------------


def test_status_name_of_an_unreachable_worker_exits_1_with_the_reason(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.unreachable()
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "unreachable" in out.lower()
    assert "timed out" in out.lower()  # the reason, from ssh


# --- R3.12: a PKGDIR writer without an arch is refused before any transfer ---------


def test_job_of_an_archless_pkgdir_writer_is_refused_before_any_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = FakeWorker.install(tmp_path, monkeypatch)
    try:
        seed_host_cache(w)
        with (
            audit.run(config.runs_dir(), command="worker-job", argv=["worker", "job"]),
            pytest.raises(worker.JobRefused) as refused,
        ):
            worker.job(
                w.remote(),
                w.entry(),
                "fac",
                ["factory"],
                allow_dirty=False,
                follow=True,
                results=tmp_path / "results",
            )
        assert "names no arch" in refused.value.reason
        assert "factory ARCH" in refused.value.fix
        assert w.rsync_calls() == []
        assert w.ssh_commands() == []
        assert w.job_invocations() == []
        assert ownership.current("v3") is None
    finally:
        w.close()


# --- an unreadable pulled <job>.rc ------------------------------------------------


@pytest.mark.parametrize("content", ["garbage\n", "", None])
def test_read_rc_of_an_unreadable_rc_raises_sync_error(tmp_path: Path, content: str | None) -> None:
    rc = tmp_path / "fac-v3.rc"
    if content is not None:
        rc.write_text(content)  # None: the file is missing
    with pytest.raises(SyncError) as err:
        worker._read_rc(rc)
    assert "job exit code" in str(err.value) and str(rc) in str(err.value)
