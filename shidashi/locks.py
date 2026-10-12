"""Advisory, process-scoped file locks on the shared cache (story 006, contract C2).

``hold(path, what=...)`` takes ``flock`` on ``path`` for the length of a ``with`` block and
writes the holder's pid into the file, so that a process kept waiting can say whom it waits
for. The kernel drops an ``flock`` when its holder dies, so a killed assemble never leaves a
lock behind (R3.8); the file itself stays, with a stale pid that the next holder overwrites.

Why not :mod:`shidashi.ownership`: that is a per-arch owner RECORD that outlives its holder
until released and never blocks; these locks block and must vanish with their holder.

Three properties keep the pid a waiter reads trustworthy:

- the record is fixed-width (:data:`RECORD_WIDTH` bytes, ten zero-padded digits and a
  newline) and written in one ``pwrite`` at offset 0 -- never empty, never a mix of a short
  pid over a longer one;
- a lock file that does not exist yet comes into existence already written: a temp file
  holding the record is linked into place, so a waiter opening it the instant it appears
  never reads it empty;
- ``hold`` is re-entrant per process, keyed by the RESOLVED path: ``flock`` belongs to the
  open file description, so a second ``open`` + ``flock`` of the same file in the same
  process would deadlock against the first. A nested hold reuses the open description and
  the lock is released when the outermost block ends. The registry serves one thread of
  control per lock: shidashi runs one assemble per process.

``shared=True`` takes ``LOCK_SH`` instead of ``LOCK_EX``. A nested hold in the other mode is
refused (:class:`LockError`): ``flock`` converts a held lock by releasing it first, so another
process could slip in between.

A shared holder never hides another holder's pid (R3.9): an exclusive acquire always writes its
record, a shared one only when the record is unreadable, names this process, or names no live
process -- so however many shared holders follow, the record keeps naming a live one. A waiter
whose record names itself (it was the last to write, then let go) or a dead process asks
``/proc/locks`` for a live ``flock`` holder of the same file instead.

Lock files outside a trusted tree (a world-writable ``/tmp`` directory shared by root and
users, task 8.3) take ``nofollow=True``: the file is opened ``O_NOFOLLOW`` and must be a regular
file with one link, so a lock file another user planted as a symlink is refused
(:class:`LockError` naming it) and one planted as a hardlink to someone's file, a FIFO, a
directory or an unreadable file is refused as :class:`UnusableLockFile`; the target of either
is never created nor written. Such a file is created ``O_EXCL`` (never through a planted
name), opened read-only unless this user owns it, and its pid record is written only when it
does: a file another user owns is flocked, never written (a waiter falls back to
``/proc/locks``). The lock is keyed by the resolved directory plus the literal file name.
``mode`` is the file's mode at creation (``fchmod``, so the umask does not strip it). Every
lock fd is close-on-exec. :func:`held_fd` hands a held lock's open description out (to be
passed to another process, e.g. ``pass_fds``); such a lock is released by closing it WITHOUT
``LOCK_UN`` -- the description is shared with the process it was passed to, which keeps
holding the lock. :func:`write_holder` names that process in the record.

The library raises; the command reports -- except the wait line, which every waiter prints
on stderr itself (R3.9), so that no call site can forget it.
"""

import contextlib
import errno
import fcntl
import os
import re
import stat
import sys
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

#: Bytes of one pid record: ten zero-padded digits and a newline.
RECORD_WIDTH = 11

_FILE_MODE = 0o644
#: The largest pid one record holds (ten digits).
_PID_MAX = 10**10 - 1
_PID = re.compile(rb"[0-9]+")
_PROC_LOCKS = Path("/proc/locks")
_MOUNTINFO = Path("/proc/self/mountinfo")


class LockError(Exception):
    """A lock cannot be taken the way it was asked (e.g. a nested hold in the other mode)."""


class UnusableLockFile(LockError):
    """A lock file outside a trusted tree (``nofollow``) that is not safe to use: not a
    regular file, more than one link, or not openable. A symlink is a plain :class:`LockError`.
    """


class LockBusy(Exception):
    """``hold(..., wait=False)`` found the lock taken by another process."""

    def __init__(self, what: str, pid: int | None) -> None:
        self.what = what
        self.pid = pid
        super().__init__(f"{what} is held by {_name_pid(pid)}")


