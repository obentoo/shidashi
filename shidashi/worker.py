"""Status, jobs and cache sync of a paired worker, as functions over a :class:`Remote`.

The worker mirrors the host's cache under ``/mnt/work/cache`` (``SHIDASHI_CACHE`` of a
job) and runs the host's virtual environment from ``/mnt/work/runtime/venv``. A push
sends only one arch's subset -- its PKGDIR of the pinned generation and its fork
points -- plus the shared caches, never deletes on the worker, and resumes an
interrupted transfer (``--partial``). A pull brings back a job's log, audit trails,
ISOs and the shared caches always, and the arch's binpkgs, fork points and index only
for the holder of the arch's owner lock; it never deletes on the host either.
"""

import datetime
import grp
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import shidashi
from shidashi import audit, config, isaguard, ownership, workers
from shidashi.remote import (
    Remote,
    RemoteUnreachable,
    SyncError,
    put_tree,
    rsync_argv,
    run,
    stream,
)
from shidashi.seed import load_pointer
from shidashi.workers import WorkerEntry

#: The worker's work disk; everything a push writes lives under it (never the RAM root).
WORK = "/mnt/work"
#: The worker's mirror of the host cache (``config.cache_dir()``).
WORKER_CACHE = f"{WORK}/cache"
#: Where the host's virtual environment runs on the worker.
WORKER_VENV = f"{WORK}/runtime/venv"

_SENT_RE = re.compile(r"^sent ([\d.,]+) bytes", re.M)
_RECEIVED_RE = re.compile(r"^sent [\d.,]+ bytes\s+received ([\d.,]+) bytes", re.M)
_GLOB_CHARS = frozenset("*?[")
#: Commands that write ISOs: a job forces their ``--output-dir`` under the work disk.
_ISO_COMMANDS = frozenset({"assemble", "build"})
#: A run id as ``audit`` names it (``<UTC stamp>-<hex>``); never a path.
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: ``emaint binhost --fix`` reads every binpkg of the PKGDIR: minutes, not seconds.
_INDEX_TIMEOUT = 3600.0
_PROBE_TIMEOUT = 60.0
#: The pre-job probe is one short ssh round trip, bounded as a whole (story 009).
_JOB_PROBE_TIMEOUT = 10.0
#: ``systemd-run`` returns once the unit has started.
_START_TIMEOUT = 60.0


@dataclass(frozen=True)
class PullResult:
    """What a pull brought back, as host paths.

    ``isos`` are the ISOs the worker had in ``out/iso/<job>/`` at this pull, never an
    earlier run's left in ``results``. ``run_ids`` are exactly the runs listed in the
    job's ``<job>.runs``, each now in ``config.runs_dir()/<id>``. ``binhost`` says
    whether the arch's binpkgs and index were pulled (only for the holder of the
    arch's lock, and only when the worker has the PKGDIR); ``binhost_reason`` says
    why not, and is None when they were.
    """

    bytes: int
    isos: tuple[Path, ...]
    run_ids: tuple[str, ...]
    log: Path | None
    binhost: bool
    binhost_reason: str | None = None


@dataclass(frozen=True)
class JobResult:
    """What :func:`job` returns (story 014 consumes it): host paths only.

    ``followed`` is False when the job was left running on the worker: started
    without following (``exit_code`` 0: the unit started), or the follow was
    interrupted (Ctrl+C) or lost (``exit_code`` None). Either way ``log``, ``isos``
    and ``run_ids`` are empty. The library never raises ``SystemExit``.
    """

    exit_code: int | None
    log: Path | None
    isos: tuple[Path, ...]
    run_ids: tuple[str, ...]
    duration_s: float
    followed: bool = True


class JobRefused(Exception):
    """A job refused before any transfer: why, and what to do about it."""

    def __init__(self, reason: str, fix: str) -> None:
        self.reason, self.fix = reason, fix
        super().__init__(f"{reason}\n  fix: {fix}")


@dataclass(frozen=True)
class WorkerStatus:
    """One probe of a worker (story 014 consumes it with these names and types).

    Unreachable, every field but ``name``, ``reachable`` and ``reason`` is empty: ``""``,
    ``0``, ``0.0``, ``()`` or None -- never None where a number is expected. Sizes are
    bytes. ``work_free`` is None without a mounted ``/mnt/work`` (``df`` on the bare
    directory answers for the RAM root). ``smart`` is the work disk's SMART verdict,
    None when the image has no ``smartctl``. ``trunks`` are the assemble checkpoints on
    the work disk as ``<arch>-<init>/<step>-<fp24>``; ``jobs`` the active
    ``shidashi-job-*`` units, by exact name without ``.service``.
    """

    name: str
    reachable: bool
    reason: str | None
    cpu_model: str = ""
    threads: int = 0
    cpu_flags: tuple[str, ...] = ()
    runnable_arches: tuple[str, ...] = ()
    max_target: str | None = None
    load1: float = 0.0
    mem_total: int = 0
    mem_available: int = 0
    work_free: int | None = None
    accepts_jobs: bool = False
    image: str = ""
    smart: str | None = None
    trunks: tuple[str, ...] = ()
    jobs: tuple[str, ...] = ()


#: ``status``'s whole-command bound for one worker (the listing uses 5 s, R1.4).
STATUS_TIMEOUT = 10.0
#: The work disk's filesystem label (story 009's ``disk-init``).
_WORK_LABEL = "SHIDASHI-WORK"
#: Where the assembler keeps its checkpoints under a job's ``SHIDASHI_SCRATCH``.
WORKER_CHECKPOINTS = f"{WORK}/scratch/assemble/checkpoints"
#: The running Shidashi job units, by exact name (``shidashi-worker-*`` is not a job):
#: a unit still starting (``activating``) or stopping (``deactivating``) runs too.
_LIST_JOBS = (
    "systemctl list-units 'shidashi-job-*' --state=active,activating,deactivating,reloading "
    "--plain --no-legend --full"
)
#: The status probe: one shell script, one ssh round trip. Each section starts with an
#: ``@name`` line; nothing in it is interpolated from input. ``df`` runs only once
#: ``findmnt`` has proved ``/mnt/work`` mounted. SMART reads the disk holding the work
#: filesystem, found by its mount or, unmounted, by its label.
_STATUS_PROBE = f"""\
echo @threads; nproc 2>/dev/null
echo @model; grep -m1 '^model name' /proc/cpuinfo 2>/dev/null
echo @flags; grep -m1 '^flags' /proc/cpuinfo 2>/dev/null
echo @load; cat /proc/loadavg 2>/dev/null
echo @mem; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo 2>/dev/null
echo @image; grep -h -m1 '^BUILD_ID=' /etc/os-release /usr/lib/os-release 2>/dev/null
echo @work
if findmnt -no TARGET -M {WORK} >/dev/null 2>&1; then
  echo mounted
  echo @free; df -B1 --output=avail {WORK} 2>/dev/null
  echo @trunks
  (cd {WORKER_CHECKPOINTS} 2>/dev/null &&
    for f in */*.json; do if [ -f "$f" ]; then printf '%s\\n' "$f"; fi; done)
fi
echo @jobs
{_LIST_JOBS} 2>/dev/null
echo @smart
if command -v smartctl >/dev/null 2>&1; then
  echo present
  src=$(findmnt -no SOURCE -M {WORK} 2>/dev/null ||
    readlink -e /dev/disk/by-label/{_WORK_LABEL} 2>/dev/null)
  src=${{src%%\\[*}}
  disk=
  if [ -n "$src" ]; then
    parent=$(lsblk -no PKNAME "$src" 2>/dev/null | head -n 1)
    if [ -n "$parent" ]; then disk=/dev/$parent; else disk=$src; fi
  fi
  if [ -n "$disk" ]; then
    echo "device $disk"; smartctl -H "$disk" 2>&1; echo "smartctl-exit $?"
  else
    echo nodevice
  fi
fi
exit 0
"""
_SMART_VERDICT_RE = re.compile(
    r"(?:self-assessment test result|SMART Health Status):\s*(\S.*?)\s*$", re.M
)


