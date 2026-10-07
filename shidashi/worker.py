"""Status, jobs and cache sync of a paired worker, as functions over a :class:`Remote`.

The worker mirrors the host's cache under ``/mnt/work/cache`` (``SHIDASHI_CACHE`` of a
job) and runs the host's virtual environment from ``/mnt/work/runtime/venv``. A push
sends only one arch's subset -- its PKGDIR of the pinned generation and its fork
points -- plus the shared caches, never deletes on the worker, and resumes an
interrupted transfer (``--partial``). A pull brings back a job's log, audit trails,
ISOs and the shared caches always, and the arch's binpkgs, fork points and index only
for the holder of the arch's owner lock; it never deletes on the host either.
"""

import grp
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import shidashi
from shidashi import config, ownership
from shidashi.remote import Remote, SyncError, rsync_argv, run
from shidashi.seed import load_pointer

#: The worker's work disk; everything a push writes lives under it (never the RAM root).
WORK = "/mnt/work"
#: The worker's mirror of the host cache (``config.cache_dir()``).
WORKER_CACHE = f"{WORK}/cache"
#: Where the host's virtual environment runs on the worker.
WORKER_VENV = f"{WORK}/runtime/venv"

_SENT_RE = re.compile(r"^sent ([\d.,]+) bytes", re.M)
_RECEIVED_RE = re.compile(r"^sent [\d.,]+ bytes\s+received ([\d.,]+) bytes", re.M)
_GLOB_CHARS = frozenset("*?[")
_JOB_NAME_RE = re.compile(r"[a-z0-9-]+")
#: A run id as ``audit`` names it (``<UTC stamp>-<hex>``); never a path.
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: ``emaint binhost --fix`` reads every binpkg of the PKGDIR: minutes, not seconds.
_INDEX_TIMEOUT = 3600.0
_PROBE_TIMEOUT = 60.0


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


def generation(init: str) -> str:
    """The pinned generation (the stage3 snapshot) of ``init``."""
    return load_pointer(init, seeds_dir=config.seeds_dir()).snapshot


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
    venv = Path(sys.prefix)
    home = _venv_home(venv)
    _require_work_disk(remote)
    _require_interpreter(remote, home)

    sent = _rsync(
        remote,
        [f"{venv}/"],
        f"{WORKER_VENV}/",
        step="push runtime/venv",
        bwlimit=bwlimit,
        excludes=_host_bound_pths(venv),
    )
    for host_path, worker_path in push_plan(arch, generation(init)):
        sources = _expand(host_path)
        if not sources:
            continue  # nothing of that kind on the host yet (no sccache, no fork point)
        step = "push " + worker_path.removeprefix(f"{WORK}/").rstrip("/")
        sent += _rsync(remote, sources, worker_path, step=step, bwlimit=bwlimit)
    return sent


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
    for path in paths:
        if path.exists():
            blocked = _first_unwritable_dir(path)
        else:
            parent = path
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            blocked = None if _writable_dir(parent) else parent
        if blocked is not None:
            raise SyncError("host destination", None, _unwritable(path, blocked))


def pull(
    remote: Remote,
    arch: str | None,
    job: str,
    *,
    results: Path,
    init: str = "systemd",
    owner: ownership.Owner | None = None,
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
    before any transfer (:class:`SyncError` naming the directory and the fix).
    """
    if not _JOB_NAME_RE.fullmatch(job):
        raise ValueError(f"invalid job name {job!r}: only a-z, 0-9 and '-'")
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
        received += _fetch(remote, pulled_files, f"{results}/")
    run_ids = _run_ids(results / f"{job}.runs") if job_files[2] in present else ()
    if run_ids:
        runs = [f"{WORK}/out/runs/{run_id}" for run_id in run_ids]
        received += _fetch(remote, runs, f"{config.runs_dir()}/", step="pull out/runs")
    isos: tuple[Path, ...] = ()
    if arch is not None:
        if iso_dir in present:
            received += _fetch(remote, [f"{iso_dir}/"], f"{results}/iso/")
            listed = sorted(p for p in present if p.startswith(f"{iso_dir}/"))
            isos = tuple(results / "iso" / Path(p).name for p in listed)
        for worker_dir, host_dir in caches:
            if worker_dir in present:
                received += _fetch(remote, [f"{worker_dir}/"], f"{host_dir}/")
    if arch is not None and gen is not None and worker_pkgdir is not None:
        fork_points = sorted(p for p in present if p.startswith(f"{fork_dir}/"))
        received += _pull_binhost(
            remote,
            worker_pkgdir if index_ready else None,
            config.pkgdir(arch, gen),
            fork_points,
        )

    log = results / f"{job}.log" if job_files[0] in present else None
    return PullResult(received, isos, run_ids, log, binhost=index_ready, binhost_reason=reason)


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


def _unwritable(dest: Path, blocked: Path) -> str:
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
        return f"{where} cannot be inspected by {user} ({err}); nothing was transferred"
    target = dest if dest.exists() else blocked
    recursive = "-R " if dest.exists() else ""
    if st.st_uid == os.getuid():
        fix = f"chmod {recursive}u+w {target}"
    elif st.st_gid in {os.getegid(), *os.getgroups()}:
        fix = f"chmod {recursive}g+w {target}"
    else:
        mine = _group_name(os.getgid())
        fix = f"chgrp {recursive}{mine} {target} && chmod {recursive}g+w {target}"
    return (
        f"{where} is not writable by {user} (owned by {_user_name(st.st_uid)}:"
        f"{_group_name(st.st_gid)}, mode {stat.S_IMODE(st.st_mode):04o}); "
        f"nothing was transferred. Fix it as root: {fix}"
    )


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
    remote: Remote, worker_pkgdir: str | None, host_pkgdir: Path, fork_points: Sequence[str]
) -> int:
    """The arch's binpkgs, then its fork points, then its index -- replaced atomically.

    ``worker_pkgdir`` is None when the worker has no PKGDIR (nothing was built): only
    the fork points come, and the host's binpkgs and index are left as they are.
    """
    received = 0
    if worker_pkgdir is not None:
        received += _fetch(remote, [f"{worker_pkgdir}/"], f"{host_pkgdir}/", excludes=["/Packages"])
    if fork_points:
        fork_dest = f"{config.fork_points_dir()}/"
        received += _fetch(remote, list(fork_points), fork_dest, step="pull cache/fork-points")
    if worker_pkgdir is not None:
        staged = host_pkgdir / "Packages.tmp"
        received += _fetch(remote, [f"{worker_pkgdir}/Packages"], str(staged))
        os.replace(staged, host_pkgdir / "Packages")
    return received


def _fetch(
    remote: Remote,
    sources: list[str],
    dest: str,
    *,
    step: str | None = None,
    excludes: Sequence[str] = (),
) -> int:
    """One pull rsync (worker ``sources`` into host ``dest``); bytes received.

    The step defaults to the first source under ``/mnt/work`` (``pull cache/ccache``).
    """
    step = step or "pull " + sources[0].removeprefix(f"{WORK}/").rstrip("/")
    return _rsync(remote, sources, dest, step=step, bwlimit=None, excludes=excludes, push=False)


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