@dataclass
class _Held:
    """One lock this process holds: its open description, nesting depth and mode.

    ``writable``: the pid record may be written (False for another user's ``nofollow``
    file). ``handed_out``: :func:`held_fd` gave the fd away; the release closes it without
    ``LOCK_UN``, which would unlock the description for the process it was passed to.
    """

    fd: int
    depth: int
    shared: bool
    writable: bool = True
    handed_out: bool = False


#: The locks this process holds, keyed by resolved path.
_held: dict[Path, _Held] = {}


def _name_pid(pid: int | None) -> str:
    return f"pid {pid}" if pid is not None else "pid unknown"


def _mode_name(shared: bool) -> str:
    return "shared" if shared else "exclusive"


def _record(pid: int | None = None) -> bytes:
    """The fixed-width record of ``pid`` (this process by default)."""
    return f"{os.getpid() if pid is None else pid:010d}\n".encode()


def _key(path: Path, *, nofollow: bool = False) -> Path:
    """The one name of the lock ``path``, whatever its spelling.

    ``..`` is collapsed lexically first (``a/link/..`` is ``a``, as a shell's ``cd`` reads
    it), then symlinks are resolved: every spelling a caller builds from one root names one
    lock, created and opened at this name. With ``nofollow`` only the directory is resolved;
    the final component stays literal (it is never followed).
    """
    absolute = Path(os.path.abspath(path))
    if nofollow:
        return absolute.parent.resolve() / absolute.name
    return absolute.resolve()


def _parse(data: bytes) -> int | None:
    """The pid in the first record ``data``; None when empty or not a pid."""
    match = _PID.fullmatch(data.strip())
    if match is None:
        return None
    pid = int(match.group())
    return pid if pid > 0 else None


def holder_pid(path: Path) -> int | None:
    """The pid recorded in the lock file ``path``; None when missing, empty or not a pid."""
    try:
        with path.open("rb") as handle:
            data = handle.read(RECORD_WIDTH)
    except OSError:
        return None
    return _parse(data)


def _read_record(fd: int) -> int | None:
    """The pid recorded in the open lock file ``fd``; None when unreadable."""
    try:
        return _parse(os.pread(fd, RECORD_WIDTH, 0))
    except OSError:
        return None