def status(remote: Remote, *, timeout: float = STATUS_TIMEOUT) -> WorkerStatus:
    """Probe the worker once, the whole ssh command bounded by ``timeout`` seconds.

    A worker that does not answer -- refused, silent, or stalled after connecting --
    is a :class:`WorkerStatus` with ``reachable=False`` and the reason: it never
    raises for one (R1.5). A changed host key (:class:`HostKeyMismatch`) propagates: a
    security event, not an outage. :class:`isaguard.UnknownFlag` propagates too: an
    arch fragment this guard cannot read is the host's problem, not the worker's.
    """
    try:
        result = run(remote, _STATUS_PROBE, timeout=timeout)
    except RemoteUnreachable as err:
        return WorkerStatus(name=remote.name, reachable=False, reason=str(err))
    sections = _probe_sections(result.stdout)
    flags = tuple(_after_colon(_first(sections, "flags")).split())
    mounted = "mounted" in sections.get("work", [])
    meminfo = _meminfo(sections.get("mem", []))
    runnable = isaguard.runnable(flags) if flags else ()
    return WorkerStatus(
        name=remote.name,
        reachable=True,
        reason=None,
        cpu_model=_after_colon(_first(sections, "model")),
        threads=_int(_first(sections, "threads")),
        cpu_flags=flags,
        runnable_arches=runnable,
        max_target=runnable[0] if runnable else None,
        load1=_float(_first(sections, "load").partition(" ")[0]),
        mem_total=meminfo.get("MemTotal", 0),
        mem_available=meminfo.get("MemAvailable", 0),
        work_free=_df_avail(sections.get("free", [])) if mounted else None,
        accepts_jobs=mounted,
        image=_first(sections, "image").partition("=")[2].strip().strip("\"'"),
        smart=_smart_verdict(sections.get("smart", [])),
        # each manifest is <arch>-<init>/<step>-<fp24>.json: the trunk is its name
        trunks=tuple(sorted(m.removesuffix(".json") for m in sections.get("trunks", []) if m)),
        jobs=_active_jobs(sections.get("jobs", [])),
    )


def refresh_registry(entry: WorkerEntry, st: WorkerStatus) -> WorkerEntry:
    """``entry`` with the CPU flags the probe ``st`` read, saved to the host's registry
    when they changed (R1.6). An unreachable probe, or one that read no flags, changes
    nothing. ``OSError`` and :class:`workers.RegistryError` propagate."""
    if not st.reachable or not st.cpu_flags or st.cpu_flags == entry.cpu_flags:
        return entry
    fresh = entry.model_copy(update={"cpu_flags": st.cpu_flags})
    path = config.workers_dir() / "workers.json"
    registry = workers.load_registry(path)  # re-read: keep what changed meanwhile
    registry[entry.name] = fresh
    workers.save_registry(path, registry)
    return fresh


def _probe_sections(stdout: str) -> dict[str, list[str]]:
    """The probe's output split on its ``@name`` lines (each line stripped)."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if re.fullmatch(r"@[a-z]+", line):
            current = sections.setdefault(line[1:], [])
        elif current is not None:
            current.append(line)
    return sections


def _first(sections: dict[str, list[str]], name: str) -> str:
    return next((line for line in sections.get(name, []) if line), "")


def _after_colon(line: str) -> str:
    return line.partition(":")[2].strip()


def _int(text: str) -> int:
    return int(text) if text.isdigit() else 0


def _float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


def _meminfo(lines: list[str]) -> dict[str, int]:
    """``MemTotal``/``MemAvailable`` lines of ``/proc/meminfo``, in bytes."""
    values: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        words = rest.split()
        if words and words[0].isdigit():
            unit = words[1].lower() if len(words) > 1 else ""
            values[key.strip()] = int(words[0]) * (1024 if unit == "kb" else 1)
    return values


def _df_avail(lines: list[str]) -> int | None:
    """The byte count of ``df -B1 --output=avail`` (its last numeric line)."""
    numbers = [line for line in lines if line.isdigit()]
    return int(numbers[-1]) if numbers else None


def _active_jobs(lines: list[str]) -> tuple[str, ...]:
    """The ``shidashi-job-*`` unit names of ``systemctl list-units``, without
    ``.service``; any other unit (``shidashi-worker-restore``) is not a job."""
    jobs = []
    for line in lines:
        words = line.replace("\u25cf", " ").split()
        if words and words[0].startswith("shidashi-job-"):
            jobs.append(words[0].removesuffix(".service"))
    return tuple(sorted(jobs))


def _smart_verdict(lines: list[str]) -> str | None:
    """The work disk's SMART verdict; None when the image has no ``smartctl``."""
    if "present" not in lines:
        return None
    if "nodevice" in lines:
        return "unknown: no work disk device found"
    match = _SMART_VERDICT_RE.search("\n".join(lines))
    if match:
        return match.group(1)
    exits = [line.removeprefix("smartctl-exit ") for line in lines if line.startswith("smartctl-")]
    # smartctl's exit bits 0-1: the command line did not parse, or the device could not
    # be opened or identified (a device-mapper disk: "Unable to detect device type")
    if exits and exits[-1].isdigit() and int(exits[-1]) & 0b11:
        return "unknown: no SMART device"
    said = [
        line
        for line in lines
        if line and line != "present" and not line.startswith(("device ", "smartctl-exit "))
    ]
    return f"unknown: {said[-1]}" if said else "unknown: smartctl printed no verdict"


def generation(init: str) -> str:
    """The pinned generation (the stage3 snapshot) of ``init``."""
    return load_pointer(init, seeds_dir=config.seeds_dir()).snapshot


def generation_at(repo: Path, commit: str, init: str) -> str:
    """The generation of ``init`` as commit ``commit`` pins it. I/O (git).

    A job runs the commit it ships, with that commit's ``seeds/``: the binhost it
    builds is that generation's, whatever the host's checkout pins by the time the
    results come back. A commit without ``seeds/stage3.toml`` falls back to the host
    checkout's pin (:func:`generation`).
    """
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:seeds/stage3.toml"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if shown.returncode != 0:
        return generation(init)
    text = shown.stdout
    with tempfile.TemporaryDirectory(prefix="shidashi-seeds-") as tmp:
        (Path(tmp) / "stage3.toml").write_text(text, encoding="utf-8")
        return load_pointer(init, seeds_dir=Path(tmp)).snapshot


def validate_job_name(job: str) -> None:
    """Raise :class:`ValueError` unless ``job`` is ``[a-z0-9-]+``.

    The name becomes a unit name and a file name on the worker, and is spliced into
    the unit's shell script: nothing else may get through.
    """
    if not re.fullmatch(r"[a-z0-9-]+", job):
        raise ValueError(f"invalid job name {job!r}: only a-z, 0-9 and '-'")


