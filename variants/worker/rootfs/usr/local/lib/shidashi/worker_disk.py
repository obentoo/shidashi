"""Turn a named, confirmed disk into the worker's SHIDASHI-WORK disk (stdlib only).

``shidashi disk-init DISK SERIAL`` on the worker (issued from the host by
``shidashi worker disk-init``). A disk is only ever written when the person named it
AND confirmed its serial: :func:`plan` decides every refusal from ``lsblk -J -O``
alone, before any write -- a serial that is not exactly the disk's (kernel names swap
between boots), anything mounted on it (the stick the worker booted from), or the
label already on ANOTHER device. :func:`apply` then partitions, formats, mounts and
persists the pairing, handling the races met on bentoo-lab (2026-10-05): one combined
``sgdisk`` call left an empty table, and the partition node appears asynchronously.

Every command is an argv list run through ``runner``; tests never touch a disk.
"""

import json
import os
import re
import subprocess
import sys
import time as _time  # not "time": tests patch a module attribute of that name
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kyomei_worker import PersistError, persist

Runner = Callable[..., subprocess.CompletedProcess[Any]]

LABEL = "SHIDASHI-WORK"
NODE_TIMEOUT = 10.0
_WORK_DIRS = ("cache", "scratch", "out/jobs", "out/runs", "out/iso")
#: Held by something without a mount point: an open LUKS mapping, LVM, md, swap.
_IN_USE_TYPES = ("crypt", "lvm", "dm", "mpath")
_IN_USE_FSTYPES = ("crypto_LUKS", "LVM2_member", "linux_raid_member", "swap")


class DiskRefused(Exception):
    """The disk named is not one this command may write."""


class DiskError(Exception):
    """Preparing the disk failed part-way."""


@dataclass(frozen=True)
class DiskPlan:
    """The confirmed disk: its path, model, size and serial."""

    path: str
    model: str
    size: str
    serial: str


# --- plan (5.1) ---------------------------------------------------------------------


def plan(lsblk: dict[str, Any], disk: str, serial: str) -> DiskPlan:
    """Every refusal, decided from the listing alone; the plan when none applies."""
    devices = lsblk.get("blockdevices") or []
    target = next((d for d in devices if d.get("path") == disk), None)
    if target is None or target.get("type") != "disk":
        raise DiskRefused(f"{disk} is not a whole disk in lsblk's listing")
    if not serial or target.get("serial") != serial:
        raise DiskRefused(f"the serial {serial!r} does not confirm {disk} (it is not that disk's)")
    twins = [
        d.get("path") for d in devices if d.get("type") == "disk" and d.get("serial") == serial
    ]
    if len(twins) > 1:
        raise DiskRefused(
            f"the serial {serial!r} is shared by {', '.join(map(str, twins))}: it cannot tell "
            "them apart (a USB bridge or a cloned disk); unplug the others"
        )
    for node in _walk(target):
        kind, fstype = str(node.get("type") or ""), node.get("fstype")
        if kind in _IN_USE_TYPES or kind.startswith("raid") or fstype in _IN_USE_FSTYPES:
            raise DiskRefused(
                f"{node.get('path')} is in use ({fstype or kind}): close or stop it first; "
                f"not touching {disk}"
            )
        mounted = [m for m in (node.get("mountpoints") or []) if m] or (
            [node["mountpoint"]] if node.get("mountpoint") else []
        )
        if mounted:
            raise DiskRefused(f"{node.get('path')} is mounted at {mounted[0]}: not touching {disk}")
    for other in devices:
        if other is target:
            continue
        for node in _walk(other):
            if node.get("label") == LABEL:
                raise DiskRefused(
                    f"{node.get('path')} already carries the label {LABEL}: two work disks "
                    "would race for /mnt/work"
                )
    return DiskPlan(
        path=disk,
        model=str(target.get("model") or "?"),
        size=str(target.get("size") or "?"),
        serial=serial,
    )


def _walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


# --- apply (5.2) ----------------------------------------------------------------------


