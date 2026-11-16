"""What refuses a dev VM on a build worker, and what a start that fails leaves behind
(shidashi/vm_dev_remote.py): nothing, on either machine.

Every refusal comes before anything slow -- no code shipped, no ISO sent -- and before
anything lasting: no record on the host, no session on the worker. A start that fails
once the worker was told to boot is undone there, and its record removed here. The
stand-ins are tests/test_vm_dev_remote.py's.
"""

import json
import os
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import (
    audit,
    remote,
    stop_signals,
    vm,
    vm_dev_record,
    vm_dev_remote,
    vm_dev_remote_shell,
    worker,
)
from tests._dev_relay import (
    FakeShipHead,
    InMemoryTrail,
    _nothing_begun,
    _start,
    _stop_group,
    _told,
    _wait_for,
    install_lab,
    make_iso,
)
from tests._fake_worker_shell import SHIPPED, FakeWorkerShell
from tests.test_vm_dev import _PINS


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorkerShell]:
    shell = install_lab(tmp_path, monkeypatch)
    yield shell
    shell.close()


@pytest.fixture
def ship(lab: FakeWorkerShell, monkeypatch: pytest.MonkeyPatch) -> FakeShipHead:
    shipping = FakeShipHead(lab)
    monkeypatch.setattr(worker, "ship_head", shipping)
    return shipping


@pytest.fixture
def iso(tmp_path: Path) -> Path:
    return make_iso(tmp_path)


@pytest.fixture
def trail() -> Iterator[InMemoryTrail]:
    recorder = InMemoryTrail()
    with audit.recording(recorder):
        yield recorder


# --- the refusals, before anything slow -----------------------------------------------------