def job_unit_argv(job: str, commit: str, args: Sequence[str]) -> list[str]:
    """The ``systemd-run`` argv that runs ``shidashi ARGS…`` as unit ``shidashi-job-<job>``.

    Pure. The unit runs the shipped tree ``/mnt/work/src/<commit>`` (working directory
    and ``PYTHONPATH``) with the shipped venv's interpreter, and keeps every path it
    writes on the work disk: cache, scratch and runs through the environment, and for
    ``assemble``/``build`` an ``--output-dir /mnt/work/out/iso/<job>`` unless ``args``
    already name one -- appended after ``args`` (before a ``--`` among them). A bash
    wrapper clears what an earlier job of the same name left (``out/iso/<job>``,
    ``<job>.rc``, ``<job>.runs``), runs the command into
    ``out/jobs/<job>.log``, writes the ids of the runs that appeared meanwhile to
    ``<job>.runs`` (only when there are some) and finally ``<job>.rc``: systemd-run
    returns when the unit's command has been executed (``--service-type=exec``), not
    when it ends, so the rc file is the job's end. ``args`` reach the command as
    separate words, never through the shell.

    systemd expands ``${VAR}``, a whole-word ``$VAR`` and ``$$`` in a unit's command
    line, so every ``$`` of the command (the wrapper's own and the user's) is doubled:
    systemd turns ``$$`` back into ``$`` and nothing else, on any version.
    """
    validate_job_name(job)
    if not commit or not re.fullmatch(r"[0-9a-f]+", commit):
        raise ValueError(f"invalid commit {commit!r}: expected a hex object name")
    src = f"{WORK}/src/{commit}"
    out = f"{WORK}/out"
    jobs = f"{out}/jobs/{job}"
    command = list(args)
    if command and command[0] in _ISO_COMMANDS and not _has_output_dir(command[1:]):
        # after the user's words, so the worker runs the command line as typed; before an
        # end-of-options ``--``, after which it would be a positional
        at = command.index("--", 1) if "--" in command[1:] else len(command)
        command[at:at] = ["--output-dir", f"{out}/iso/{job}"]
    # $@, $? and $(…) are the unit's shell's, not Python's; ``job`` is [a-z0-9-]+.
    script = (
        f'mkdir -p "{out}/jobs" "{out}/runs"; '
        f'rm -rf "{out}/iso/{job}"; rm -f "{jobs}.rc" "{jobs}.runs"; '
        f'before=$(ls "{out}/runs"); '
        f'"$@" > "{jobs}.log" 2>&1; rc=$?; '
        f'new=$(ls "{out}/runs" | grep -vxF -e "$before"); '
        f'[ -z "$new" ] || printf "%s\\n" "$new" > "{jobs}.runs"; '
        f'echo $rc > "{jobs}.rc.tmp" && mv -f "{jobs}.rc.tmp" "{jobs}.rc"'
    )
    exec_start = [
        "/bin/bash",
        "-c",
        script,
        "bash",
        f"{WORKER_VENV}/bin/python",
        "-c",
        "from shidashi.cli import app; app()",
        *command,
    ]
    return [
        "systemd-run",
        f"--unit=shidashi-job-{job}",
        "--collect",
        "--service-type=exec",
        f"--working-directory={src}",
        f"--setenv=SHIDASHI_CACHE={WORKER_CACHE}",
        f"--setenv=SHIDASHI_SCRATCH={WORK}/scratch",
        f"--setenv=SHIDASHI_RUNS={out}/runs",
        f"--setenv=PYTHONPATH={src}",
        # systemd un-doubles $$ when it executes the command: what bash and shidashi
        # get is exactly ``exec_start``
        *(word.replace("$", "$$") for word in exec_start),
    ]


def _has_output_dir(options: Sequence[str]) -> bool:
    """Whether a command's ``options`` already set ``--output-dir`` (``-o``), in any spelling."""
    for arg in options:
        if arg == "--":
            return False  # what follows is positional
        if arg in ("--output-dir", "-o") or arg.startswith("--output-dir="):
            return True
        if arg.startswith("-o") and not arg.startswith("--"):
            return True  # click's attached short form: -o/path
    return False


def push_plan(arch: str, generation: str) -> list[tuple[str, str]]:
    """``(host path, worker path)`` pairs a push of ``arch`` sends, in order. Pure.

    A directory ends with ``/`` (its content goes into the worker directory); a host
    path whose last component is a glob (the stage3, the fork points) is expanded at
    push time into the worker directory it names. Only ``arch``'s PKGDIR of this
    ``generation`` and its fork points of this generation are named -- never another
    arch or generation -- and the fork points (the largest files) come last.
    """
    cache = config.cache_dir()

    def mirror(path: Path) -> tuple[str, str]:
        return f"{path}/", f"{WORKER_CACHE}/{path.relative_to(cache)}/"

    fork_points = config.fork_points_dir()
    return [
        mirror(config.pkgdir(arch, generation)),
        mirror(config.distdir()),
        mirror(config.ccache_dir()),
        mirror(config.sccache_dir()),
        mirror(cache / "trees"),
        mirror(cache / "repos"),
        # the stage3 filename carries its snapshot: every init's of this generation
        (f"{cache}/stage3-*{generation}*", f"{WORKER_CACHE}/"),
        (
            f"{fork_points}/{arch}-*{generation}*",
            f"{WORKER_CACHE}/{fork_points.relative_to(cache)}/",
        ),
    ]


def push(
    remote: Remote,
    arch: str,
    *,
    init: str = "systemd",
    bwlimit: int | None,
    gen: str | None = None,
) -> int:
    """Send ``arch``'s cache subset and the runtime venv to the worker; return bytes sent.

    Refuses (:class:`SyncError`, before any transfer) a worker without its work disk
    mounted at ``/mnt/work`` and a worker lacking the interpreter the venv's
    ``pyvenv.cfg`` names. The venv goes first (small), the plan after it, fork points
    last. Every rsync is capped by ``bwlimit`` (KiB/s) when given; a non-zero exit
    raises :class:`SyncError` naming the step. ``gen`` is the generation to send (a
    job's, from its commit); without it, the host checkout's.
    """
    _require_arch(arch)
    sent = _push_runtime(remote, bwlimit=bwlimit)
    for host_path, worker_path in push_plan(arch, gen or generation(init)):
        sources = _expand(host_path)
        if not sources:
            continue  # nothing of that kind on the host yet (no sccache, no fork point)
        step = "push " + worker_path.removeprefix(f"{WORK}/").rstrip("/")
        sent += _rsync(remote, sources, worker_path, step=step, bwlimit=bwlimit)
    return sent


def _push_runtime(remote: Remote, *, bwlimit: int | None) -> int:
    """Send the host's venv (without its host-bound ``.pth``) to the worker; bytes sent.

    Refuses, before the transfer, a worker without its work disk and one lacking the
    interpreter ``pyvenv.cfg`` names. All an archless job needs besides its commit.
    """
    venv = Path(sys.prefix)
    home = _venv_home(venv)
    _require_work_disk(remote)
    _require_interpreter(remote, home)
    return _rsync(
        remote,
        [f"{venv}/"],
        f"{WORKER_VENV}/",
        step="push runtime/venv",
        bwlimit=bwlimit,
        excludes=_host_bound_pths(venv),
    )


def pull_destinations(
    arch: str | None, *, results: Path, binhost_generation: str | None = None
) -> list[Path]:
    """The host directories a pull writes. Pure.

    Always ``results`` and ``config.runs_dir()``; with an arch also ``results/iso``
    and the shared caches; with ``binhost_generation`` (a pull by the lock holder) also
    the arch's PKGDIR of that generation and the fork points.
    """
    dests = [results, config.runs_dir()]
    if arch is not None:
        dests += [results / "iso", config.distdir(), config.ccache_dir(), config.sccache_dir()]
        if binhost_generation is not None:
            dests += [config.pkgdir(arch, binhost_generation), config.fork_points_dir()]
    return dests


def require_writable(paths: Sequence[Path]) -> None:
    """Raise :class:`SyncError` naming the first directory of ``paths`` this user
    cannot write into, with its owner, group, mode and the fix.

    An existing destination is walked, directories only: rsync writes each file
    through a temp file in the directory the file lands in, so one read-only leaf (a
    ccache bucket a root build left 2755) fails the transfer midway. A missing
    destination counts as writable when its nearest existing parent is (rsync
    creates it).
    """
    problem = _writability_problem(paths)
    if problem is not None:
        reason, fix = problem
        raise SyncError("host destination", None, f"{reason}. {fix}" if fix else reason)


def _writability_problem(paths: Sequence[Path]) -> tuple[str, str] | None:
    """``(reason, fix)`` for the first of ``paths`` this user cannot write; None if none.

    The walk :func:`require_writable` describes; ``fix`` is empty when the blocking
    directory cannot even be inspected.
    """
    for path in paths:
        if path.exists():
            blocked = _first_unwritable_dir(path)
        else:
            parent = path
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            blocked = None if _writable_dir(parent) else parent
        if blocked is not None:
            return _unwritable(path, blocked)
    return None


