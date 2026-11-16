"""The command lines a host sends a build worker to run a dev VM there -- all pure.

:mod:`shidashi.vm_dev_remote` and :mod:`shidashi.vm_dev_remote_run` hand these to ssh;
nothing here touches a network, so every line can be read, and tested, as text.

Each is ONE shell line for the worker's root shell. What runs Shidashi in it is
:func:`shidashi.worker.shipped_shell`: the tree shipped for the session, the shipped
venv, cache, scratch and runs on the work disk -- what a job's unit runs, without the
unit. A session therefore lives in :data:`SESSIONS`/``<name>/``. Each line also sets
``SHIDASHI_DEV_RELAYED`` (:func:`shidashi.config.dev_relayed`): the Shidashi it runs is
the end of the chain, and never takes a record in the worker's own state directory for
a reason to relay the command again.

A start and a relayed ``vm run`` are wrapped by :func:`shidashi.vm_exec.guarded`: the
shell sshd started records its pid -- its process group, since sshd gives each command a
session of its own -- in a file before it runs anything. :func:`stop_group_script` is
the other half: SIGTERM to that group, then a wait until it is gone.

A session's name reaches these lines unquoted: it is 1-64 of ``A-Z a-z 0-9 . _ -``
(:func:`shidashi.vm_dev_record.session_name_error`), checked before any line is made.
Everything else that is not a constant is quoted (:func:`shlex.quote`).
"""

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from shidashi import config, vm_exec, worker

#: Where a shipped Shidashi keeps its VM sessions (its ``SHIDASHI_SCRATCH``/vm).
SESSIONS = f"{worker.WORK}/scratch/vm"
#: What :func:`relayed_stop_command` prints when the worker has no such session any more.
GONE_MARK = "shidashi-dev-session=gone"
#: Seconds a start's process group gets to end once it is told to.
_GROUP_WAIT = 10
#: What marks the Shidashi a line runs as the end of the chain: it never relays again.
_RELAYED = {config.DEV_RELAYED_ENV: "1"}


@dataclass(frozen=True)
class WorkerProbe:
    """What the start's probe found on the worker."""

    mounted: bool
    threads: int | None
    alive: bool
    starting: bool


def session_path(name: str) -> str:
    return f"{SESSIONS}/{name}"


def start_pidfile(name: str) -> str:
    """Where a start on the worker records its process group: beside the session, which
    has no directory yet."""
    return f"{SESSIONS}/{name}.start.pid"


def run_pidfile(name: str, token: str) -> str:
    """Where one relayed ``vm run`` records its process group: in the session."""
    return f"{session_path(name)}/relay-run-{token}.pid"


def stop_group_script(pidfile: str, *, wait_s: int) -> str:
    """Shell that ends the process group ``pidfile`` names and waits for it to go. Pure.

    SIGTERM to the whole group, as :mod:`shidashi.vm_exec` stops a guest command: the
    relayed ``vm run`` in it unwinds and stops its command in the guest. Then up to
    ``wait_s`` seconds until nothing of the group is left, so the caller's stop is done
    when this returns. Only a process still showing ``pidfile`` in its own command line
    is signalled -- the shell :func:`shidashi.vm_exec.guarded` started: the work disk
    outlives a reboot, and a number left there by a shell that died with the worker
    names whatever has it now. POSIX ``kill`` forms: a worker's ``sh`` may be dash.

    "Left" means a member that has not ended: ``kill -0`` also answers for a zombie,
    and under an init that does not reap (PID 1 of the container ``act`` starts) the
    group's ended processes would hold the wait to its end (2026-11-16). The state
    and the group are read from ``/proc/<pid>/stat``, past the last ``) `` that closes
    the command name, with ``read`` -- a builtin, no process per look.
    """
    left = (
        "left() { for s in /proc/[0-9]*/stat; do "
        'read -r r 2>/dev/null < "$s" || continue; r=${r##*) }; set -- $r; '
        '[ "$1" != Z ] && [ "$3" = "$p" ] && return 0; done; return 1; }; '
    )
    return (
        f"{left}"
        f"p=$(cat {pidfile} 2>/dev/null) && grep -aqsF -- {pidfile} /proc/$p/cmdline && "
        f'{{ kill -s TERM -- "-$p"; i=0; '
        f"while left && [ $i -lt {wait_s * 5} ]; "
        f"do sleep 0.2; i=$((i+1)); done; }}; rm -f {pidfile}"
    )


