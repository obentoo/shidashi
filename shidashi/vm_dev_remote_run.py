"""``vm run`` in a dev VM that is on a build worker: one more hop, the same contract.

The host runs the worker's own ``vm run`` over ssh (the tree recorded with the session,
:mod:`shidashi.vm_dev_record`) and gets out of the way: ssh inherits our stdin, stdout
and stderr -- no terminal, so a ``tar`` piped in arrives byte for byte and a build's log
arrives as it is written -- and its exit code is ours. That is the guest command's, 124
when the WORKER's ``vm run`` timed it out (the timeout is enforced there, next to the
guest, and its ``timed out`` line comes through), or ssh's own 255 when the worker could
not be reached.

Stopping it is the one thing that does not cross by itself. ssh runs no terminal here,
so ending the local ssh leaves the worker's ``vm run`` running -- and with it the build
in the guest. The same idea as :mod:`shidashi.vm_exec`, one hop up: the remote shell
records its process group in a file in the session's directory first
(:func:`shidashi.vm_dev_remote_shell.run_pidfile`), and on Ctrl+C, SIGTERM or SIGHUP a second
ssh sends that group SIGTERM and waits for it to be gone
(:func:`shidashi.vm_dev_remote_shell.stop_group_script`). The worker's ``vm run`` unwinds on
SIGTERM and stops its command in the guest before it ends, so when the host's ``vm run``
exits -- 128 + the signal, as a local one does -- nothing of the command is left on
either machine. A worker that does not answer the stop is said on stderr.

Nothing is captured: the audit trail in force records the command, its exit code and
its duration (``vm.dev.worker.command``), not its output.
"""

import secrets
import subprocess
import sys
import time
from typing import IO

from shidashi import audit, stop_signals, vm_dev_record, vm_dev_remote_shell
from shidashi import remote as remote_mod
from shidashi.remote import Remote

#: Seconds the worker's ``vm run`` gets to stop its guest command and end: its own stop
#: of the guest is bounded at 15 s (:data:`shidashi.vm_exec._STOP_TIMEOUT`).
_REMOTE_STOP_WAIT = 20
#: Seconds the second ssh, the one that stops the worker's ``vm run``, may take.
_STOP_SSH_TIMEOUT = 40
#: Seconds the local ssh gets to end after SIGTERM before it is killed.
_LOCAL_TERM_GRACE = 5
#: ssh's own exit code: it reached nobody.
_NOBODY_ANSWERED = 255

_Stdio = int | IO[bytes] | IO[str] | None


def run_on_worker(
    remote: Remote,
    session: vm_dev_record.WorkerSession,
    command: str,
    *,
    timeout: int,
    stream: bool,
    stdin: _Stdio = None,
    stdout: _Stdio = None,
    stderr: _Stdio = None,
) -> int:
    """Run ``command`` in ``session``'s guest through the worker; its exit code.

    ``timeout`` is ``vm run``'s own, in seconds, 0 for none, handed to the worker
    unchanged. ``stdin``/``stdout``/``stderr`` default to ours (inherited). Ctrl+C,
    SIGTERM and SIGHUP stop the worker's ``vm run`` -- and so the guest command -- and
    are raised on: ``KeyboardInterrupt``, ``SystemExit(128 + signum)``. A SIGHUP
    ignored on purpose (``nohup``) stays ignored, as for a local ``vm run``.
    """
    began = time.monotonic()
    pidfile = vm_dev_remote_shell.run_pidfile(session.name, secrets.token_hex(8))
    line = vm_dev_remote_shell.relayed_run_command(
        session.commit, session.name, command, timeout=timeout, stream=stream, pidfile=pidfile
    )
    with stop_signals.unwinding_on_stop(keep_ignored=True):
        child = subprocess.Popen(
            remote_mod.ssh_argv(remote, line), stdin=stdin, stdout=stdout, stderr=stderr
        )
        try:
            code = child.wait()
        except BaseException:
            _abandon_on_worker(remote, pidfile, child)
            raise
    if code < 0:  # ssh itself killed by a signal: what a shell would report
        code = 128 - code
    audit.current().event(
        "vm.dev.worker.command",
        worker=remote.name,
        session=session.name,
        command=command,
        exit_code=code,
        duration_s=round(time.monotonic() - began, 3),
        stream=stream,
    )
    return code


def _abandon_on_worker(remote: Remote, pidfile: str, child: subprocess.Popen[bytes]) -> None:
    """Ctrl+C, SIGTERM or SIGHUP: leave neither the worker's ``vm run`` nor the local ssh.

    A stop is rarely one signal (:func:`shidashi.vm_exec._abandon` says why): from here
    on SIGTERM and SIGHUP are ignored, and the second ssh inherits them ignored.
    """
    stop_signals.stop_under_way()
    stop = vm_dev_remote_shell.stop_group_script(pidfile, wait_s=_REMOTE_STOP_WAIT)
    try:
        stopped = subprocess.run(
            remote_mod.ssh_argv(remote, stop),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_STOP_SSH_TIMEOUT,
            check=False,
        )
        answered = stopped.returncode != _NOBODY_ANSWERED
    except subprocess.TimeoutExpired, OSError:
        answered = False
    if not answered:
        print(
            f"shidashi vm run: {remote.name} did not answer the stop; the command may "
            "still be running there",
            file=sys.stderr,
            flush=True,
        )
    child.terminate()
    try:
        child.wait(timeout=_LOCAL_TERM_GRACE)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
