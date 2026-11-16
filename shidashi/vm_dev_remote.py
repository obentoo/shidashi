"""``vm start --dev --worker NAME``: the dev VM on a build worker, the host as a relay.

A worker has the memory and the threads a developer's machine lacks (bentoo-lab: 46 GB,
16 threads, the binpkg pool on its own disk). The host does not grow a second way to run
a VM: it runs the SAME ``vm start --dev`` on the worker over ssh, from a Shidashi tree it
ships there, and prints what that printed. bentoolkit sees no difference.

What the host does itself, because only it can:

- the refusals, all before anything slow: a session of that name recorded on another
  worker, or alive on this host; a worker that does not answer; a job running there
  (a job has priority, :mod:`shidashi.vm_dev_evict`); no work disk; the name alive there;
- the ISO: chosen on the host by the rule of a local start, then put under
  :data:`DEV_ISO_DIR` on the worker with rsync -- not sent again when the one there
  has the same size and time (:func:`put_iso`);
- the pins: the shipped tree has no ``.git``, so the host reads them from the ISO or
  from its own checkout and hands them over (``--pins-json``);
- the record (:mod:`shidashi.vm_dev_record`), written BEFORE the VM is started there.

What changes on a worker: 24G of memory unless ``--memory`` says otherwise (the guest's
RAM root grows with it), the worker's own thread count unless ``--cpus`` does (the
guest's MAKEOPTS are fixed at ``-j32``: its processors are the limit), and a maximum
life of 12 hours unless ``--max-life`` does -- a session nobody stops must not hold a
worker's memory for ever. The session lives in ``/mnt/work/scratch/vm/<name>/``.

The VM is never a job: QEMU and virtiofsd outlive the ssh command that started them,
and would not outlive a ``systemd-run`` unit.

The lines sent to the worker are :mod:`shidashi.vm_dev_remote_shell`'s, all pure; a
relayed ``vm run`` is :mod:`shidashi.vm_dev_remote_run`'s. A start records its shell's
process group in ``<sessions>/<name>.start.pid`` on the worker
(:func:`shidashi.vm_exec.guarded`); whatever undoes a start -- its own failure, Ctrl+C,
a ``vm stop`` after the host's start was killed outright -- ends that group first, then
runs the worker's ``vm stop``
(:func:`shidashi.vm_dev_remote_shell.relayed_stop_command`). A group is signalled only
while the process the file names still shows the file's path in its command line: the
work disk outlives a reboot, a pid does not.

The audit trail in force gets ``vm.dev.worker.probe``, the steps ``vm.dev.worker.ship``
and ``vm.dev.worker.iso`` (``sent`` or ``reused``, with the bytes), the step
``vm.dev.worker.boot`` and ``vm.dev.worker.start``; a stop, ``vm.dev.worker.stop``.
"""

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shidashi import (
    audit,
    config,
    iso_pins,
    stop_signals,
    vm,
    vm_dev,
    vm_dev_expiry,
    vm_dev_record,
    worker,
)
from shidashi import remote as remote_mod
from shidashi.remote import Remote
from shidashi.vm_dev_remote_shell import (
    GONE_MARK,
    WorkerProbe,
    probe_script,
    read_probe,
    relayed_start_command,
    relayed_stop_command,
)

#: Where the dev ISOs are kept on a worker: on the work disk, never its RAM root.
DEV_ISO_DIR = f"{worker.WORKER_CACHE}/dev-isos"
#: A dev VM's memory on a worker without ``--memory``: the guest's RAM root is 4.7 GB
#: at 24G against 1.6 GB at 8G (measured on bentoo-lab, 2026-11-15).
WORKER_MEMORY = "24G"
#: Hours a session on a worker lives without ``--max-life``.
WORKER_MAX_LIFE = 12.0

#: Seconds the worker's ``vm start --dev`` may take: trees to ready, a boot, the setup.
_BOOT_TIMEOUT = 1800.0
#: Seconds its ``vm stop`` may take -- under the two minutes bentoolkit gives a stop.
_WORKER_STOP_TIMEOUT = 100.0
_DEV_PROBE_TIMEOUT = 20.0
#: An ISO's file name as it may cross rsync and the worker's shell unquoted.
_ISO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_RSYNC_SENT_RE = re.compile(r"^sent ([\d.,]+) bytes", re.M)

