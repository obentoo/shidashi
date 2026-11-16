"""A dev VM started on a build worker (shidashi/vm_dev_remote.py).

The worker is a ``FakeWorkerShell`` (tests/_fake_worker_shell.py): the lines the relay
sends run in a local shell and a fake shipped Shidashi plays the worker's ``vm start``.
Shipping the code is stood in for by a ``FakeShipHead`` and the ISO's build.json is
handed over ready (tests/_dev_relay.py).

Here: what the worker is told (memory, life, processors, the tree it runs from), the
ISO it is sent and not sent again, the pins handed over, the record the host keeps and
the audit trail. What refuses or fails a start is tests/test_vm_dev_remote_refusals.py's;
the stop, tests/test_vm_dev_remote_stop.py's.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import (
    audit,
    iso_pins,
    vm,
    vm_dev,
    vm_dev_record,
    vm_dev_remote,
    vm_dev_remote_shell,
    worker,
)
from tests._dev_relay import (
    _ISO_NAME,
    _ON_WORKER,
    FakeShipHead,
    InMemoryTrail,
    _nothing_begun,
    _start,
    _told,
    _value,
    install_lab,
    make_iso,
)
from tests._fake_worker_shell import SHIPPED, FakeWorkerShell
from tests.test_vm_dev import _PINS, _info


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


# --- a start that works -------------------------------------------------------------------


def test_a_start_returns_the_workers_line_with_the_worker_added(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    line = _start(lab, iso)
    assert line == {
        "name": "dev",
        "cid": 43,
        "iso": str(lab.root) + _ON_WORKER,  # the worker's path: the fake shell's root
        "pin": "p20261101.28ad071a",
        "binhost": [],
        "staged": {"repo": "bentoo-staged", "path": "/var/db/repos/staged"},
        "ready": True,
        "expires": "2026-11-16T12:00:00Z",
        "worker": "bentoo-lab",
    }
    assert (lab.sessions / "dev" / "session.json").is_file()


def test_the_host_records_the_worker_the_commit_and_the_deadline(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso)
    record = vm_dev_record.read_record("dev")
    assert record is not None
    assert (record.worker, record.commit, record.expires) == (
        "bentoo-lab",
        SHIPPED,
        "2026-11-16T12:00:00Z",
    )
    assert record.started.endswith("Z") and len(record.started) == 20


def test_a_worker_gets_24g_twelve_hours_and_its_own_threads_unless_told(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso)
    args = _told(lab)
    assert args[:5] == ["vm", "start", "--dev", "--local", "--name"]
    assert _value(args, "--memory") == "24G"
    assert _value(args, "--max-life") == "12.0"
    assert _value(args, "--cpus") == "16"
    assert "--disk-size" not in args


def test_what_the_user_gave_is_what_the_worker_gets(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso, memory="16G", cpus=4, max_life=3, disk_size="80G")
    args = _told(lab)
    assert _value(args, "--memory") == "16G"
    assert _value(args, "--max-life") == "3.0"
    assert _value(args, "--cpus") == "4"
    assert _value(args, "--disk-size") == "80G"


@pytest.mark.parametrize("answer", ["", "many\n", "0\n"])
def test_a_thread_count_that_does_not_read_leaves_the_workers_default(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, answer: str
) -> None:
    lab.threads(answer)
    _start(lab, iso)
    assert "--cpus" not in _told(lab)


def test_the_shipped_shidashi_runs_from_its_tree_on_the_work_disk(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso)
    call = lab.shidashi_calls()[-1]
    work = str(lab.work)
    assert call["cwd"] == f"{work}/src/{SHIPPED}"
    assert call["env"] == {
        "SHIDASHI_CACHE": f"{work}/cache",
        "SHIDASHI_SCRATCH": f"{work}/scratch",
        "SHIDASHI_RUNS": f"{work}/out/runs",
        "SHIDASHI_SOURCE_COMMIT": SHIPPED,
        "SHIDASHI_DEV_RELAYED": "1",  # the end of the chain: it never relays again
        "PYTHONPATH": f"{work}/src/{SHIPPED}",
    }


def test_the_line_sent_is_the_one_the_builders_make(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso)
    pins = iso_pins.pins_block(iso_pins.read_iso_pins(_info(), repo=lab.base))
    expected = vm_dev_remote_shell.relayed_start_command(
        SHIPPED, "dev", iso=_ON_WORKER, pins=pins, memory="24G", cpus=16, max_life=12.0
    )
    assert lab.worker_commands()[-1] == expected


# --- the ISO ------------------------------------------------------------------------------


def test_the_iso_is_sent_once_and_reused_after(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, trail: InMemoryTrail
) -> None:
    _start(lab, iso, name="one")
    there = lab.root / _ON_WORKER.lstrip("/")
    assert there.read_bytes() == iso.read_bytes() and len(lab.rsync_calls()) == 1
    assert lab.rsync_calls()[0].endswith(f"{iso} root@bentoo-lab:{_ON_WORKER}")
    _start(lab, iso, name="two")
    assert len(lab.rsync_calls()) == 1, "the ISO already there was sent again"
    first, second = trail.of("step", "vm.dev.worker.iso")
    assert (first["action"], first["bytes_sent"], first["size"]) == ("sent", 21, 21)
    assert (second["action"], second["bytes_sent"], second["size"]) == ("reused", 0, 21)
    assert first["path"] == second["path"] == _ON_WORKER
    assert [e["iso_action"] for e in trail.of("vm.dev.worker.start")] == ["sent", "reused"]


@pytest.mark.parametrize("change", ["time", "size"])
def test_an_iso_that_changed_on_the_host_is_sent_again(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, change: str
) -> None:
    _start(lab, iso, name="one")
    if change == "time":
        os.utime(iso, (1_800_000_000, 1_800_000_000))
    else:
        before = iso.stat()
        iso.write_bytes(b"another ISO, of another size")
        os.utime(iso, (before.st_atime, before.st_mtime))
    _start(lab, iso, name="two")
    assert len(lab.rsync_calls()) == 2
    assert (lab.root / _ON_WORKER.lstrip("/")).read_bytes() == iso.read_bytes()


def test_an_iso_that_does_not_arrive_fails_the_start_and_leaves_nothing(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    lab.rsync_fails()
    with pytest.raises(vm_dev_remote.DevWorkerError, match="rsync exit 11.*No space left"):
        _start(lab, iso)
    assert lab.shidashi_calls() == [] and vm_dev_record.read_record("dev") is None


def test_an_iso_whose_name_cannot_cross_a_shell_is_refused_unsent(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    odd = iso.with_name("my iso; reboot.iso")
    iso.rename(odd)
    with pytest.raises(vm_dev_remote.DevWorkerError, match="rename the ISO"):
        _start(lab, odd)
    assert lab.rsync_calls() == [] and lab.shidashi_calls() == []


def test_without_an_iso_named_the_host_chooses_one_by_its_own_rule(
    lab: FakeWorkerShell, ship: FakeShipHead, tmp_path: Path
) -> None:
    releases = vm_dev.releases_dir()
    for date in ("2026.11.05", "2026.11.12"):
        newest = releases / date / f"bentoo-{date}-minimal-systemd-v3.iso"
        newest.parent.mkdir(parents=True)
        newest.write_bytes(date.encode())
    line = _start(lab, None)
    assert str(line["iso"]).endswith("/cache/dev-isos/bentoo-2026.11.12-minimal-systemd-v3.iso")
    assert lab.rsync_calls()[0].split()[-2] == str(newest)


def test_no_iso_to_boot_is_said_after_the_refusals_and_before_anything_is_shipped(
    lab: FakeWorkerShell, ship: FakeShipHead
) -> None:
    with pytest.raises(vm_dev.NoDevIsoError):
        _start(lab, None)
    assert len(lab.worker_commands()) == 2  # the jobs, the probe: the worker was asked
    _nothing_begun(lab, ship)


# --- the pins -----------------------------------------------------------------------------


def test_an_iso_that_names_its_pins_has_them_handed_over_as_they_are(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path
) -> None:
    _start(lab, iso)
    handed = json.loads(_value(_told(lab), "--pins-json"))
    own = iso_pins.read_iso_pins(_info(), repo=lab.base)
    assert handed == iso_pins.pins_block(own)
    assert iso_pins.handed_pins(handed).id == own.id


def test_an_older_iso_has_its_pins_resolved_on_the_host_with_git(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no pins block: only the commit that built it -- and the worker has no .git
    older = _info(repository={"commit": "f" * 40})
    del older["pins"]
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: older)
    resolved = iso_pins.handed_pins({k: v for k, v in _PINS.items() if k != "id"})
    asked: list[tuple[Any, Path]] = []

    def from_git(repository: Any, repo: Path) -> iso_pins.IsoPins:
        asked.append((repository, repo))
        return resolved

    monkeypatch.setattr(iso_pins, "_from_commit", from_git)
    _start(lab, iso)
    assert asked == [({"commit": "f" * 40}, lab.base)]  # the host's checkout was read
    handed = json.loads(_value(_told(lab), "--pins-json"))
    assert handed == iso_pins.pins_block(resolved) and handed["id"] == resolved.id


def test_pins_that_cannot_be_told_fail_the_start_before_anything_is_shipped(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: {"name": "old"})
    with pytest.raises(vm.VmError, match=f"{_ISO_NAME}: .*names neither its pins"):
        _start(lab, iso)
    _nothing_begun(lab, ship)


# --- the audit trail ------------------------------------------------------------------------


def test_the_audit_trail_names_each_step_of_a_start(
    lab: FakeWorkerShell, ship: FakeShipHead, iso: Path, trail: InMemoryTrail
) -> None:
    _start(lab, iso)
    assert [e["name"] for e in trail.of("step")] == [
        "vm.dev.worker.ship",
        "vm.dev.worker.iso",
        "vm.dev.worker.boot",
    ]
    (probe,) = trail.of("vm.dev.worker.probe")
    assert (probe["worker"], probe["threads"], probe["mounted"]) == ("bentoo-lab", 16, True)
    (shipped,) = trail.of("step", "vm.dev.worker.ship")
    assert (shipped["commit"], shipped["bytes_sent"]) == (SHIPPED, 4096)
    (sent,) = trail.of("command")
    assert sent["argv"][:2] == ["sh", "-c"] and sent["argv"][2] == lab.worker_commands()[-1]
    (started,) = trail.of("vm.dev.worker.start")
    assert started["worker"] == "bentoo-lab" and started["session"] == "dev"
    assert started["commit"] == SHIPPED and started["iso"] == _ON_WORKER
    assert started["pin"] == iso_pins.read_iso_pins(_info(), repo=lab.base).id
    assert started["expires"] == "2026-11-16T12:00:00Z" and started["duration_s"] >= 0
