"""``shidashi vm start --dev``: a Bentoo VM ready to build ebuilds, for bentoolkit.

bentoolkit builds a staged ebuild in a disposable VM instead of on the developer's host:
real isolation, Bentoo's own ``make.conf`` and profile, and root without sudo. This is the
host side of that contract (the guest side is :mod:`shidashi.vm_dev_guest`):

- the ISO: the one named, else the newest minimal of the init and arch -- by the date
  in its file name -- among the releases directory and the ISO stash
  (:func:`shidashi.config.dev_iso_dir`: copies kept so that releases can be pruned),
  the releases directory winning a tie. The stash is searched only when it is the
  invoking user's alone (:func:`stash_distrust`); one that is not is skipped and said
  so. None found is :class:`NoDevIsoError`, exit :data:`NO_ISO_EXIT`;
- the trees: the ::gentoo snapshot and overlay commits the ISO's packages came from
  (:mod:`shidashi.iso_pins`; handed in by the caller where there is no git to ask, a
  build worker), from the shared cache, each shared read-only -- no cooldown check,
  the build that made the ISO already passed it;
- the binhost: the host's pool for the ISO's generation when it has an index;
- the work disk: removed and created again on every start, so a fresh start is a clean
  machine -- bentoolkit's verdict build relies on it;
- the vsock CID: the first free one, held by QEMU for as long as it runs;
- the result: :class:`DevVm`, printed by the CLI as one JSON line.

One session per name: a name whose QEMU still runs is refused, never restarted under it.

Nothing is left behind (2026-11-14: bentoolkit names every build VM afresh, and each stopped
session kept a work disk full of builds): ``vm stop`` removes a dev session's directory
(:func:`discard_dev_session`), and a start removes the dev sessions whose QEMU died
without a stop -- a ``kill -9``, a power cut (:func:`sweep_dead_dev_sessions`). Only a
session marked dev (``VmSpec.dev``) is ever removed: a plain session's ``--disk`` is the
user's. A session whose caller died while its QEMU runs is neither: ``--max-life`` gives
it a deadline and a watchdog that stops it (:mod:`shidashi.vm_dev_expiry`), and whatever
removes a session here ends that watchdog first.
"""

import contextlib
import os
import re
import shutil
import signal
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from shidashi import (
    audit,
    config,
    iso_pins,
    process_state,
    tree,
    vm,
    vm_dev_expiry,
    vm_dev_guest,
)

#: The exit code of ``vm start --dev`` when no ISO is there to boot.
NO_ISO_EXIT = 3
#: The work disk's size: sparse, so it costs what the builds write.
DEFAULT_DISK_SIZE = "40G"
#: Seconds the guest setup may take: it formats a disk and probes one binhost.
_SETUP_TIMEOUT = 600
#: Seconds QEMU gets to exit after ``vm stop`` before it is killed.
_QEMU_EXIT_TIMEOUT = 30.0

SessionFactory = Callable[[vm.VmSpec, Path], vm.Session]


class NoDevIsoError(vm.VmError):
    """No minimal ISO of the init and arch exists to boot."""


@dataclass(frozen=True)
class DevVm:
    """A dev VM that is up and ready, as bentoolkit reads it."""

    name: str
    cid: int
    iso: Path
    pin: str
    binhost: list[dict[str, str]]
    directory: Path
    #: When the session powers itself off (``--max-life``); None: it never does.
    expires: datetime | None = None

    def as_json(self) -> dict[str, object]:
        expires = None if self.expires is None else vm_dev_expiry.utc_stamp(self.expires)
        return {
            "name": self.name,
            "cid": self.cid,
            "iso": str(self.iso),
            "pin": self.pin,
            "binhost": self.binhost,
            "staged": {"repo": vm_dev_guest.STAGED_REPO, "path": vm_dev_guest.STAGED_DIR},
            "ready": True,
            "expires": expires,
        }


def releases_dir() -> Path:
    """Where ``shidashi release`` writes its ISOs (``<cache>/releases/<date>/``)."""
    return config.cache_dir() / "releases"


@dataclass(frozen=True)
class StashSkip:
    """A directory of the ISO stash that was not searched, and why."""

    directory: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.directory} was not searched for an ISO: {self.reason}"