def pull(
    remote: Remote,
    arch: str | None,
    job: str | None,
    *,
    results: Path,
    init: str = "systemd",
    owner: ownership.Owner | None = None,
    bwlimit: int | None = None,
) -> PullResult:
    """Bring a job's results back from the worker; never deletes on the host.

    See :func:`_pull_unmarked` for what comes back. A pull as ``owner`` writes the
    arch's binhost, so it marks itself for that whole time
    (:func:`ownership.pulling`): ``worker unlock`` without --force refuses while it
    runs, and a second pull of the arch raises :class:`ownership.PullRunning`.
    """
    if owner is None or arch is None:
        return _pull_unmarked(
            remote, arch, job, results=results, init=init, owner=owner, bwlimit=bwlimit
        )
    with ownership.pulling(arch):
        return _pull_unmarked(
            remote, arch, job, results=results, init=init, owner=owner, bwlimit=bwlimit
        )


def _pull_unmarked(
    remote: Remote,
    arch: str | None,
    job: str | None,
    *,
    results: Path,
    init: str = "systemd",
    owner: ownership.Owner | None = None,
    bwlimit: int | None = None,
) -> PullResult:
    """Bring a job's results back from the worker; never deletes on the host.

    Always ``out/jobs/<job>.log``, ``.rc`` and ``.runs`` into ``results`` and the runs
    listed in ``<job>.runs`` into ``config.runs_dir()``; with an ``arch`` also
    ``out/iso/<job>/`` into ``results/iso/`` and the distfiles, ccache and sccache (an
    archless pull takes only the log, rc and runs; a pull without a ``job`` takes only
    the arch's distfiles, ccache and sccache). Only with ``owner`` -- and
    :func:`ownership.require_free` raises :class:`ownership.OwnedElsewhere` when someone
    else holds the arch -- the arch's fork points come, and, when the worker has the
    arch's PKGDIR, its index is regenerated ON THE WORKER, its binpkgs (without the
    index) come, then the index into a temp sibling that ``os.replace`` puts in
    place: never merged, never replaced without the lock. A worker without that
    PKGDIR (a factory that built nothing) is not an error: the pull succeeds with
    ``binhost=False`` and the reason. Every host destination is checked writable
    before any transfer (:class:`SyncError` naming the directory and the fix). Every
    rsync is capped by ``bwlimit`` (KiB/s) when given.
    """
    if job is not None:
        validate_job_name(job)
    elif arch is None:
        raise ValueError("a pull names a job, an arch or both")
    if arch is not None:
        _require_arch(arch)
    gen: str | None = None
    if owner is not None:
        if arch is None:
            raise ValueError("an archless pull has no binhost to bring back")
        ownership.require_free(arch, as_owner=owner)
        # the generation the owner's job built, not the one the host pins by now
        gen = owner.generation or generation(init)
    require_writable(pull_destinations(arch, results=results, binhost_generation=gen))

    cache = config.cache_dir()
    job_files = [f"{WORK}/out/jobs/{job}{ext}" for ext in (".log", ".rc", ".runs")] if job else []
    iso_dir = f"{WORK}/out/iso/{job}" if job else None
    caches = [
        (f"{WORKER_CACHE}/{d.relative_to(cache)}", d)
        for d in (config.distdir(), config.ccache_dir(), config.sccache_dir())
    ]
    fork_dir = f"{WORKER_CACHE}/{config.fork_points_dir().relative_to(cache)}"
    patterns = list(job_files)
    if arch is not None:
        if iso_dir is not None:
            patterns += [iso_dir, f"{iso_dir}/*.iso"]
        patterns += [w for w, _ in caches]
    worker_pkgdir: str | None = None
    if arch is not None and gen is not None:
        worker_pkgdir = f"{WORKER_CACHE}/binpkgs/{arch}/{gen}"
        patterns += [worker_pkgdir, f"{fork_dir}/{arch}-*{gen}*"]
    present = _present(remote, patterns)

    index_ready = False
    if arch is None:
        reason: str | None = "an archless job has no binhost"
    elif worker_pkgdir is None:
        reason = (
            f"the caller does not hold {arch}'s owner lock: "
            f"{arch}'s binpkgs, fork points and index stay as they are on the host"
        )
    elif worker_pkgdir not in present:
        reason = (
            f"{worker_pkgdir} does not exist on {remote.name} (the job built no binpkg): "
            f"{arch}'s binpkgs and index stay as they are on the host"
        )
    else:
        _regenerate_index(remote, worker_pkgdir)
        index_ready, reason = True, None

    received = 0
    pulled_files = [f for f in job_files if f in present]
    if pulled_files:
        received += _fetch(remote, pulled_files, f"{results}/", bwlimit=bwlimit)
    run_ids = _run_ids(results / f"{job}.runs") if job_files and job_files[2] in present else ()
    if run_ids:
        runs = [f"{WORK}/out/runs/{run_id}" for run_id in run_ids]
        received += _fetch(
            remote, runs, f"{config.runs_dir()}/", step="pull out/runs", bwlimit=bwlimit
        )
    isos: tuple[Path, ...] = ()
    if arch is not None:
        if iso_dir is not None and iso_dir in present:
            received += _fetch(remote, [f"{iso_dir}/"], f"{results}/iso/", bwlimit=bwlimit)
            listed = sorted(p for p in present if p.startswith(f"{iso_dir}/"))
            isos = tuple(results / "iso" / Path(p).name for p in listed)
        for worker_dir, host_dir in caches:
            if worker_dir in present:
                received += _fetch(remote, [f"{worker_dir}/"], f"{host_dir}/", bwlimit=bwlimit)
    if arch is not None and gen is not None and worker_pkgdir is not None:
        fork_points = sorted(p for p in present if p.startswith(f"{fork_dir}/"))
        received += _pull_binhost(
            remote,
            worker_pkgdir if index_ready else None,
            config.pkgdir(arch, gen),
            fork_points,
            bwlimit=bwlimit,
        )

    log = results / f"{job}.log" if job_files and job_files[0] in present else None
    return PullResult(received, isos, run_ids, log, binhost=index_ready, binhost_reason=reason)


