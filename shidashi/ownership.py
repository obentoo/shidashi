"""One writer per arch's binhost: the per-arch owner lock in the host cache.

Only PKGDIR writers own an arch: a factory, or a build that runs its factory -- on the
host or on a worker. An assemble only reads the binhost and never touches the lock.

The lock is ``<cache>/locks/<arch>.owner.json``. It is written to a temp file, synced,
then hard-linked into place: ``os.link`` fails if the name exists, so of two acquirers
exactly one wins, and a reader never sees a half-written lock. Host builds run as root
and ``shidashi worker`` as the user, so the directory is group-writable (02775) with
the cache's group, and every lock file is 0664.

The library raises; the command reports.
"""

import contextlib
import os
import pwd
import tempfile
from collections.abc import Callable, Generator, Sequence
from pathlib import Path

import pydantic

from shidashi import config

_DIR_MODE = 0o2775
_FILE_MODE = 0o664


class Owner(pydantic.BaseModel):
    """Who writes an arch's binhost: a worker job, or a host build (``host:<hostname>``)."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    arch: str
    worker: str
    job: str
    commit: str
    since: str
    host_pid: int | None = None
    #: The binhost generation (stage3 snapshot) the owner writes, as its commit pins
    #: it; empty in a lock written before it was recorded.
    generation: str = ""


class OwnedElsewhere(Exception):
    """The arch's lock is held by another owner."""

    def __init__(self, holder: Owner) -> None:
        self.holder = holder
        super().__init__(
            f"{holder.arch} is owned by {holder.worker} (job {holder.job}, "
            f"commit {holder.commit[:12]}, since {holder.since}); "
            f"if that job is gone, run: shidashi worker unlock {holder.arch}"
        )


class PullRunning(Exception):
    """A process on this host is pulling the arch's results into its binhost."""

    def __init__(self, arch: str, pid: int) -> None:
        self.arch, self.pid = arch, pid
        super().__init__(
            f"a pull of {arch}'s results into its binhost is running on this host "
            f"(pid {pid}); wait for it to end"
        )


class LockError(Exception):
    """The locks directory or a lock file cannot be created or written."""

    def __init__(self, path: Path, owner: str, fix: str) -> None:
        self.path, self.owner, self.fix = path, owner, fix
        super().__init__(f"cannot write the owner lock at {path} (owned by {owner}); {fix}")


class CorruptLock(LockError):
    """The lock file exists but is not an :class:`Owner` (truncated or edited)."""

    def __init__(self, path: Path, arch: str, detail: str) -> None:
        self.arch = arch
        Exception.__init__(
            self,
            f"the owner lock at {path} cannot be read ({detail}); if no build of {arch} "
            f"runs, remove it with: shidashi worker unlock {arch} --force",
        )
        self.path, self.owner, self.fix = path, _owner_of(path), "unlock --force"


def writes_pkgdir(args: Sequence[str]) -> bool:
    """Whether a Shidashi command line writes a PKGDIR: ``factory``, or ``build``
    unless ``--skip-factory``. Pure; the same answer for the host and a worker job."""
    if not args:
        return False
    if args[0] == "factory":
        return True
    return args[0] == "build" and "--skip-factory" not in args[1:]


def _owner_of(path: Path) -> str:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        st = probe.stat()
        return f"{pwd.getpwuid(st.st_uid).pw_name}:{st.st_gid}"
    except OSError, KeyError:
        return "unknown"


def _lock_error(path: Path) -> LockError:
    locks = config.cache_dir() / "locks"
    return LockError(
        path,
        _owner_of(path),
        f"give the user's group write access: chgrp <group> {locks} && chmod g+w {locks}",
    )


def locks_dir() -> Path:
    """``<cache>/locks``; created mode 02775 with the cache's group."""
    cache = config.cache_dir()
    path = cache / "locks"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir()
        except FileExistsError:
            return path  # whoever created it set its mode; never loosen it here
        # group-writable on purpose (02775): shared by root and the user, see the module doc
        os.chmod(path, _DIR_MODE)  # nosemgrep: insecure-file-permissions
        gid = cache.stat().st_gid
        if path.stat().st_gid != gid:
            os.chown(path, -1, gid)
    except PermissionError as err:
        raise _lock_error(path) from err
    return path


def _lock_path(arch: str) -> Path:
    return locks_dir() / f"{arch}.owner.json"


