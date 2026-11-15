"""``shidashi vm start --dev``: a Bentoo VM ready to build ebuilds, for bentoolkit.

bentoolkit builds a staged ebuild in a disposable VM instead of on the developer's host:
real isolation, Bentoo's own ``make.conf`` and profile, and root without sudo. This is the
host side of that contract (the guest side is :mod:`shidashi.vm_dev_guest`):

- the ISO: the one named, else the newest minimal of the init and arch under the
  releases directory; none is :class:`NoDevIsoError`, exit :data:`NO_ISO_EXIT`;
- the trees: the ::gentoo snapshot and overlay commits the ISO's packages came from
  (:mod:`shidashi.iso_pins`), from the shared cache, each shared read-only -- no
  cooldown check, the build that made the ISO already passed it;
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
user's.
"""

import contextlib
import os
import shutil
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from shidashi import audit, config, iso_pins, tree, vm, vm_dev_guest

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

    def as_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "cid": self.cid,
            "iso": str(self.iso),
            "pin": self.pin,
            "binhost": self.binhost,
            "staged": {"repo": vm_dev_guest.STAGED_REPO, "path": vm_dev_guest.STAGED_DIR},
            "ready": True,
        }


def releases_dir() -> Path:
    """Where ``shidashi release`` writes its ISOs (``<cache>/releases/<date>/``)."""
    return config.cache_dir() / "releases"


def find_dev_iso(releases: Path, *, init: str, arch: str) -> Path:
    """The newest ``bentoo-<date>-minimal-<init>-<arch>.iso`` under ``releases``. I/O."""
    found = sorted(
        releases.glob(f"*/bentoo-*-minimal-{init}-{arch}.iso"),
        key=lambda p: (p.parent.name, p.name),
    )
    if not found:
        raise NoDevIsoError(
            f"no minimal ISO for {arch}/{init} under {releases}: build one with "
            f"`shidashi build {arch} minimal {init}`, or name one with --iso"
        )
    return found[-1]


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
    memory: str = "8G",
    cpus: int = 8,
    display: str = "none",
    disk_size: str = DEFAULT_DISK_SIZE,
    download: bool = True,
    session_factory: SessionFactory = vm.Session,
) -> DevVm:
    """Boot a dev VM and ready it; :class:`vm.VmError` naming the step that failed.

    ``repo`` is the Shidashi checkout, where an older ISO's pins are read with git.
    A setup that fails powers the VM off before the error is raised.
    """
    sweep_dead_dev_sessions(directory.parent, keep=directory.name)
    iso = (iso or find_dev_iso(releases_dir(), init=init, arch=arch)).resolve()
    info = vm.read_build_info(iso)
    try:
        pins = iso_pins.read_iso_pins(info, repo=repo)
    except iso_pins.IsoPinsError as err:
        raise vm.VmError(f"{iso.name}: {err}") from err
    trees = _trees(pins, download=download)
    _refuse_running(directory, name)
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
        except BaseException:
            session.stop()
            raise
    dev = DevVm(name, cid, iso, pins.id, vm_dev_guest.parse_binhosts(setup.stdout), directory)
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
    as :class:`vm.VmError`. A session without the dev mark is left as it is.
    """
    if not session.spec.dev:
        return
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
    readable session marked dev.
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
        shutil.rmtree(directory, ignore_errors=True)
        removed.append(directory.name)
    if removed:
        audit.current().event("vm.dev.swept", root=str(root), sessions=removed)
    return removed


def _alive(pid: int) -> bool:
    """Whether process ``pid`` exists (another user's included)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
