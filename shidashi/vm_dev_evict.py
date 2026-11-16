"""A job has priority over the dev VMs on its worker (decided 2026-11-15).

A dev VM (``vm start --dev --worker``, :mod:`shidashi.vm_dev_remote`) holds 24G of a
worker's memory and half its threads' worth of builds; a ``worker job`` is the scheduled
work the worker exists for. So a job never waits for a dev session and never runs beside
one: the sessions are stopped first, and the job's audit trail names them.

Two halves, one on each machine:

- the host (:func:`shidashi.worker.job`): the job's opening probe also lists the dev
  sessions on the work disk (:func:`list_script`, :func:`listed_dev_sessions`). Only
  when there are some, and once the job's own tree is shipped -- the one code on the
  worker sure to know how -- that tree's ``shidashi vm stop --every-dev``
  (:data:`STOP_ARGS`) runs over ssh (:func:`evict`). A session that will not stop
  refuses the job: it does not start beside a VM that keeps its memory;
- the worker (:func:`stop_every_dev_session`, behind the hidden ``--every-dev``): every
  session marked dev under the scratch ends as ``vm stop`` ends it -- VM off, watchdog
  ended, directory and work disk removed. A plain session's disk is the user's: kept.

The host that started a session keeps its record of it; its next ``vm stop`` finds the
session gone, says so and forgets it. A session still being started when the job probes
has no ``session.json`` yet and is not seen: the relay refuses a start while a job
runs, so that window is the one start already under way.
"""

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from shidashi import audit
from shidashi.remote import Remote, SyncError, run

#: The ``shidashi`` arguments that stop every dev session where they run.
STOP_ARGS = ("vm", "stop", "--every-dev")
#: How a dev session is named in the probe's output and in ``--every-dev``'s.
_MARK = "devvm="
#: Seconds the stop may take: a QEMU that ignores its quit is killed after 30 s, each.
_EVICT_TIMEOUT = 300.0


def list_script(scratch: str) -> str:
    """Shell that prints ``devvm=<name>`` for each dev session under ``scratch``. Pure.

    A session is its ``session.json``; the dev mark is matched as ``json.dumps`` writes
    it (:meth:`shidashi.vm.Session.start`). No session at all prints nothing.
    """
    return (
        f"for f in {scratch}/vm/*/session.json; do "
        'if grep -qs \'"dev": true\' "$f"; then '
        f'd=${{f%/session.json}}; echo "{_MARK}${{d##*/}}"; fi; done'
    )


def listed_dev_sessions(lines: Sequence[str]) -> list[str]:
    """The session names among a probe's (or ``--every-dev``'s) output ``lines``. Pure."""
    return [line.removeprefix(_MARK) for line in lines if line.startswith(_MARK)]


def evict(remote: Remote, command: str, rec: audit.Recorder) -> list[str]:
    """Stop every dev session on the worker by running ``command`` there; their names.

    ``command`` is the shipped tree's :data:`STOP_ARGS` as one shell line. Recorded as
    ``worker.job.dev_sessions_stopped`` and said on stderr. :class:`SyncError` when a
    session would not stop; :class:`shidashi.remote.RemoteError` when the worker went.
    """
    result = run(remote, command, timeout=_EVICT_TIMEOUT, input="")
    stopped = listed_dev_sessions(result.stdout.splitlines())
    rec.event(
        "worker.job.dev_sessions_stopped",
        worker=remote.name,
        sessions=stopped,
        exit_code=result.exit_code,
    )
    if result.exit_code != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise SyncError("stop the dev sessions", result.exit_code, tail)
    if stopped:
        print(
            f"a job has priority: stopped the dev VM session(s) {', '.join(stopped)} "
            f"on {remote.name}",
            file=sys.stderr,
        )
    return stopped


def stop_every_dev_session(
    root: Path | None = None, *, end_session: Callable[[Path], None] | None = None
) -> int:
    """Worker side: end every dev session under ``root``; the exit code. I/O.

    ``root`` is where the sessions live (the scratch's ``vm/`` by default) and
    ``end_session`` what ends one (:func:`shidashi.vm_dev_watchdog.end_dev_session`).
    Prints ``devvm=<name>`` on stdout for each session ended and one line on stderr for
    each that could not be; 1 when there is such a one, 0 otherwise -- none at all too.
    """
    # looked up here: the host imports this module with ``worker``, and boots nothing
    from shidashi import vm, vm_dev_watchdog

    end = vm_dev_watchdog.end_dev_session if end_session is None else end_session
    where = vm.session_dir("any").parent if root is None else root
    try:
        entries = sorted(where.iterdir())
    except OSError:
        return 0
    failed = False
    for directory in entries:
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            session = vm.load_session_at(directory)
        except vm.VmError:
            continue
        if not session.spec.dev:
            continue
        try:
            end(directory)
        except (vm.VmError, OSError) as err:
            failed = True
            sys.stderr.write(f"error: dev session {directory.name} was not stopped: {err}\n")
        else:
            sys.stdout.write(f"{_MARK}{directory.name}\n")
    sys.stdout.flush()
    return 1 if failed else 0