def job(
    remote: Remote,
    entry: WorkerEntry,
    job: str,
    args: Sequence[str],
    *,
    allow_dirty: bool,
    follow: bool,
    results: Path | None = None,
    bwlimit: int | None = None,
) -> JobResult:
    """Run ``shidashi ARGS…`` on the worker as job ``job``, from the checkout's HEAD.

    Before any transfer, in this order, each refusal a :class:`JobRefused`: the job
    name; a dirty checkout (unless ``allow_dirty``: HEAD ships, the changes do not);
    the CPU guard on the target arch (an archless command has none); the
    writability of every host directory the pull writes; one probe of the worker --
    reachable, ``/mnt/work`` mounted, no ``shidashi-job-*`` active. A PKGDIR writer
    (:func:`ownership.writes_pkgdir`) then takes the arch's owner lock. Then the
    push (the arch's subset and the venv, or the venv alone for an archless
    command), the commit's tree, and the unit; a failure in any of the three
    releases the lock (nothing runs on the worker) and re-raises.

    With ``follow`` the job's log streams to stdout until its ``<job>.rc`` appears,
    then its results come back into ``results`` (default
    ``./worker-results/<worker>/<job>/``) and the lock is released. Without
    ``follow``, or when the follow is interrupted or lost, the job keeps running,
    the lock stays, the commands to follow and pull it are printed and the result
    says ``followed=False`` -- with ``exit_code`` 0 (the unit started) when not
    followed by request, None when the follow broke. A failed pull keeps the lock,
    prints the retry and ``unlock`` commands and re-raises. Every step, the commit,
    the unit and the bytes moved go to the audit trail in force.
    """
    started = time.monotonic()
    rec = audit.current()
    args = list(args)
    try:
        validate_job_name(job)
    except ValueError as err:
        raise JobRefused(str(err), "name the job with a-z, 0-9 and '-' only") from err
    name = entry.name
    repo = _host_repo()
    commit = _checkout_head(repo, allow_dirty=allow_dirty)
    target = _cpu_guard(name, args, entry.cpu_flags)
    arch, init = target if target is not None else (None, "systemd")
    writer = ownership.writes_pkgdir(args)
    if writer and arch is None:
        raise JobRefused(
            f"{args[0]} writes an arch's PKGDIR but names no arch",
            f"name the arch: shidashi worker job {name} {job} -- {args[0]} ARCH …",
        )
    out = (results if results is not None else Path("worker-results") / name / job).absolute()
    job_gen = generation_at(repo, commit, init) if arch is not None else None
    binhost_gen = job_gen if writer else None
    problem = _writability_problem(
        pull_destinations(arch, results=out, binhost_generation=binhost_gen)
    )
    if problem is not None:
        reason, fix = problem
        raise JobRefused(reason, fix or "make the directory readable and writable by this user")
    _probe_for_job(remote, name)

    unit = f"shidashi-job-{job}"
    rec.event(
        "worker.job",
        worker=name,
        job=job,
        unit=unit,
        commit=commit,
        args=args,
        arch=arch,
        init=init,
        writes_pkgdir=writer,
        results=str(out),
    )
    owner = None
    if writer and arch is not None:
        owner = _take_lock(
            ownership.Owner(
                arch=arch,
                worker=name,
                job=job,
                commit=commit,
                since=_utc_now(),
                host_pid=None,
                generation=job_gen or "",
            )
        )
    locked = owner.arch if owner is not None else None

    resume = _resume_commands(name, job, arch, init, out if results is not None else None)
    try:
        if owner is not None:
            rec.event("worker.lock", action="acquired", arch=locked, owner=owner.model_dump())
            _clear_stale_job_files(remote, job)
        with rec.step("worker.push", worker=name, arch=arch) as step:
            if arch is not None:
                sent = push(remote, arch, init=init, bwlimit=bwlimit, gen=job_gen)
            else:
                sent = _push_runtime(remote, bwlimit=bwlimit)
            step.add(bytes_sent=sent)
        rec.metric("bytes_pushed", sent, "B", worker=name)
        with rec.step("worker.ship", worker=name, commit=commit):
            put_tree(remote, commit, f"{WORK}/src/{commit}", repo=repo)
        unit_argv = job_unit_argv(job, commit, args)
        rec.command(unit_argv, worker=name, unit=unit)
    except BaseException:
        _release_unstarted(owner, rec)
        raise
    try:
        with rec.step("worker.start", worker=name, unit=unit):
            _start_unit(remote, job, unit_argv)
    except SyncError:
        # systemd-run itself reported the failure: nothing runs on the worker
        _release_unstarted(owner, rec)
        raise
    except (KeyboardInterrupt, RemoteUnreachable) as err:
        # the unit may have started before the connection or the user gave up
        why = "interrupted" if isinstance(err, KeyboardInterrupt) else str(err)
        _start_unconfirmed(name, job, locked, resume, why=why)
        rec.event("worker.job.detached", worker=name, unit=unit, why=f"start unconfirmed: {why}")
        return JobResult(None, None, (), (), time.monotonic() - started, followed=False)
    except BaseException:
        _start_unconfirmed(name, job, locked, resume, why="the start failed")
        raise

    if not follow:
        _left_running(name, job, locked, resume, why=None)
        rec.event("worker.job.detached", worker=name, unit=unit, why="not followed")
        # 0: the unit started; the job's own exit code comes back with its results
        return JobResult(0, None, (), (), time.monotonic() - started, followed=False)

    lost: str | None
    try:
        with rec.step("worker.follow", worker=name, unit=unit) as step:
            code = stream(remote, _follow_command(job), sink=_echo)
            step.add(stream_exit=code)
    except KeyboardInterrupt:
        lost = "interrupted"
    except Exception as err:  # noqa: BLE001 - the job runs on; say how to pick it up
        lost = f"the follow failed ({type(err).__name__}: {err})"
    else:
        lost = None if code == 0 else _stream_end(code, unit)
    if lost is not None:
        _left_running(name, job, locked, resume, why=lost)
        rec.event("worker.job.detached", worker=name, unit=unit, why=lost)
        return JobResult(None, None, (), (), time.monotonic() - started, followed=False)

    try:
        with rec.step("worker.pull", worker=name, arch=arch) as step:
            pulled = pull(remote, arch, job, results=out, init=init, owner=owner, bwlimit=bwlimit)
            step.add(bytes_received=pulled.bytes, binhost=pulled.binhost)
    except BaseException:
        _failed_pull(job, locked, resume[1])
        raise
    rec.metric("bytes_pulled", pulled.bytes, "B", worker=name)
    if owner is not None:
        if not pulled.binhost and pulled.binhost_reason:
            print(f"binhost not pulled: {pulled.binhost_reason}", file=sys.stderr)
        ownership.release(owner.arch, expected=owner)
        rec.event("worker.lock", action="released", arch=locked, why="results pulled")
    exit_code = _read_rc(out / f"{job}.rc")
    duration = time.monotonic() - started
    rec.metric("exit_code", exit_code, worker=name, unit=unit)
    rec.event(
        "worker.job.end",
        worker=name,
        unit=unit,
        commit=commit,
        exit_code=exit_code,
        duration_s=round(duration, 3),
        bytes_pushed=sent,
        bytes_pulled=pulled.bytes,
        isos=[str(p) for p in pulled.isos],
        run_ids=list(pulled.run_ids),
    )
    return JobResult(exit_code, pulled.log, pulled.isos, pulled.run_ids, duration)


def _take_lock(owner: ownership.Owner) -> ownership.Owner:
    """Acquire ``owner.arch``'s owner lock for a job; another holder refuses the job."""
    try:
        return ownership.acquire(owner.arch, owner)
    except ownership.OwnedElsewhere as err:
        raise JobRefused(
            str(err),
            f"wait for {err.holder.job} to end and its results to be pulled, or release the "
            f"lock with `shidashi worker unlock {owner.arch}` once that job has ended",
        ) from err


def _utc_now() -> str:
    """Now, ISO-8601 UTC to the second (``2026-10-05T14:02:31Z``)."""
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def _git(repo: Path, *args: str) -> str:
    """``git -C repo ARGS…``'s stdout; a failure refuses the job (nothing to ship)."""
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if done.returncode != 0:
        raise JobRefused(
            f"git {' '.join(args)} failed in the Shidashi checkout {repo} "
            f"(exit {done.returncode}): {_tail(done.stderr)}",
            "run shidashi from a git checkout of it: a job ships a commit",
        )
    return done.stdout


def _checkout_head(repo: Path, *, allow_dirty: bool) -> str:
    """The checkout's HEAD commit; refuse uncommitted changes to tracked files.

    Untracked files never count (``git archive`` never ships them). With
    ``allow_dirty`` HEAD ships and a line says the changes do not.
    """
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    changed = [line[3:] for line in status.splitlines() if len(line) > 3]
    if changed and not allow_dirty:
        raise JobRefused(
            f"the Shidashi checkout {repo} has uncommitted changes to tracked files: "
            + ", ".join(changed),
            "commit or stash them, or pass --allow-dirty to ship HEAD without them",
        )
    if changed:
        print(
            "--allow-dirty: shipping HEAD; the uncommitted changes to "
            f"{', '.join(changed)} are not included",
            file=sys.stderr,
        )
    return _git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()