@dataclass(frozen=True)
class FoundDevIso:
    """The ISO a lookup chose, and the stash directories it would not search."""

    iso: Path
    skipped: tuple[StashSkip, ...] = ()


def stash_distrust(directory: Path, *, uid: int) -> str | None:
    """Why ``directory`` is not user ``uid``'s alone; ``None`` when it is. I/O.

    The stash's default is under /var/tmp, where any local user can create it first
    and leave an ISO in it -- one the dev VM would boot, with the host's trees and
    binhost shared. Only its owner can put a file in a directory nobody else may write
    to. A symbolic link is refused whatever it points at: the target is the choice of
    whoever made the link. One that cannot be examined is not trusted either;
    ``FileNotFoundError`` when there is none.
    """
    try:
        status = directory.lstat()
    except FileNotFoundError:
        raise
    except OSError as err:
        return f"it cannot be examined ({err.strerror or err})"
    if stat.S_ISLNK(status.st_mode):
        return "it is a symbolic link"
    if not stat.S_ISDIR(status.st_mode):
        return "it is not a directory"
    if status.st_uid != uid:
        return f"it belongs to uid {status.st_uid}, not to uid {uid}"
    if status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"group or others can write to it (mode {stat.S_IMODE(status.st_mode):04o})"
    return None


def _dated_isos(directory: Path, named: re.Pattern[str]) -> list[tuple[str, Path]]:
    """``(date, path)`` of each file right inside ``directory`` whose whole name
    ``named`` matches, its first group the date; none when it cannot be listed. I/O."""
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    return [(m[1], p) for p in entries if (m := named.fullmatch(p.name)) and p.is_file()]


def _subdirectories(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in directory.iterdir() if p.is_dir())
    except OSError:
        return []


def _stashed_isos(
    stash: Path, named: re.Pattern[str], *, uid: int
) -> tuple[list[tuple[str, Path]], list[StashSkip]]:
    """The ISOs ``named`` matches in ``stash`` -- flat, or one directory down -- and
    the directories left unsearched; neither when there is no stash. I/O.

    A sub-directory answers to the same rule as the stash: an ISO is taken from no
    directory someone else can write to. One holding no ISO is passed over in silence.
    """
    try:
        reason = stash_distrust(stash, uid=uid)
    except FileNotFoundError:
        return [], []
    if reason is not None:
        return [], [StashSkip(stash, reason)]
    found = _dated_isos(stash, named)
    skipped: list[StashSkip] = []
    for directory in _subdirectories(stash):
        inside = _dated_isos(directory, named)
        if not inside:
            continue
        try:
            reason = stash_distrust(directory, uid=uid)
        except FileNotFoundError:
            continue  # gone since it was listed
        if reason is None:
            found += inside
        else:
            skipped.append(StashSkip(directory, reason))
    return found, skipped


def find_dev_iso(
    releases: Path,
    stash: Path | None = None,
    *,
    init: str,
    arch: str,
    uid: int | None = None,
) -> FoundDevIso:
    """The newest ``bentoo-<date>-minimal-<init>-<arch>.iso`` of ``releases`` and ``stash``. I/O.

    ``releases`` holds one directory per release; ``stash`` holds ISOs flat, or one
    directory down. Newest is by the date in the file NAME (``YYYY.MM.DD``, as
    :func:`shidashi.publish.release_name` writes it; a file named otherwise is no
    candidate): a stashed ISO has no dated parent to go by. On the same date the one
    under ``releases`` wins: it is the tool's own output.

    A stash directory that is not user ``uid``'s alone (:func:`stash_distrust`; the
    process's user by default) is left unsearched, returned in ``skipped`` and recorded
    as ``vm.dev.stash_skipped``; an absent stash is nothing to report. None found is
    :class:`NoDevIsoError`, naming every directory searched and every one skipped.
    """
    named = re.compile(
        rf"bentoo-(\d{{4}}\.\d{{2}}\.\d{{2}})-minimal-{re.escape(init)}-{re.escape(arch)}\.iso"
    )
    # (date, 1 for a release / 0 for a stashed copy, path): the greatest is the choice
    candidates = [
        (date, 1, str(path))
        for release in _subdirectories(releases)
        for date, path in _dated_isos(release, named)
    ]
    searched = [releases]
    skipped: list[StashSkip] = []
    if stash is not None:
        stashed, skipped = _stashed_isos(stash, named, uid=os.getuid() if uid is None else uid)
        candidates += [(date, 0, str(path)) for date, path in stashed]
        if stash not in [skip.directory for skip in skipped]:
            searched.append(stash)
    for skip in skipped:
        audit.current().event(
            "vm.dev.stash_skipped", directory=str(skip.directory), reason=skip.reason
        )
    if not candidates:
        where = " or ".join(str(directory) for directory in searched)
        notes = "".join(f"; {skip}" for skip in skipped)
        raise NoDevIsoError(
            f"no minimal ISO for {arch}/{init} under {where}{notes}: build one with "
            f"`shidashi build {arch} minimal {init}`, or name one with --iso"
        )
    return FoundDevIso(Path(max(candidates)[2]), tuple(skipped))