Warn = Callable[[str], None]


class DevWorkerError(vm.VmError):
    """A dev VM could not be started, or stopped, on a worker; the message says why."""


class NoFirstAnswer(remote_mod.RemoteUnreachable):
    """The worker did not answer the start's first contact: nothing was begun there, so
    the same start may be tried at another address."""


@dataclass(frozen=True)
class StartRequest:
    """What ``vm start --dev`` was asked for; None is "not given"."""

    name: str
    iso: Path | None = None
    init: str = "systemd"
    arch: str = "v3"
    memory: str | None = None
    cpus: int | None = None
    max_life: float | None = None
    disk_size: str | None = None


@dataclass(frozen=True)
class IsoPut:
    """An ISO on the worker: where, and whether it had to be sent."""

    path: str
    sent: bool
    size: int
    bytes_sent: int


# --- the worker, asked ------------------------------------------------------------------


def _last_lines(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def refuse_busy(remote: Remote, name: str) -> WorkerProbe:
    """The refusals only the worker can decide, in two round trips; its probe. I/O.

    :class:`NoFirstAnswer` when it does not answer; :class:`DevWorkerError` for a job
    running there, a missing work disk, and session ``name`` alive or being started.
    """
    try:
        jobs = worker.active_jobs(remote)
    except remote_mod.RemoteUnreachable as err:
        raise NoFirstAnswer(err.name, err.address, err.reason) from err
    except remote_mod.SyncError as err:
        raise DevWorkerError(f"{remote.name}: {err}") from err
    if jobs:
        first = jobs[0].removeprefix("shidashi-job-")
        raise DevWorkerError(
            f"{remote.name} is running {', '.join(jobs)}: a dev VM is not started beside a "
            f"job (follow it with `shidashi worker logs {remote.name} {first} -f`, or use "
            "--local)"
        )
    answer = remote_mod.run(remote, probe_script(name), timeout=_DEV_PROBE_TIMEOUT, input="")
    if answer.exit_code != 0:
        raise DevWorkerError(
            f"the probe of {remote.name} failed (exit {answer.exit_code}): "
            f"{_last_lines(answer.stderr)}"
        )
    probe = read_probe(answer.stdout)
    audit.current().event(
        "vm.dev.worker.probe",
        worker=remote.name,
        session=name,
        mounted=probe.mounted,
        threads=probe.threads,
        alive=probe.alive,
        starting=probe.starting,
    )
    if not probe.mounted:
        raise DevWorkerError(
            f"{worker.WORK} is not mounted on {remote.name}: a dev VM there would keep its "
            "ISO and work disk in the RAM root"
        )
    if probe.alive or probe.starting:
        state = "already running" if probe.alive else "being started"
        raise DevWorkerError(
            f"VM session {name!r} is {state} on {remote.name}: stop it with "
            f"`shidashi vm stop --name {name}` on the host that started it, or choose "
            "another name"
        )
    return probe


def put_iso(remote: Remote, iso: Path) -> IsoPut:
    """Have ``iso`` on the worker under :data:`DEV_ISO_DIR`; where it is. I/O.

    Not sent when the file there has the host's size and modification time (whole
    seconds) -- rsync's own quick check, asked first so that the answer is known.
    Otherwise rsync sends it (``--partial``: an interrupted 2 GB resumes), into a
    temporary name first, so a VM booted from the previous file keeps reading that one.
    :class:`DevWorkerError` for a name that cannot cross the worker's shell and for a
    failed transfer.
    """
    if not _ISO_NAME_RE.fullmatch(iso.name):
        raise DevWorkerError(
            f"{iso.name!r} cannot be sent to a worker under that name: rename the ISO to "
            "letters, digits and . _ + - only"
        )
    dest = f"{DEV_ISO_DIR}/{iso.name}"
    status = iso.stat()
    wanted = f"{status.st_size} {int(status.st_mtime)}"
    there = remote_mod.run(
        remote, f"stat -c '%s %Y' -- {dest} 2>/dev/null; true", timeout=_DEV_PROBE_TIMEOUT, input=""
    )
    if there.stdout.strip() == wanted:
        return IsoPut(dest, sent=False, size=status.st_size, bytes_sent=0)
    argv = remote_mod.rsync_argv(remote, [str(iso)], dest, push=True)
    done = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if done.returncode != 0:
        raise DevWorkerError(
            f"sending {iso.name} to {remote.name} failed (rsync exit {done.returncode}): "
            f"{_last_lines(done.stderr)}"
        )
    match = _RSYNC_SENT_RE.search(done.stdout)
    # every non-digit dropped: the thousands separator follows the locale
    sent = int(re.sub(r"\D", "", match.group(1))) if match else status.st_size
    return IsoPut(dest, sent=True, size=status.st_size, bytes_sent=sent)


def _undo_start(remote: Remote, commit: str, name: str, warn: Warn) -> None:
    """A start that failed or was stopped leaves nothing: what it began on the worker
    is ended and the host's record removed. Best effort on the worker, said when it
    is not known to have worked: the session's maximum life is then what ends it."""
    with stop_signals.deaf_to_stop():  # a second signal must not cut the undoing short
        try:
            undone = remote_mod.run(
                remote, relayed_stop_command(commit, name), timeout=_WORKER_STOP_TIMEOUT, input=""
            )
            left = None if undone.exit_code == 0 else _last_lines(undone.stderr, 3)
        except (remote_mod.RemoteError, OSError) as err:
            left = str(err)
    if left is not None:
        warn(
            f"session {name} may be left on {remote.name} ({left}): it ends itself at "
            "its maximum life"
        )
    vm_dev_record.remove_record(name)


def _session_line(stdout: str, where: str) -> dict[str, object]:
    """The one JSON line the worker's ``vm start --dev`` printed."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        line = json.loads(lines[-1])
    except (IndexError, ValueError) as err:
        raise DevWorkerError(
            f"vm start --dev on {where} printed no session line: {_last_lines(stdout, 3)!r}"
        ) from err
    if not isinstance(line, dict) or line.get("ready") is not True:
        raise DevWorkerError(f"vm start --dev on {where} did not report a ready VM: {line!r}")
    return line


def _refuse_taken_here(request: StartRequest, where: str) -> None:
    """:class:`DevWorkerError` when the host already knows a session of this name:
    recorded on another worker, or a VM alive on this host."""
    known = vm_dev_record.read_record(request.name)
    if known is not None and known.worker != where:
        raise DevWorkerError(
            f"VM session {request.name!r} is recorded on worker {known.worker}: stop it "
            f"with `shidashi vm stop --name {request.name}` first"
        )
    try:
        pid = vm.load_session(request.name).qemu_pid()
    except vm.VmError:
        return  # no session of that name on this host
    if pid is not None and Path(f"/proc/{pid}").exists():
        raise DevWorkerError(
            f"VM session {request.name!r} is running on this host (QEMU pid {pid}): "
            f"stop it with `shidashi vm stop --name {request.name}` first"
        )


def start_on_worker(
    remote: Remote,
    request: StartRequest,
    *,
    repo: Path,
    warn: Warn = lambda _message: None,
    clock: vm_dev_expiry.Clock = vm_dev_expiry.utc_now,
) -> dict[str, object]:
    """Start dev session ``request.name`` on the worker; the line to print. I/O.

    The worker's own JSON line with ``"worker"`` added. ``repo`` is the Shidashi
    checkout, where an older ISO's pins are read; ``warn`` is told what a person should
    see on stderr. Raises :class:`vm_dev.NoDevIsoError` (no ISO), :class:`vm.VmError`
    (:class:`DevWorkerError` included) and :class:`shidashi.remote.RemoteError`;
    whatever is raised once the worker was told to start, the start is undone first.
    """
    rec = audit.current()
    began = time.monotonic()
    name = request.name
    unusable = vm_dev_record.session_name_error(name)
    if unusable is not None:  # the name reaches the worker's shell unquoted
        raise DevWorkerError(unusable)
    _refuse_taken_here(request, remote.name)
    probe = refuse_busy(remote, name)

    iso = request.iso
    if iso is None:
        found = vm_dev.find_dev_iso(
            vm_dev.releases_dir(), config.dev_iso_dir(), init=request.init, arch=request.arch
        )
        for skip in found.skipped:
            warn(str(skip))
        iso = found.iso
    iso = iso.resolve()
    if not iso.is_file():
        raise DevWorkerError(f"{iso} is not a file: nothing to send to {remote.name}")
    try:
        pins = iso_pins.read_iso_pins(vm.read_build_info(iso), repo=repo)
    except iso_pins.IsoPinsError as err:
        raise vm.VmError(f"{iso.name}: {err}") from err

    try:
        with rec.step("vm.dev.worker.ship", worker=remote.name) as step:
            commit, venv_bytes = worker.ship_head(remote)
            step.add(commit=commit, bytes_sent=venv_bytes)
        with rec.step("vm.dev.worker.iso", worker=remote.name, iso=str(iso)) as step:
            put = put_iso(remote, iso)
            step.add(
                action="sent" if put.sent else "reused",
                path=put.path,
                size=put.size,
                bytes_sent=put.bytes_sent,
            )
    except (worker.JobRefused, remote_mod.SyncError) as err:
        reason = err.reason if isinstance(err, worker.JobRefused) else str(err)
        raise DevWorkerError(f"{remote.name}: {reason}") from err

    session = vm_dev_record.WorkerSession(
        name=name, worker=remote.name, commit=commit, started=vm_dev_expiry.utc_stamp(clock())
    )
    command = relayed_start_command(
        commit,
        name,
        iso=put.path,
        pins=iso_pins.pins_block(pins),
        memory=request.memory or WORKER_MEMORY,
        cpus=request.cpus if request.cpus is not None else probe.threads,
        max_life=WORKER_MAX_LIFE if request.max_life is None else request.max_life,
        disk_size=request.disk_size,
    )
    rec.command(["sh", "-c", command], worker=remote.name, session=name)
    vm_dev_record.write_record(session)
    try:
        with rec.step("vm.dev.worker.boot", worker=remote.name, session=name):
            booted = remote_mod.run(remote, command, timeout=_BOOT_TIMEOUT, input="")
        if booted.exit_code != 0:
            raise DevWorkerError(
                f"vm start --dev failed on {remote.name} (exit {booted.exit_code}): "
                f"{_last_lines(booted.stderr) or _last_lines(booted.stdout, 3)}"
            )
        for said in booted.stderr.splitlines():  # the worker's own warnings
            warn(f"{remote.name}: {said}")
        line = _session_line(booted.stdout, remote.name)
        expires = line.get("expires")
        vm_dev_record.write_record(session.ready(expires if isinstance(expires, str) else None))
    except BaseException:
        _undo_start(remote, commit, name, warn)
        raise
    line["worker"] = remote.name
    rec.event(
        "vm.dev.worker.start",
        worker=remote.name,
        session=name,
        commit=commit,
        iso=put.path,
        iso_action="sent" if put.sent else "reused",
        iso_bytes_sent=put.bytes_sent,
        pin=pins.id,
        expires=expires,
        duration_s=round(time.monotonic() - began, 3),
    )
    return line


def stop_on_worker(remote: Remote, session: vm_dev_record.WorkerSession) -> bool:
    """Stop ``session`` on the worker and forget it; whether it was still there. I/O.

    False: the worker no longer had it (it expired, the worker restarted, a job took
    its place) -- not an error, and the record goes all the same. A worker that does
    not answer (:class:`shidashi.remote.RemoteError`) or whose ``vm stop`` fails
    (:class:`DevWorkerError`) keeps the record: the session may still be there.
    """
    began = time.monotonic()
    command = relayed_stop_command(session.commit, session.name)
    done = remote_mod.run(remote, command, timeout=_WORKER_STOP_TIMEOUT, input="")
    there = GONE_MARK not in done.stdout.splitlines()
    audit.current().event(
        "vm.dev.worker.stop",
        worker=remote.name,
        session=session.name,
        commit=session.commit,
        outcome="failed" if done.exit_code != 0 else "stopped" if there else "gone",
        exit_code=done.exit_code,
        duration_s=round(time.monotonic() - began, 3),
    )
    if done.exit_code != 0:
        raise DevWorkerError(
            f"vm stop failed on {remote.name} (exit {done.exit_code}): "
            f"{_last_lines(done.stderr) or _last_lines(done.stdout)}"
        )
    vm_dev_record.remove_record(session.name)
    return there
