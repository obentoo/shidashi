"""Run a command in a VM with its output streamed -- ``shidashi vm run --stream``.

:meth:`vm.Session.run_command` captures the guest's output and hands it back when the
command ends: right for a check, wrong for a build that writes its log for an hour.
bentoolkit follows a build as it happens, so here ssh inherits our stdin, stdout and
stderr: each line reaches the caller as the guest writes it, and whatever is piped into
``vm run`` (``tar | shidashi vm run -- tar -x``) reaches the guest command unchanged.

Stopping it (the timeout, Ctrl+C) stops it IN THE GUEST too. ssh runs no terminal here, so
killing the local ssh leaves the remote command running: a build would go on writing
``/var/tmp/portage`` while bentoolkit starts the next attempt (caught by the CI run of
2026-11-13, where ``sleep`` outlived its timeout). sshd starts the command's shell in a
new session, so that shell leads a process group holding everything the command starts;
it writes its pid to a file first, and a second ssh kills the whole group.

Nothing is captured, so the audit trail records the command, its exit code and its
duration -- not its output, which went to the caller.
"""

import secrets
import shlex
import subprocess
import sys
import time
from typing import IO

from shidashi import audit, vm

#: The exit code of a command stopped at its timeout -- coreutils ``timeout``'s.
TIMEOUT_EXIT = 124
#: Seconds ssh gets to end after SIGTERM before it is killed.
_TERM_GRACE = 5
#: Seconds the second ssh, the one that stops the guest command, may take.
_STOP_TIMEOUT = 15

_Stream = int | IO[bytes] | IO[str] | None


def stream(
    session: vm.Session,
    command: str,
    *,
    timeout: float | None,
    stdin: _Stream = None,
    stdout: _Stream = None,
    stderr: _Stream = None,
) -> int:
    """Run ``command`` as root in the guest's shell; its exit code.

    ``stdin``/``stdout``/``stderr`` default to ours (inherited). ``timeout`` None is no
    limit; past it ssh is stopped (SIGTERM, then SIGKILL after a grace), a line on
    stderr says so and the result is :data:`TIMEOUT_EXIT`. ssh's own failure to reach
    the guest is its 255, passed through.
    """
    began = time.monotonic()
    pidfile = _pidfile()
    child = subprocess.Popen(
        vm.ssh_argv(session.key, session.spec.cid, guarded(command, pidfile)),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    try:
        code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_guest(session, pidfile)
        child.terminate()
        try:
            child.wait(timeout=_TERM_GRACE)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        print(f"shidashi vm run: timed out after {timeout:g} s", file=sys.stderr, flush=True)
        code = TIMEOUT_EXIT
    except BaseException:
        # Ctrl+C or a signal: never leave an ssh (and the guest command) behind
        _stop_guest(session, pidfile)
        child.kill()
        child.wait()
        raise
    audit.current().event(
        "vm.command",
        command=command,
        exit_code=code,
        duration_s=round(time.monotonic() - began, 3),
        stream=True,
    )
    return code


def run_captured(session: vm.Session, command: str, *, timeout: float | None) -> vm.GuestResult:
    """``vm run`` without ``--stream``: the output comes back when the command ends.

    The same stop as :func:`stream` -- in the guest too -- and the same
    :data:`TIMEOUT_EXIT`; our stdin is the command's. Recorded in the audit trail with the
    tail of its output, as :meth:`vm.Session.run_command` records its own.
    """
    began = time.monotonic()
    pidfile = _pidfile()
    child = subprocess.Popen(
        vm.ssh_argv(session.key, session.spec.cid, guarded(command, pidfile)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = child.communicate(timeout=timeout)
        code = child.returncode
    except subprocess.TimeoutExpired:
        _stop_guest(session, pidfile)
        child.kill()
        out, err = child.communicate()
        err += f"shidashi vm run: timed out after {timeout:g} s\n".encode()
        code = TIMEOUT_EXIT
    except BaseException:
        _stop_guest(session, pidfile)
        child.kill()
        child.wait()
        raise
    result = vm.GuestResult(
        command,
        code,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
        round(time.monotonic() - began, 3),
    )
    audit.current().event(
        "vm.command",
        command=command,
        exit_code=code,
        duration_s=result.duration_s,
        stdout=result.stdout[-4000:],
        stderr=result.stderr[-2000:],
    )
    return result


def _pidfile() -> str:
    """A guest path no other ``vm run`` uses."""
    return f"/tmp/shidashi-vm-run-{secrets.token_hex(8)}.pid"


def guarded(command: str, pidfile: str) -> str:
    """``command`` run by a shell that records its pid in ``pidfile`` first. Pure.

    That shell is the session leader sshd made, so its pid is the process group of all
    the command starts; the command runs as its child, with its stdin, and the file goes
    when the command ends.
    """
    return f"echo $$ > {pidfile}; sh -c {shlex.quote(command)}; rc=$?; rm -f {pidfile}; exit $rc"


def _stop_guest(session: vm.Session, pidfile: str) -> None:
    """Kill, in the guest, the process group ``pidfile`` names. Best effort: a guest
    that does not answer is reported on stderr, and the local ssh is stopped anyway."""
    # POSIX's form: dash's kill refuses ``-TERM -- -PGID`` ("Illegal number"), and a
    # guest's or a stand-in's /bin/sh may be dash (the CI container's is)
    kill = f'p=$(cat {pidfile} 2>/dev/null) && kill -s TERM -- "-$p"; rm -f {pidfile}'
    try:
        subprocess.run(
            vm.ssh_argv(session.key, session.spec.cid, kill),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_STOP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            "shidashi vm run: the guest did not answer; its command may still be running",
            file=sys.stderr,
            flush=True,
        )