def local_pool(info: Mapping[str, object]) -> Path | None:
    """The host's PKGDIR of the ISO's arch and stage3 generation, when it has an index."""
    stage3 = info.get("stage3")
    arch = info.get("arch")
    snapshot = stage3.get("snapshot") if isinstance(stage3, Mapping) else None
    if not isinstance(arch, str) or not isinstance(snapshot, str):
        return None
    pkgdir = config.pkgdir(arch, snapshot)
    return pkgdir if (pkgdir / "Packages").is_file() else None


def dev_shares(trees: Mapping[str, Path], pool: Path | None) -> tuple[vm.Share, ...]:
    """One read-only share per tree (``repo-<name>``), then the pool's. Pure."""
    shares = [vm.Share(f"repo-{name}", path) for name, path in trees.items()]
    if pool is not None:
        shares.append(vm.Share(vm_dev_guest.POOL_TAG, pool))
    return tuple(shares)


def start_dev(
    *,
    name: str,
    iso: Path | None,
    init: str,
    arch: str,
    directory: Path,
    repo: Path,
    memory: str = vm.DEFAULT_MEMORY,
    cpus: int = vm.DEFAULT_CPUS,
    display: str = "none",
    disk_size: str = DEFAULT_DISK_SIZE,
    download: bool = True,
    session_factory: SessionFactory = vm.Session,
    warn: Callable[[str], None] = lambda _message: None,
    pins: iso_pins.IsoPins | None = None,
    max_life: float | None = None,
    clock: vm_dev_expiry.Clock = vm_dev_expiry.utc_now,
    launch_watchdog: vm_dev_expiry.WatchdogLauncher = vm_dev_expiry.launch_watchdog,
) -> DevVm:
    """Boot a dev VM and ready it; :class:`vm.VmError` naming the step that failed.

    ``repo`` is the Shidashi checkout, where an older ISO's pins are read with git --
    unless ``pins`` hands them in, which an ISO naming its own must agree with.
    A setup that fails powers the VM off before the error is raised. ``warn`` is told
    of each stash directory the ISO lookup would not search: the VM then boots an
    older ISO than the user expects, which must not pass in silence. ``max_life``, in
    hours from the moment the VM is ready, is enforced by the watchdog
    ``launch_watchdog`` starts; one that does not start fails the start like the setup.
    """
    sweep_dead_dev_sessions(directory.parent, keep=directory.name)
    if iso is None:
        found = find_dev_iso(releases_dir(), config.dev_iso_dir(), init=init, arch=arch)
        for skip in found.skipped:
            warn(str(skip))
        iso = found.iso
    iso = iso.resolve()
    info = vm.read_build_info(iso)
    try:
        pins = iso_pins.read_iso_pins(info, repo=repo, handed=pins)
    except iso_pins.IsoPinsError as err:
        raise vm.VmError(f"{iso.name}: {err}") from err
    trees = _trees(pins, download=download)
    _refuse_running(directory, name)
    # a session of this name whose QEMU died left its watchdog: not this session's
    vm_dev_expiry.disarm(directory)
    disk = directory / "work.qcow2"
    directory.mkdir(parents=True, exist_ok=True)
    disk.unlink(missing_ok=True)
    vm.ensure_disk(disk, disk_size)
    pool = local_pool(info)
    with vm.allocate_cid(vm.DEFAULT_CID) as cid:
        spec = vm.VmSpec(
            iso=iso,
            cid=cid,
            memory=memory,
            cpus=cpus,
            display=display,
            disks=(disk,),
            shares=dev_shares(trees, pool),
            dev=True,
        )
        session = session_factory(spec, directory)
        # the same file allocate_cid holds: QEMU inherits it and keeps the CID taken
        session.start(cid_lock=vm.cid_lock_dir() / f"cid-{cid}.lock")
        try:
            session.wait_ssh()
            script = vm_dev_guest.setup_script(tuple(trees), local_pool=pool is not None)
            setup = session.run_command(script, timeout=_SETUP_TIMEOUT)
            if setup.exit_code != 0:
                raise vm.VmError(
                    f"the guest setup failed (exit {setup.exit_code}): {setup.stderr.strip()}"
                )
            expires = None
            if max_life is not None:
                expires = vm_dev_expiry.arm(
                    directory, name=name, hours=max_life, clock=clock, launch=launch_watchdog
                )
        except BaseException:
            session.stop()
            raise
    binhosts = vm_dev_guest.parse_binhosts(setup.stdout)
    dev = DevVm(name, cid, iso, pins.id, binhosts, directory, expires)
    audit.current().event("vm.dev", **dev.as_json())
    return dev


