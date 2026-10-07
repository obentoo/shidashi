"""Tests of the worker's disk preparation (variants/worker/rootfs/usr/local/lib/shidashi/
worker_disk.py), imported from the rootfs as the image runs it. No real disk is ever
touched: ``plan`` reads ``lsblk -J -O`` fixtures, ``apply`` runs through a recording
runner, and the partition node and the clock are faked.

Sections: ``plan`` (task 5.1) and ``apply`` (task 5.2).

The worker fixture reconstructs the 2026-10-05 layout of bentoo-lab: a Netac SATA SSD
(the work disk to be) and the USB stick it boots from, whose live partition is
mounted. Replace it with the recorded ``lsblk -J -O`` capture when one is kept.

Requirements exercised: R6.1, R6.2, R6.3, R6.4, R6.5, R6.6.
"""

import copy
import dataclasses
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


WORKER_LIB = _repo_root() / "variants/worker/rootfs/usr/local/lib/shidashi"

NETAC_SERIAL = "AA000000000000000412"
STICK_SERIAL = "4C530001230915104193"
LABEL = "SHIDASHI-WORK"


def _dev(name: str, kind: str, **over: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "name": name,
        "kname": name,
        "path": f"/dev/{name}",
        "type": kind,
        "serial": None,
        "model": None,
        "size": None,
        "label": None,
        "partlabel": None,
        "fstype": None,
        "mountpoint": None,
        "mountpoints": [None],  # util-linux's shape for "not mounted"
        "tran": None,
        "rm": False,
    }
    node.update(over)
    return node


def _worker_lsblk() -> dict[str, Any]:
    netac = _dev(
        "sda",
        "disk",
        serial=NETAC_SERIAL,
        model="Netac SSD 256GB",
        size="238.5G",
        tran="sata",
        children=[_dev("sda1", "part", size="238.5G", fstype="ntfs", label="Data")],
    )
    stick = _dev(
        "sdb",
        "disk",
        serial=STICK_SERIAL,
        model="Ultra Fit",
        size="57.3G",
        tran="usb",
        rm=True,
        children=[
            _dev(
                "sdb1",
                "part",
                size="2.1G",
                fstype="iso9660",
                label="BENTOO_WORKER",
                mountpoint="/run/initramfs/live",
                mountpoints=["/run/initramfs/live"],
            ),
            _dev("sdb2", "part", size="16M", fstype="vfat", label="EFI"),
        ],
    )
    zram = _dev("zram0", "disk", size="8G", mountpoint="[SWAP]", mountpoints=["[SWAP]"])
    return {"blockdevices": [netac, stick, zram]}