def _cpu_guard(name: str, args: Sequence[str], cpu_flags: Sequence[str]) -> tuple[str, str] | None:
    """``(arch, init)`` of the job's command, refused when the worker's CPU cannot run
    that arch; None for an archless command (no guard)."""
    target = isaguard.job_target(args)
    if target is None:
        return None
    arch = target[0]
    try:
        _require_arch(arch)
    except ValueError as err:
        raise JobRefused(str(err), "name an arch fragment of variants/arch/") from err
    try:
        lacking = isaguard.missing(arch, cpu_flags)
    except isaguard.UnknownFlag as err:
        raise JobRefused(
            str(err), f"map {err.flag} to its /proc/cpuinfo name in isaguard.CPUINFO_NAME"
        ) from err
    if lacking:
        raise JobRefused(
            f"{name}'s CPU cannot run {arch}: it lacks {', '.join(lacking)}",
            f"send the {arch} job to a worker whose CPU runs it "
            "(shidashi worker status lists the arches each worker runs)",
        )
    return target


def _probe_for_job(remote: Remote, name: str) -> None:
    """One ssh round trip: refuse an unreachable worker, one without its work disk,
    and one already running a Shidashi job (one job at a time per worker).

    A changed host key (:class:`HostKeyMismatch`) propagates: a security event.
    """
    script = (
        f"if findmnt -no TARGET -M {WORK} >/dev/null 2>&1; then echo work=mounted; fi; "
        f"{_LIST_JOBS} 2>/dev/null; true"
    )
    try:
        result = run(remote, script, timeout=_JOB_PROBE_TIMEOUT)
    except RemoteUnreachable as err:
        raise JobRefused(
            str(err), f"power the worker on and check it answers: shidashi worker status {name}"
        ) from err
    if result.exit_code != 0:
        raise JobRefused(
            f"the probe of {name} failed (exit {result.exit_code}): {_tail(result.stderr)}",
            f"check the worker: shidashi worker status {name}",
        )
    lines = result.stdout.splitlines()
    running = _active_jobs(lines)
    if "work=mounted" not in lines:
        raise JobRefused(
            f"{WORK} is not mounted on {name}: a job there would write its RAM root",
            f"attach and mount the work disk at {WORK}, then start the job again",
        )
    if running:
        other = running[0].removeprefix("shidashi-job-")
        raise JobRefused(
            f"{name} is already running {', '.join(running)}: one job at a time per worker",
            f"wait for it to end (shidashi worker logs {name} {other} -f), then start this job",
        )


def _release_unstarted(owner: ownership.Owner | None, rec: audit.Recorder) -> None:
    """Release a job's lock when its unit never started (R3.13)."""
    if owner is not None:
        ownership.release(owner.arch, expected=owner)
        rec.event("worker.lock", action="released", arch=owner.arch, why="the job did not start")


def _start_unit(remote: Remote, job: str, argv: Sequence[str]) -> None:
    """Start the job's unit; a non-zero exit (of ``systemd-run`` or of the cleanup
    before it) is a failed start: :class:`SyncError`.

    The previous run's ``<job>.rc``, ``.rc.tmp`` and ``.runs`` go in the SAME ssh
    command, before the unit exists: a follow must never see an old rc as this
    job's end, whenever the unit's own cleanup runs.
    """
    result = run(remote, f"{_clear_command(job)} && {shlex.join(argv)}", timeout=_START_TIMEOUT)
    if result.exit_code != 0:
        raise SyncError(
            f"start: {argv[1].removeprefix('--unit=')}",
            result.exit_code,
            _tail(result.stderr or result.stdout),
        )


def _clear_command(job: str) -> str:
    """``rm -f`` of what an earlier run of ``job`` left that would read as this run's
    end or runs: ``<job>.rc``, ``.rc.tmp`` and ``.runs``."""
    jobs = f"{WORK}/out/jobs/{job}"
    return "rm -f " + " ".join(shlex.quote(f"{jobs}{ext}") for ext in (".rc", ".rc.tmp", ".runs"))


def _clear_stale_job_files(remote: Remote, job: str) -> None:
    """Remove an earlier run's rc and runs as soon as a writer holds the lock: until the
    unit exists, ``sync pull --job`` and ``unlock`` must not read an old rc as this
    job's end. Safe: the probe found no ``shidashi-job-*`` running, and the lock is
    ours."""
    result = run(remote, _clear_command(job), timeout=_JOB_PROBE_TIMEOUT)
    if result.exit_code != 0:
        raise SyncError(
            f"clear the stale files of job {job}", result.exit_code, _tail(result.stderr)
        )


def _follow_command(job: str, *, grace: int = 0) -> str:
    """The worker-side follow: the job's log from its first line until its rc exists.

    ``tail -F`` never ends by itself: it follows ``--pid`` of a waiter that ends
    when ``<job>.rc`` appears (or the unit has not been active for more than
    ``grace`` consecutive checks, a second apart, without one), then flushes what is
    left and exits. The command exits 0 only when the rc exists. ``job`` follows a
    unit it has just started (``grace`` 0); ``logs -f`` may start on a unit that is
    not active yet or any more, and waits ``grace`` seconds for its rc.
    """
    jobs = f"{WORK}/out/jobs/{job}"
    log, rc = shlex.quote(f"{jobs}.log"), shlex.quote(f"{jobs}.rc")
    unit = shlex.quote(f"shidashi-job-{job}")
    waiter = (
        f"n=0; while [ ! -e {rc} ]; do if systemctl is-active --quiet {unit}; then n=0; "
        f"else n=$((n+1)); [ $n -gt {int(grace)} ] && break; fi; sleep 1; done"
    )
    return f"( {waiter} ) & w=$!; tail -n +1 -F --pid=$w {log} 2>/dev/null; wait $w; [ -e {rc} ]"


def _echo(line: str) -> None:
    """One log line to stdout; what the terminal's encoding cannot show is replaced."""
    out = sys.stdout
    encoding = getattr(out, "encoding", None) or "utf-8"
    out.write(line.encode(encoding, errors="replace").decode(encoding, errors="replace"))
    out.flush()


#: ``logs -f`` waits this many seconds for the rc of a unit that is not active: a job
#: stopped by hand (``systemctl stop``) never writes one, and the follow must end.
LOGS_GRACE = 10
#: ssh's last words when the worker closed the connection under a sent command.
_CLOSED_RE = re.compile(
    r"closed by remote host|^Connection to \S+ closed|client_loop: send disconnect"
    r"|Connection reset by peer",
    re.M,
)
_POWEROFF_TIMEOUT = 30.0


def run_command(remote: Remote, argv: Sequence[str], *, sink: Callable[[str], None] = _echo) -> int:
    """Run ``argv`` on the worker, each word quoted for its shell, each output line
    (stderr merged) to ``sink`` as it comes; returns ssh's exit code -- the remote
    command's, or 255 when ssh itself failed (R2.1)."""
    return stream(remote, shlex.join(argv), sink=sink)


def logs(remote: Remote, job: str, *, follow: bool, sink: Callable[[str], None] = _echo) -> int:
    """The job's log to ``sink``; with ``follow``, until the job ends (R7.1).

    Raises :class:`ValueError` for an invalid job name, before any contact. Returns
    ssh's exit code: 0 once the log was printed -- with ``follow``, once
    ``<job>.rc`` exists; 1 when the unit was not active for :data:`LOGS_GRACE` s
    without writing an rc; 255 when ssh failed.
    """
    validate_job_name(job)
    if follow:
        return stream(remote, _follow_command(job, grace=LOGS_GRACE), sink=sink)
    log = shlex.quote(f"{WORK}/out/jobs/{job}.log")
    return stream(remote, f"cat -- {log}", sink=sink)


def active_jobs(remote: Remote, *, timeout: float = _JOB_PROBE_TIMEOUT) -> tuple[str, ...]:
    """The worker's active ``shidashi-job-*`` units, without ``.service``; one ssh round
    trip. Raises :class:`RemoteError` (unreachable, host key) and :class:`SyncError`
    when ``systemctl`` fails."""
    result = run(remote, _LIST_JOBS, timeout=timeout)
    if result.exit_code != 0:
        raise SyncError("list the jobs", result.exit_code, _tail(result.stderr))
    return _active_jobs(result.stdout.splitlines())


