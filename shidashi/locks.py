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

The library raises; the command reports -- except the wait line, which every waiter prints
on stderr itself (R3.9), so that no call site can forget it.
"""

import contextlib
import fcntl
import os
import re
import sys
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

#: Bytes of one pid record: ten zero-padded digits and a newline.
RECORD_WIDTH = 11

_FILE_MODE = 0o644
_PID = re.compile(rb"[0-9]+")


class LockError(Exception):
    """A lock cannot be taken the way it was asked (e.g. a nested hold in the other mode)."""


class LockBusy(Exception):
    """``hold(..., wait=False)`` found the lock taken by another process."""

    def __init__(self, what: str, pid: int | None) -> None:
        self.what = what
        self.pid = pid
        super().__init__(f"{what} is held by {_name_pid(pid)}")


@dataclass
class _Held:
    """One lock this process holds: its open description, nesting depth and mode."""

    fd: int
    depth: int
    shared: bool


#: The locks this process holds, keyed by resolved path.
_held: dict[Path, _Held] = {}


def _name_pid(pid: int | None) -> str:
    return f"pid {pid}" if pid is not None else "pid unknown"


def _mode_name(shared: bool) -> str:
    return "shared" if shared else "exclusive"


def _record() -> bytes:
    return f"{os.getpid():010d}\n".encode()


def _key(path: Path) -> Path:
    """The one name of the lock ``path``, whatever its spelling.

    ``..`` is collapsed lexically first (``a/link/..`` is ``a``, as a shell's ``cd`` reads
    it), then symlinks are resolved: every spelling a caller builds from one root names one
    lock, created and opened at this name.
    """
    return Path(os.path.abspath(path)).resolve()


def holder_pid(path: Path) -> int | None:
    """The pid recorded in the lock file ``path``; None when missing, empty or not a pid."""
    try:
        with path.open("rb") as handle:
            data = handle.read(RECORD_WIDTH)
    except OSError:
        return None
    match = _PID.fullmatch(data.strip())
    if match is None:
        return None
    pid = int(match.group())
    return pid if pid > 0 else None


def _create(path: Path) -> None:
    """Bring ``path`` into existence already holding this process's record.

    Losing the race to another creator (``FileExistsError``) is fine: the file is there.
    """
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            os.write(fd, _record())
            os.fchmod(fd, _FILE_MODE)
        finally:
            os.close(fd)
        with contextlib.suppress(FileExistsError):
            os.link(temp, path)
    finally:
        os.unlink(temp)


def _open(path: Path) -> int:
    """Open the lock file ``path`` read-write, creating it already written if missing."""
    try:
        return os.open(path, os.O_RDWR)
    except FileNotFoundError:
        _create(path)
    return os.open(path, os.O_RDWR)


def _acquire(fd: int, path: Path, *, what: str, wait: bool, shared: bool) -> None:
    """``flock`` ``fd``: at once, or after saying once on stderr whom it waits for."""
    op = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        fcntl.flock(fd, op | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        pid = holder_pid(path)
    if not wait:
        raise LockBusy(what, pid)
    # One write for the whole line: print() writes the text and the newline apart, and
    # another writer on the same stderr can land between them.
    sys.stderr.write(f"waiting for {what} ({_name_pid(pid)})\n")
    sys.stderr.flush()
    fcntl.flock(fd, op)


@contextlib.contextmanager
def hold(path: Path, *, what: str, wait: bool = True, shared: bool = False) -> Generator[None]:
    """Hold the lock ``path`` (exclusive, or shared when ``shared``) for the block.

    ``what`` names the lock in the wait line and in :class:`LockBusy`. When another process
    holds it, ``wait=True`` prints ``waiting for <what> (pid N)`` once on stderr and blocks;
    ``wait=False`` raises :class:`LockBusy`. A nested hold of a lock this process already
    holds -- through any spelling -- takes no second ``flock``; in the other mode it raises
    :class:`LockError`.
    """
    key = _key(path)
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

    fd = _open(key)
    try:
        _acquire(fd, key, what=what, wait=wait, shared=shared)
        os.pwrite(fd, _record(), 0)
    except BaseException:
        os.close(fd)
        raise
    held = _held[key] = _Held(fd=fd, depth=1, shared=shared)
    try:
        yield
    finally:
        held.depth -= 1
        if held.depth == 0:
            del _held[key]
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