def _alive(pid: int) -> bool:
    """Whether a process ``pid`` exists. One this user may not signal is alive."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError, OverflowError:  # OverflowError: ten digits past pid_t
        return False
    return True


def _mount_device(fd: int) -> tuple[int, int] | None:
    """The device of the filesystem ``fd`` lives on, as ``/proc/locks`` names it.

    On btrfs ``st_dev`` is the subvolume's anonymous device, while ``/proc/locks`` prints the
    superblock's: the fd's mount (``mnt_id`` in its fdinfo) gives that one through mountinfo.
    """
    try:
        fdinfo = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii")
        mnt_id = next(
            (line.split()[1] for line in fdinfo.splitlines() if line.startswith("mnt_id:")), None
        )
        if mnt_id is None:
            return None
        for line in _MOUNTINFO.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split()
            if fields and fields[0] == mnt_id:
                major, minor = fields[2].split(":")
                return int(major), int(minor)
    except OSError, ValueError, IndexError:
        return None
    return None


def _flock_holder(fd: int, *, besides: set[int]) -> int | None:
    """A process holding an ``flock`` on the file of ``fd``, from ``/proc/locks``.

    Only granted FLOCK entries on the file's ``dev:inode`` count (a POSIX lock never conflicts
    with ``flock``; a blocked request -- ``->`` -- holds nothing); pids in ``besides`` and pids
    not visible here (0 or below) are skipped. None when there is none or it cannot be read.
    """
    try:
        stat = os.fstat(fd)
        text = _PROC_LOCKS.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    devices = {(os.major(stat.st_dev), os.minor(stat.st_dev))}
    mount = _mount_device(fd)
    if mount is not None:
        devices.add(mount)
    for line in text.splitlines():
        # "<n>: FLOCK  ADVISORY  READ <pid> <major>:<minor>:<inode> <start> <end>" (hex dev)
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            pid = int(fields[4])
            major, minor, inode = fields[5].split(":")
            device = (int(major, 16), int(minor, 16))
            same_file = int(inode) == stat.st_ino and device in devices
        except ValueError:
            continue
        if same_file and pid > 0 and pid not in besides:
            return pid
    return None


def _waited_for(fd: int) -> int | None:
    """The pid a waiter on ``fd`` names: the record's, when it is another live process.

    A record naming this process (it wrote last, then let go) or a dead one names no holder:
    a live ``flock`` holder from ``/proc/locks`` is named instead, else None (``pid unknown``).
    An unreadable record is None.
    """
    pid = _read_record(fd)
    if pid is None:
        return None
    own = os.getpid()
    if pid != own and _alive(pid):
        return pid
    return _flock_holder(fd, besides={own, pid})


def _claims_record(fd: int, *, shared: bool) -> bool:
    """Whether the acquire that just took ``fd`` writes its pid into the record.

    Exclusive: always -- this process is the one holder. Shared: only when the record is
    unreadable, names this process, or names no live process; a live one may still hold the
    lock beside this process, and overwriting it would hide it from the next waiter.
    """
    if not shared:
        return True
    pid = _read_record(fd)
    return pid is None or pid == os.getpid() or not _alive(pid)


def _create(path: Path, mode: int) -> None:
    """Bring ``path`` into existence already holding this process's record, mode ``mode``.

    Losing the race to another creator (``FileExistsError``) is fine: the file is there.
    ``link`` never follows an existing ``path``: a symlink planted there is left alone, and
    the open that follows refuses it.
    """
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            os.write(fd, _record())
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        with contextlib.suppress(FileExistsError):
            os.link(temp, path)
    finally:
        os.unlink(temp)


def _open_trusted(key: Path, *, mode: int) -> int:
    """Open the lock file ``key`` read-write, creating it already written if missing."""
    flags = os.O_RDWR | os.O_CLOEXEC
    try:
        return os.open(key, flags)
    except FileNotFoundError:
        _create(key, mode)
    return os.open(key, flags)


#: Never follow the final component; never block opening a planted FIFO.
_UNTRUSTED = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _open_checked(key: Path, access: int, *, name: Path) -> tuple[int, os.stat_result]:
    """Open the existing lock file ``key`` with ``access``, never following it, and check it
    is a regular file with one link. Errors name ``name``, the caller's spelling."""
    try:
        fd = os.open(key, access | _UNTRUSTED)
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise LockError(f"lock file {name} is a symlink; it is never followed") from err
        if err.errno in (errno.EACCES, errno.EPERM, errno.EISDIR):
            raise UnusableLockFile(f"lock file {name} cannot be opened: {err.strerror}") from err
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnusableLockFile(f"lock file {name} is not a regular file")
        if st.st_nlink != 1:
            raise UnusableLockFile(
                f"lock file {name} has {st.st_nlink} links; a hardlinked lock is never used"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _open_untrusted(key: Path, *, name: Path, mode: int) -> tuple[int, bool]:
    """Open the lock file ``key`` in a directory others can write; ``(fd, writable)``.

    Missing: created ``O_EXCL`` -- a name planted meanwhile is never opened through --
    empty, ``mode``; the record is written once the lock is taken. Existing: opened
    read-only and checked (:func:`_open_checked`); only a file this user owns is reopened
    read-write, and only when it is still the same inode.
    """
    try:
        fd = os.open(key, os.O_RDWR | os.O_CREAT | os.O_EXCL | _UNTRUSTED, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            os.fchmod(fd, mode)
        except BaseException:
            os.close(fd)
            raise
        return fd, True
    fd, st = _open_checked(key, os.O_RDONLY, name=name)
    if st.st_uid != os.geteuid():
        return fd, False
    try:
        rw, again = _open_checked(key, os.O_RDWR, name=name)
    finally:
        os.close(fd)
    if (again.st_dev, again.st_ino) != (st.st_dev, st.st_ino):
        os.close(rw)
        raise UnusableLockFile(f"lock file {name} was replaced while it was opened")
    return rw, True


def _open(key: Path, *, name: Path, mode: int, nofollow: bool) -> tuple[int, bool]:
    """Open the lock file ``key``; ``(fd, whether its record may be written)``."""
    if nofollow:
        return _open_untrusted(key, name=name, mode=mode)
    return _open_trusted(key, mode=mode), True


def _acquire(fd: int, *, what: str, wait: bool, shared: bool) -> None:
    """``flock`` ``fd``: at once, or after saying once on stderr whom it waits for."""
    op = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        fcntl.flock(fd, op | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        pid = _waited_for(fd)
    if not wait:
        raise LockBusy(what, pid)
    # One write for the whole line: print() writes the text and the newline apart, and
    # another writer on the same stderr can land between them.
    sys.stderr.write(f"waiting for {what} ({_name_pid(pid)})\n")
    sys.stderr.flush()
    fcntl.flock(fd, op)


@contextlib.contextmanager
def hold(
    path: Path,
    *,
    what: str,
    wait: bool = True,
    shared: bool = False,
    mode: int = _FILE_MODE,
    nofollow: bool = False,
) -> Generator[None]:
    """Hold the lock ``path`` (exclusive, or shared when ``shared``) for the block.

    ``what`` names the lock in the wait line and in :class:`LockBusy`. When another process
    holds it, ``wait=True`` prints ``waiting for <what> (pid N)`` once on stderr and blocks;
    ``wait=False`` raises :class:`LockBusy`. A nested hold of a lock this process already
    holds -- through any spelling -- takes no second ``flock``; in the other mode it raises
    :class:`LockError`. ``mode`` is the lock file's mode when this call creates it;
    ``nofollow`` is for a lock directory others can write: a symlink raises
    :class:`LockError`, any other unsafe file :class:`UnusableLockFile`, both naming
    ``path``; another user's file is locked but its record never written.
    """
    key = _key(path, nofollow=nofollow)
    try:
        key.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise OSError(err.errno, f"cannot create the directory of lock {path}: {err}") from err

    held = _held.get(key)
    if held is not None:
        if held.shared != shared:
            raise LockError(
                f"{path} is already held {_mode_name(held.shared)} by this process; "
                f"a nested {_mode_name(shared)} hold would convert it, and flock "
                "conversion is not atomic"
            )
        held.depth += 1
        try:
            yield
        finally:
            held.depth -= 1
        return

    fd, writable = _open(key, name=path, mode=mode, nofollow=nofollow)
    try:
        _acquire(fd, what=what, wait=wait, shared=shared)
        if writable and _claims_record(fd, shared=shared):
            os.pwrite(fd, _record(), 0)
    except BaseException:
        os.close(fd)
        raise
    held = _held[key] = _Held(fd=fd, depth=1, shared=shared, writable=writable)
    try:
        yield
    finally:
        held.depth -= 1
        if held.depth == 0:
            del _held[key]
            try:
                if not held.handed_out:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _lookup(path: Path) -> _Held:
    """The lock ``path`` this process holds, under either way of keying it."""
    for key in (_key(path, nofollow=True), _key(path)):
        held = _held.get(key)
        if held is not None:
            return held
    raise LockError(f"{path} is not held by this process")


def held_fd(path: Path) -> int:
    """The open description of the lock ``path`` this process holds (:class:`LockError`
    when it holds none), to hand to another process -- e.g. ``pass_fds`` -- which then
    holds the lock beside this one and past it. The lock is from now on released by closing
    the fd only, never ``LOCK_UN``: that would unlock the description that process shares.
    """
    held = _lookup(path)
    held.handed_out = True
    return held.fd


def write_holder(path: Path, pid: int) -> bool:
    """Name ``pid`` in the record of the lock ``path`` this process holds -- e.g. the
    process its fd was handed to (:func:`held_fd`). One fixed-width ``pwrite``; False, and
    nothing written, when the file is another user's. :class:`LockError` when this process
    holds no such lock or ``pid`` fits no record."""
    if not 0 < pid <= _PID_MAX:
        raise LockError(f"pid {pid} cannot be recorded in lock {path}")
    held = _lookup(path)
    if not held.writable:
        return False
    os.pwrite(held.fd, _record(pid), 0)
    return True