#: A unit's ``ActiveState`` when it does not run; a unit that is not loaded (never
#: started, or collected after it ended) reads ``inactive``. Every other state --
#: ``active``, ``activating``, ``deactivating``, ``reloading``... -- is running.
_STOPPED_STATES = frozenset({"inactive", "failed"})


@dataclass(frozen=True)
class JobState:
    """A job on the worker: whether its ``<job>.rc`` exists and its unit's ActiveState."""

    rc: bool
    active_state: str

    @property
    def running(self) -> bool:
        """The unit is starting, running or stopping: anything but inactive or failed."""
        return self.active_state not in _STOPPED_STATES

    @property
    def ended(self) -> bool:
        """Ended: the rc exists AND the unit no longer runs (R6.9). The wrapper writes
        the rc just before the unit ends, and :func:`job` clears an earlier run's rc
        once it holds the lock, so neither alone is the end."""
        return self.rc and not self.running


def job_state(remote: Remote, job: str, *, timeout: float = _JOB_PROBE_TIMEOUT) -> JobState:
    """Job ``job``'s :class:`JobState` on the worker; one ssh round trip.

    Raises :class:`ValueError` for an invalid name (before any contact),
    :class:`RemoteError` when the worker does not answer and :class:`SyncError` when
    ``systemctl`` itself fails.
    """
    validate_job_name(job)
    rc = shlex.quote(f"{WORK}/out/jobs/{job}.rc")
    unit = f"shidashi-job-{job}"
    script = (
        f"if [ -e {rc} ]; then echo rc=present; fi; "
        f's=$(systemctl show -p ActiveState --value {shlex.quote(unit)}) && echo "state=$s"'
    )
    result = run(remote, script, timeout=timeout)
    lines = result.stdout.splitlines()
    states = [line.removeprefix("state=") for line in lines if line.startswith("state=")]
    if result.exit_code != 0 or len(states) != 1 or not states[0].strip():
        raise SyncError(f"probe {unit}", result.exit_code, _tail(result.stderr))
    return JobState(rc="rc=present" in lines, active_state=states[0].strip())


def job_active(remote: Remote, job: str, *, timeout: float = _JOB_PROBE_TIMEOUT) -> bool:
    """Whether ``shidashi-job-<job>`` -- that exact unit -- runs on the worker: starting,
    active or stopping (:attr:`JobState.running`). Raises what :func:`job_state` raises."""
    return job_state(remote, job, timeout=timeout).running


def job_ended(remote: Remote, job: str, *, timeout: float = _JOB_PROBE_TIMEOUT) -> bool:
    """Whether job ``job`` has ended on the worker (:attr:`JobState.ended`). Raises what
    :func:`job_state` raises."""
    return job_state(remote, job, timeout=timeout).ended


def poweroff(remote: Remote, *, timeout: float = _POWEROFF_TIMEOUT) -> None:
    """``systemctl poweroff`` on the worker (R7.2); whether a job runs is the caller's.

    The worker usually closes the connection under the command: ssh's exit 255 with
    "closed by remote host" is the poweroff taking effect, not a failure. Any other
    ssh failure propagates (:class:`RemoteUnreachable`, :class:`HostKeyMismatch`);
    any other non-zero exit is :class:`SyncError` with systemctl's message.
    """
    try:
        # --no-block: queue the poweroff and return, before the shutdown takes ssh down
        result = run(remote, "systemctl --no-block poweroff", timeout=timeout)
    except RemoteUnreachable as err:
        if err.reason and _CLOSED_RE.search(err.reason):
            return
        raise
    if result.exit_code != 0:
        raise SyncError("poweroff", result.exit_code, _tail(result.stderr or result.stdout))


def _stream_end(code: int, unit: str) -> str:
    if code == 255:
        return "the connection to the worker was lost"
    return f"the log stream ended (exit {code}) before {unit} wrote its exit code"


def _resume_commands(
    name: str, job: str, arch: str | None, init: str, results: Path | None = None
) -> tuple[str, str]:
    """The commands that follow the job again and pull its results; a job started with
    its own results directory gets it back in the pull (R6.13), quoted for the shell."""
    pull_cmd = f"shidashi worker sync pull {name}"
    if arch is not None:
        pull_cmd += f" --arch {arch}"
    pull_cmd += f" --job {job}"
    if arch is not None and init != "systemd":
        pull_cmd += f" --init {init}"
    if results is not None:
        pull_cmd += f" --results {shlex.quote(str(results))}"
    return f"shidashi worker logs {name} {job} -f", pull_cmd


def _left_running(
    name: str, job: str, locked: str | None, resume: tuple[str, str], *, why: str | None
) -> None:
    """Say the job keeps running on the worker and how to pick it up again."""
    head = f"{why}: " if why else ""
    lock = f"; {locked}'s owner lock stays held by it" if locked else ""
    print(
        f"{head}job {job} keeps running on {name}{lock}.\n"
        f"  follow it:              {resume[0]}\n"
        f"  pull its results after: {resume[1]}",
        file=sys.stderr,
    )


def _start_unconfirmed(
    name: str, job: str, locked: str | None, resume: tuple[str, str], *, why: str
) -> None:
    """The unit's start was not confirmed: it may run. Keep the lock and say how to
    follow it, pull it, or -- if it never started -- release the lock."""
    lines = [
        f"{why}: the start of job {job} on {name} was not confirmed; it may be running.",
        f"  follow it:              {resume[0]}",
        f"  pull its results after: {resume[1]}",
    ]
    if locked:
        lines[0] += f" {locked}'s owner lock stays held by it."
        lines.append(f"  if it did not start, release the lock: shidashi worker unlock {locked}")
    print("\n".join(lines), file=sys.stderr)


def _failed_pull(job: str, locked: str | None, retry: str) -> None:
    """Say the pull failed, how to retry it and, for a lock holder, how to give up."""
    lines = [f"pulling the results of job {job} failed.", f"  retry:  {retry}"]
    if locked:
        lines[0] += f" {locked}'s owner lock stays held by {job}."
        lines.append(
            f"  or give its binpkgs up and release the lock: shidashi worker unlock {locked}"
        )
    print("\n".join(lines), file=sys.stderr)


def _read_rc(path: Path) -> int:
    """The job's exit code from its pulled ``<job>.rc``."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as err:
        raise SyncError("job exit code", None, f"{path}: {err}") from err


def _require_arch(arch: str) -> None:
    """Refuse a name that is not an arch fragment (``*`` would glob every arch)."""
    known = config.available_names("arch")
    if arch not in known:
        raise ValueError(f"unknown arch {arch!r}: expected one of {', '.join(known)}")


def _writable_dir(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _first_unwritable_dir(root: Path) -> Path | None:
    """``root`` or the first directory under it this user cannot write; None if none.

    Symlinks are not followed: rsync replaces a link, it does not write through it.
    """
    if not _writable_dir(root):
        return root
    for top, dirs, _files in os.walk(root):
        dirs.sort()
        for name in dirs:
            path = Path(top) / name
            if not path.is_symlink() and not _writable_dir(path):
                return path
    return None


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _unwritable(dest: Path, blocked: Path) -> tuple[str, str]:
    """Why ``blocked`` stops a pull into ``dest``, and the root command that fixes it.

    The fix keeps the directory's group when this user is in it (``chmod g+w``) and
    changes it to the user's group only when not; inside an existing destination it
    is recursive, since its sibling leaves are usually alike.
    """
    user = _user_name(os.getuid())
    where = f"{dest}" if blocked == dest else f"{dest}: {blocked}"
    try:
        st = blocked.stat()
    except OSError as err:
        return f"{where} cannot be inspected by {user} ({err}); nothing was transferred", ""
    target = dest if dest.exists() else blocked
    recursive = "-R " if dest.exists() else ""
    if st.st_uid == os.getuid():
        fix = f"chmod {recursive}u+w {target}"
    elif st.st_gid in {os.getegid(), *os.getgroups()}:
        fix = f"chmod {recursive}g+w {target}"
    else:
        mine = _group_name(os.getgid())
        fix = f"chgrp {recursive}{mine} {target} && chmod {recursive}g+w {target}"
    reason = (
        f"{where} is not writable by {user} (owned by {_user_name(st.st_uid)}:"
        f"{_group_name(st.st_gid)}, mode {stat.S_IMODE(st.st_mode):04o}); "
        "nothing was transferred"
    )
    return reason, f"Fix it as root: {fix}"


def _regenerate_index(remote: Remote, worker_pkgdir: str) -> None:
    """``emaint binhost --fix`` over the worker's PKGDIR, then prove the index exists."""
    pkgdir = shlex.quote(worker_pkgdir)
    result = run(remote, f"PKGDIR={pkgdir} emaint binhost --fix", timeout=_INDEX_TIMEOUT)
    if result.exit_code != 0:
        raise SyncError(
            f"index: emaint binhost --fix ({worker_pkgdir})",
            result.exit_code,
            _tail(result.stderr or result.stdout),
        )
    index = f"{worker_pkgdir}/Packages"
    if index not in _present(remote, [index]):
        raise SyncError(
            f"index: emaint binhost --fix ({worker_pkgdir})",
            None,
            f"{index} does not exist after the regeneration",
        )


