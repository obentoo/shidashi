"""The host's record of a dev session that runs on a build worker.

``vm start --dev --worker`` boots the VM on a worker (:mod:`shidashi.vm_dev_remote`);
``vm run`` and ``vm stop`` take no option saying so -- bentoolkit calls them as it calls
them for a VM on the host. What tells them is this record, left by the start:

    ``$XDG_STATE_HOME/shidashi/vm/<name>/worker.json``
    (:func:`shidashi.vm.user_session_dir`; ``~/.local/state`` without the variable)

    ``{"name": "dev", "worker": "bentoo-lab", "commit": "<40 hex>",
    "started": "2026-11-16T00:10:00Z", "expires": "2026-11-16T12:10:23Z"}``

- a file of its own, never a ``session.json``: :func:`shidashi.vm.load_session` keeps
  meaning "a VM on this host", and the sweep of dead dev sessions passes over a
  directory that holds no session;
- ``commit`` is the Shidashi tree shipped to the worker for this session: ``vm run`` and
  ``vm stop`` run THAT tree there, whatever the checkout's HEAD has become since;
- ``started`` is when the record was written, just before the VM is started on the
  worker -- so a start that was killed outright still leaves a ``vm stop`` able to find
  what it began; ``expires`` is the worker's deadline, added once the VM is ready
  (``null`` until then);
- always in the user's state directory, with or without ``--work-dir``: the session's
  files are on the worker, and one place is searched.

A name is refused unless it is 1-64 of ``A-Z a-z 0-9 . _ -`` and starts with a letter
or a digit: it is spliced into shell command lines and paths on the worker. A record
that does not read is an error, never "no record": a session on a worker must not turn
into a local one, nor be forgotten, because a file was cut short.
"""

import contextlib
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from shidashi import kyomei_protocol, vm

_WORKER_RECORD = "worker.json"
#: The client key ``vm ssh-config`` makes on this host for a session on a worker.
CLIENT_KEY = "client_ed25519"
#: The ``Host`` ``vm ssh-config`` writes for the session.
SSH_CONFIG = "ssh_config"
#: What ``vm ssh-config`` leaves beside the record, gone with it (:func:`remove_record`).
ACCESS_FILES = (SSH_CONFIG, CLIENT_KEY, f"{CLIENT_KEY}.pub")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: A worker's name as pairing accepts it: nothing else can be in the registry.
_WORKER_RE = re.compile(kyomei_protocol.HOSTNAME_RE)
_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")


@dataclass(frozen=True)
class WorkerSession:
    """A dev session on a worker, as the host remembers it."""

    name: str
    worker: str
    commit: str
    started: str
    expires: str | None = None

    def __post_init__(self) -> None:
        """``ValueError`` (``TypeError`` for a field that is no text) unless every field
        is what a start writes: whoever holds a :class:`WorkerSession` may splice its
        name, worker and commit into a command line."""
        texts = (self.name, self.worker, self.commit, self.started)
        if not all(isinstance(value, str) for value in texts):
            raise TypeError("name, worker, commit and started are strings")
        if not (self.expires is None or isinstance(self.expires, str)):
            raise TypeError("expires is a string or null")
        reason = session_name_error(self.name)
        if reason is not None:
            raise ValueError(reason)
        if not _WORKER_RE.fullmatch(self.worker):
            raise ValueError(f"{self.worker!r} is no worker name")
        if not _COMMIT_RE.fullmatch(self.commit):
            raise ValueError(f"{self.commit!r} is no commit")

    def ready(self, expires: str | None) -> WorkerSession:
        """This record once the VM is up, with the worker's deadline. Pure."""
        return replace(self, expires=expires)


def session_name_error(name: str) -> str | None:
    """Why ``name`` cannot name a session on a worker; None when it can. Pure."""
    if _NAME_RE.fullmatch(name):
        return None
    return (
        f"{name!r} cannot name a session on a worker: use 1-64 of A-Z a-z 0-9 . _ - "
        "starting with a letter or a digit"
    )


def record_path(name: str) -> Path:
    return vm.user_session_dir(name) / _WORKER_RECORD


def read_record(name: str) -> WorkerSession | None:
    """The record of session ``name``; None when the host has none -- under a name no
    start accepts there is never one. I/O.

    :class:`vm.VmError` for a record that does not read, or that names another
    session, no worker or no commit (:class:`WorkerSession` checks every field): nothing
    in it reaches a command line unchecked.
    """
    if session_name_error(name) is not None:
        return None  # no start ever wrote a record under such a name
    path = record_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError, NotADirectoryError:
        return None
    except OSError as err:
        raise vm.VmError(f"cannot read {path}: {err.strerror or err}") from err
    try:
        data = json.loads(text)
        session = WorkerSession(
            name=data["name"],
            worker=data["worker"],
            commit=data["commit"],
            started=data["started"],
            expires=data.get("expires"),
        )
        if session.name != name:
            raise ValueError(f"it is the record of {session.name!r}")
    except (ValueError, KeyError, TypeError) as err:  # JSONDecodeError is a ValueError
        raise vm.VmError(
            f"{path} does not read as the record of a session on a worker ({err}): "
            "remove it once that session is stopped"
        ) from err
    return session


def write_record(session: WorkerSession) -> Path:
    """Write ``session``'s record, whole or not at all; its path. I/O.

    ``OSError`` when the state directory cannot be written.
    """
    path = record_path(session.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{_WORKER_RECORD}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(asdict(session)) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def remove_record(name: str) -> None:
    """Forget session ``name``: the record goes, with the ssh access ``vm ssh-config``
    made for it (:data:`ACCESS_FILES`), and its directory when nothing else is in it (a
    local session of the same name keeps its own files). I/O."""
    path = record_path(name)
    for leftover in (path, *(path.parent / access for access in ACCESS_FILES)):
        with contextlib.suppress(OSError):
            leftover.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        path.parent.rmdir()
