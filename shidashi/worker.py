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
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import shidashi
from shidashi import audit, config, isaguard, ownership
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


def generation(init: str) -> str:
    """The pinned generation (the stage3 snapshot) of ``init``."""
    return load_pointer(init, seeds_dir=config.seeds_dir()).snapshot


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
    already name one. A bash wrapper clears what an earlier job of the same name left
    (``out/iso/<job>``, ``<job>.rc``, ``<job>.runs``), runs the command into
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
        command[1:1] = ["--output-dir", f"{out}/iso/{job}"]
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


def push(remote: Remote, arch: str, *, init: str = "systemd", bwlimit: int | None) -> int:
    """Send ``arch``'s cache subset and the runtime venv to the worker; return bytes sent.

    Refuses (:class:`SyncError`, before any transfer) a worker without its work disk
    mounted at ``/mnt/work`` and a worker lacking the interpreter the venv's
    ``pyvenv.cfg`` names. The venv goes first (small), the plan after it, fork points
    last. Every rsync is capped by ``bwlimit`` (KiB/s) when given; a non-zero exit
    raises :class:`SyncError` naming the step.
    """
    _require_arch(arch)
    sent = _push_runtime(remote, bwlimit=bwlimit)
    for host_path, worker_path in push_plan(arch, generation(init)):
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
    job: str,
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
    archless pull takes only the log, rc and runs). Only with ``owner`` -- and
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
    validate_job_name(job)
    if arch is not None:
        _require_arch(arch)
    gen: str | None = None
    if owner is not None:
        if arch is None:
            raise ValueError("an archless pull has no binhost to bring back")
        ownership.require_free(arch, as_owner=owner)
        gen = generation(init)
    require_writable(pull_destinations(arch, results=results, binhost_generation=gen))

    cache = config.cache_dir()
    job_files = [f"{WORK}/out/jobs/{job}{ext}" for ext in (".log", ".rc", ".runs")]
    iso_dir = f"{WORK}/out/iso/{job}"
    caches = [
        (f"{WORKER_CACHE}/{d.relative_to(cache)}", d)
        for d in (config.distdir(), config.ccache_dir(), config.sccache_dir())
    ]
    fork_dir = f"{WORKER_CACHE}/{config.fork_points_dir().relative_to(cache)}"
    patterns = list(job_files)
    if arch is not None:
        patterns += [iso_dir, f"{iso_dir}/*.iso", *(w for w, _ in caches)]
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
    run_ids = _run_ids(results / f"{job}.runs") if job_files[2] in present else ()
    if run_ids:
        runs = [f"{WORK}/out/runs/{run_id}" for run_id in run_ids]
        received += _fetch(
            remote, runs, f"{config.runs_dir()}/", step="pull out/runs", bwlimit=bwlimit
        )
    isos: tuple[Path, ...] = ()
    if arch is not None:
        if iso_dir in present:
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

    log = results / f"{job}.log" if job_files[0] in present else None
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
    binhost_gen = generation(init) if writer and arch is not None else None
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
                arch=arch, worker=name, job=job, commit=commit, since=_utc_now(), host_pid=None
            )
        )
    locked = owner.arch if owner is not None else None

    resume = _resume_commands(name, job, arch, init)
    try:
        if owner is not None:
            rec.event("worker.lock", action="acquired", arch=locked, owner=owner.model_dump())
        with rec.step("worker.push", worker=name, arch=arch) as step:
            if arch is not None:
                sent = push(remote, arch, init=init, bwlimit=bwlimit)
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
        "systemctl list-units 'shidashi-job-*' --state=active --plain --no-legend --full "
        "2>/dev/null; true"
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
    running = []
    for line in lines:
        words = line.replace("\u25cf", " ").split()
        if words and words[0].startswith("shidashi-job-"):
            running.append(words[0].removesuffix(".service"))
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
    jobs = f"{WORK}/out/jobs/{job}"
    stale = " ".join(shlex.quote(f"{jobs}{ext}") for ext in (".rc", ".rc.tmp", ".runs"))
    result = run(remote, f"rm -f {stale} && {shlex.join(argv)}", timeout=_START_TIMEOUT)
    if result.exit_code != 0:
        raise SyncError(
            f"start: {argv[1].removeprefix('--unit=')}",
            result.exit_code,
            _tail(result.stderr or result.stdout),
        )


def _follow_command(job: str) -> str:
    """The worker-side follow: the job's log from its first line until its rc exists.

    ``tail -F`` never ends by itself: it follows ``--pid`` of a waiter that ends
    when ``<job>.rc`` appears (or the unit is gone without one), then flushes what
    is left and exits. The command exits 0 only when the rc exists.
    """
    jobs = f"{WORK}/out/jobs/{job}"
    log, rc = shlex.quote(f"{jobs}.log"), shlex.quote(f"{jobs}.rc")
    unit = shlex.quote(f"shidashi-job-{job}")
    return (
        f"( while [ ! -e {rc} ] && systemctl is-active --quiet {unit}; do sleep 1; done ) & "
        f"w=$!; tail -n +1 -F --pid=$w {log} 2>/dev/null; wait $w; [ -e {rc} ]"
    )


def _echo(line: str) -> None:
    """One log line to stdout; what the terminal's encoding cannot show is replaced."""
    out = sys.stdout
    encoding = getattr(out, "encoding", None) or "utf-8"
    out.write(line.encode(encoding, errors="replace").decode(encoding, errors="replace"))
    out.flush()


def _stream_end(code: int, unit: str) -> str:
    if code == 255:
        return "the connection to the worker was lost"
    return f"the log stream ended (exit {code}) before {unit} wrote its exit code"


def _resume_commands(name: str, job: str, arch: str | None, init: str) -> tuple[str, str]:
    """The commands that follow the job again and pull its results."""
    pull_cmd = f"shidashi worker sync pull {name}"
    if arch is not None:
        pull_cmd += f" --arch {arch}"
    pull_cmd += f" --job {job}"
    if arch is not None and init != "systemd":
        pull_cmd += f" --init {init}"
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
