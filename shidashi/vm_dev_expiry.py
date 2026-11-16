"""A dev session that ends itself: ``shidashi vm start --dev --max-life HOURS``.

A dev session left behind -- bentoolkit died, a relay's ssh went -- keeps a VM and a work
disk of up to 40 GB alive, and nothing ever stops it: the sweep of the next start only
removes sessions whose QEMU is already dead. With ``--max-life`` the session carries its
own end:

- the deadline: wall clock, UTC, whole seconds, fixed once -- when the VM is ready, so
  the hours are hours of a usable VM -- and never moved;
- the record: ``<session>/expiry.json``, ``{"deadline": "2026-11-15T18:30:00Z",
  "token": "<16 hex>", "pid": <watchdog>}`` (:func:`read_expiry`; the watchdog adds
  its pid once it is up, ``null`` before). ``vm start --dev`` prints the same deadline
  as ``"expires"``, ``null`` without ``--max-life``;
- the watchdog: one detached process per session (:mod:`shidashi.vm_dev_watchdog`,
  started by :func:`launch_watchdog` with this interpreter). No systemd: it must work for
  an unprivileged user on the host and for root on a worker alike. Past the deadline it
  does what ``vm stop`` does -- VM off, directory removed;
- its end: whatever removes the session ends its watchdog first (:func:`disarm`), and a
  watchdog whose record is gone, or names another token, exits by itself;
- the trace: the session's directory goes, so each expiry appends one JSON line to
  ``expiry.jsonl`` BESIDE the session directories (:func:`expiry_log`):
  ``$XDG_STATE_HOME/shidashi/vm/expiry.jsonl`` by default,
  ``$SHIDASHI_SCRATCH/vm/expiry.jsonl`` when a scratch is named
  (``<work-dir>/scratch/vm/expiry.jsonl`` with ``--work-dir``). Each line names the
  session, its directory, the deadline and the ``outcome``: ``removed``, ``failed``
  (the VM could not be powered off, or the directory not removed: said, then tried
  again) or ``crashed``.
"""

import contextlib
import json
import math
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shidashi import vm

#: The module the watchdog runs as; :func:`disarm` looks for it in a process's arguments.
WATCHDOG_MODULE = "shidashi.vm_dev_watchdog"
_RECORD = "expiry.json"
_LOG = "expiry.jsonl"
#: ISO 8601, UTC, whole seconds: what ``expires`` and every log line carry.
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
#: Seconds the watchdog's launcher gets to import, fork and print a pid.
_LAUNCH_TIMEOUT = 30
#: Seconds a watchdog gets to end after SIGTERM before :func:`disarm` moves on.
_END_GRACE = 2.0

Clock = Callable[[], datetime]
#: Starts the detached watchdog of ``(session directory, session name, token)``.
WatchdogLauncher = Callable[[Path, str, str], None]


@dataclass(frozen=True)
class Expiry:
    """A session's deadline, the token of the watchdog that enforces it, and its pid --
    None until the watchdog is up and has put it there (:func:`claim`)."""

    deadline: datetime
    token: str
    pid: int | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_stamp(moment: datetime) -> str:
    """``moment`` as ``2026-11-15T18:30:00Z``. Pure."""
    return moment.astimezone(UTC).strftime(_STAMP)


def deadline_after(hours: float, now: datetime) -> datetime:
    """The deadline ``hours`` past ``now``, in UTC and cut to the second so that what is
    printed, recorded and read back is one value. Pure; ``OverflowError`` past year 9999."""
    return (now.astimezone(UTC) + timedelta(hours=hours)).replace(microsecond=0)


def max_life_error(hours: float, *, now: datetime) -> str | None:
    """Why ``hours`` is no ``--max-life``; None when it is one. Pure."""
    if not math.isfinite(hours) or hours <= 0:
        return f"expected a positive number of hours, got {hours:g}"
    try:
        deadline_after(hours, now)
    except OverflowError:
        return f"{hours:g} hours is past the last date there is"
    return None


def expiry_log(directory: Path) -> Path:
    """Where the expiry of the session in ``directory`` is recorded: beside it."""
    return directory.parent / _LOG


def record_expiry(
    directory: Path, *, name: str, deadline: datetime, outcome: str, now: datetime, **more: object
) -> None:
    """Append one JSON line about the session's expiry to :func:`expiry_log`. I/O.

    One ``write`` of an ``O_APPEND`` file per line: two watchdogs ending at the same
    second never interleave theirs.
    """
    line = {
        "event": "vm.dev.expiry",
        "time": utc_stamp(now),
        "session": name,
        "directory": str(directory),
        "deadline": utc_stamp(deadline),
        "outcome": outcome,
        **more,
    }
    with expiry_log(directory).open("a", encoding="utf-8") as log:
        log.write(json.dumps(line) + "\n")


