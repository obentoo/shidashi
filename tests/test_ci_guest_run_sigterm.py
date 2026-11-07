"""``run_job`` stopped by SIGTERM, and the run_dir its prepare sweeps (story 034, 3.1).

Both came from the tech review of group 3 (.draft/reports/3.x-tech-review-*.md):
``systemctl stop shidashi-ci-<job>`` killed the process before its ``finally`` ran, which
left the JIT config on the work disk (R4.2, R5.6); and ``prepare`` swept the default
``/run/shidashi/ci`` instead of the caller's ``run_dir``.
"""

import os
import signal
from pathlib import Path
from typing import Any

import pytest

from shidashi import ci_guest
from tests._fake_ci_job import JOB, job_host, run
from tests._fake_ci_toolbox import Boot


class _KilledBySigterm(Exception):
    """The default SIGTERM action, made catchable: run_job installed no handler of its own."""


@pytest.fixture(autouse=True)
def _sigterm_is_catchable() -> Any:
    def killed(_signum: int, _frame: Any) -> None:
        raise _KilledBySigterm

    previous = signal.signal(signal.SIGTERM, killed)
    yield
    signal.signal(signal.SIGTERM, previous)


def _stopped_mid_boot(h: Any) -> None:
    def boot(_argv: list[str]) -> Boot:
        os.kill(os.getpid(), signal.SIGTERM)  # the unit is stopped while the VM runs
        return Boot(rc=0, serial="")

    h.toolbox.on_boot = boot


def test_sigterm_mid_boot_unwinds_and_leaves_no_jitconfig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = job_host(tmp_path, monkeypatch)
    _stopped_mid_boot(h)
    with pytest.raises(SystemExit) as stopped:
        run(h)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert not h.job_dir().exists()
    assert h.files_holding_the_jitconfig() == []
    result = h.result_file()
    assert result is not None and result["rc"] == 1


def test_sigterm_mid_boot_removes_the_vm_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = job_host(tmp_path, monkeypatch)
    _stopped_mid_boot(h)
    with pytest.raises(SystemExit):
        run(h)
    boot = max(i for i, c in enumerate(h.toolbox.calls) if any("qemu-system" in w for w in c))
    after = [c for c in h.toolbox.calls[boot + 1 :] if c[:2] == ["podman", "rm"]]
    assert any(c[-1] == f"shidashi-vm-ci-{JOB}" for c in after)


def test_sigterm_handler_restored_after_a_normal_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = job_host(tmp_path, monkeypatch)
    h.guest("0")
    before = signal.getsignal(signal.SIGTERM)
    assert run(h) == (0, "")
    assert signal.getsignal(signal.SIGTERM) is before


def test_prepare_sweeps_the_callers_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = job_host(tmp_path, monkeypatch)
    h.guest("0")
    seen: list[Path | None] = []

    def prepare_spy(_pins: Any, **kw: Any) -> Path:
        seen.append(kw.get("run_dir"))  # prepare's locked sweep takes this run_dir
        return h.prepared

    monkeypatch.setattr(ci_guest, "prepare", prepare_spy)
    assert run(h) == (0, "")
    assert seen == [h.run_dir]