def probe_script(name: str) -> str:
    """Shell for the start's one look at the worker: the work disk, the thread count,
    and whether session ``name`` is alive or being started there. Pure.

    Alive is a ``qemu.pid`` naming a process that IS a QEMU: after a reboot the file
    is still on the work disk and its number is someone else's.
    """
    starter = start_pidfile(name)
    return (
        f"if findmnt -no TARGET -M {worker.WORK} >/dev/null 2>&1; then echo work=mounted; fi; "
        'echo "threads=$(nproc 2>/dev/null)"; '
        f"p=$(cat {session_path(name)}/qemu.pid 2>/dev/null); "
        '[ -n "$p" ] && grep -qs "^qemu" "/proc/$p/comm" && echo session=alive; '
        f"p=$(cat {starter} 2>/dev/null); "
        f'[ -n "$p" ] && grep -aqsF -- {starter} "/proc/$p/cmdline" && echo session=starting; '
        "true"
    )


def read_probe(stdout: str) -> WorkerProbe:
    """:func:`probe_script`'s answer. Pure."""
    lines = stdout.splitlines()
    counts = [line.removeprefix("threads=") for line in lines if line.startswith("threads=")]
    threads = int(counts[0]) if counts and counts[0].isdecimal() else 0
    return WorkerProbe(
        mounted="work=mounted" in lines,
        threads=threads or None,
        alive="session=alive" in lines,
        starting="session=starting" in lines,
    )


def relayed_start_command(
    commit: str,
    name: str,
    *,
    iso: str,
    pins: Mapping[str, object],
    memory: str,
    cpus: int | None,
    max_life: float,
    disk_size: str | None = None,
) -> str:
    """The shell line that starts dev session ``name`` on the worker. Pure.

    ``--local``, and ``SHIDASHI_DEV_RELAYED`` in its environment as in every line
    made here: a worker never relays further, whatever its own environment names.
    ``cpus`` None leaves the worker-side default alone.
    """
    args = ["vm", "start", "--dev", "--local", "--name", name, "--iso", iso]
    args += ["--pins-json", json.dumps(pins, sort_keys=True, separators=(",", ":"))]
    args += ["--memory", memory, "--max-life", repr(float(max_life))]
    if cpus is not None:
        args += ["--cpus", str(cpus)]
    if disk_size:
        args += ["--disk-size", disk_size]
    start = worker.shipped_shell(commit, args, also=_RELAYED)
    started = vm_exec.guarded(start, start_pidfile(name))
    return f"mkdir -p {SESSIONS} && {started}"


def relayed_run_command(
    commit: str, name: str, command: str, *, timeout: int, stream: bool, pidfile: str
) -> str:
    """The shell line that runs ``command`` in session ``name``'s guest. Pure.

    ``command`` crosses as ONE word, so the worker's ``vm run`` hands the guest's shell
    the line this host was given. The timeout is the worker's to enforce: it stops the
    guest command and exits 124 as a local ``vm run`` does. A session the worker no
    longer has is said in words, before a pidfile is written into a missing directory.
    """
    args = ["vm", "run", "--name", name, "--timeout", str(timeout)]
    if stream:
        args.append("--stream")
    gone = (
        f"error: VM session {name} is no longer on this worker (its maximum life ended, "
        f"the worker restarted, or a job took its place): forget it with "
        f"`shidashi vm stop --name {name}`"
    )
    run = worker.shipped_shell(commit, [*args, "--", command], also=_RELAYED)
    guarded = vm_exec.guarded(run, pidfile)
    return (
        f"if [ ! -e {session_path(name)}/session.json ]; then "
        f"echo {shlex.quote(gone)} >&2; exit 1; fi; {guarded}"
    )


def relayed_ssh_relay_command(commit: str, name: str) -> str:
    """The shell line that joins its ssh channel to session ``name``'s guest sshd. Pure.

    It is the ``ProxyCommand`` ``vm ssh-config`` writes (:mod:`shidashi.vm_access`):
    ``systemd-ssh-proxy`` passes a file descriptor, which cannot cross the hop to the
    worker, so the worker's own Shidashi relays the bytes instead.
    """
    return worker.shipped_shell(commit, ["vm", "ssh-relay", "--name", name], also=_RELAYED)


def relayed_stop_command(commit: str, name: str) -> str:
    """The shell line that ends session ``name`` on the worker. Pure.

    A start still under way is ended first (:func:`stop_group_script`): its VM must
    not come up after the stop. Then :data:`GONE_MARK` and exit 0 when there is no
    session to stop, else the shipped ``vm stop`` and its exit code.
    """
    stop = worker.shipped_shell(commit, ["vm", "stop", "--name", name], also=_RELAYED)
    return (
        f"{stop_group_script(start_pidfile(name), wait_s=_GROUP_WAIT)}; "
        f"if [ ! -e {session_path(name)}/session.json ]; then echo {GONE_MARK}; exit 0; fi; "
        f"{stop}"
    )