def _trees(pins: iso_pins.IsoPins, *, download: bool) -> dict[str, Path]:
    """Every pinned tree of the ISO, ``gentoo`` first, from the shared cache. I/O."""
    cache = config.cache_dir()
    try:
        trees = {"gentoo": tree.ensure_tree(pins.tree, cache_dir=cache, download=download)}
        for overlay in pins.overlays:
            trees[overlay.name] = tree.ensure_overlay(overlay, cache_dir=cache, download=download)
    except (tree.TreeError, OSError) as err:
        raise vm.VmError(f"cannot ready the pinned trees ({pins.id}): {err}") from err
    return trees


def discard_dev_session(session: vm.Session) -> None:
    """Remove a stopped dev session's directory, work disk included.

    Waits for QEMU to exit (killing it after :data:`_QEMU_EXIT_TIMEOUT`): a disk removed
    under a running QEMU frees nothing. A QEMU that will not end keeps the directory,
    as :class:`vm.VmError`. A session without the dev mark is left as it is. The
    session's watchdog is ended first: from here on its stop is ours, and a deadline
    falling while QEMU exits must not start a second removal under this one.
    """
    if not session.spec.dev:
        return
    vm_dev_expiry.disarm(session.directory)
    pid = session.qemu_pid()
    if pid is not None and not _wait_gone(pid, _QEMU_EXIT_TIMEOUT):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        if not _wait_gone(pid, 5.0):
            raise vm.VmError(f"QEMU (pid {pid}) is still running: {session.directory} was kept")
    shutil.rmtree(session.directory)


def sweep_dead_dev_sessions(root: Path, *, keep: str) -> list[str]:
    """Remove the dev sessions under ``root`` whose QEMU no longer runs; their names.

    ``keep`` (the session being started) is never touched, nor anything that is not a
    readable session marked dev. A removed session's watchdog is ended with it.
    """
    removed: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return removed
    for directory in entries:
        if directory.name == keep or not directory.is_dir() or directory.is_symlink():
            continue
        try:
            session = vm.load_session_at(directory)
        except vm.VmError:
            continue
        pid = session.qemu_pid()
        if not session.spec.dev or (pid is not None and _alive(pid)):
            continue
        vm_dev_expiry.disarm(directory)
        shutil.rmtree(directory, ignore_errors=True)
        removed.append(directory.name)
    if removed:
        audit.current().event("vm.dev.swept", root=str(root), sessions=removed)
    return removed


def _alive(pid: int) -> bool:
    """Whether process ``pid`` is still running; an ended, unreaped one is not
    (:mod:`shidashi.process_state`)."""
    return process_state.process_running(pid)


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _refuse_running(directory: Path, name: str) -> None:
    """:class:`vm.VmError` when the session in ``directory`` still has a live QEMU."""
    try:
        pid = int((directory / "qemu.pid").read_text().strip())
    except OSError, ValueError:
        return
    if not _alive(pid):
        return
    raise vm.VmError(
        f"VM session {name!r} is already running (QEMU pid {pid}): "
        f"stop it with `shidashi vm stop --name {name}`"
    )
