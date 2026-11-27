"""Tests of the worker's scratch disk (variants/worker/rootfs/usr/local/lib/shidashi/
worker_scratch_disk.py), imported from the rootfs as the image runs it. No real disk is
touched: ``plan`` reads the ``lsblk -J -O`` fixture of ``tests/test_worker_disk.py``,
``apply`` runs through a recording runner over a temporary root, and the mounts are faked.

The rule under test: the scratch disk only ever takes what the worker can lose, and the
content of the paths it takes over is moved -- removed from the work disk only once it
was copied.
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from shidashi import worker
from tests.test_worker_disk import NETAC_SERIAL, WORKER_LIB, _dev, _refusal, _worker_lsblk

FSTAB = WORKER_LIB.parents[3] / "etc" / "fstab"
DISPATCHER = WORKER_LIB.parents[1] / "bin" / "shidashi"
LABEL = "SHIDASHI-SCRATCH"


@pytest.fixture
def sd(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(WORKER_LIB))
    for name in ("worker_scratch_disk", "worker_disk", "kyomei_worker", "kyomei_protocol"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("worker_scratch_disk")


def _fstab_rows() -> list[list[str]]:
    lines = FSTAB.read_text(encoding="utf-8").splitlines()
    return [line.split() for line in lines if line.strip() and not line.lstrip().startswith("#")]


# --- the three copies of the scratch paths agree -------------------------------------


def test_the_fstab_mounts_each_scratch_subvolume_over_its_path_without_blocking_boot(
    sd: Any,
) -> None:
    rows = [r for r in _fstab_rows() if r[0] == f"LABEL={LABEL}"]
    found = {}
    for source, where, fstype, options, *_ in rows:
        opts = options.split(",")
        assert fstype == "btrfs" and "nofail" in opts, (source, where, options)
        assert "x-systemd.device-timeout=10s" in opts, where
        (subvol,) = [o.removeprefix("subvol=") for o in opts if o.startswith("subvol=")]
        found[subvol] = where
    assert found == {sv: f"/mnt/work/{path}" for sv, path in sd.SCRATCH_PATHS.items()}


def test_the_host_measures_free_space_by_the_same_scratch_paths(sd: Any) -> None:
    assert tuple(sd.SCRATCH_PATHS.values()) == worker.SCRATCH_PATHS


def test_hostile_no_scratch_path_holds_state_the_worker_cannot_lose(sd: Any) -> None:
    """The cache's state, locks, releases and fork points stay on the work disk."""
    keep = (
        "cache",
        "cache/state",
        "cache/locks",
        "cache/releases",
        "cache/fork-points",
        ".shidashi",
    )
    for path in sd.SCRATCH_PATHS.values():
        assert path not in keep and not any(k.startswith(path + "/") for k in keep), path


def test_the_dispatcher_sends_scratch_to_its_own_module() -> None:
    script = DISPATCHER.read_text(encoding="utf-8")
    assert "worker_scratch_disk.py" in script
    assert subprocess.run(["bash", "-n", str(DISPATCHER)], check=False).returncode == 0


# --- plan ----------------------------------------------------------------------------


def test_plan_refuses_when_another_disk_already_carries_the_scratch_label(sd: Any) -> None:
    lsblk = _worker_lsblk()
    lsblk["blockdevices"].append(
        _dev("sdc", "disk", serial="X1", children=[_dev("sdc1", "part", label=LABEL)])
    )
    assert "sdc" in _refusal(sd.plan, lsblk, "/dev/sda", NETAC_SERIAL)


def test_plan_does_not_take_the_work_disks_label_for_its_own(sd: Any) -> None:
    """A mounted SHIDASHI-WORK elsewhere is the normal case, not a second scratch disk."""
    lsblk = _worker_lsblk()
    lsblk["blockdevices"].append(
        _dev(
            "nvme0n1",
            "disk",
            serial="N1",
            children=[
                _dev(
                    "nvme0n1p1",
                    "part",
                    label="SHIDASHI-WORK",
                    fstype="btrfs",
                    mountpoint="/mnt/work",
                    mountpoints=["/mnt/work"],
                )
            ],
        )
    )
    assert sd.plan(lsblk, "/dev/sda", NETAC_SERIAL).path == "/dev/sda"


# --- apply ---------------------------------------------------------------------------


