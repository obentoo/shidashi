"""Tests of shidashi.vm_dev -- ``shidashi vm start --dev``, the host side.

QEMU, the trees and the ISO's build.json are stood in for: a ``FakeDevSession`` records
start/run/stop instead of booting, ``read_build_info`` returns a build.json, and the
trees come from a ``FakeTrees``. What is checked is the plan the host makes and the
JSON bentoolkit reads.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import config, iso_pins, tree, vm, vm_dev, vm_dev_guest
from shidashi.cli import app

_COMMIT = "1d98e5f4fc41ae67431e674b28b4acc238fac1f1"
_PINS = {
    "id": "p20261101.28ad071a",
    "gentoo": {
        "date": "20261101",
        "base_url": "https://distfiles.gentoo.org/snapshots",
        "sha512": "ab" * 64,
    },
    "overlays": {"bentoo": {"url": "https://github.com/obentoo/bentoo.git", "commit": _COMMIT}},
}


def _info(**extra: Any) -> dict[str, Any]:
    return {
        "name": "bentoo-2026.11.12-minimal-systemd-v3",
        "flavor": "minimal",
        "init": "systemd",
        "arch": "v3",
        "stage3": {"snapshot": "20260823T153057Z"},
        "pins": _PINS,
        **extra,
    }


def _iso(releases: Path, date: str, init: str = "systemd", arch: str = "v3") -> Path:
    path = releases / date / f"bentoo-{date}-minimal-{init}-{arch}.iso"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"iso")
    return path


# --- choosing the ISO --------------------------------------------------------------------


def test_the_newest_minimal_iso_of_the_init_and_arch_is_chosen(tmp_path: Path) -> None:
    _iso(tmp_path, "2026.11.10")
    newest = _iso(tmp_path, "2026.11.12")
    _iso(tmp_path, "2026.11.13", init="openrc")
    _iso(tmp_path, "2026.11.14", arch="znver5")
    (tmp_path / "2026.11.15").mkdir()
    (tmp_path / "2026.11.15" / "bentoo-2026.11.15-kde-systemd-v3.iso").write_bytes(b"")
    assert vm_dev.find_dev_iso(tmp_path, init="systemd", arch="v3") == newest


@pytest.mark.parametrize("make", [False, True], ids=["no-releases-dir", "no-match"])
def test_no_minimal_iso_raises_and_says_how_to_make_one(tmp_path: Path, make: bool) -> None:
    if make:
        _iso(tmp_path, "2026.11.12", init="openrc")
    with pytest.raises(vm_dev.NoDevIsoError) as caught:
        vm_dev.find_dev_iso(tmp_path, init="systemd", arch="v3")
    assert str(tmp_path) in str(caught.value)
    assert "shidashi build v3 minimal systemd" in str(caught.value)


# --- the local pool ----------------------------------------------------------------------


def test_the_local_pool_is_the_generation_pkgdir_with_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path))
    pkgdir = config.pkgdir("v3", "20260823T153057Z")
    assert vm_dev.local_pool(_info()) is None  # no PKGDIR yet
    pkgdir.mkdir(parents=True)
    assert vm_dev.local_pool(_info()) is None  # a PKGDIR without a Packages index
    (pkgdir / "Packages").write_text("")
    assert vm_dev.local_pool(_info()) == pkgdir


def test_shares_mount_each_tree_and_the_pool_read_only(tmp_path: Path) -> None:
    trees = {"gentoo": tmp_path / "g", "bentoo": tmp_path / "b"}
    shares = vm_dev.dev_shares(trees, tmp_path / "pool")
    assert shares == (
        vm.Share("repo-gentoo", tmp_path / "g"),
        vm.Share("repo-bentoo", tmp_path / "b"),
        vm.Share(vm_dev_guest.POOL_TAG, tmp_path / "pool"),
    )
    assert vm_dev.dev_shares(trees, None)[-1].tag == "repo-bentoo"


# --- start ---------------------------------------------------------------------------------


class FakeTrees:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.asked: list[str] = []

    def gentoo(self, pin: tree.TreePin, **_k: object) -> Path:
        self.asked.append(f"gentoo-{pin.date}")
        return self.root / f"gentoo-{pin.date}"

    def overlay(self, pin: tree.OverlayPin, **_k: object) -> Path:
        self.asked.append(f"{pin.name}-{pin.commit[:12]}")
        return self.root / f"{pin.name}-{pin.commit[:12]}"


class FakeDevSession:
    instances: list[FakeDevSession] = []

    def __init__(self, spec: vm.VmSpec, directory: Path, *, setup_rc: int = 0) -> None:
        self.spec = spec
        self.directory = directory
        self.events: list[str] = []
        self.setup_rc = setup_rc
        FakeDevSession.instances.append(self)

    def start(self, *, cid_lock: Path | None = None) -> None:
        self.events.append(f"start:{cid_lock is not None}")

    def wait_ssh(self, **_k: object) -> float:
        self.events.append("ssh")
        return 20.0

    def run_command(self, command: str, *, timeout: float | None = 600) -> vm.GuestResult:
        self.events.append("setup")
        self.script = command
        out = "shidashi-dev: binhost none file:///mnt/work/empty-pool\n"
        return vm.GuestResult(command, self.setup_rc, out, "boom" if self.setup_rc else "", 1.0)

    def stop(self) -> None:
        self.events.append("stop")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTrees:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_CID_LOCKS", str(tmp_path / "cids"))
    (tmp_path / "cids").mkdir(mode=0o700)
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: _info())
    monkeypatch.setattr(vm, "ensure_disk", lambda path, size: path)
    trees = FakeTrees(tmp_path / "cache" / "repos")
    monkeypatch.setattr(tree, "ensure_tree", trees.gentoo)
    monkeypatch.setattr(tree, "ensure_overlay", trees.overlay)
    FakeDevSession.instances.clear()
    return trees


def _start(tmp_path: Path, **kw: Any) -> vm_dev.DevVm:
    iso = _iso(tmp_path / "cache" / "releases", "2026.11.12")
    defaults: dict[str, Any] = {
        "name": "dev",
        "iso": iso,
        "init": "systemd",
        "arch": "v3",
        "directory": tmp_path / "session",
        "session_factory": FakeDevSession,
        "repo": tmp_path,
    }
    return vm_dev.start_dev(**{**defaults, **kw})


def test_start_boots_the_iso_with_its_own_trees_and_reports_the_vm(
    tmp_path: Path, host: FakeTrees
) -> None:
    dev = _start(tmp_path)
    (session,) = FakeDevSession.instances
    assert session.events == ["start:True", "ssh", "setup"]
    assert host.asked == ["gentoo-20261101", f"bentoo-{_COMMIT[:12]}"]
    assert [s.tag for s in session.spec.shares] == ["repo-gentoo", "repo-bentoo"]
    assert session.spec.disks == (tmp_path / "session" / "work.qcow2",)
    assert session.script == vm_dev_guest.setup_script(("gentoo", "bentoo"), local_pool=False)
    assert dev.as_json() == {
        "name": "dev",
        "cid": session.spec.cid,
        "iso": str(tmp_path / "cache/releases/2026.11.12/bentoo-2026.11.12-minimal-systemd-v3.iso"),
        "pin": iso_pins.read_iso_pins(_info(), repo=tmp_path).id,
        "binhost": [{"name": "none", "uri": "file:///mnt/work/empty-pool"}],
        "staged": {"repo": vm_dev_guest.STAGED_REPO, "path": vm_dev_guest.STAGED_DIR},
        "ready": True,
    }


def test_the_work_disk_is_new_on_every_start(tmp_path: Path, host: FakeTrees) -> None:
    disk = tmp_path / "session" / "work.qcow2"
    disk.parent.mkdir(parents=True)
    disk.write_text("leftovers of the last session")
    _start(tmp_path)
    assert not disk.exists()  # removed; ensure_disk (faked here) creates the new one


def test_a_failed_setup_powers_the_vm_off_and_says_why(
    tmp_path: Path, host: FakeTrees, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(spec: vm.VmSpec, directory: Path) -> FakeDevSession:
        return FakeDevSession(spec, directory, setup_rc=1)

    with pytest.raises(vm.VmError, match="boom"):
        _start(tmp_path, session_factory=failing)
    assert FakeDevSession.instances[0].events[-1] == "stop"


def test_a_running_session_of_the_same_name_is_refused(tmp_path: Path, host: FakeTrees) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "session.json").write_text("{}")
    (session / "qemu.pid").write_text(f"{os.getpid()}\n")  # a live pid
    with pytest.raises(vm.VmError, match="already running"):
        _start(tmp_path)
    assert FakeDevSession.instances == []


def test_an_iso_whose_pins_cannot_be_told_is_refused(
    tmp_path: Path, host: FakeTrees, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: {"name": "old"})
    with pytest.raises(vm.VmError, match="pins"):
        _start(tmp_path)
    assert FakeDevSession.instances == []


# --- where the session lives --------------------------------------------------------------


def test_a_dev_session_found_in_the_user_state_dir_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = vm.user_session_dir("dev")
    assert directory == tmp_path / "state" / "shidashi" / "vm" / "dev"
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(
        json.dumps({"iso": "/b.iso", "uefi": False, "cid": 43, "memory": "8G", "cpus": 8})
    )
    assert vm.load_session("dev").directory == directory


# --- the CLI -------------------------------------------------------------------------------


def test_cli_without_a_minimal_iso_exits_with_its_own_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    result = CliRunner().invoke(app, ["vm", "start", "--dev"])
    assert result.exit_code == vm_dev.NO_ISO_EXIT == 3
    assert "no minimal ISO" in result.output


def test_cli_prints_the_vm_as_one_json_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_start(**kw: Any) -> vm_dev.DevVm:
        captured.update(kw)
        return vm_dev.DevVm("dev", 43, Path("/i.iso"), "p1", [], Path("/s"))

    monkeypatch.setattr(vm_dev, "start_dev", fake_start)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SHIDASHI_SCRATCH", raising=False)
    result = CliRunner().invoke(app, ["vm", "start", "--dev", "--name", "dev"])
    assert result.exit_code == 0, result.output
    line = result.stdout.strip().splitlines()[-1]
    assert json.loads(line)["ready"] is True and json.loads(line)["cid"] == 43
    assert captured["directory"] == tmp_path / "state" / "shidashi" / "vm" / "dev"


def test_cli_dev_without_work_dir_never_moves_the_tree_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the trees are hundreds of MB: a dev VM reads them from the shared cache
    seen: dict[str, Any] = {}

    def fake_start(**_kw: Any) -> vm_dev.DevVm:
        seen["cache"] = config.cache_dir()
        return vm_dev.DevVm("dev", 43, Path("/i.iso"), "p1", [], Path("/s"))

    monkeypatch.setattr(vm_dev, "start_dev", fake_start)
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    CliRunner().invoke(app, ["vm", "start", "--dev"])
    assert seen["cache"] == tmp_path / "cache"


def test_cli_without_dev_still_needs_an_iso() -> None:
    result = CliRunner().invoke(app, ["vm", "start"])
    assert result.exit_code != 0
    assert "ISO" in result.output