def _shell_pattern(path: str) -> str:
    """``path`` quoted for the worker's shell, its ``*``/``?`` left to expand."""
    return "".join(
        part if part in ("*", "?") else shlex.quote(part)
        for part in re.split(r"([*?])", path)
        if part
    )


def _present(remote: Remote, patterns: Sequence[str]) -> list[str]:
    """Which of ``patterns`` exist on the worker (a glob yields each match), in one ssh.

    rsync cannot skip a missing ``dir/`` source (``--ignore-missing-args`` covers
    files only), so a pull asks first instead of failing on an absent sccache.
    """
    words = " ".join(_shell_pattern(p) for p in patterns)
    command = f'for p in {words}; do [ -e "$p" ] && printf \'%s\\n\' "$p"; done; true'
    result = run(remote, command, timeout=_PROBE_TIMEOUT)
    if result.exit_code != 0:
        raise SyncError("pull: list the job's results", result.exit_code, _tail(result.stderr))
    return [line for line in result.stdout.splitlines() if line]


def _run_ids(runs_file: Path) -> tuple[str, ...]:
    """The run ids a job listed in its ``<job>.runs`` (one per line)."""
    ids = tuple(line.strip() for line in runs_file.read_text(encoding="utf-8").splitlines())
    ids = tuple(i for i in ids if i)
    for run_id in ids:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise SyncError(
                "pull out/runs", None, f"{runs_file} lists an invalid run id {run_id!r}"
            )
    return ids


def _pull_binhost(
    remote: Remote,
    worker_pkgdir: str | None,
    host_pkgdir: Path,
    fork_points: Sequence[str],
    *,
    bwlimit: int | None = None,
) -> int:
    """The arch's binpkgs, then its fork points, then its index -- replaced atomically.

    ``worker_pkgdir`` is None when the worker has no PKGDIR (nothing was built): only
    the fork points come, and the host's binpkgs and index are left as they are.
    """
    received = 0
    if worker_pkgdir is not None:
        received += _fetch(
            remote,
            [f"{worker_pkgdir}/"],
            f"{host_pkgdir}/",
            excludes=["/Packages"],
            bwlimit=bwlimit,
        )
    if fork_points:
        fork_dest = f"{config.fork_points_dir()}/"
        received += _fetch(
            remote, list(fork_points), fork_dest, step="pull cache/fork-points", bwlimit=bwlimit
        )
    if worker_pkgdir is not None:
        staged = host_pkgdir / "Packages.tmp"
        received += _fetch(remote, [f"{worker_pkgdir}/Packages"], str(staged), bwlimit=bwlimit)
        os.replace(staged, host_pkgdir / "Packages")
    return received


def _fetch(
    remote: Remote,
    sources: list[str],
    dest: str,
    *,
    step: str | None = None,
    excludes: Sequence[str] = (),
    bwlimit: int | None = None,
) -> int:
    """One pull rsync (worker ``sources`` into host ``dest``); bytes received.

    The step defaults to the first source under ``/mnt/work`` (``pull cache/ccache``).
    """
    step = step or "pull " + sources[0].removeprefix(f"{WORK}/").rstrip("/")
    return _rsync(remote, sources, dest, step=step, bwlimit=bwlimit, excludes=excludes, push=False)


def _venv_home(venv: Path) -> str:
    """The interpreter directory ``pyvenv.cfg`` names (``home = …``)."""
    cfg = venv / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError as err:
        raise SyncError("runtime", None, f"cannot read {cfg}: {err}") from err
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home" and value.strip():
            return value.strip()
    raise SyncError("runtime", None, f"{cfg} names no interpreter (no home = …)")


def _require_work_disk(remote: Remote) -> None:
    result = run(remote, f"findmnt -no TARGET -M {WORK}")
    if result.exit_code != 0:
        raise SyncError(
            "work disk",
            result.exit_code,
            f"{WORK} is not mounted on {remote.name}: nothing is pushed onto its RAM root "
            "(attach and mount the work disk, then push again)",
        )


def _require_interpreter(remote: Remote, home: str) -> None:
    result = run(remote, f"test -d {shlex.quote(home)}")
    if result.exit_code != 0:
        raise SyncError(
            "runtime",
            result.exit_code,
            f"the venv's interpreter {home} (pyvenv.cfg home=) does not exist on "
            f"{remote.name}: the worker image must ship that Python",
        )


def _host_repo() -> Path:
    """The Shidashi checkout this process imports (where an editable install points)."""
    return Path(shidashi.__file__).resolve().parent.parent


def _host_bound_pths(venv: Path) -> list[str]:
    """rsync excludes, anchored at the venv root, for each ``.pth`` naming the host repo.

    Matched by content, not by name: uv writes ``_editable_impl_shidashi.pth``,
    setuptools ``__editable__.*.pth``. Shipped, they would point the worker at a
    path it does not have; the job sets ``PYTHONPATH`` to the shipped commit instead.
    """
    repo = _host_repo()
    needles = {str(repo), str(repo.resolve())}
    excludes: list[str] = []
    for pth in sorted(venv.glob("lib/python*/site-packages/*.pth")):
        try:
            content = pth.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # unreadable: rsync reports it if it matters
        if any(needle in content for needle in needles):
            rel = pth.relative_to(venv).as_posix()
            excludes.append("/" + re.sub(r"([*?\[\\])", r"\\\1", rel))
    return excludes


def _expand(host_path: str) -> list[str]:
    """The existing host sources of one plan entry (a glob expands, a missing path drops)."""
    path = Path(host_path)
    if _GLOB_CHARS.intersection(path.name):
        return [str(p) for p in sorted(path.parent.glob(path.name))]
    return [host_path] if path.exists() else []


def _rsync(
    remote: Remote,
    sources: list[str],
    dest: str,
    *,
    step: str,
    bwlimit: int | None,
    excludes: Sequence[str] = (),
    push: bool = True,
) -> int:
    """One rsync; returns the bytes that crossed (sent on a push, received on a pull)."""
    argv = rsync_argv(remote, sources, dest, push=push, bwlimit=bwlimit, excludes=excludes)
    done = subprocess.run(
        argv, capture_output=True, encoding="utf-8", errors="replace", check=False
    )
    if done.returncode != 0:
        raise SyncError(step, done.returncode, _tail(done.stderr))
    return _stat_bytes(done.stdout, _SENT_RE if push else _RECEIVED_RE)


def _stat_bytes(stats: str, pattern: re.Pattern[str]) -> int:
    """``sent``/``received N bytes`` of rsync's ``--info=stats1``.

    Every non-digit is dropped: the thousands separator follows the locale.
    """
    match = pattern.search(stats)
    return int(re.sub(r"\D", "", match.group(1))) if match else 0


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
