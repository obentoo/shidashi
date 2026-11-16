"""The watchdog of one dev session: ``python -m shidashi.vm_dev_watchdog``.

Started by ``vm start --dev --max-life`` (:func:`shidashi.vm_dev_expiry.launch_watchdog`),
one per session. It waits for the deadline recorded with the session, then does what
``vm stop`` does: the VM is powered off and the session's directory, work disk included,
is removed. The record, the log and who ends this process are
:mod:`shidashi.vm_dev_expiry`'s.

It detaches first: the process that was started prints the pid of the one that stays and
exits; the one that stays leads a session of its own with ``/dev/null`` for stdin, stdout
and stderr, so neither the end of ``vm start`` nor of the ssh session around it reaches
it. Nothing is printed after that: all it has to say is a line in the expiry log.

While it waits it looks at the record every :data:`_TICK_S` seconds. A session that was
removed, or started again under the same name (another token), is no longer its own:
it exits, quietly, and powers nothing off.

A session it cannot end -- a QEMU that survives SIGKILL, a directory it may not remove --
is never left running in silence: the failure is one ``failed`` line in the log, with the
error, and the stop is tried again every :data:`_RETRY_S` seconds for as long as the
session is there. A failure repeating with the same error is logged once.
"""

import argparse
import contextlib
import os
import signal
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from shidashi import vm, vm_dev, vm_dev_expiry

#: Seconds between two looks at the session: how long a watchdog outlives a session
#: removed without ``vm stop`` (an ``rm -rf``), and how late a changed clock is noticed.
_TICK_S = 30.0
#: Seconds between two attempts to end a session that would not end.
_RETRY_S = 60.0

Watched = Literal["gone", "removed"]


def end_dev_session(directory: Path) -> None:
    """What ``vm stop`` does to the dev session in ``directory``: VM off, directory
    removed. :class:`vm.VmError` or ``OSError`` when either cannot be done. I/O."""
    session = vm.load_session_at(directory)
    session.stop()
    vm_dev.discard_dev_session(session)


def watch(
    directory: Path,
    *,
    name: str,
    token: str,
    clock: vm_dev_expiry.Clock = vm_dev_expiry.utc_now,
    sleep: Callable[[float], None] = time.sleep,
    end_session: Callable[[Path], None] = end_dev_session,
) -> Watched:
    """Wait for the deadline of the session in ``directory``, then end it. I/O.

    ``gone``: the session stopped being this watchdog's -- no record, or another token --
    before it ended it. ``removed``: it ended the session and logged it. While the
    session is its own and will not end it does not return: each new failure is logged
    and the stop tried again.
    """
    while True:
        expiry = vm_dev_expiry.read_expiry(directory)
        if expiry is None or expiry.token != token:
            return "gone"
        remaining = (expiry.deadline - clock()).total_seconds()
        if remaining <= 0:
            break
        sleep(min(remaining, _TICK_S))
    attempts = 0
    said = ""
    while True:
        attempts += 1
        try:
            end_session(directory)
        except (vm.VmError, OSError) as err:
            if not directory.exists():
                return "gone"  # removed under us by a `vm stop`: that failure is no news
            if str(err) != said:
                said = str(err)
                _log(
                    directory,
                    name=name,
                    deadline=expiry.deadline,
                    outcome="failed",
                    now=clock(),
                    attempts=attempts,
                    error=said,
                    detail=f"the session is still there; trying again every {_RETRY_S:g} s",
                )
            sleep(_RETRY_S)
            again = vm_dev_expiry.read_expiry(directory)
            if again is None or again.token != token:
                return "gone"
            continue
        _log(
            directory,
            name=name,
            deadline=expiry.deadline,
            outcome="removed",
            now=clock(),
            attempts=attempts,
            detail="the VM was powered off and the session's directory removed",
        )
        return "removed"


def _log(directory: Path, **line: Any) -> None:
    """One line in the expiry log. A log that cannot be written (a full disk) changes
    nothing else: the session is still ended, or the stop still tried again. I/O."""
    with contextlib.suppress(OSError):
        vm_dev_expiry.record_expiry(directory, **line)


def _detach() -> tuple[int, int]:
    """Fork; ``(pid, fd)``. The process that was started gets the other's pid and the
    read end of a pipe; the other gets 0 and the write end, leads a new session and has
    ``/dev/null`` for stdin, stdout and stderr. I/O."""
    sys.stdout.flush()
    sys.stderr.flush()
    reading, writing = os.pipe()
    pid = os.fork()
    if pid:
        os.close(writing)
        return pid, reading
    os.close(reading)
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    os.close(null)
    os.chdir("/")
    return 0, writing


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"python -m {vm_dev_expiry.WATCHDOG_MODULE}")
    parser.add_argument("--directory", type=Path, required=True, help="The session's directory.")
    parser.add_argument("--name", required=True, help="The session's name, for the log.")
    parser.add_argument("--token", required=True, help="The token of the session's record.")
    parser.add_argument("--foreground", action="store_true", help="Do not detach (tests).")
    args = parser.parse_args(argv)
    claimed_at = None
    if not args.foreground:
        staying, claimed_at = _detach()
        if staying:
            # until the one that stays has put its pid in the record (or died trying):
            # `vm start` goes on only once a `vm stop` can find the watchdog
            os.read(claimed_at, 1)
            sys.stdout.write(f"{staying}\n")  # read by vm_dev_expiry.launch_watchdog
            return 0
    claimed = vm_dev_expiry.claim(args.directory, args.token, pid=os.getpid())
    if claimed_at is not None:
        os.close(claimed_at)
    if not claimed:
        return 0  # no record of this token: the session went before we were up
    # vm_dev_expiry.disarm ends us with SIGTERM, and an ignored signal is inherited: a
    # `vm start` run by a caller that ignores it must not leave a watchdog deaf to a stop
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        watch(args.directory, name=args.name, token=args.token)
    except Exception as err:
        # stderr is /dev/null by now: the log is the one place left to say it
        expiry = vm_dev_expiry.read_expiry(args.directory)
        now = vm_dev_expiry.utc_now()
        _log(
            args.directory,
            name=args.name,
            deadline=now if expiry is None else expiry.deadline,
            outcome="crashed",
            now=now,
            error=f"{type(err).__name__}: {err}",
            detail="the watchdog ended on an error; the session may still be running",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
