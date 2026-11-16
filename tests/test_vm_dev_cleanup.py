"""A dev VM leaves nothing behind: its work disk goes with ``vm stop`` and with a later start.

bentoolkit names each build VM afresh (``bentoo-build-<pid>-<n>``), so every stopped
session kept a work disk full of its builds in ``$XDG_STATE_HOME/shidashi/vm`` -- two had
piled up by 2026-11-14. The disk is created anew on every start anyway, so nothing on it
is worth keeping. What is never removed: a session without the dev mark (its ``--disk``
is the user's, a builder's 200 GB disk among them) and a dev session whose QEMU runs.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from shidashi import tree, vm, vm_dev
from shidashi.cli import app


def _dead_pid() -> int:
    """The pid of a process that has ended (and been collected)."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def _session(root: Path, name: str, *, dev: bool | None, pid: int | None) -> Path:
    """A session directory as ``vm start`` leaves it; ``dev`` None writes no mark at all."""
    directory = root / name
    directory.mkdir(parents=True)
    data: dict[str, Any] = {
        "iso": "/b.iso",
        "uefi": False,
        "cid": 42,
        "memory": "8G",
        "cpus": 8,
        "disks": [str(directory / "work.qcow2")],
    }
    if dev is not None:
        data["dev"] = dev
    (directory / "session.json").write_text(json.dumps(data))
    (directory / "work.qcow2").write_bytes(b"builds")
    if pid is not None:
        (directory / "qemu.pid").write_text(f"{pid}\n")
    return directory


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's session root, the one ``vm start --dev`` uses with no scratch named."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    root = tmp_path / "state" / "shidashi" / "vm"
    root.mkdir(parents=True)
    return root


# --- the dev mark ------------------------------------------------------------------------


def test_the_dev_mark_survives_the_session_file(state: Path) -> None:
    _session(state, "dev", dev=True, pid=None)
    _session(state, "plain", dev=None, pid=None)
    assert vm.load_session("dev").spec.dev is True
    assert vm.load_session("plain").spec.dev is False  # sessions written before the mark


def test_a_dev_start_marks_its_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_vm_dev import FakeDevSession, FakeTrees, _info, _iso

    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_CID_LOCKS", str(tmp_path / "cids"))
    (tmp_path / "cids").mkdir(mode=0o700)
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: _info())
    monkeypatch.setattr(vm, "ensure_disk", lambda path, size: path)
    trees = FakeTrees(tmp_path / "repos")
    monkeypatch.setattr(tree, "ensure_tree", trees.gentoo)
    monkeypatch.setattr(tree, "ensure_overlay", trees.overlay)
    FakeDevSession.instances.clear()
    vm_dev.start_dev(
        name="dev",
        iso=_iso(tmp_path / "cache" / "releases", "2026.11.12"),
        init="systemd",
        arch="v3",
        directory=tmp_path / "sessions" / "dev",
        repo=tmp_path,
        session_factory=cast(Any, FakeDevSession),
    )
    assert FakeDevSession.instances[0].spec.dev is True


# --- vm stop ---------------------------------------------------------------------------------


def test_stopping_a_dev_session_removes_its_directory_and_disk(state: Path) -> None:
    directory = _session(state, "dev", dev=True, pid=_dead_pid())
    result = CliRunner().invoke(app, ["vm", "stop", "--name", "dev"])
    assert result.exit_code == 0, result.output
    assert not directory.exists()


def test_stopping_a_session_without_the_dev_mark_keeps_its_disk(state: Path) -> None:
    directory = _session(state, "builder", dev=None, pid=_dead_pid())
    result = CliRunner().invoke(app, ["vm", "stop", "--name", "builder"])
    assert result.exit_code == 0, result.output
    assert (directory / "work.qcow2").read_bytes() == b"builds"