def test_a_dev_vm_is_refused_while_a_job_runs_and_the_jobs_are_named(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    lab.running_job("kde-v3", "fac-v3")
    with pytest.raises(vm_dev_remote.DevWorkerError) as refused:
        _start(lab, iso)
    said = str(refused.value)
    assert "bentoo-lab is running shidashi-job-fac-v3, shidashi-job-kde-v3" in said
    assert "shidashi worker logs bentoo-lab fac-v3 -f" in said
    _nothing_begun(lab, ship)


def test_a_worker_that_does_not_answer_is_the_first_refusal(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    lab.unreachable()
    with pytest.raises(vm_dev_remote.NoFirstAnswer, match="bentoo-lab .* is unreachable"):
        _start(lab, iso)
    assert len(lab.ssh_calls()) == 1
    _nothing_begun(lab, ship)


def test_a_worker_without_its_work_disk_is_refused(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    lab.unmounted()
    with pytest.raises(vm_dev_remote.DevWorkerError, match="/mnt/work is not mounted on bentoo"):
        _start(lab, iso)
    _nothing_begun(lab, ship)


def test_a_name_alive_on_the_worker_is_refused_and_left_running(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso)
    qemu = int((lab.sessions / "dev" / "qemu.pid").read_text())
    before = (ship.calls, len(lab.rsync_calls()), len(lab.shidashi_calls()))
    with pytest.raises(vm_dev_remote.DevWorkerError, match="'dev' is already running on bentoo"):
        _start(lab, iso)
    assert (ship.calls, len(lab.rsync_calls()), len(lab.shidashi_calls())) == before
    os.kill(qemu, 0)  # still there
    assert vm_dev_record.read_record("dev") is not None  # the first start's, untouched


def test_a_name_being_started_on_the_worker_is_refused(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    lab.plan("start", hold_s=30)
    lab.shipped_tree()
    line = vm_dev_remote_shell.relayed_start_command(
        SHIPPED, "dev", iso="/i.iso", pins=_PINS, memory="8G", cpus=1, max_life=1
    )
    other = subprocess.Popen(remote.ssh_argv(lab.remote(), line), stdin=subprocess.DEVNULL)
    try:
        _wait_for(lambda: bool(lab.shidashi_calls()))
        with pytest.raises(vm_dev_remote.DevWorkerError, match="'dev' is being started on"):
            _start(lab, iso)
        assert ship.calls == 0
    finally:
        _stop_group(lab, "dev")
        other.wait(timeout=20)


def test_a_name_recorded_on_another_worker_is_refused_before_any_contact(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    elsewhere = vm_dev_record.WorkerSession("dev", "other-lab", "6" * 40, "2026-11-16T00:00:00Z")
    vm_dev_record.write_record(elsewhere)
    with pytest.raises(vm_dev_remote.DevWorkerError, match="recorded on worker other-lab"):
        _start(lab, iso)
    assert lab.ssh_calls() == [] and ship.calls == 0
    assert vm_dev_record.read_record("dev") == elsewhere


def test_a_name_alive_on_this_host_is_refused_before_any_contact(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    local = vm.session_dir("dev")
    local.mkdir(parents=True)
    spec = {"iso": "/b.iso", "uefi": False, "cid": 61, "memory": "1G", "cpus": 1}
    (local / "session.json").write_text(json.dumps(spec))
    (local / "qemu.pid").write_text(f"{os.getpid()}\n")  # a process that is alive
    with pytest.raises(vm_dev_remote.DevWorkerError, match="running on this host"):
        _start(lab, iso)
    assert lab.ssh_calls() == []
    (local / "qemu.pid").write_text("999999999\n")  # a session whose VM is gone
    assert _start(lab, iso)["ready"] is True


@pytest.mark.parametrize("name", ["a b", "x;reboot", "../x", "-x", ""])
def test_a_name_that_cannot_cross_a_shell_is_refused_before_any_contact(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, name: str
) -> None:
    with pytest.raises(vm_dev_remote.DevWorkerError, match="cannot name a session on a worker"):
        _start(lab, iso, name=name)
    assert lab.ssh_calls() == [] and ship.calls == 0


def test_a_stale_record_of_the_same_worker_does_not_block_a_new_start(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    # the session expired there: the record is all that is left of it
    stale = vm_dev_record.WorkerSession("dev", "bentoo-lab", "6" * 40, "2026-11-01T00:00:00Z")
    vm_dev_record.write_record(stale)
    _start(lab, iso)
    record = vm_dev_record.read_record("dev")
    assert record is not None and record.commit == SHIPPED


# --- a start that fails leaves nothing ------------------------------------------------------


def test_the_record_is_there_before_the_worker_is_told_to_start(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a start killed outright must leave `vm stop` able to find what it began
    seen: list[vm_dev_record.WorkerSession | None] = []
    real = remote.run

    def spying(target: remote.Remote, command: str, **kw: Any) -> remote.RemoteResult:
        if " vm start " in command:
            seen.append(vm_dev_record.read_record("dev"))
        return real(target, command, **kw)

    monkeypatch.setattr(remote, "run", spying)
    _start(lab, iso)
    assert len(seen) == 1 and seen[0] is not None
    assert (seen[0].worker, seen[0].commit, seen[0].expires) == ("bentoo-lab", SHIPPED, None)


def test_a_start_that_fails_on_the_worker_says_why_and_leaves_no_record(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    lab.plan("start", exit=1, stderr="error: the guest setup failed (exit 1): no binhost\n")
    with pytest.raises(vm_dev_remote.DevWorkerError) as failed:
        _start(lab, iso)
    said = str(failed.value)
    assert "vm start --dev failed on bentoo-lab (exit 1)" in said
    assert "the guest setup failed (exit 1): no binhost" in said
    assert vm_dev_record.read_record("dev") is None
    # the undoing was sent: a session that had started would have been stopped
    assert lab.worker_commands()[-1] == vm_dev_remote_shell.relayed_stop_command(SHIPPED, "dev")


@pytest.mark.parametrize(
    "printed",
    ["", "not json\n", '{"name": "dev", "ready": false}\n', '["ready"]\n'],
    ids=["nothing", "garbage", "not-ready", "not-a-table"],
)
def test_a_session_that_started_without_a_ready_line_is_stopped_again(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, printed: str
) -> None:
    lab.plan("start", stdout=printed)  # the VM comes up, the line does not
    with pytest.raises(vm_dev_remote.DevWorkerError, match="vm start --dev on bentoo-lab"):
        _start(lab, iso)
    assert not (lab.sessions / "dev").exists(), "the session that did start was left running"
    assert _told(lab, "stop") == ["vm", "stop", "--name", "dev"]
    assert vm_dev_record.read_record("dev") is None


def test_a_worker_lost_while_it_boots_is_said_and_the_record_goes(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = remote.run

    def lost_at_boot(target: remote.Remote, command: str, **kw: Any) -> remote.RemoteResult:
        if " vm start " in command or " vm stop " in command:
            raise remote.RemoteUnreachable(target.name, target.address, "Connection reset")
        return real(target, command, **kw)

    monkeypatch.setattr(remote, "run", lost_at_boot)
    warnings: list[str] = []
    with pytest.raises(remote.RemoteUnreachable) as lost:
        _start(lab, iso, warnings=warnings)
    assert not isinstance(lost.value, vm_dev_remote.NoFirstAnswer)  # never tried elsewhere
    assert vm_dev_record.read_record("dev") is None
    assert len(warnings) == 1 and "may be left on bentoo-lab" in warnings[0]
    assert "ends itself at its maximum life" in warnings[0]


@pytest.mark.parametrize(
    "failure",
    [
        worker.JobRefused("git status failed in the Shidashi checkout /x (exit 128)", "run it"),
        remote.SyncError("runtime", 1, "the venv's interpreter /usr/bin does not exist"),
    ],
    ids=["no-checkout", "no-interpreter"],
)
def test_code_that_cannot_be_shipped_fails_the_start_before_the_iso_goes(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, failure: Exception
) -> None:
    ship.fails_with = failure
    with pytest.raises(vm_dev_remote.DevWorkerError, match="^bentoo-lab: "):
        _start(lab, iso)
    assert lab.rsync_calls() == [] and lab.shidashi_calls() == []
    assert vm_dev_record.read_record("dev") is None


def test_undoing_a_start_ignores_a_second_stop_and_restores_what_it_found(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a stop is rarely one signal: the undoing must not be cut short by the next one,
    # and a caller that installed no handler must get its own back
    before = [signal.getsignal(sig) for sig in stop_signals.STOP_SIGNALS]
    during: list[object] = []
    real = remote.run

    def looking(target: remote.Remote, command: str, **kw: Any) -> remote.RemoteResult:
        if " vm stop " in command:
            during.extend(signal.getsignal(sig) for sig in stop_signals.STOP_SIGNALS)
        return real(target, command, **kw)

    monkeypatch.setattr(remote, "run", looking)
    lab.plan("start", exit=1, stderr="error: no\n")
    with pytest.raises(vm_dev_remote.DevWorkerError):
        _start(lab, iso)
    assert during == [signal.SIG_IGN, signal.SIG_IGN]
    assert [signal.getsignal(sig) for sig in stop_signals.STOP_SIGNALS] == before


def test_the_workers_own_warnings_reach_the_caller(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    lab.plan("start", stderr="warning: /var/tmp/x was not searched for an ISO\n")
    warnings: list[str] = []
    _start(lab, iso, warnings=warnings)
    assert warnings == ["bentoo-lab: warning: /var/tmp/x was not searched for an ISO"]