@pytest.fixture
def wd(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(WORKER_LIB))
    for name in ("worker_disk", "kyomei_worker", "kyomei_protocol"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("worker_disk")


_PROGRAMMING_ERRORS = (
    TypeError,
    AttributeError,
    NameError,
    KeyError,
    IndexError,
    AssertionError,
    ImportError,
)


def _refusal(fn: Any, *args: Any, **kwargs: Any) -> str:
    """The message of a deliberate refusal (an exception or exit 1); a programming
    error is re-raised so it never passes for one."""
    try:
        fn(*args, **kwargs)
    except _PROGRAMMING_ERRORS:
        raise
    except SystemExit as exit_:
        assert exit_.code not in (0, None), "exited 0: not a refusal"
        return str(exit_.code)
    except Exception as err:
        return str(err)
    raise AssertionError("no refusal")


def _fields(obj: Any) -> dict[str, Any]:
    """A plan's fields, whether a dataclass, a NamedTuple or a plain object."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())
    return dict(vars(obj))


def _disk(lsblk: dict[str, Any], name: str) -> dict[str, Any]:
    return next(d for d in lsblk["blockdevices"] if d["name"] == name)


# ===================================================================================
# plan -- task 5.1
# ===================================================================================

# Serial (R6.2) -- hostile halves first: a serial that only LOOKS like the disk's, or
# is another disk's, refuses; then the exact serial plans.


@pytest.mark.parametrize(
    "serial",
    [
        STICK_SERIAL,  # the other disk's serial
        NETAC_SERIAL[:-1],  # a prefix
        NETAC_SERIAL + "0",  # an extension
        NETAC_SERIAL[1:],  # a suffix
        "",
    ],
)
def test_plan_refuses_a_serial_that_is_not_exactly_the_disks(wd: Any, serial: str) -> None:
    message = _refusal(wd.plan, _worker_lsblk(), "/dev/sda", serial)
    assert "serial" in message.lower()


def test_plan_refuses_a_renamed_device_whose_serial_is_another_disks(wd: Any) -> None:
    """After a reboot the kernel names the stick sda: the Netac's serial no longer
    confirms /dev/sda, and nothing is planned on the stick."""
    lsblk = _worker_lsblk()
    netac, stick = _disk(lsblk, "sda"), _disk(lsblk, "sdb")
    for dev, name in ((netac, "sdb"), (stick, "sda")):
        dev["name"] = dev["kname"] = name
        dev["path"] = f"/dev/{name}"
        for i, child in enumerate(dev.get("children", []), start=1):
            child["name"] = child["kname"] = f"{name}{i}"
            child["path"] = f"/dev/{name}{i}"
            child["mountpoint"], child["mountpoints"] = None, [None]  # serial is the only guard
    message = _refusal(wd.plan, lsblk, "/dev/sda", NETAC_SERIAL)
    assert "serial" in message.lower()


def test_plan_of_the_confirmed_disk_carries_its_path_model_size_and_serial(wd: Any) -> None:
    plan = wd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    values = {str(v) for v in _fields(plan).values()}
    assert "/dev/sda" in values
    assert NETAC_SERIAL in values
    assert "Netac SSD 256GB" in values
    assert "238.5G" in values


def test_plan_does_not_take_a_partition_for_the_disk(wd: Any) -> None:
    lsblk = _worker_lsblk()
    _disk(lsblk, "sda")["children"][0]["serial"] = NETAC_SERIAL  # some lsblk inherit it
    _refusal(wd.plan, lsblk, "/dev/sda1", NETAC_SERIAL)


def test_plan_refuses_a_device_that_is_not_in_the_listing(wd: Any) -> None:
    _refusal(wd.plan, _worker_lsblk(), "/dev/sdz", NETAC_SERIAL)


# Mounted (R6.3) -- hostile halves first: lsblk's "not mounted" shapes are not mounts;
# then a mount anywhere under the disk refuses.


@pytest.mark.parametrize("shape", [[None], [], None])
def test_plan_does_not_read_lsblks_empty_mountpoints_as_mounted(wd: Any, shape: Any) -> None:
    lsblk = _worker_lsblk()
    for node in (_disk(lsblk, "sda"), *_disk(lsblk, "sda")["children"]):
        node["mountpoints"] = shape
        node["mountpoint"] = None
    assert wd.plan(lsblk, "/dev/sda", NETAC_SERIAL) is not None


def test_plan_refuses_the_disk_it_booted_from_its_live_partition_is_mounted(wd: Any) -> None:
    message = _refusal(wd.plan, _worker_lsblk(), "/dev/sdb", STICK_SERIAL)
    assert "mount" in message.lower() or "/run/initramfs/live" in message


@pytest.mark.parametrize("where", ["the disk", "a partition", "a nested device", "swap"])
def test_plan_refuses_a_disk_with_anything_mounted_on_it(wd: Any, where: str) -> None:
    lsblk = _worker_lsblk()
    netac = _disk(lsblk, "sda")
    if where == "the disk":
        netac["children"] = []
        netac["mountpoint"], netac["mountpoints"] = "/mnt/old", ["/mnt/old"]
    elif where == "a partition":
        part = netac["children"][0]
        part["mountpoint"], part["mountpoints"] = "/media/data", ["/media/data"]
    elif where == "a nested device":  # LUKS in the partition, its mapping mounted
        netac["children"][0]["children"] = [
            _dev(
                "luks-data",
                "crypt",
                path="/dev/mapper/luks-data",
                mountpoint="/srv",
                mountpoints=["/srv"],
            )
        ]
    else:
        part = netac["children"][0]
        part["mountpoint"], part["mountpoints"] = "[SWAP]", ["[SWAP]"]
    _refusal(wd.plan, lsblk, "/dev/sda", NETAC_SERIAL)


# The label (R6.4) -- hostile halves first: a label that only LOOKS like it is not it,
# and the label on the very disk being prepared is not "another disk"; then the label
# on another disk -- whole or in a partition -- refuses, naming that disk.


@pytest.mark.parametrize("label", ["SHIDASHI-WORK2", "SHIDASHI-WORK-OLD", "WORK"])
def test_plan_ignores_a_lookalike_label_on_another_disk(wd: Any, label: str) -> None:
    lsblk = _worker_lsblk()
    lsblk["blockdevices"].append(
        _dev("sdc", "disk", serial="X1", children=[_dev("sdc1", "part", label=label)])
    )
    assert wd.plan(lsblk, "/dev/sda", NETAC_SERIAL) is not None


def test_plan_allows_redoing_the_disk_that_already_carries_the_label(wd: Any) -> None:
    lsblk = _worker_lsblk()
    _disk(lsblk, "sda")["children"][0].update(label=LABEL, fstype="btrfs")
    assert wd.plan(lsblk, "/dev/sda", NETAC_SERIAL) is not None


@pytest.mark.parametrize("where", ["a partition", "the whole disk"])
def test_plan_refuses_when_another_disk_carries_the_label_and_names_it(wd: Any, where: str) -> None:
    lsblk = _worker_lsblk()
    if where == "a partition":
        other = _dev("sdc", "disk", serial="X1", children=[_dev("sdc1", "part", label=LABEL)])
    else:
        other = _dev("sdc", "disk", serial="X1", label=LABEL, fstype="btrfs")
    lsblk["blockdevices"].append(other)
    message = _refusal(wd.plan, lsblk, "/dev/sda", NETAC_SERIAL)
    assert "sdc" in message


def test_plan_does_not_modify_the_listing_it_reads(wd: Any) -> None:
    lsblk = _worker_lsblk()
    before = copy.deepcopy(lsblk)
    wd.plan(lsblk, "/dev/sda", NETAC_SERIAL)
    assert lsblk == before


# ===================================================================================
# apply -- task 5.2
# ===================================================================================


class _Clock:
    """A fake clock: sleeping advances it; a poll that never gives up is caught."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps = 0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += max(float(seconds), 0.01)
        if self.now - 1000.0 > 120 or self.sleeps > 5000:
            raise AssertionError("the partition-node poll never gave up")


class _DiskRunner:
    def __init__(self, *, table: bool = True, fail: str | None = None, partx_u: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.table, self.fail, self.partx_u = table, fail, partx_u
        self.partitioned = False

    def __call__(self, argv: Any, *_a: Any, **kw: Any) -> subprocess.CompletedProcess:
        assert isinstance(argv, (list, tuple)), "an argv list, never a shell string"
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        out, err, rc = "", "", 0
        tool = os.path.basename(argv[0])
        if tool == "sgdisk" and "-n" in argv:
            self.partitioned = self.table
        if tool == "sgdisk" and "-p" in argv:
            out = "Number  Start (sector)    End (sector)  Size       Code  Name\n"
            if self.table:
                out += "   1            2048       500118158   238.5 GiB   8300  SHIDASHI-WORK\n"
        elif tool == "partx" and "-u" in argv:
            rc = self.partx_u
        elif tool == "blkid":
            out = f"DEVNAME=/dev/sda1\nLABEL={LABEL}\nTYPE=btrfs\n"
        if self.fail and tool == self.fail:
            rc, err = 1, f"ERROR: {tool} failed on /dev/sda1: Device or resource busy\n"
        if kw.get("check") and rc:
            raise subprocess.CalledProcessError(rc, argv, out, err)
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        if text:
            return subprocess.CompletedProcess(argv, rc, out, err)
        return subprocess.CompletedProcess(argv, rc, out.encode(), err.encode())

    def index(self, predicate: Any) -> int:
        for i, call in enumerate(self.calls):
            if predicate(call):
                return i
        return -1

    def ran(self, tool: str) -> bool:
        return any(os.path.basename(c[0]) == tool for c in self.calls)


@pytest.fixture
def disk_env(tmp_path: Path, wd: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The clock, the partition node (it appears once the table has a partition, if
    ``node`` says so) and the worker's persist, all faked."""
    clock = _Clock()
    for name, fake in (("monotonic", clock.time), ("time", clock.time), ("sleep", clock.sleep)):
        monkeypatch.setattr(time, name, fake)
        monkeypatch.setattr(wd, name, fake, raising=False)
    state: dict[str, Any] = {"clock": clock, "node": True, "runner": None, "persisted": []}
    real_exists = os.path.exists

    def _exists(path: Any) -> bool:
        if str(path) == "/dev/sda1":
            runner = state["runner"]
            return bool(state["node"] and runner is not None and runner.partitioned)
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", _exists)
    monkeypatch.setattr(Path, "is_block_device", lambda self: _exists(self))

    kyomei_worker = importlib.import_module("kyomei_worker")

    def _persist(*args: Any, **kwargs: Any) -> None:
        runner = state["runner"]
        state["persisted"].append(
            {"args": args, "kwargs": kwargs, "after": len(runner.calls) if runner else 0}
        )

    monkeypatch.setattr(kyomei_worker, "persist", _persist)
    monkeypatch.setattr(wd, "persist", _persist, raising=False)
    root = tmp_path / "root"
    (root / "mnt" / "work").mkdir(parents=True)
    state["root"] = root
    return state


def _apply(wd: Any, env: dict[str, Any], runner: _DiskRunner) -> None:
    env["runner"] = runner
    plan = wd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    wd.apply(plan, runner=runner, root=env["root"])


def test_apply_partitions_formats_mounts_and_persists_in_order(
    wd: Any, disk_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _DiskRunner()
    _apply(wd, disk_env, runner)
    calls = runner.calls

    def is_tool(tool: str, *flags: str) -> Any:
        return lambda c: os.path.basename(c[0]) == tool and all(f in c for f in flags)

    new_table = runner.index(is_tool("sgdisk", "-o"))
    new_part = runner.index(is_tool("sgdisk", "-n"))
    reread = runner.index(is_tool("sgdisk", "-p"))
    mkfs = runner.index(is_tool("mkfs.btrfs"))
    mount = runner.index(is_tool("mount"))
    blkid = runner.index(is_tool("blkid"))
    # two separate sgdisk calls (one combined call left an EMPTY table), then a re-read
    assert 0 <= new_table < new_part < reread < mkfs < mount, calls
    assert "-n" not in calls[new_table] and "-o" not in calls[new_part]
    assert "/dev/sda" in calls[new_table] and "/dev/sda" in calls[new_part]
    # the filesystem: on the partition, labelled, without the whole-device TRIM
    mkfs_argv = calls[mkfs]
    assert "-K" in mkfs_argv or "--nodiscard" in mkfs_argv
    assert LABEL in mkfs_argv
    assert mkfs_argv[-1] == "/dev/sda1"
    assert "/dev/sda" not in mkfs_argv
    # mounted where the fstab expects it, the label read back after mkfs
    assert any(a.rstrip("/").endswith("mnt/work") for a in calls[mount])
    assert blkid > mkfs
    # the pairing persisted once, onto the mounted disk, requiring the mount (v2)
    assert len(disk_env["persisted"]) == 1
    assert disk_env["persisted"][0]["kwargs"].get("require_mount") is True
    assert disk_env["persisted"][0]["after"] > mount
    # only the confirmed disk is ever named
    for call in calls:
        for arg in call:
            if arg.startswith("/dev/"):
                assert arg in ("/dev/sda", "/dev/sda1"), call
    # R6.5: what it made
    out = capsys.readouterr().out
    for shown in ("/dev/sda1", LABEL, "238.5", "/mnt/work"):
        assert shown in out, out


def test_apply_falls_back_to_partx_add_when_update_fails(wd: Any, disk_env: dict[str, Any]) -> None:
    runner = _DiskRunner(partx_u=1)
    _apply(wd, disk_env, runner)
    update = runner.index(lambda c: os.path.basename(c[0]) == "partx" and "-u" in c)
    add = runner.index(lambda c: os.path.basename(c[0]) == "partx" and "-a" in c)
    assert 0 <= update < add


def test_apply_stops_before_mkfs_when_the_partition_node_never_appears(
    wd: Any, disk_env: dict[str, Any]
) -> None:
    disk_env["node"] = False
    runner = _DiskRunner()
    disk_env["runner"] = runner
    plan = wd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    message = _refusal(wd.apply, plan, runner=runner, root=disk_env["root"])
    assert "/dev/sda1" in message or "partition" in message.lower()
    assert not runner.ran("mkfs.btrfs")
    assert not runner.ran("mount")
    assert disk_env["persisted"] == []
    waited = disk_env["clock"].now - 1000.0
    assert 9.0 <= waited <= 30.0, f"waited {waited} s for the node, not about 10 s"


def test_apply_stops_before_mkfs_when_the_table_comes_back_empty(
    wd: Any, disk_env: dict[str, Any]
) -> None:
    runner = _DiskRunner(table=False)
    disk_env["runner"] = runner
    plan = wd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    _refusal(wd.apply, plan, runner=runner, root=disk_env["root"])
    assert not runner.ran("mkfs.btrfs")
    assert disk_env["persisted"] == []


def test_apply_surfaces_a_mkfs_failure_and_never_mounts(wd: Any, disk_env: dict[str, Any]) -> None:
    runner = _DiskRunner(fail="mkfs.btrfs")
    disk_env["runner"] = runner
    plan = wd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    message = _refusal(wd.apply, plan, runner=runner, root=disk_env["root"])
    assert "Device or resource busy" in message
    assert not runner.ran("mount")
    assert disk_env["persisted"] == []


# ===================================================================================
# v2 (review): a late failure unmounts (R6.7); the worker command's own main
# ===================================================================================


def _unmounted_after(runner: _DiskRunner) -> bool:
    mount = runner.index(lambda c: os.path.basename(c[0]) == "mount")
    umount = runner.index(lambda c: os.path.basename(c[0]) == "umount")
    return 0 <= mount < umount


def test_apply_unmounts_when_persisting_fails_after_the_mount(
    wd: Any, disk_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken_persist(*_a: Any, **_k: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(wd, "persist", _broken_persist, raising=False)
    monkeypatch.setattr(importlib.import_module("kyomei_worker"), "persist", _broken_persist)
    runner = _DiskRunner()
    disk_env["runner"] = runner
    plan = wd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    message = _refusal(wd.apply, plan, runner=runner, root=disk_env["root"])
    assert "persist" in message.lower() or "No space left" in message
    assert _unmounted_after(runner), runner.calls


def test_apply_unmounts_when_the_label_read_back_is_not_shidashi_work(
    wd: Any, disk_env: dict[str, Any]
) -> None:
    class _WrongLabel(_DiskRunner):
        def __call__(self, argv: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess:
            if os.path.basename(str(list(argv)[0])) == "blkid":
                self.calls.append([str(x) for x in argv])
                out = "DEVNAME=/dev/sda1\nLABEL=sandbox\nTYPE=btrfs\n"
                text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
                return subprocess.CompletedProcess(argv, 0, out if text else out.encode(), "")
            return super().__call__(argv, *a, **kw)

    runner = _WrongLabel()
    disk_env["runner"] = runner
    plan = wd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    message = _refusal(wd.apply, plan, runner=runner, root=disk_env["root"])
    assert "sandbox" in message or LABEL in message
    assert _unmounted_after(runner), runner.calls


def test_main_maps_a_refusal_to_exit_1_with_its_message(
    wd: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = json.dumps(_worker_lsblk())

    def _lsblk(argv: Any, *_a: Any, **kw: Any) -> subprocess.CompletedProcess:
        assert os.path.basename(str(list(argv)[0])) == "lsblk"
        return subprocess.CompletedProcess(argv, 0, listing, "")

    monkeypatch.setattr(subprocess, "run", _lsblk)
    monkeypatch.setattr(wd, "apply", lambda *_a, **_k: pytest.fail("refused, yet applied"))
    assert wd.main(["/dev/sda", "WRONG-SERIAL"]) == 1
    out, err = capsys.readouterr()
    assert "serial" in (out + err).lower()


def test_main_exits_1_with_lsblks_stderr_when_lsblk_fails(
    wd: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _broken(argv: Any, *_a: Any, **kw: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 32, "", "lsblk: failed to access sysfs\n")

    monkeypatch.setattr(subprocess, "run", _broken)
    assert wd.main(["/dev/sda", NETAC_SERIAL]) == 1
    out, err = capsys.readouterr()
    assert "failed to access sysfs" in out + err
