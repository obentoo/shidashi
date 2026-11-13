"""Run a command in a VM with its output streamed -- ``shidashi vm run --stream``.

:meth:`vm.Session.run_command` captures the guest's output and hands it back when the
command ends: right for a check, wrong for a build that writes its log for an hour.
bentoolkit follows a build as it happens, so here ssh inherits our stdin, stdout and
stderr: each line reaches the caller as the guest writes it, and whatever is piped into
``vm run`` (``tar | shidashi vm run -- tar -x``) reaches the guest command unchanged.

Nothing is captured, so the audit trail records the command, its exit code and its
duration -- not its output, which went to the caller.
"""

import subprocess
import sys
import time
from typing import IO

from shidashi import audit, vm

#: The exit code of a command stopped at its timeout -- coreutils ``timeout``'s.
TIMEOUT_EXIT = 124
#: Seconds ssh gets to end after SIGTERM before it is killed.
_TERM_GRACE = 5

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
    child = subprocess.Popen(
        vm.ssh_argv(session.key, session.spec.cid, command),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    try:
        code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
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