def test_a_dev_session_whose_qemu_will_not_end_is_kept(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # removing the disk under a running QEMU would free nothing and break the guest
    directory = _session(state, "dev", dev=True, pid=os.getpid())
    monkeypatch.setattr(vm_dev, "_QEMU_EXIT_TIMEOUT", 0.2)
    monkeypatch.setattr(os, "kill", lambda _pid, _sig: None)  # nothing dies here
    session = vm.load_session("dev")
    with pytest.raises(vm.VmError, match="still running"):
        vm_dev.discard_dev_session(session)
    assert (directory / "work.qcow2").is_file()


def test_a_qemu_that_ended_unreaped_is_gone_and_its_session_removed(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: under an init that never reaps (PID 1 of the container ``act`` starts),
    an ended QEMU stays a zombie and ``kill -0`` still answers for it. It has ended:
    the stop must remove the session, not wait for it and refuse (2026-11-16)."""
    qemu = subprocess.Popen(["true"])  # not waited for until the end: it stays a zombie
    try:
        while not Path(f"/proc/{qemu.pid}/stat").read_text().rpartition(") ")[2].startswith("Z"):
            pass
        directory = _session(state, "dev", dev=True, pid=qemu.pid)
        monkeypatch.setattr(vm_dev, "_QEMU_EXIT_TIMEOUT", 0.5)
        vm_dev.discard_dev_session(vm.load_session("dev"))
        assert not directory.exists()
    finally:
        qemu.wait()


def test_discard_refuses_a_session_without_the_dev_mark(state: Path) -> None:
    directory = _session(state, "builder", dev=None, pid=None)
    vm_dev.discard_dev_session(vm.load_session("builder"))
    assert (directory / "work.qcow2").is_file()


# --- the sweep at start ------------------------------------------------------------------------


def test_a_start_sweeps_only_dead_dev_sessions(state: Path) -> None:
    dead = _session(state, "bentoo-build-1-1", dev=True, pid=_dead_pid())
    crashed = _session(state, "bentoo-build-2-1", dev=True, pid=None)  # QEMU never wrote one
    alive = _session(state, "bentoo-build-3-1", dev=True, pid=os.getpid())
    builder = _session(state, "builder", dev=None, pid=_dead_pid())
    unmarked = _session(state, "old", dev=False, pid=_dead_pid())
    (state / "stray-file").write_text("not a session")
    (state / "no-json").mkdir()

    removed = vm_dev.sweep_dead_dev_sessions(state, keep="bentoo-build-9-1")
    assert sorted(removed) == ["bentoo-build-1-1", "bentoo-build-2-1"]
    assert not dead.exists() and not crashed.exists()
    assert alive.is_dir() and builder.is_dir() and unmarked.is_dir()
    assert (state / "stray-file").is_file() and (state / "no-json").is_dir()


def test_the_sweep_never_touches_the_session_being_started(state: Path) -> None:
    mine = _session(state, "dev", dev=True, pid=_dead_pid())
    assert vm_dev.sweep_dead_dev_sessions(state, keep="dev") == []
    assert mine.is_dir()


def test_the_sweep_of_a_missing_root_is_nothing(tmp_path: Path) -> None:
    assert vm_dev.sweep_dead_dev_sessions(tmp_path / "absent", keep="dev") == []


def test_a_start_runs_the_sweep_in_its_own_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    swept: list[tuple[Path, str]] = []

    def record(root: Path, *, keep: str) -> list[str]:
        swept.append((root, keep))
        return []

    monkeypatch.setattr(vm_dev, "sweep_dead_dev_sessions", record)
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: {"name": "old"})
    with pytest.raises(vm.VmError):  # it stops at the pins; the sweep came first
        vm_dev.start_dev(
            name="dev",
            iso=tmp_path / "x.iso",
            init="systemd",
            arch="v3",
            directory=tmp_path / "sessions" / "dev",
            repo=tmp_path,
        )
    assert swept == [(tmp_path / "sessions", "dev")]