class _ScratchRunner:
    def __init__(self, *, jobs: str = "", fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.jobs, self.fail = jobs, fail
        self.partitioned = False

    def __call__(self, argv: Any, *_a: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, (list, tuple)), "an argv list, never a shell string"
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool, out, rc, err = os.path.basename(argv[0]), "", 0, ""
        if tool == "sgdisk" and "-n" in argv:
            self.partitioned = True
        elif tool == "sgdisk" and "-p" in argv:
            out = "   1            2048       976773134   465.8 GiB   8300  SHIDASHI-SCRATCH\n"
        elif tool == "systemctl":
            out = self.jobs
        elif tool == "blkid":
            out = f"DEVNAME=/dev/sda1\nLABEL={LABEL}\nTYPE=btrfs\n"
        if self.fail and tool == self.fail:
            rc, err = 1, f"{tool}: No space left on device\n"
        return subprocess.CompletedProcess(argv, rc, out, err)

    def tools(self) -> list[str]:
        return [os.path.basename(c[0]) for c in self.calls]

    def first(self, tool: str, *flags: str) -> int:
        return next(
            i
            for i, c in enumerate(self.calls)
            if os.path.basename(c[0]) == tool and all(f in c for f in flags)
        )


@pytest.fixture
def env(sd: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A root whose /mnt/work is 'mounted' and holds distfiles and a tmp; the partition
    node appears once partitioned."""
    root = tmp_path / "root"
    work = root / "mnt" / "work"
    (work / "cache" / "distfiles").mkdir(parents=True)
    (work / "cache" / "distfiles" / "big-1.0.tar.gz").write_text("src\n")
    (work / "tmp").mkdir()
    (work / "cache" / "state").mkdir()
    state: dict[str, Any] = {"root": root, "work": work, "mounts": {str(work)}, "runner": None}
    real_exists = os.path.exists

    def _exists(path: Any) -> bool:
        if str(path) == "/dev/sda1":
            return bool(state["runner"] and state["runner"].partitioned)
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", _exists)
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) in state["mounts"])
    return state


def _apply(sd: Any, env: dict[str, Any], runner: _ScratchRunner) -> None:
    env["runner"] = runner
    plan = sd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    sd.apply(plan, root=env["root"], runner=runner, sleep=lambda _s: None)


def test_apply_formats_fills_clears_and_mounts_in_order(
    sd: Any, env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _ScratchRunner()
    _apply(sd, env, runner)
    work, staging = env["work"], env["root"] / "run" / "shidashi-scratch"
    mkfs = runner.first("mkfs.btrfs")
    assert LABEL in runner.calls[mkfs] and runner.calls[mkfs][-1] == "/dev/sda1"
    stage = runner.first("mount", "subvolid=5")
    creates = [i for i, c in enumerate(runner.calls) if c[:3] == ["btrfs", "subvolume", "create"]]
    assert [runner.calls[i][3] for i in creates] == [str(staging / sv) for sv in sd.SCRATCH_PATHS]
    copy = runner.first("cp")
    assert runner.calls[copy][-2:] == [f"{work}/cache/distfiles/.", f"{staging}/@distfiles/"]
    assert runner.tools().count("cp") == 1  # the empty tmp and the missing paths: no copy
    unstage = runner.first("umount", str(staging))
    delete = runner.first("find")
    assert runner.calls[delete][1] == str(work / "cache" / "distfiles")
    assert runner.tools().count("find") == 1
    mounts = [
        c[1] for c in runner.calls if os.path.basename(c[0]) == "mount" and "subvolid=5" not in c
    ]
    assert mounts == [str(work / p) for p in sd.SCRATCH_PATHS.values()]
    first_mount = runner.first("mount", str(work / "scratch"))
    assert mkfs < stage < creates[0] < copy < unstage < delete < first_mount
    assert all((work / p).is_dir() for p in sd.SCRATCH_PATHS.values())
    out = capsys.readouterr().out
    assert "/dev/sda1" in out and LABEL in out and "cache/distfiles" in out


def test_hostile_a_failed_copy_unmounts_and_leaves_the_work_disk_untouched(
    sd: Any, env: dict[str, Any]
) -> None:
    runner = _ScratchRunner(fail="cp")
    env["runner"] = runner
    plan = sd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    message = _refusal(sd.apply, plan, root=env["root"], runner=runner, sleep=lambda _s: None)
    assert "untouched" in message and "No space left" in message
    assert "find" not in runner.tools()  # nothing deleted
    assert runner.calls[-1] == ["umount", str(env["root"] / "run" / "shidashi-scratch")]
    assert (env["work"] / "cache" / "distfiles" / "big-1.0.tar.gz").is_file()


def test_apply_refuses_before_any_write_without_the_work_disk(sd: Any, env: dict[str, Any]) -> None:
    env["mounts"].clear()
    runner = _ScratchRunner()
    env["runner"] = runner
    plan = sd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    assert "not mounted" in _refusal(sd.apply, plan, root=env["root"], runner=runner)
    assert runner.calls == []


def test_apply_refuses_before_any_write_while_a_job_runs(sd: Any, env: dict[str, Any]) -> None:
    runner = _ScratchRunner(jobs="shidashi-job-kde.service loaded active running kde\n")
    env["runner"] = runner
    plan = sd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    assert "shidashi-job-kde" in _refusal(sd.apply, plan, root=env["root"], runner=runner)
    assert runner.tools() == ["systemctl"]


def test_apply_refuses_a_scratch_path_that_is_already_a_mount(sd: Any, env: dict[str, Any]) -> None:
    """Redoing over a mounted scratch disk would copy it onto itself, then delete it."""
    env["mounts"].add(str(env["work"] / "tmp"))
    runner = _ScratchRunner()
    env["runner"] = runner
    plan = sd.plan(_worker_lsblk(), "/dev/sda", NETAC_SERIAL)
    assert "already a mount" in _refusal(sd.apply, plan, root=env["root"], runner=runner)
    assert runner.calls == []


def test_main_without_disk_and_serial_prints_the_scratch_usage(
    sd: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sd.main(["/dev/sda"]) == 2
    assert "--scratch" in capsys.readouterr().err