def current(arch: str) -> Owner | None:
    """The arch's holder, or None when the arch is free."""
    path = _lock_path(arch)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except PermissionError as err:
        raise _lock_error(path) from err
    except UnicodeDecodeError as err:
        raise CorruptLock(path, arch, "not UTF-8 text") from err
    try:
        return Owner.model_validate_json(text)
    except pydantic.ValidationError as err:
        first = err.errors()[0].get("msg", "invalid") if err.errors() else "invalid"
        raise CorruptLock(path, arch, str(first)) from err


def discard(arch: str) -> None:
    """Remove the arch's lock file whatever it holds (``unlock --force`` on a corrupt one)."""
    path = _lock_path(arch)
    try:
        path.unlink(missing_ok=True)
    except PermissionError as err:
        raise _lock_error(path) from err


def acquire(arch: str, owner: Owner) -> Owner:
    """Take the arch's lock for ``owner``, or raise :class:`OwnedElsewhere`."""
    if owner.arch != arch:
        raise ValueError(f"an owner of {owner.arch} cannot take the lock of {arch}")
    locks = locks_dir()
    target = locks / f"{arch}.owner.json"
    tmp: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{arch}.", suffix=".tmp", dir=locks)
        tmp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # group-writable on purpose: root's host builds and the user's worker jobs
            # share the lock through the cache's group; it holds no secret
            os.fchmod(fh.fileno(), _FILE_MODE)  # nosemgrep: insecure-file-permissions
            fh.write(owner.model_dump_json(indent=1) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, target)
    except FileExistsError:
        holder = current(arch)
        if holder is None:  # released between the link and the read: try once more
            return acquire(arch, owner)
        raise OwnedElsewhere(holder) from None
    except PermissionError as err:
        raise _lock_error(target if tmp else locks) from err
    finally:
        if tmp is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
    return owner


def release(arch: str, *, expected: Owner) -> None:
    """Remove the arch's lock, refusing one held by anyone but ``expected``."""
    holder = current(arch)
    if holder is None:
        return
    if holder != expected:
        raise OwnedElsewhere(holder)
    path = _lock_path(arch)
    try:
        path.unlink(missing_ok=True)
    except PermissionError as err:
        raise _lock_error(path) from err


def require_free(arch: str, *, as_owner: Owner | None = None) -> None:
    """Raise :class:`OwnedElsewhere` unless the arch is free or held by ``as_owner``."""
    holder = current(arch)
    if holder is not None and holder != as_owner:
        raise OwnedElsewhere(holder)


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` runs on this host (a pid this user may not signal is alive)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pull_path(arch: str) -> Path:
    return locks_dir() / f"{arch}.pull"


def pulling_pid(arch: str) -> int | None:
    """The pid of a live pull of ``arch``'s results on this host, or None. A marker
    left by a process that died, or one that cannot be read, counts as none."""
    try:
        text = _pull_path(arch).read_text(encoding="utf-8")
        pid = int(text.strip())
    except OSError, UnicodeDecodeError, ValueError:
        return None
    return pid if pid > 0 and _pid_alive(pid) else None


@contextlib.contextmanager
def pulling(arch: str) -> Generator[None]:
    """Mark this process as pulling ``arch``'s results into its binhost for the block.

    The lock alone does not say a pull is still writing once the job has ended:
    ``unlock`` without --force refuses while the marker's process lives
    (:func:`pulling_pid`), and a second pull of the arch raises
    :class:`PullRunning`. A marker left by a dead process is replaced. Removed
    however the block ends, and only if it is still this process's.
    """
    path = _pull_path(arch)
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        except FileExistsError:
            other = pulling_pid(arch)
            if other is not None:
                raise PullRunning(arch, other) from None
            path.unlink(missing_ok=True)  # a dead process's marker
            continue
        except PermissionError as err:
            raise _lock_error(path) from err
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(f"{os.getpid()}\n")
        break
    else:
        # someone recreated it between our unlink and our open: a live pull
        raise PullRunning(arch, pulling_pid(arch) or 0)
    try:
        yield
    finally:
        try:
            if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass


def holder_alive(owner: Owner, *, probe: Callable[[Owner], bool]) -> bool:
    """Whether the holder still runs: a host holder by its pid on this host (a pid
    this user may not signal is alive), a worker holder by ``probe``."""
    if owner.worker.startswith("host:"):
        return owner.host_pid is not None and _pid_alive(owner.host_pid)
    return probe(owner)


@contextlib.contextmanager
def held(arch: str, owner: Owner) -> Generator[Owner]:
    """Hold the arch's lock for the block; released however the block ends."""
    acquire(arch, owner)
    try:
        yield owner
    finally:
        release(arch, expected=owner)