def apply(
    disk: DiskPlan,
    *,
    root: Path = Path("/"),
    runner: Runner = subprocess.run,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Partition, format, mount and persist; ``DiskError`` leaves /mnt/work unmounted."""
    now = clock or _time.monotonic
    wait = sleep or _time.sleep
    part = _partition_path(disk.path)
    _run(runner, ["wipefs", "-a", disk.path])
    _run(runner, ["sgdisk", "-o", disk.path])
    _run(runner, ["sgdisk", "-n", "1:0:0", "-t", "1:8300", "-c", f"1:{LABEL}", disk.path])
    table = _run(runner, ["sgdisk", "-p", disk.path])
    if not re.search(r"^\s*1\s+\d+\s+\d+", table, re.M):
        raise DiskError(f"the partition table of {disk.path} came back without partition 1")
    if runner(["partx", "-u", disk.path], capture_output=True, text=True).returncode != 0:
        runner(["partx", "-a", disk.path], capture_output=True, text=True)
    runner(["udevadm", "settle"], capture_output=True, text=True)
    deadline = now() + NODE_TIMEOUT
    while not os.path.exists(part):
        if now() >= deadline:
            raise DiskError(f"{part} never appeared after partitioning; nothing was formatted")
        wait(0.2)
    _run(runner, ["mkfs.btrfs", "-q", "-f", "-K", "-L", LABEL, part])
    # the by-label link comes from an asynchronous udev event; the fstab mount unit is
    # bound to it, and mounting before it exists gets the mount undone by systemd
    runner(["udevadm", "settle"], capture_output=True, text=True)
    work = root / "mnt" / "work"
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise DiskError(f"cannot create {work}: {err}") from err
    if os.path.ismount(work):
        raise DiskError(f"{work} is already mounted: unmount it first; {part} stays unmounted")
    _run(runner, ["mount", str(work)])  # through the fstab entry: LABEL, options, its unit
    try:
        for sub in _WORK_DIRS:
            (work / sub).mkdir(parents=True, exist_ok=True)
        persist(root, require_mount=True)
        found = _export(_run(runner, ["blkid", "-o", "export", part])).get("LABEL")
        if found != LABEL:
            raise DiskError(f"{part} reads back the label {found!r}, not {LABEL}")
    except (OSError, PersistError, DiskError) as err:
        undone = runner(["umount", str(work)], capture_output=True, text=True)
        state = "unmounted again" if undone.returncode == 0 else "STILL MOUNTED (umount failed)"
        raise DiskError(f"{part} {state}: could not persist or verify: {err}") from err
    print(f"{part} {LABEL} {disk.size} /mnt/work ({disk.model}, serial {disk.serial})")


def _partition_path(disk: str) -> str:
    """``/dev/sda`` -> ``/dev/sda1``; ``/dev/nvme0n1`` -> ``/dev/nvme0n1p1``."""
    return f"{disk}p1" if disk[-1].isdigit() else f"{disk}1"


def _run(runner: Runner, argv: list[str]) -> str:
    done = runner(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise DiskError(f"{' '.join(argv)} failed: {(done.stderr or '').strip()}")
    return str(done.stdout or "")


def _export(text: str) -> dict[str, str]:
    pairs = (line.partition("=") for line in text.splitlines())
    return {key: value for key, sep, value in pairs if sep}


# --- the command ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``worker_disk.py DISK SERIAL``: plan from lsblk, then apply."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: shidashi disk-init DISK SERIAL", file=sys.stderr)
        return 2
    disk, serial = args
    listing = subprocess.run(["lsblk", "-J", "-O"], capture_output=True, text=True, check=False)
    if listing.returncode != 0:
        print(f"disk-init: lsblk failed: {listing.stderr.strip()}", file=sys.stderr)
        return 1
    try:
        apply(plan(json.loads(listing.stdout), disk, serial))
    except (DiskRefused, DiskError, ValueError, OSError) as err:
        print(f"disk-init: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
