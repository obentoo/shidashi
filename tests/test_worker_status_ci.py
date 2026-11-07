"""``shidashi worker status`` lists an active CI job beside Shidashi jobs (story 034,
task 5.1; R9.2, R7.3).

Contract (design.md, Integration Points -> ``shidashi/worker.py``: "the status probe lists
``shidashi-ci-*`` units too"; story.md R7.3):

- ``WorkerStatus.ci_jobs``: the active ``shidashi-ci-*`` units, by exact name without
  ``.service``, sorted -- beside ``jobs``;
- ``WorkerStatus.jobs`` stays the Shidashi jobs (``shidashi-job-*``) ONLY: a CI VM must not
  make ``shidashi build`` refuse the worker (``schedule`` treats any ``jobs`` as busy), or
  a Shidashi job could never run beside a CI job (R7.3);
- ``shidashi worker status NAME`` prints the CI job marked ``ci`` on the jobs line.

The probe runs for real against tests/_fake_worker.py.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shidashi import worker
from shidashi.cli import app
from tests._fake_worker import FakeWorker

status_runner = CliRunner()


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    w.register()
    yield w
    w.close()


def test_status_lists_the_ci_job_apart_from_the_shidashi_jobs(fw: FakeWorker) -> None:
    """R9.2, and R7.3's hostile collapse: a CI unit is not a Shidashi job."""
    fw.activate_unit("shidashi-ci-9001")
    fw.activate_unit("shidashi-job-fac-v3")
    st = worker.status(fw.remote())
    assert st.ci_jobs == ("shidashi-ci-9001",)
    assert st.jobs == ("shidashi-job-fac-v3",)


def test_status_a_shidashi_job_named_ci_is_still_a_shidashi_job(fw: FakeWorker) -> None:
    """Hostile collapse the other way: ``shidashi-job-ci-x`` is a Shidashi job named ci-x."""
    fw.activate_unit("shidashi-job-ci-x")
    fw.activate_unit("shidashi-ci-9002")
    st = worker.status(fw.remote())
    assert st.jobs == ("shidashi-job-ci-x",)
    assert st.ci_jobs == ("shidashi-ci-9002",)


def test_status_a_worker_running_only_a_ci_job_still_accepts_shidashi_jobs(fw: FakeWorker) -> None:
    """R7.3: the build scheduler reads ``jobs`` and ``accepts_jobs``."""
    fw.activate_unit("shidashi-ci-9003")
    st = worker.status(fw.remote())
    assert st.ci_jobs == ("shidashi-ci-9003",)
    assert st.jobs == () and st.accepts_jobs is True


def test_status_name_prints_the_ci_job_marked_ci(fw: FakeWorker) -> None:
    fw.activate_unit("shidashi-ci-9004")
    fw.activate_unit("shidashi-job-kde")
    result = status_runner.invoke(app, ["worker", "status", fw.name])
    assert result.exit_code == 0, result.output
    jobs_line = next(
        (line for line in result.output.splitlines() if line.strip().startswith("jobs")), ""
    )
    assert "kde" in jobs_line, result.output
    assert "9004" in jobs_line and "ci" in jobs_line.replace("shidashi-ci-", ""), jobs_line
