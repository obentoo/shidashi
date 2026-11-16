"""A dev session on a build worker, stopped (shidashi/vm_dev_remote.py).

A stop ends the session there and forgets it here; a session the worker no longer has
is forgotten without an error; a worker that does not answer, or whose ``vm stop``
fails, keeps the record -- the session may still be there. And a stop that arrives
while the worker is still starting the VM ends that start, so no VM comes up after it.
The stand-ins are tests/test_vm_dev_remote.py's.
"""

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import audit, remote, vm_dev_record, vm_dev_remote, vm_dev_remote_shell, worker
from tests._dev_relay import (
    FakeShipHead,
    InMemoryTrail,
    _recorded,
    _start,
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


# --- the stop -------------------------------------------------------------------------------


def test_a_stop_ends_the_session_on_the_worker_and_forgets_it(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, trail: InMemoryTrail
) -> None:
    _start(lab, iso)
    qemu = int((lab.sessions / "dev" / "qemu.pid").read_text())
    assert vm_dev_remote.stop_on_worker(lab.remote(), _recorded()) is True
    assert not (lab.sessions / "dev").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(qemu, 0)
    assert vm_dev_record.read_record("dev") is None
    assert not vm_dev_record.record_path("dev").parent.exists()
    (stopped,) = trail.of("vm.dev.worker.stop")
    assert (stopped["outcome"], stopped["commit"], stopped["exit_code"]) == ("stopped", SHIPPED, 0)


def test_a_stop_runs_the_tree_recorded_with_the_session_not_the_checkouts_head(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso)
    ship.commit = "7" * 40  # the checkout moved on since the start
    vm_dev_remote.stop_on_worker(lab.remote(), _recorded())
    assert lab.shidashi_calls()[-1]["cwd"].endswith(f"/src/{SHIPPED}")
    assert ship.calls == 1  # a stop ships nothing


def test_a_session_the_worker_no_longer_has_is_forgotten_without_an_error(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, trail: InMemoryTrail
) -> None:
    _start(lab, iso)
    record = _recorded()
    lab.close()
    shutil.rmtree(lab.sessions / "dev")  # expired: the watchdog removed the directory
    calls = len(lab.shidashi_calls())
    assert vm_dev_remote.stop_on_worker(lab.remote(), record) is False
    assert len(lab.shidashi_calls()) == calls  # no `vm stop` was run for nothing
    assert vm_dev_record.read_record("dev") is None
    assert trail.of("vm.dev.worker.stop")[0]["outcome"] == "gone"


def test_a_worker_that_does_not_answer_a_stop_keeps_the_record(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso)
    record = _recorded()
    lab.unreachable()
    with pytest.raises(remote.RemoteUnreachable):
        vm_dev_remote.stop_on_worker(lab.remote(), record)
    assert vm_dev_record.read_record("dev") == record  # the session may still be there
    lab.unreachable(False)
    assert vm_dev_remote.stop_on_worker(lab.remote(), record) is True


def test_a_stop_that_fails_on_the_worker_keeps_the_record_and_says_why(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, trail: InMemoryTrail
) -> None:
    _start(lab, iso)
    record = _recorded()
    lab.plan("stop", exit=1, stderr="error: QEMU (pid 7) is still running: /s was kept\n")
    with pytest.raises(vm_dev_remote.DevWorkerError) as failed:
        vm_dev_remote.stop_on_worker(lab.remote(), record)
    assert "vm stop failed on bentoo-lab (exit 1)" in str(failed.value)
    assert "QEMU (pid 7) is still running" in str(failed.value)
    assert vm_dev_record.read_record("dev") == record
    assert trail.of("vm.dev.worker.stop")[0]["outcome"] == "failed"


def test_a_stop_ends_a_start_still_under_way_so_no_vm_comes_up_after_it(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    # the host's `vm start` was killed outright; the worker is still booting
    lab.plan("start", hold_s=3)
    lab.shipped_tree()
    line = vm_dev_remote_shell.relayed_start_command(
        SHIPPED, "dev", iso="/i.iso", pins=_PINS, memory="8G", cpus=1, max_life=1
    )
    orphan = subprocess.Popen(remote.ssh_argv(lab.remote(), line), stdin=subprocess.DEVNULL)
    _wait_for(lambda: bool(lab.shidashi_calls()))
    record = vm_dev_record.WorkerSession("dev", "bentoo-lab", SHIPPED, "2026-11-16T00:00:00Z")
    vm_dev_record.write_record(record)
    assert vm_dev_remote.stop_on_worker(lab.remote(), record) is False
    orphan.wait(timeout=20)
    time.sleep(3.5)  # past the moment the start would have brought its VM up
    assert not (lab.sessions / "dev").exists(), "a VM came up after its stop"
    assert not Path(str(lab.root) + vm_dev_remote_shell.start_pidfile("dev")).exists()
    assert vm_dev_record.read_record("dev") is None
