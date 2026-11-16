"""What the tests of the dev VM relay share: the stand-ins that are not the worker's
shell (tests/_fake_worker_shell.py is that one) and the few calls every test makes.

- ``FakeShipHead`` stands in for ``worker.ship_head``: no git, no venv -- the tree is
  "there". ``worker.ship_head`` itself is checked in tests/test_vm_dev_evict.py;
- ``InMemoryTrail`` stands in for an audit run and keeps the events it is given;
- :func:`install_lab` is a ``FakeWorkerShell`` whose ISO has a build.json to read.
"""

import contextlib
import subprocess
import time
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, remote, vm, vm_dev_record, vm_dev_remote, vm_dev_remote_shell
from tests._fake_worker_shell import SHIPPED, FakeWorkerShell
from tests.test_vm_dev import _info

_ISO_NAME = "bentoo-2026.11.12-minimal-systemd-v3.iso"
_ON_WORKER = f"/mnt/work/cache/dev-isos/{_ISO_NAME}"


class FakeShipHead:
    """Stands in for ``worker.ship_head``: no git, no venv; the tree is "there"."""

    def __init__(self, shell: FakeWorkerShell, commit: str = SHIPPED) -> None:
        self.shell, self.commit = shell, commit
        self.calls = 0
        self.fails_with: Exception | None = None

    def __call__(self, target: remote.Remote, *, bwlimit: int | None = None) -> tuple[str, int]:
        self.calls += 1
        if self.fails_with is not None:
            raise self.fails_with
        self.shell.shipped_tree(self.commit)
        return self.commit, 4096


class InMemoryTrail(audit.Recorder):
    """Stands in for an audit run: keeps the events instead of writing them."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append({"kind": kind, **fields})

    @contextlib.contextmanager
    def step(self, name: str, **fields: Any) -> Generator[audit.Step]:
        handle = audit.Step(name)
        try:
            yield handle
        finally:
            self.events.append({"kind": "step", "name": name, **fields, **handle.fields})

    def command(self, argv: Sequence[str], **fields: Any) -> None:
        self.events.append({"kind": "command", "argv": list(argv), **fields})

    def of(self, kind: str, name: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.events if e["kind"] == kind and name in (None, e.get("name"))]


def install_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeWorkerShell:
    """A ``FakeWorkerShell``, and an ISO's build.json handed over ready (no xorriso)."""
    shell = FakeWorkerShell.install(tmp_path, monkeypatch)
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: _info())
    return shell


def make_iso(tmp_path: Path) -> Path:
    path = tmp_path / "isos" / _ISO_NAME
    path.parent.mkdir()
    path.write_bytes(b"an ISO, 21 bytes long")
    return path


def _start(
    lab: FakeWorkerShell, iso: Path | None, name: str = "dev", **asked: Any
) -> dict[str, Any]:
    """Start session ``name`` on the stand-in worker; ``warnings`` collects what a
    person would be told."""
    warnings = asked.pop("warnings", [])
    request = vm_dev_remote.StartRequest(name=name, iso=iso, **asked)
    return vm_dev_remote.start_on_worker(lab.remote(), request, repo=lab.base, warn=warnings.append)


def _told(lab: FakeWorkerShell, command: str = "start") -> list[str]:
    """The arguments the worker's last ``vm <command>`` got."""
    calls = [call["args"] for call in lab.shidashi_calls() if call["args"][1] == command]
    return list(calls[-1])


def _value(args: list[str], option: str) -> str:
    return args[args.index(option) + 1]


def _nothing_begun(lab: FakeWorkerShell, ship: FakeShipHead, name: str = "dev") -> None:
    """Nothing slow happened and nothing lasting was left, on either machine."""
    assert ship.calls == 0 and lab.rsync_calls() == []
    assert lab.shidashi_calls() == []
    assert vm_dev_record.read_record(name) is None
    assert not (lab.sessions / name).exists()


def _recorded(name: str = "dev") -> vm_dev_record.WorkerSession:
    record = vm_dev_record.read_record(name)
    assert record is not None
    return record


def _wait_for(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "never happened"
        time.sleep(0.05)


def _stop_group(lab: FakeWorkerShell, name: str) -> None:
    """End a start left running on the stand-in worker by a test."""
    pidfile = vm_dev_remote_shell.start_pidfile(name)
    script = vm_dev_remote_shell.stop_group_script(pidfile, wait_s=10)
    subprocess.run(remote.ssh_argv(lab.remote(), script), check=False, timeout=60)
