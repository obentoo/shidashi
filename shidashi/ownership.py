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


class OwnedElsewhere(Exception):
    """The arch's lock is held by another owner."""

    def __init__(self, holder: Owner) -> None:
        self.holder = holder
        super().__init__(
            f"{holder.arch} is owned by {holder.worker} (job {holder.job}, "
            f"commit {holder.commit[:12]}, since {holder.since}); "
            f"if that job is gone, run: shidashi worker unlock {holder.arch}"
        )


class LockError(Exception):
    """The locks directory or a lock file cannot be created or written."""

    def __init__(self, path: Path, owner: str, fix: str) -> None:
        self.path, self.owner, self.fix = path, owner, fix
        super().__init__(f"cannot write the owner lock at {path} (owned by {owner}); {fix}")


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
        os.chmod(path, _DIR_MODE)
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
    return Owner.model_validate_json(text)


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
            os.fchmod(fh.fileno(), _FILE_MODE)
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


def holder_alive(owner: Owner, *, probe: Callable[[Owner], bool]) -> bool:
    """Whether the holder still runs: a host holder by its pid on this host (a pid
    this user may not signal is alive), a worker holder by ``probe``."""
    if owner.worker.startswith("host:"):
        if owner.host_pid is None:
            return False
        try:
            os.kill(owner.host_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return probe(owner)


@contextlib.contextmanager
def held(arch: str, owner: Owner) -> Generator[Owner]:
    """Hold the arch's lock for the block; released however the block ends."""
    acquire(arch, owner)
    try:
        yield owner
    finally:
        release(arch, expected=owner)
