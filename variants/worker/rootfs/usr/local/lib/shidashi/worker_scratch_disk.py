"""Turn a named, confirmed disk into the worker's SHIDASHI-SCRATCH disk (stdlib only).

``shidashi disk-init --scratch DISK SERIAL`` on the worker (issued from the host by
``shidashi worker disk-init --scratch``). The scratch disk holds only what the worker
can lose: one btrfs subvolume per entry of :data:`SCRATCH_PATHS`, each mounted over
that path under ``/mnt/work`` by the image's fstab. Every row there is ``nofail``:
without the disk the paths are plain directories on the work disk again, and the
worker runs on.

Decided 2026-11-27 for bentoo-lab's worn Samsung 850 EVO (SATA, ~206 TB written of
150 TB rated): the temporaries and the re-downloadable caches. Measured that day, 4 KiB
random I/O on it is on par with the NVMe work disk -- only sequential transfers are
3-5x slower -- so these writes leave the work disk at little cost.

The content those paths already hold is MOVED onto the new disk: copied, and removed
from the work disk only once every copy succeeded -- never left hidden under a mount.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from worker_disk import (
    DiskError,
    DiskPlan,
    Runner,
    blkid_fields,
    format_disk,
    run_checked,
    run_command,
)
from worker_disk import plan as plan_disk

SCRATCH_LABEL = "SHIDASHI-SCRATCH"
#: subvolume -> the path under /mnt/work it is mounted over. The image's fstab carries
#: one row per entry, and the host's push measures free space by the same paths
#: (``shidashi.worker.SCRATCH_PATHS``): ``tests/test_worker_scratch_disk.py`` pins all three.
SCRATCH_PATHS = {
    "@scratch": "scratch",
    "@tmp": "tmp",
    "@distfiles": "cache/distfiles",
    "@ccache": "cache/ccache",
    "@binpkgs": "cache/binpkgs",
}
#: Where the new filesystem's top level is mounted while its subvolumes are filled.
STAGING = "run/shidashi-scratch"
_STAGING_OPTIONS = "subvolid=5,noatime,compress=zstd:1"
#: A job or CI unit running now would write into a path while it is moved.
_JOB_UNITS = ("shidashi-job-*", "shidashi-ci-*")


def plan(lsblk: dict[str, Any], disk: str, serial: str) -> DiskPlan:
    """:func:`worker_disk.plan`'s refusals, for the scratch label."""
    return plan_disk(lsblk, disk, serial, label=SCRATCH_LABEL)


def apply(
    disk: DiskPlan,
    *,
    root: Path = Path("/"),
    runner: Runner | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Check, format, fill the subvolumes, clear the moved paths, mount them over.

    Every refusal comes before the first write. A failed copy unmounts the staging
    mount and leaves the work disk untouched; the originals go only after it.
    """
    import subprocess  # here, not at import: tests pass ``runner`` and never reach it

    run = runner or subprocess.run
    work = root / "mnt" / "work"
    if not os.path.ismount(work):
        raise DiskError(
            f"{work} is not mounted: the scratch paths live under the work disk "
            "(shidashi worker disk-init makes one); nothing was written"
        )
    for sub in SCRATCH_PATHS.values():
        if os.path.ismount(work / sub):
            raise DiskError(
                f"{work / sub} is already a mount: unmount it first; nothing was written"
            )
    units = ["systemctl", "list-units", "--state=active", "--plain", "--no-legend", *_JOB_UNITS]
    active = run_checked(run, units).split()
    if active:
        raise DiskError(f"{active[0]} is running: wait for it to end; nothing was written")

    part = format_disk(disk.path, SCRATCH_LABEL, runner=run, clock=clock, sleep=sleep)
    staging = root / STAGING
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise DiskError(f"cannot create {staging}: {err}") from err
    # the fstab's compression too: what the copy writes stays as the mounts will write it
    # (bentoo-lab, 2026-11-27: without it the first 91 GiB landed uncompressed)
    run_checked(run, ["mount", "-o", _STAGING_OPTIONS, part, str(staging)])
    moved = _fill(run, work, staging)
    run_checked(run, ["umount", str(staging)])

    for src in moved:  # every copy succeeded: the originals, hidden by the mounts, go
        run_checked(run, ["find", str(src), "-mindepth", "1", "-delete"])
    for sub in SCRATCH_PATHS.values():
        try:
            (work / sub).mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise DiskError(f"cannot create {work / sub}: {err}") from err
        run_checked(run, ["mount", str(work / sub)])  # through its fstab row
    found = blkid_fields(run_checked(run, ["blkid", "-o", "export", part])).get("LABEL")
    if found != SCRATCH_LABEL:
        raise DiskError(f"{part} reads back the label {found!r}, not {SCRATCH_LABEL}")
    paths = ", ".join(SCRATCH_PATHS.values())
    print(f"{part} {SCRATCH_LABEL} {disk.size} under {work}: {paths}")
    print(f"({disk.model}, serial {disk.serial}; moved: {len(moved)} non-empty path(s))")


def _fill(run: Runner, work: Path, staging: Path) -> list[Path]:
    """One subvolume per path, holding a copy of what the work disk had there; the
    paths copied. A failure unmounts the staging mount and leaves the originals."""
    moved: list[Path] = []
    try:
        for subvol, sub in SCRATCH_PATHS.items():
            src, dst = work / sub, staging / subvol
            run_checked(run, ["btrfs", "subvolume", "create", str(dst)])
            if not src.is_dir():
                continue
            run_checked(run, ["chmod", f"--reference={src}", str(dst)])  # tmp's 1777 stays
            if any(src.iterdir()):
                run_checked(run, ["cp", "-a", f"{src}/.", f"{dst}/"])
                moved.append(src)
    except (DiskError, OSError) as err:
        undone = run(["umount", str(staging)], capture_output=True, text=True)
        state = "unmounted" if undone.returncode == 0 else "STILL MOUNTED (umount failed)"
        raise DiskError(f"{staging} {state}; the work disk is untouched: {err}") from err
    return moved


def main(argv: list[str] | None = None) -> int:
    """``worker_scratch_disk.py DISK SERIAL``: plan from lsblk, then apply."""
    return run_command(argv, plan, apply, "shidashi disk-init --scratch DISK SERIAL")


if __name__ == "__main__":
    sys.exit(main())