def read_expiry(directory: Path) -> Expiry | None:
    """The deadline recorded with the session in ``directory``; None when it has none
    (no ``--max-life``, a session already removed) or the record does not read. I/O."""
    try:
        data = json.loads((directory / _RECORD).read_text(encoding="utf-8"))
        deadline = datetime.strptime(data["deadline"], _STAMP).replace(tzinfo=UTC)
        token, pid = data["token"], data.get("pid")
    except OSError, ValueError, KeyError, TypeError:
        return None
    if not isinstance(token, str) or not (pid is None or type(pid) is int):
        return None
    return Expiry(deadline, token, pid)


def _write_expiry(directory: Path, expiry: Expiry) -> None:
    """Whole or not at all: a ``vm stop`` may read this file while the watchdog claims it."""
    record = {"deadline": utc_stamp(expiry.deadline), "token": expiry.token, "pid": expiry.pid}
    temp = directory / f"{_RECORD}.{os.getpid()}.tmp"
    temp.write_text(json.dumps(record) + "\n", encoding="utf-8")
    temp.replace(directory / _RECORD)


def claim(directory: Path, token: str, *, pid: int) -> bool:
    """Put ``pid`` in the record as the watchdog of the session in ``directory``. I/O.

    False, and nothing written, when the record is not ``token``'s: the session went,
    or was started again, before its watchdog was up.
    """
    expiry = read_expiry(directory)
    if expiry is None or expiry.token != token:
        return False
    try:
        _write_expiry(directory, Expiry(expiry.deadline, token, pid))
    except OSError:
        return False
    return True


def launch_watchdog(directory: Path, name: str, token: str) -> None:
    """Start the detached watchdog of the session in ``directory``. I/O.

    The launcher forks, waits until the process that stays has put its pid in the
    record (:func:`claim`), prints that pid and exits. The one that stays is a session
    leader with no file of ours open, so it outlives ``vm start`` and the ssh session
    that ran it, and holds no pipe a caller reading our output would wait on. ``-P``
    and an explicit ``PYTHONPATH`` find this package and nothing a current directory
    offers -- a worker runs a shipped source tree, installed nowhere.
    :class:`vm.VmError` when it does not start: the caller asked for a limit, and a VM
    without one is not that.
    """
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
    argv = [sys.executable, "-P", "-m", WATCHDOG_MODULE]
    argv += ["--directory", str(directory), "--name", name, "--token", token]
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_LAUNCH_TIMEOUT,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise vm.VmError(f"the expiry watchdog did not start: {err}") from err
    if done.returncode != 0 or not done.stdout.strip().isdecimal():
        raise vm.VmError(
            f"the expiry watchdog did not start (exit {done.returncode}): "
            f"{done.stderr.strip()[-500:]}"
        )


def arm(
    directory: Path,
    *,
    name: str,
    hours: float,
    clock: Clock = utc_now,
    launch: WatchdogLauncher = launch_watchdog,
) -> datetime:
    """Give the session in ``directory`` ``hours`` to live from now; its deadline. I/O.

    The record is written before the watchdog starts: it reads its deadline there, adds
    its own pid, and exits when it finds no record of its token. A watchdog that does
    not start leaves no record behind; the error is the launcher's.
    """
    expiry = Expiry(deadline_after(hours, clock()), secrets.token_hex(8))
    _write_expiry(directory, expiry)
    try:
        launch(directory, name, expiry.token)
    except BaseException:
        (directory / _RECORD).unlink(missing_ok=True)
        raise
    return expiry.deadline


def disarm(directory: Path, *, own_pid: int | None = None) -> None:
    """End the watchdog of the session in ``directory`` and forget its deadline. I/O.

    For whatever removes or restarts the session. Called by the watchdog itself -- its
    own stop goes through the same code as ``vm stop`` -- it does nothing: a watchdog
    that killed itself would leave the directory behind. The pid is signalled only
    while that process still shows the watchdog's module and this session's token in
    its arguments: a pid outlives its process and is handed to another. The record goes
    first, so a watchdog that cannot be signalled (another user's) still exits at its
    next look.
    """
    expiry = read_expiry(directory)
    pid = None if expiry is None else expiry.pid
    if pid is not None and pid == (os.getpid() if own_pid is None else own_pid):
        return
    with contextlib.suppress(OSError):
        (directory / _RECORD).unlink(missing_ok=True)
    if expiry is None or pid is None or not _is_watchdog(pid, expiry.token):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    ended = time.monotonic() + _END_GRACE
    while _is_watchdog(pid, expiry.token) and time.monotonic() < ended:
        time.sleep(0.05)


def _is_watchdog(pid: int, token: str) -> bool:
    """Whether process ``pid`` is the watchdog started with ``token``. I/O (``/proc``).

    A process that has ended, a zombie included, has no arguments left to show.
    """
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return WATCHDOG_MODULE.encode() in args and token.encode() in args
