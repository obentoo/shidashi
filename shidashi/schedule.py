"""The distributed build scheduler of ``shidashi build`` (story 014).

Before a build assembles anything, a pure planner decides which node -- the host or a
paired worker -- makes each image. This part is pure, a function of data only:

- :class:`Node`, :class:`ImageSpec`, :class:`Estimate`, :class:`Plan`,
  :class:`BuildOptions`, :class:`Outcome`, :class:`WorkerJob` -- the models;
- :func:`free_threads` and :func:`with_history` -- what a node has to offer (R2.2, R2.4);
- :func:`load_history` -- each node's measured costs, read from the audit runs the
  pipeline already writes (R2.1, R2.2, R2.6, R2.7);
- :func:`image_specs` -- every image of the build with its package count and trunk key,
  from the runs or the recipe's world (R2.2, R2.5, R2.8); with :func:`load_history`,
  read-only;
- :func:`discover` -- the host and each paired worker of the invoking user as nodes,
  every worker probed with story 010's status within 10 s (R1.6, R3.1, R3.6, R6.1, R6.2);
- :func:`estimate` -- an image's cost on a node, from its history, its package count or
  the 1.7 s default (R2.1-R2.5);
- :func:`plan` -- the assignment with the smallest makespan, deterministic on ties
  (R1.1-R1.4, R1.7);
- :func:`render_plan` and :func:`render_report` -- the lines the CLI prints and the
  build's exit code (R1.5, R3.1, R4.4, R4.5, R4.9);
- :func:`execute` -- the I/O part that runs a plan: the host's assemble children and one
  ``shidashi worker job`` child per worker in ONE ``run_children`` call, then the
  workers' results moved into the output directory (R1.7, R3.2, R4.1-R4.3, R4.6-R4.9,
  R6.1, R6.3).

Every input of the planner is data, so its decision is reproducible from the audit trail.
The library raises, it does not log -- the command reports.
"""

import contextlib
import dataclasses
import errno
import hashlib
import itertools
import json
import os
import pwd
import re
import shutil
import socket
import stat
import statistics
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from shidashi import (
    assembler,
    checkpoint,
    config,
    isaguard,
    kyomei_protocol,
    locks,
    parallel,
    publish,
    remote,
    resolve,
    worker,
    workers,
    world,
)

#: Seconds per package when a node has no measured rate (R2.3).
DEFAULT_SEC_PER_PKG = 1.7

#: The largest ``nodes ** images`` searched exhaustively; above it the planner is greedy.
EXHAUSTIVE_CAP = 4096

#: What the report says of a worker ISO that arrived without its SBOM (R4.9).
NO_SBOM = "no SBOM (syft absent on the worker)"


@dataclass(frozen=True)
class Node:
    """A machine that can assemble images: the host or a paired worker.

    ``name`` is ``socket.gethostname()`` for the host and the registry name for a worker
    (C3). ``trunks`` holds the trunk IMAGE KEYS (``v3-desktop-systemd``) that claim the
    node's install checkpoints -- never checkpoint ids, whose fingerprints embed cache
    paths that differ between nodes. ``reachable=False`` also covers a worker that
    answers but cannot take a job (R3.6); ``note`` says why.
    """

    name: str
    kind: Literal["host", "worker"]
    threads: int
    load1: float
    runnable_arches: frozenset[str]
    trunks: frozenset[str]
    sec_per_pkg: float | None
    reachable: bool
    note: str = ""


@dataclass(frozen=True)
class ImageSpec:
    """One image of the build: ``key`` is ``<arch>-<image>-<init>``, ``image`` the target name."""

    key: str
    arch: str
    image: str
    packages: int
    trunk_key: str | None = None
    trunk_packages: int = 0


@dataclass(frozen=True)
class Estimate:
    """An image's cost on one node; ``trunk_seconds`` is 0 when the node holds the trunk."""

    seconds: float
    trunk_seconds: float
    source: Literal["history", "packages", "default"]


@dataclass(frozen=True)
class Plan:
    """Which node makes each image (``assignments``: image key -> node name).

    ``finish`` is each reachable node's estimated finish time and ``makespan`` the
    slowest of them; ``skipped_nodes`` maps an unreachable node to why, ``unplaceable``
    an image no reachable node runs to ``"no node runs <arch>"`` (R1.7).
    """

    assignments: dict[str, str]
    finish: dict[str, float]
    makespan: float
    method: Literal["exhaustive", "greedy"]
    skipped_nodes: dict[str, str]
    unplaceable: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildOptions:
    """The options of ``shidashi build`` that reach an assemble, built once by the CLI."""

    arch: str
    init: str
    output_dir: Path
    images: tuple[str, ...]
    jobs: int | None = None
    boot_test: bool = False
    compressions: tuple[str, ...] = ("zstd",)
    stage4: bool = False
    sbom: bool = True
    fresh: bool = False
    trunk: bool = True
    download: bool = True
    keep: bool = False
    allow_dirty: bool = False
    serial: bool = False
    verbose: bool = False
    #: The worker that owns the arch's cache (story 028): every image goes to it.
    cache_owner: str | None = None
    #: The owner's build runs the arch's factory too (the host does not have its binhost).
    owner_factory: bool = False


@dataclass(frozen=True)
class Outcome:
    """How one image ended, on which node; ``sbom`` is False for an ISO without one (R4.9)."""

    image: str
    node: str
    ok: bool
    duration_s: float
    estimate_s: float
    log: Path | None
    isos: tuple[Path, ...] = ()
    error: str = ""
    sbom: bool = True


@dataclass(frozen=True)
class WorkerJob:
    """A started worker share, named in the interrupt message (R4.7)."""

    worker: str
    job: str
    results: Path


class CostHistory(Protocol):
    """What the estimator reads of one node's measured runs (see ``load_history``)."""

    def duration(self, image_key: str) -> float | None: ...

    def trunk_duration(self, trunk_key: str) -> float | None: ...

    def sec_per_pkg(self) -> float | None: ...

    def run_ids(self) -> list[str]: ...


@dataclass(frozen=True)
class History:
    """One node's measured costs (see :func:`load_history`); every duration is whole-node."""

    durations: dict[str, float] = field(default_factory=dict)
    trunk_durations: dict[str, float] = field(default_factory=dict)
    package_counts: dict[str, int] = field(default_factory=dict)
    rate: float | None = None
    used: tuple[str, ...] = ()

    def duration(self, image_key: str) -> float | None:
        return self.durations.get(image_key)

    def trunk_duration(self, trunk_key: str) -> float | None:
        return self.trunk_durations.get(trunk_key)

    def packages(self, image_key: str) -> int | None:
        return self.package_counts.get(image_key)

    def sec_per_pkg(self) -> float | None:
        return self.rate

    def run_ids(self) -> list[str]:
        return list(self.used)


@dataclass(frozen=True)
class _Run:
    """What :func:`load_history` keeps of one successful assemble run."""

    run_id: str
    started: str
    image_key: str
    seconds: float
    trunk: tuple[str, float] | None
    install_seconds: float | None
    packages: int | None
    restored_own: bool
    restored_any: bool


def _image_key(argv: Sequence[str]) -> str | None:
    """``<arch>-<image>-<init>`` from an assemble argv, wherever it starts (a child's: ``-c``)."""
    args = list(argv)
    if "assemble" not in args:
        return None
    at = args.index("assemble")
    target = args[at + 1 : at + 4]
    if len(target) < 3 or any(a.startswith("-") for a in target):
        return None
    return "-".join(target)


def _jobs(argv: Sequence[str]) -> int | None:
    args = list(argv)
    for n, arg in enumerate(args):
        value = None
        if arg == "--jobs" and n + 1 < len(args):
            value = args[n + 1]
        elif arg.startswith("--jobs="):
            value = arg.removeprefix("--jobs=")
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _share(argv: Sequence[str], threads: object) -> float:
    """The share of the node's threads the run had: ``jobs / threads``, 1 when unknown (R2.7)."""
    jobs = _jobs(argv)
    if jobs is None or not isinstance(threads, int) or threads <= 0 or jobs <= 0:
        return 1.0
    return min(1.0, jobs / threads)


def _read_run(run_dir: Path, node: str | None) -> _Run | None:
    """The run in ``run_dir`` when it is an all-ok assemble that ``node`` made, else None.

    ``node=None`` accepts the run whichever node made it.

    Raises ``OSError`` or ``ValueError`` when its manifest cannot be read.
    """
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return None  # still running, or killed: no manifest yet
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("the manifest is not an object")
    inputs = manifest.get("inputs") or {}
    hostname = manifest.get("hostname") or inputs.get("hostname")
    if manifest.get("command") != "assemble" or (node is not None and hostname != node):
        return None
    steps = manifest.get("steps") or []
    if manifest.get("status") != "ok" or not steps:
        return None
    if any(step.get("status") != "ok" for step in steps):
        return None
    argv = manifest.get("argv") or []
    key = _image_key(argv)
    if key is None:
        return None
    share = _share(argv, inputs.get("threads"))
    top = [step for step in steps if "/" not in str(step.get("step"))]
    seconds = 0.0
    trunk: tuple[str, float] | None = None
    install: float | None = None
    for step in top:
        name = str(step.get("step"))
        took = float(step.get("duration_s") or 0.0)
        if name == "trunk":
            if isinstance(step.get("built"), str):
                trunk = (step["built"], took * share)
        elif not name.startswith("boot-test"):  # the boot test is not the image's cost (R2.6)
            seconds += took
        if name == "install":
            install = took
    packages = None
    with_packages = run_dir / "packages.json"
    if with_packages.is_file():
        listed = json.loads(with_packages.read_text(encoding="utf-8"))
        packages = len(listed) if isinstance(listed, list) else None
    return _Run(
        run_id=str(manifest.get("run") or run_dir.name),
        started=str(manifest.get("started") or ""),
        image_key=key,
        seconds=seconds * share,
        trunk=trunk,
        install_seconds=None if install is None else install * share,
        packages=packages,
        # a restore anywhere but the trunk is the image's own install or packages checkpoint
        restored_own=any("restored" in s for s in steps if s.get("step") != "trunk"),
        restored_any=any("restored" in s for s in steps),
    )


def _read_runs(runs_dir: Path, node: str | None) -> list[_Run]:
    """The all-ok assemble runs ``node`` (any node when None) recorded, oldest first.

    A run whose manifest cannot be read is skipped with one warning naming it; a run
    without a manifest (running or killed) is skipped silently.
    """
    runs: list[_Run] = []
    if runs_dir.is_dir():
        for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
            try:
                run = _read_run(run_dir, node)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                warnings.warn(f"skipping unreadable run {run_dir.name}: {exc}", stacklevel=3)
                continue
            if run is not None:
                runs.append(run)
    runs.sort(key=lambda r: (r.started, r.run_id))
    return runs


def load_history(runs_dir: Path, *, node: str) -> History:
    """``node``'s measured costs, from the assemble runs it recorded in ``runs_dir``.

    Per image, the latest all-ok run that restored none of its own checkpoints gives the
    duration (R2.1) and the package count; a ``trunk`` step that built its trunk gives the
    trunk's duration (R2.6); the node's seconds per package is the median over the runs
    that restored nothing at all (R2.2). Durations are scaled to the whole node by the
    run's ``jobs / threads`` (R2.7). A run whose manifest cannot be read is skipped with
    one warning naming it; a run without a manifest (running or killed) is skipped.
    """
    runs = _read_runs(runs_dir, node)
    durations: dict[str, float] = {}
    counts: dict[str, int] = {}
    trunks: dict[str, float] = {}
    used: set[str] = set()
    for run in runs:  # oldest first: a later run overwrites
        if not run.restored_own and run.seconds > 0:
            durations[run.image_key] = run.seconds
            if run.packages is not None:
                counts[run.image_key] = run.packages
            used.add(run.run_id)
        if run.trunk is not None:
            trunks[run.trunk[0]] = run.trunk[1]
            used.add(run.run_id)
    rates = [
        run.install_seconds / run.packages
        for run in runs
        if not run.restored_any and run.install_seconds and run.packages
    ]
    used |= {
        run.run_id for run in runs if not run.restored_any and run.install_seconds and run.packages
    }
    return History(
        durations=durations,
        trunk_durations=trunks,
        package_counts=counts,
        rate=statistics.median(rates) if rates else None,
        used=tuple(sorted(used)),
    )


class ImageSpecError(Exception):
    """An image of the build whose recipe or world cannot be resolved; names the image."""


def _world_size(arch: str, target: str, init: str, *, any_stage: bool = False) -> int:
    """The length of ``target``'s world (R2.8); raises :class:`ImageSpecError` naming it."""
    try:
        recipe = config.load_recipe(arch, target, init, any_stage=any_stage)
        try:
            return len(world.current_atoms(recipe, config.variants_dir()))
        except world.StaleWorldError:
            if not any_stage:
                raise
            # a stage (the trunk) has no committed world file: count what its kits ask for
            return len(resolve.world_atoms(recipe))
    except (config.UnknownAxisError, world.StaleWorldError, OSError, ValueError) as exc:
        raise ImageSpecError(f"cannot resolve {arch}-{target}-{init}: {exc}") from exc


def image_specs(
    arch: str, init: str, images: Sequence[str], *, runs_dir: Path
) -> dict[str, ImageSpec]:
    """Every image of the build as an :class:`ImageSpec`, keyed by ``<arch>-<image>-<init>``.

    ``packages`` is the length of ``packages.json`` of the latest all-ok assemble of the
    image on ANY node -- the package set does not depend on the machine -- else the length
    of its recipe's world (R2.8). An image with a trunk carries ``trunk_key``
    (``<arch>-<trunk stage>-<init>``, the key its trunk checkpoint is claimed by, R2.5) and
    ``trunk_packages``, counted the same way for that key. Read-only: no fingerprint is
    computed and no tree is fetched -- a fingerprint embeds the host's cache paths and
    never matches a worker's checkpoint. Raises :class:`ImageSpecError` naming an image
    whose recipe or world cannot be resolved.
    """
    counts: dict[str, int] = {}
    for run in _read_runs(runs_dir, None):  # oldest first: a later run overwrites
        if run.packages is not None:
            counts[run.image_key] = run.packages
    specs: dict[str, ImageSpec] = {}
    for image in images:
        key = f"{arch}-{image}-{init}"
        try:
            recipe = config.load_recipe(arch, image, init)
        except (config.UnknownAxisError, OSError, ValueError) as exc:
            raise ImageSpecError(f"cannot resolve image {image!r} ({key}): {exc}") from exc
        if key not in counts:
            counts[key] = _world_size(arch, image, init)
        spec = ImageSpec(key=key, arch=arch, image=image, packages=counts[key])
        trunk = assembler.trunk_stage(recipe)
        if trunk is not None:
            trunk_key = f"{arch}-{trunk}-{init}"
            if trunk_key not in counts:
                # the trunk is a stage, not an image: only any_stage loads its recipe
                counts[trunk_key] = _world_size(arch, trunk, init, any_stage=True)
            spec = dataclasses.replace(spec, trunk_key=trunk_key, trunk_packages=counts[trunk_key])
        specs[key] = spec
    return specs


# --- node discovery ----------------------------------------------------------------------------

#: The name of the unreachable node that says why no worker was read (R6.2).
REGISTRY_NODE = "worker registry"


@dataclass(frozen=True)
class _InvokingUser:
    """The person behind ``sudo``: who a worker child runs as (R6.1)."""

    name: str
    uid: int
    gid: int
    home: Path
    data_home: Path


def _invoking_user() -> _InvokingUser | None:
    """``SUDO_USER`` when the build runs under ``sudo`` (effective uid 0 with ``SUDO_USER``
    set), else None. Their data home is ``$XDG_DATA_HOME`` when set, else their
    ``~/.local/share`` from ``pwd.getpwnam``. Raises :class:`LookupError` when
    ``SUDO_USER`` names no user of this system."""
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(user)
    except KeyError as err:
        raise LookupError(f"SUDO_USER={user!r} is not a user of this system") from err
    home = Path(entry.pw_dir)
    data_home = os.environ.get("XDG_DATA_HOME")
    return _InvokingUser(
        name=user,
        uid=entry.pw_uid,
        gid=entry.pw_gid,
        home=home,
        data_home=Path(data_home) if data_home else home / ".local" / "share",
    )


def invoking_user_workers_dir() -> Path:
    """The workers directory of the user who invoked the build (R6.1).

    Under ``sudo`` (effective uid 0 with ``SUDO_USER`` set) it is ``SUDO_USER``'s:
    ``$XDG_DATA_HOME`` when set, else their ``~/.local/share`` from ``pwd.getpwnam``, plus
    ``shidashi/worker`` -- never root's, whose registry is empty and key absent. Otherwise
    :func:`config.workers_dir`. Raises :class:`LookupError` when ``SUDO_USER`` names no
    user of this system.
    """
    user = _invoking_user()
    if user is None:
        return config.workers_dir()
    return user.data_home / "shidashi" / "worker"


def _unreachable(name: str, note: str) -> Node:
    """A worker the plan skips, with why (R3.1, R3.6, R6.2)."""
    return Node(
        name=name,
        kind="worker",
        threads=0,
        load1=0.0,
        runnable_arches=frozenset(),
        trunks=frozenset(),
        sec_per_pkg=None,
        reachable=False,
        note=note,
    )


def _host_trunks() -> frozenset[str]:
    """The image keys claiming the host's install checkpoints (R2.5).

    The union of the ``images`` field of every ``install-*.json`` manifest under
    ``<scratch>/assemble/checkpoints/*/``, ``install-partial-*`` excluded (it matches the
    same glob). An absent store or an unreadable manifest contributes nothing.
    """
    root = config.scratch_dir() / "assemble" / "checkpoints"
    keys: set[str] = set()
    try:
        manifests = sorted(root.glob("*/install-*.json"))
    except OSError:
        return frozenset()
    for manifest in manifests:
        if manifest.name.startswith(f"{checkpoint.PARTIAL}-"):
            continue
        try:
            images = json.loads(manifest.read_text(encoding="utf-8")).get("images")
        except OSError, ValueError, AttributeError:
            continue
        if isinstance(images, list):
            keys.update(i for i in images if isinstance(i, str) and i)
    return frozenset(keys)


def _host() -> Node:
    """This machine as a node: its threads, load, ISA and trunks (Q4: never an error)."""
    note = ""
    try:
        load1 = os.getloadavg()[0]
    except OSError as err:
        load1, note = 0.0, f"load average unavailable ({err}); counted as 0"
    return Node(
        name=socket.gethostname(),
        kind="host",
        threads=os.cpu_count() or 1,
        load1=load1,
        runnable_arches=frozenset(isaguard.runnable(isaguard.local_cpu_flags())),
        trunks=_host_trunks(),
        sec_per_pkg=None,
        reachable=True,
        note=note,
    )


def _remote(entry: workers.WorkerEntry, address: str, workers_dir: Path) -> remote.Remote:
    """How to reach ``entry`` at ``address`` with the key and pins of ``workers_dir``.

    Not :meth:`remote.Remote.for_worker`: that one reads :func:`config.workers_dir`,
    which under ``sudo`` is root's (R6.1).
    """
    return remote.Remote(
        name=entry.name,
        address=address,
        key=workers_dir / "id_ed25519",
        known_hosts=workers_dir / "known_hosts",
        expected_fingerprint=entry.host_key_fingerprint,
    )


def _probe(
    entry: workers.WorkerEntry,
    workers_dir: Path,
    timeout: float,
    first: dict[str, str] | None = None,
) -> worker.WorkerStatus:
    """Story 010's status of ``entry``, wherever it is now -- as ``cli._on_worker`` finds it.

    The recorded address, else the one the worker answers from over mDNS. When the
    RECORDED address does not answer, the worker is looked up by name once and probed
    once more there; a changed host key is never retried. The recorded address's failure
    goes into ``first`` (keyed by the worker's name) before the retry, so a deadline that
    cuts the retry still names it, and a failed retry carries both reasons (R3.8). Raises
    as ``worker.status`` and :func:`remote.resolve_address` do.
    """
    recorded = entry.address is not None
    where = remote.resolve_address(entry)
    try:
        answer = worker.status(_remote(entry, where, workers_dir), timeout=timeout)
    except remote.RemoteUnreachable as err:
        if not recorded:
            raise
        failure = str(err)
    else:
        if not recorded or answer.reachable is True:
            return answer
        failure = answer.reason or "no answer"
    failure = " ".join(failure.split())
    if first is not None:
        first[entry.name] = failure
    try:
        where = remote.resolve_address(entry.model_copy(update={"address": None}))
        answer = worker.status(_remote(entry, where, workers_dir), timeout=timeout)
    except remote.RemoteUnreachable as retry:
        raise remote.RemoteUnreachable(entry.name, None, f"{failure}; {retry}") from retry
    if answer.reachable is not True:
        return dataclasses.replace(answer, reason=f"{failure}; {answer.reason or 'no answer'}")
    return answer


#: What :func:`remote.resolve_address` tells the ``worker`` commands to do, which
#: ``build`` cannot: it takes no ``--address`` (R3.7).
_WORKER_ADDRESS_FIX = "pass --address ADDR"


def _build_fix(name: str, reason: str) -> str:
    """``reason`` with the address fix of the ``worker`` commands replaced by fixes
    ``shidashi build`` accepts (R3.7)."""
    fix = (
        f"build with --local, or reach it once with `shidashi worker status {name} --address ADDR`"
    )
    return reason.replace(_WORKER_ADDRESS_FIX, fix)


def _worker_node(name: str, status: worker.WorkerStatus) -> Node:
    """The node a status describes, named by the registry (C3); unreachable when the
    worker did not answer or cannot take a job (R3.6). A status missing a field raises
    ``AttributeError`` or :class:`ValueError` (the caller makes it unreachable)."""
    if status.reachable is not True:
        return _unreachable(name, status.reason or "no answer")
    if status.accepts_jobs is not True:
        return _unreachable(name, "no work disk mounted: it does not accept jobs")
    if status.jobs:
        return _unreachable(name, f"job {', '.join(status.jobs)} already running")
    threads, load1 = status.threads, status.load1
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise ValueError(f"no thread count (threads={threads!r})")
    if isinstance(load1, bool) or not isinstance(load1, int | float):
        raise ValueError(f"no load average (load1={load1!r})")
    return Node(
        name=name,
        kind="worker",
        threads=threads,
        load1=float(load1),
        runnable_arches=frozenset(status.runnable_arches),
        trunks=frozenset(status.trunk_keys),
        sec_per_pkg=None,
        reachable=True,
    )


def _workers(workers_dir: Path, timeout: float) -> list[Node]:
    """Every paired worker of ``workers_dir`` as a node, all probed at once within
    ``timeout`` seconds; an unreadable registry is one unreachable node saying why (R6.2)."""
    registry = workers_dir / "workers.json"
    try:
        entries = workers.load_registry(registry)
    except (workers.RegistryError, OSError) as err:
        return [_unreachable(REGISTRY_NODE, f"cannot read {registry}: {err}")]
    found: dict[str, Node] = {}
    first: dict[str, str] = {}  # a recorded address's failure, while its retry runs
    lock = threading.Lock()

    def probe(entry: workers.WorkerEntry) -> None:
        try:
            node = _worker_node(entry.name, _probe(entry, workers_dir, timeout, first))
        except (AttributeError, TypeError, ValueError) as err:
            node = _unreachable(entry.name, f"malformed status: {err}")
        except Exception as err:  # RemoteUnreachable, HostKeyMismatch, UnknownFlag, OSError
            node = _unreachable(entry.name, _build_fix(entry.name, str(err)) or type(err).__name__)
        with lock:
            found[entry.name] = node

    threads = [
        threading.Thread(target=probe, args=(entry,), name=f"discover-{name}", daemon=True)
        for name, entry in sorted(entries.items())
    ]
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    with lock:  # a probe still running is left behind (daemon): its answer is too late
        late = f"timed out: no answer within {timeout:g} s"
        return [
            found.get(name) or _unreachable(name, "; ".join(filter(None, (first.get(name), late))))
            for name in sorted(entries)
        ]


def discover(*, local: bool, timeout: float = 10.0, workers_dir: Path | None = None) -> list[Node]:
    """The nodes of a build: the host, then each paired worker (R1.6, R3.1, R3.6, R6.1, R6.2).

    ``local=True`` is the host alone, the registry untouched. Otherwise the registry of
    ``workers_dir`` -- by default the invoking user's (:func:`invoking_user_workers_dir`)
    -- is read and every worker probed in its own thread, joined by one ``timeout``
    deadline. A worker that cannot be planned is a node with ``reachable=False`` and the
    reason in ``note``; nothing here raises for a worker.
    """
    nodes = [_host()]
    if local:
        return nodes
    if workers_dir is None:
        try:
            workers_dir = invoking_user_workers_dir()
        except LookupError as err:
            return [*nodes, _unreachable(REGISTRY_NODE, f"cannot read the worker registry: {err}")]
    return [*nodes, *_workers(workers_dir, timeout)]


# --- the tree every node builds ----------------------------------------------------------------

#: The Shidashi checkout this module runs from: the tree the host builds (C6).
_CHECKOUT = Path(__file__).resolve().parent.parent

#: How long ``git status`` may take before the tree is called unknown.
_GIT_TIMEOUT_S = 30.0


class TreeStateError(Exception):
    """The checkout's changes could not be listed (no git, not a checkout, git failed)."""


def dirty_files() -> list[str]:
    """Every path ``git status`` lists in the Shidashi checkout, untracked files included
    (R3.4, C6).

    Stricter on purpose than ``worker job``'s own check, which ignores untracked files:
    the host builds the working tree while a worker builds ``git archive HEAD``, so any
    difference splits the build across two commits. Run with ``-c
    safe.directory=<checkout>``: git refuses a checkout another user owns, and the build
    runs as root on the user's. As root it must also leave the checkout as it found it:
    ``--no-optional-locks`` keeps ``status`` from refreshing the user's ``.git/index``
    (it would be rewritten root-owned) and ``core.fsmonitor=false`` runs no hook the
    checkout configures. A rename names both paths. Raises :class:`TreeStateError` when
    git cannot answer.
    """
    argv = [
        shutil.which("git") or "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"safe.directory={_CHECKOUT}",
        "-C",
        str(_CHECKOUT),
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=_GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise TreeStateError(f"git status in {_CHECKOUT}: {err}") from err
    if done.returncode != 0:
        why = os.fsdecode(done.stderr or b"").strip() or f"exit {done.returncode}"
        raise TreeStateError(f"git status in {_CHECKOUT}: {why}")
    entries = iter(os.fsdecode(done.stdout or b"").split("\0"))
    paths: list[str] = []
    for entry in entries:
        if len(entry) < 4:  # the empty tail after the last NUL
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:  # -z: the source path is the next entry
            source = next(entries, "")
            if source:
                paths.append(source)
    return paths


def free_threads(node: Node) -> float:
    """The node's threads not taken by its one-minute load, never below one (R2.4)."""
    return max(1.0, node.threads - node.load1)


def with_history(node: Node, history: CostHistory) -> Node:
    """The node with its measured seconds per package -- the one writer of that field (R2.2)."""
    return dataclasses.replace(node, sec_per_pkg=history.sec_per_pkg())


def _positive(value: float | None) -> float | None:
    return value if value is not None and value > 0 else None


def estimate(image: ImageSpec, node: Node, history: CostHistory) -> Estimate:
    """The image's cost on ``node``, scaled by the node's share of free threads.

    The image's own measured duration first (R2.1), else its packages at the node's
    measured rate (R2.2), else at 1.7 s (R2.3). The trunk costs nothing when the node
    already holds its key (R2.5), else its own measured duration, else its packages at
    the same rate.
    """
    # a node reporting no threads or a zero rate must not look free: it would take everything
    scale = max(1, node.threads) / free_threads(node)
    measured_rate = _positive(node.sec_per_pkg)
    rate = measured_rate if measured_rate is not None else DEFAULT_SEC_PER_PKG
    source: Literal["history", "packages", "default"]
    measured = _positive(history.duration(image.key))
    if measured is not None:
        seconds, source = measured, "history"
    elif measured_rate is not None:
        seconds, source = image.packages * measured_rate, "packages"
    else:
        seconds, source = image.packages * DEFAULT_SEC_PER_PKG, "default"
    trunk_seconds = 0.0
    if image.trunk_key is not None and image.trunk_key not in node.trunks:
        trunk = _positive(history.trunk_duration(image.trunk_key))
        trunk_seconds = trunk if trunk is not None else image.trunk_packages * rate
    return Estimate(seconds=seconds * scale, trunk_seconds=trunk_seconds * scale, source=source)


# --- the planner -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Score:
    """One assignment's finish times and its place in the order of the tie-breaks (R1.4)."""

    finish: dict[str, float]
    makespan: float
    trunks: int
    host_work: float

    def key(self, choice: tuple[str, ...]) -> tuple[float, int, float, tuple[str, ...]]:
        # rounded: a sum in another order must not break a tie that is one
        return (round(self.makespan, 6), self.trunks, -round(self.host_work, 6), choice)


def _score(
    placed: Sequence[tuple[ImageSpec, Node]],
    nodes: Sequence[Node],
    estimates: Mapping[tuple[str, str], Estimate],
) -> _Score:
    """Each node's finish: its images' seconds plus each distinct trunk's seconds ONCE (R1.3)."""
    finish = {node.name: 0.0 for node in nodes}
    trunks: dict[tuple[str, str], float] = {}
    host_work = 0.0
    for image, node in placed:
        cost = estimates[(image.key, node.name)]
        finish[node.name] += cost.seconds
        if node.kind == "host":
            host_work += cost.seconds
        if image.trunk_key is not None and cost.trunk_seconds > 0:
            where = (node.name, image.trunk_key)
            trunks[where] = max(trunks.get(where, 0.0), cost.trunk_seconds)
    for (name, _), seconds in trunks.items():
        finish[name] += seconds
    return _Score(
        finish=finish,
        makespan=max(finish.values(), default=0.0),
        trunks=len(trunks),
        host_work=host_work,
    )


def _greedy(
    images: Sequence[ImageSpec],
    candidates: Mapping[str, Sequence[Node]],
    nodes: Sequence[Node],
    estimates: Mapping[tuple[str, str], Estimate],
) -> dict[str, str]:
    """Longest image first, onto the node where it finishes earliest."""

    def cheapest(image: ImageSpec) -> float:
        return min(
            estimates[(image.key, n.name)].seconds + estimates[(image.key, n.name)].trunk_seconds
            for n in candidates[image.key]
        )

    finish = {node.name: 0.0 for node in nodes}
    held: set[tuple[str, str]] = set()
    assignments: dict[str, str] = {}
    for image in sorted(images, key=lambda i: (-cheapest(i), i.key)):

        def ends(node: Node, image: ImageSpec = image) -> tuple[float, int, str]:
            cost = estimates[(image.key, node.name)]
            trunk = (
                cost.trunk_seconds
                if image.trunk_key is not None and (node.name, image.trunk_key) not in held
                else 0.0
            )
            return (finish[node.name] + cost.seconds + trunk, node.kind != "host", node.name)

        best = min(candidates[image.key], key=ends)
        finish[best.name] = ends(best)[0]
        if image.trunk_key is not None and estimates[(image.key, best.name)].trunk_seconds > 0:
            held.add((best.name, image.trunk_key))
        assignments[image.key] = best.name
    return assignments


def plan(
    images: Sequence[ImageSpec],
    nodes: Sequence[Node],
    estimates: Mapping[tuple[str, str], Estimate],
    *,
    owner: str | None = None,
) -> Plan:
    """The assignment of ``images`` to ``nodes`` with the smallest makespan.

    A node is a candidate for an image when it is reachable and its CPU runs the image's
    arch (R1.2, R3.1, R3.6); an image without a candidate is unplaceable (R1.7).
    Exhaustive while ``reachable nodes ** placeable images <= 4096`` (an offline worker or
    an image nobody runs must not push a small search into greedy), greedy above it; ties go to
    fewer trunks installed, then more work on the host, then the lowest assignment
    (R1.4) -- the result does not depend on the order of ``nodes``. With ``owner``
    (story 028) the owner of the arch's cache is the only candidate: when it cannot
    take the images they stay unplaced naming it, never falling back to another node.
    """
    reachable = sorted((n for n in nodes if n.reachable), key=lambda n: n.name)
    skipped = {n.name: n.note or "unreachable" for n in nodes if not n.reachable}
    candidates = {
        image.key: [
            n
            for n in reachable
            if image.arch in n.runnable_arches and (owner is None or n.name == owner)
        ]
        for image in images
    }
    unplaceable = {
        i.key: (
            f"{i.arch}'s cache lives on {owner}, which cannot take it now"
            f" ({skipped.get(owner, 'not paired or not running ' + i.arch)})"
            if owner is not None
            else f"no node runs {i.arch}"
        )
        for i in images
        if not candidates[i.key]
    }
    placeable = sorted((i for i in images if candidates[i.key]), key=lambda i: i.key)
    by_name = {n.name: n for n in reachable}

    method: Literal["exhaustive", "greedy"]
    if len(reachable) ** len(placeable) <= EXHAUSTIVE_CAP:
        method = "exhaustive"
        best: tuple[tuple[float, int, float, tuple[str, ...]], _Score] | None = None
        for combo in itertools.product(*(candidates[i.key] for i in placeable)):
            score = _score(list(zip(placeable, combo, strict=True)), reachable, estimates)
            key = score.key(tuple(n.name for n in combo))
            if best is None or key < best[0]:
                best = (key, score)
        choice = best[0][3] if best is not None else ()
        assignments = dict(zip((i.key for i in placeable), choice, strict=True))
    else:
        method = "greedy"
        assignments = _greedy(placeable, candidates, reachable, estimates)

    score = _score([(i, by_name[assignments[i.key]]) for i in placeable], reachable, estimates)
    return Plan(
        assignments=assignments,
        finish=score.finish,
        makespan=score.makespan,
        method=method,
        skipped_nodes=skipped,
        unplaceable=unplaceable,
    )


# --- what the CLI prints -----------------------------------------------------------------------


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m{secs:02d}s"


def render_plan(plan: Plan, estimates: Mapping[tuple[str, str], Estimate]) -> list[str]:
    """One line per image (node, estimate, source), then the trunks each node installs
    (once per node, as the planner counts them), the unplaceable images, the skipped
    workers with their reasons and the search method (R1.5, R1.7, R3.1)."""
    lines = []
    work: dict[str, float] = {}
    for key, node in sorted(plan.assignments.items()):
        cost = estimates[(key, node)]
        lines.append(f"{key}  {node}  ~{_duration(cost.seconds)}  ({cost.source})")
        work[node] = work.get(node, 0.0) + cost.seconds
    for node, seconds in sorted(work.items()):
        # a node's finish holds each of its trunks once: what its images do not account for
        trunks = plan.finish.get(node, seconds) - seconds
        if trunks > 0.5:
            lines.append(f"{node}  trunk install ~{_duration(trunks)}")
    lines += [f"{key}  not built: {why}" for key, why in sorted(plan.unplaceable.items())]
    lines += [f"{name}  skipped: {why}" for name, why in sorted(plan.skipped_nodes.items())]
    lines.append(f"plan: {plan.method} search, makespan ~{_duration(plan.makespan)}")
    return lines


def render_report(plan: Plan, outcomes: Sequence[Outcome]) -> tuple[int, list[str]]:
    """The build's exit code -- 1 when any image failed (R4.5) -- and one line per image:
    its node, measured and estimated durations, its ISOs or its failure with the log
    (R4.4, R4.9); then the plan's skipped workers with their reasons (R3.1)."""
    lines = []
    for outcome in outcomes:
        node = outcome.node or "-"
        if outcome.ok:
            isos = " ".join(str(iso) for iso in outcome.isos) or "no ISO"
            line = (
                f"{outcome.image}  {node}  ok in {_duration(outcome.duration_s)}"
                f" (estimated {_duration(outcome.estimate_s)})  {isos}"
            )
            if not outcome.sbom:
                line += f"  {NO_SBOM}"
        else:
            line = f"{outcome.image}  {node}  FAILED: {outcome.error or 'failed'}"
            if outcome.log is not None:
                line += f"  log: {outcome.log}"
        lines.append(line)
    lines += [f"{name}  skipped: {why}" for name, why in sorted(plan.skipped_nodes.items())]
    return (0 if all(o.ok for o in outcomes) else 1), lines


# --- the executor ------------------------------------------------------------------------------

#: The key of the one child that builds the host's share with ``--serial`` (R4.8).
SERIAL_KEY = "host-serial"

#: Set to ``1`` in the environment of that child alone (its argv starts with ``env``):
#: it then assembles its images in its own process instead of planning and executing
#: again -- a ``build --serial --local`` would otherwise start itself forever.
SERIAL_SHARE_ENV = "SHIDASHI_SERIAL_SHARE"

#: A host child ``index`` boot-tests from vsock CID ``100 + index``: away from the
#: hand-run default 42; each child allocates upward from its base (story 006 R4.1).
CHILD_BASE_CID = 100

#: What ``run_children`` says of a child whose argv has no ``--result-file``: a worker
#: child and a serial build child have none, so it is no failure of theirs.
NO_RESULT_FILE = "no result file"

#: What a worker ISO carries beside it, by the suffix added to the ISO's name.
_ISO_COMPANIONS = (".DIGESTS", ".contents.gz", ".packages", ".spdx.json")

#: The ``build --compression`` value producing a set of compressions.
_COMPRESSION_FLAGS = {("zstd",): None, ("xz",): "xz", ("xz", "zstd"): "both"}

#: How much of a child's log is read for its error text.
_LOG_TAIL_BYTES = 64 * 1024

#: What ``parallel.run_children`` says of a child the signal stopped (or never started).
_STOPPED = "interrupted"

#: The scratch subdirectory holding each worker job's results, one per worker name.
_WORKER_RESULTS = "worker-results"

#: Opening a directory the invoking user may rearrange: never through a symlink.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

#: Opening a file the invoking user may rearrange: never through a symlink, never
#: blocking on a FIFO planted in its place.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

#: How much of a worker's small results file (``<job>.rc``, ``<job>.runs``, SHA256SUMS)
#: is read.
_SMALL_BYTES = 1024 * 1024

#: One SHA-256 digest as ``sha256sum`` writes it.
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class Interrupted(KeyboardInterrupt):
    """A distributed build stopped by a signal (R4.7).

    ``outcomes`` are the host's images (and the unplaceable ones) as they ended, and
    those of every worker job that had ended on its own before the signal, its results
    moved in as after a full run; ``jobs`` the worker jobs the signal stopped, left
    running on their workers; ``signum`` the signal, so the command exits
    ``128 + signum``.
    """

    def __init__(self, outcomes: Sequence[Outcome], jobs: Sequence[WorkerJob], signum: int) -> None:
        super().__init__(f"interrupted by signal {signum}")
        self.outcomes: tuple[Outcome, ...] = tuple(outcomes)
        self.jobs: tuple[WorkerJob, ...] = tuple(jobs)
        self.signum: int = int(signum)


def _build_flags(options: BuildOptions) -> list[str]:
    """The ``build`` options that differ from its defaults and reach every assemble."""
    try:
        compression = _COMPRESSION_FLAGS[tuple(sorted(set(options.compressions)))]
    except KeyError:
        raise ValueError(f"no build --compression produces {options.compressions!r}") from None
    flags = ["--compression", compression] if compression is not None else []
    switches = (
        (options.stage4, "--stage4"),
        (not options.sbom, "--no-sbom"),
        (options.fresh, "--fresh"),
        (not options.trunk, "--no-trunk"),
        (options.keep, "--keep"),
        (not options.download, "--no-download"),
        (options.boot_test, "--boot-test"),
        (options.verbose, "--verbose"),
    )
    return flags + [flag for wanted, flag in switches if wanted]


def _host_argvs(
    share: Sequence[ImageSpec], options: BuildOptions, logs: Path
) -> dict[str, list[str]]:
    """The host share's children: one ``assemble`` per image exactly as ``cli.build``
    passes it (story 006), or with ``serial`` ONE ``build --serial`` child holding every
    host image with the unsplit jobs (R4.8), marked by :data:`SERIAL_SHARE_ENV` in its
    own environment only."""
    jobs = options.jobs or os.cpu_count() or 1
    if options.serial:
        return {
            SERIAL_KEY: [
                shutil.which("env") or "env",
                f"{SERIAL_SHARE_ENV}=1",
                sys.executable,
                *parallel.CHILD_COMMAND,
                "build",
                options.arch,
                options.init,
                "--images",
                ",".join(spec.image for spec in share),
                "--serial",
                "--local",
                "--skip-factory",
                "--jobs",
                str(jobs),
                "--output-dir",
                str(options.output_dir),
                *_build_flags(options),
            ]
        }
    each = parallel.job_share(jobs, len(share))
    return {
        spec.image: parallel.assemble_argv(
            spec.image,
            arch=options.arch,
            init=options.init,
            jobs=each,
            output_dir=options.output_dir,
            result_file=logs / f"{spec.image}.json",
            compressions=options.compressions,
            stage4=options.stage4,
            sbom=options.sbom,
            fresh=options.fresh,
            trunk=options.trunk,
            download=options.download,
            keep=options.keep,
            boot_test=options.boot_test,
            cid=CHILD_BASE_CID + index if options.boot_test else None,
            verbose=options.verbose,
            status_file=logs / f"{spec.image}.status",
        )
        for index, spec in enumerate(share)
    }


def _worker_argv(
    name: str,
    job: str,
    results: Path,
    share: Sequence[ImageSpec],
    options: BuildOptions,
    user: _InvokingUser | None,
) -> list[str]:
    """A worker share's child: ``shidashi worker job`` running ONE ``build`` of its images
    there (C4), as the invoking user under ``sudo`` (R6.1).

    ``--local``: the worker does not plan again; ``--skip-factory``: it does not take the
    arch's owner lock; no ``--jobs``: it splits its own threads. The identity drop keeps
    the environment -- every ``SHIDASHI_*`` included -- and sets the user's ``HOME``,
    ``USER``, ``LOGNAME`` and ``XDG_DATA_HOME``, so the job reads their pairing key and
    pinned host keys, never root's.
    """
    drop: list[str] = []
    if user is not None:
        drop = [
            shutil.which("setpriv") or "setpriv",
            "--reuid",
            str(user.uid),
            "--regid",
            str(user.gid),
            "--init-groups",
            shutil.which("env") or "env",
            f"HOME={user.home}",
            f"USER={user.name}",
            f"LOGNAME={user.name}",
            f"XDG_DATA_HOME={user.data_home}",
        ]
    job_options = ["worker", "job", name, job, "--results", str(results)]
    if options.allow_dirty:
        job_options.append("--allow-dirty")
    # the owner of the arch's cache builds its binhost itself (story 028)
    factory_here = options.owner_factory and options.cache_owner == name
    remote_args = [
        "build",
        options.arch,
        options.init,
        "--images",
        ",".join(spec.image for spec in share),
        "--local",
        *(() if factory_here else ("--skip-factory",)),
        *_build_flags(options),
    ]
    return [*drop, sys.executable, *parallel.CHILD_COMMAND, *job_options, "--", *remote_args]


def _results_dir(node: str, job: str) -> Path:
    """``<scratch>/worker-results/<node>/<job>``, the worker share's results directory.

    Raises ``ValueError`` when ``node`` is no host name -- ``../..`` would put root's
    directories outside ``worker-results``, ``-x`` would read as an option of the child --
    or the path would leave ``worker-results`` anyway."""
    if not re.fullmatch(kyomei_protocol.HOSTNAME_RE, node):
        raise ValueError(f"invalid worker name {node!r}: not a host name")
    base = config.scratch_dir() / _WORKER_RESULTS
    results = base / node / job
    if not Path(os.path.normpath(results)).is_relative_to(os.path.normpath(base)):
        raise ValueError(f"invalid worker name {node!r}: {results} leaves {base}")
    return results


def _open_dir(name: str, dir_fd: int, *, create: bool = False) -> int:
    """Open directory ``name`` under ``dir_fd`` without following a symlink; with
    ``create``, make it first (mode 0o755). Raises ``OSError`` (``ELOOP`` for a symlink,
    ``ENOTDIR`` for anything else that is no directory)."""
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o755, dir_fd=dir_fd)
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _open_results(results: Path) -> int:
    """A descriptor of ``results`` (``<scratch>/worker-results/<node>/<job>``), every
    component below the scratch directory opened without following a symlink.
    Raises ``OSError``."""
    scratch = results.parent.parent.parent
    fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in (_WORKER_RESULTS, results.parent.name, results.name):
            inner = _open_dir(part, fd)
            os.close(fd)
            fd = inner
    except BaseException:
        os.close(fd)
        raise
    return fd


def _prepare_results(results: Path, job: str, user: _InvokingUser | None) -> None:
    """Make ``results`` (``<scratch>/worker-results/<node>/<job>``) for a worker share's
    child, without an earlier job's ``<job>.rc`` or ``<job>.runs`` (they would pass for
    this job's end).

    Every component below the scratch directory is made and opened without following a
    symlink, and the stale files are removed through the job directory's descriptor
    (``unlink`` never follows). Under ``sudo``, ``worker-results/`` and
    ``worker-results/<node>/`` stay owned like the scratch directory -- root's -- mode
    0o755, so the user can neither rename the job's directory nor plant a link in its
    place; only the ``<job>`` leaf goes to the user the child runs as, through its own
    descriptor. Raises ``OSError`` naming ``results``."""
    scratch = results.parent.parent.parent
    fds: list[int] = []
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        fds.append(os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        top = os.fstat(fds[0])
        for part in (_WORKER_RESULTS, results.parent.name):
            fds.append(_open_dir(part, fds[-1], create=True))
            if user is not None:
                st = os.fstat(fds[-1])
                if (st.st_uid, st.st_gid) != (top.st_uid, top.st_gid):
                    os.fchown(fds[-1], top.st_uid, top.st_gid)
                if stat.S_IMODE(st.st_mode) != 0o755:
                    os.fchmod(fds[-1], 0o755)
        fds.append(_open_dir(job, fds[-1], create=True))
        for stale in (f"{job}.rc", f"{job}.runs"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(stale, dir_fd=fds[-1])
        if user is not None:
            # a refusal is not fatal here: the child, as the user, names what it cannot write
            with contextlib.suppress(OSError):
                os.fchown(fds[-1], user.uid, user.gid)
    except OSError as err:
        raise OSError(f"preparing {results}: {_why(err)}") from err
    finally:
        for fd in fds:
            os.close(fd)


def _why(err: OSError) -> str:
    """``err`` in words, a refused symlink said plainly."""
    if err.errno == errno.ELOOP:
        return f"{err.filename} is a symlink" if err.filename else "a symlink"
    return f"{err.filename}: {err.strerror}" if err.filename and err.strerror else str(err)


def _read_small(name: str, dir_fd: int) -> str:
    """The text of regular file ``name`` under ``dir_fd``, read without following a
    symlink, at most :data:`_SMALL_BYTES`. Raises ``OSError``."""
    fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", name)
        return handle.read(_SMALL_BYTES).decode("utf-8", "replace")


def _sha256_fd(fd: int) -> str:
    """The SHA-256 of the open file ``fd``, from its first byte."""
    os.lseek(fd, 0, os.SEEK_SET)
    with open(fd, "rb", closefd=False) as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _safe_name(name: str) -> bool:
    """Whether ``name`` is one plain file name (a run id read from a results file)."""
    return bool(name) and name not in (".", "..") and Path(name).name == name


def _manifest(run_id: str | None) -> dict[str, object] | None:
    """The manifest of ``run_id`` in the host's runs, None when absent or unreadable."""
    if not run_id or not _safe_name(run_id):
        return None
    try:
        data = json.loads((config.runs_dir() / run_id / "manifest.json").read_text("utf-8"))
    except OSError, ValueError:
        return None
    return data if isinstance(data, dict) else None


def _steps(manifest: Mapping[str, object]) -> list[dict[str, object]]:
    steps = manifest.get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _seconds(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _estimate_s(
    estimates: Mapping[tuple[str, str], Estimate],
    spec: ImageSpec,
    node: str,
    manifest: Mapping[str, object] | None,
) -> float:
    """The image's estimate on ``node``, plus its trunk's when its run installed it (R4.4)."""
    cost = estimates.get((spec.key, node))
    if cost is None:
        return 0.0
    built = manifest is not None and any(
        s.get("step") == "trunk" and s.get("built") == spec.trunk_key
        for s in _steps(manifest)
        if spec.trunk_key is not None
    )
    return cost.seconds + (cost.trunk_seconds if built else 0.0)


def _log_tail(log: Path) -> list[str]:
    """The last lines of a child's log; none when it cannot be read."""
    try:
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - _LOG_TAIL_BYTES))
            return handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _log_error(log: Path) -> str:
    """The last ``error:`` of a child's log with its indented continuation lines (a
    ``JobRefused``'s ``fix:``), on one line; ``""`` when there is none."""
    lines = _log_tail(log)
    starts = [n for n, line in enumerate(lines) if line.lstrip().startswith("error:")]
    if not starts:
        return ""
    first = starts[-1]
    block = [lines[first].lstrip().removeprefix("error:").strip()]
    for line in lines[first + 1 :]:
        if not line[:1].isspace() or not line.strip():
            break
        block.append(line.strip())
    return "; ".join(part for part in block if part)


def _last_line(log: Path) -> str:
    """The last non-empty line of a child's log; ``""`` when there is none."""
    return next((line.strip() for line in reversed(_log_tail(log)) if line.strip()), "")


def _host_outcomes(
    share: Sequence[ImageSpec],
    node: str,
    by_key: Mapping[str, parallel.ChildResult],
    estimates: Mapping[tuple[str, str], Estimate],
    options: BuildOptions,
    started: float,
) -> dict[str, Outcome]:
    """Each host image's outcome from its child's result (C1); with ``serial``, from the
    one build child holding them all."""
    outcomes: dict[str, Outcome] = {}
    serial = by_key.get(SERIAL_KEY) if options.serial else None
    for spec in share:
        result = serial if options.serial else by_key.get(spec.image)
        if result is None:
            outcomes[spec.key] = Outcome(
                image=spec.key,
                node=node,
                ok=False,
                duration_s=0.0,
                estimate_s=_estimate_s(estimates, spec, node, None),
                log=None,
                error="its child reported nothing",
            )
            continue
        if options.serial:
            # the serial build writes no result file: its exit code is its verdict
            error = result.error if result.error != NO_RESULT_FILE else None
            ok = result.exit_code == 0 and error is None
            isos = _latest_isos(options.output_dir, spec.key, since=started) if ok else ()
            manifest = None
        else:
            error, ok, isos = result.error, not result.failed, result.isos
            manifest = _manifest(result.run_id)
        outcomes[spec.key] = Outcome(
            image=spec.key,
            node=node,
            ok=ok,
            duration_s=result.duration_s,
            estimate_s=_estimate_s(estimates, spec, node, manifest),
            log=result.log,
            isos=isos,
            error="" if ok else (error or f"exit code {result.exit_code}"),
        )
    return outcomes


def _latest_isos(output_dir: Path, key: str, *, since: float) -> tuple[Path, ...]:
    """The ISO ``latest-<key>.txt`` names, when this build wrote it (a serial share)."""
    latest = output_dir / f"latest-{key}.txt"
    try:
        if latest.stat().st_mtime < since:
            return ()
        lines = latest.read_text("utf-8").splitlines()
    except OSError:
        return ()
    names = [line.split()[0] for line in lines if line.strip() and not line.startswith("#")]
    found = [output_dir / n for n in names[:1] if _safe_name(n)]
    return tuple(iso for iso in found if iso.is_file())


@dataclass
class _Mover:
    """Moves worker results into the output directory, owned like it (R4.2, R6.3), and
    gathers their SHA256SUMS lines -- the host's own hashes -- for the one write after
    every child (R4.2).

    Both ends belong to the invoking user, who may rearrange them while root works:
    a source is opened without following a symlink and must be a regular file with one
    link; the destination is reached through the output directory's descriptor, refused
    when it is a symlink or a directory, and replaced by ``rename`` (which never follows
    its last component) -- across filesystems by a fresh ``O_EXCL`` file renamed into
    place. Every chown and hash goes through the moved file's own descriptor.
    """

    output_dir: Path
    sums: dict[str, str] = field(default_factory=dict)
    _out: int | None = None
    _owner: tuple[int, int] = (-1, -1)

    def _out_fd(self) -> int:
        if self._out is None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            st = os.fstat(fd)
            self._out, self._owner = fd, (st.st_uid, st.st_gid)
        return self._out

    def close(self) -> None:
        if self._out is not None:
            os.close(self._out)
            self._out = None

    def move(
        self, iso_dir: Path, iso_fd: int, names: Sequence[str], worker_sums: Mapping[str, str]
    ) -> list[Path]:
        """Move ``names`` from ``iso_dir`` (open as ``iso_fd``) into the output directory:
        all of them, or none when one is not a regular file with one link, its SHA-256
        is not the one the worker's SHA256SUMS lists for it, or its destination exists
        and is no regular file. Raises ``OSError`` naming the file."""
        try:
            out = self._out_fd()
        except OSError as err:
            raise OSError(f"opening {self.output_dir}: {_why(err)}") from err
        with contextlib.ExitStack() as stack:
            ready: list[tuple[str, int, os.stat_result, str]] = []
            for name in names:
                src = iso_dir / name
                try:
                    fd = os.open(name, _FILE_FLAGS, dir_fd=iso_fd)
                except OSError as err:
                    raise OSError(f"{src}: {_why(err)}") from err
                stack.callback(os.close, fd)
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise OSError(f"{src} is not a regular file")
                if st.st_nlink != 1:
                    raise OSError(f"{src} has {st.st_nlink} hard links")
                digest = _sha256_fd(fd)
                claimed = worker_sums.get(name)
                if claimed is not None and claimed != digest:
                    raise OSError(
                        f"{src}: its SHA256 {digest} is not the {claimed} "
                        f"the worker's SHA256SUMS lists"
                    )
                self._check_dest(out, name)
                ready.append((name, fd, st, digest))
            moved = []
            for name, fd, st, digest in ready:
                try:
                    self._place(iso_fd, out, name, fd, st)
                except OSError as err:
                    raise OSError(
                        f"moving {iso_dir / name} into {self.output_dir}: {_why(err)}"
                    ) from err
                if not name.startswith("latest-"):
                    self.sums[name] = digest
                moved.append(self.output_dir / name)
            return moved

    def _check_dest(self, out: int, name: str) -> None:
        dest = self.output_dir / name
        try:
            st = os.stat(name, dir_fd=out, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as err:
            raise OSError(f"{dest}: {_why(err)}") from err
        if not stat.S_ISREG(st.st_mode):
            what = "a symlink" if stat.S_ISLNK(st.st_mode) else "not a regular file"
            raise OSError(f"{dest} already exists and is {what}")

    def _place(self, iso_fd: int, out: int, name: str, fd: int, st: os.stat_result) -> None:
        """Rename ``name`` into the output directory and chown it there; across
        filesystems, copy it in and remove the source."""
        try:
            os.replace(name, name, src_dir_fd=iso_fd, dst_dir_fd=out)
        except OSError as err:
            if err.errno != errno.EXDEV:
                raise
            self._copy(out, name, fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=iso_fd)
            return
        landed = os.stat(name, dir_fd=out, follow_symlinks=False)
        if (landed.st_dev, landed.st_ino) != (st.st_dev, st.st_ino):
            # what landed is not the file checked: take it back out of the output dir
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=out)
            raise OSError(errno.EBUSY, "replaced while it was moved", name)
        os.fchown(fd, *self._owner)

    def _copy(self, out: int, name: str, src: int) -> None:
        """Copy open file ``src`` to ``name`` in the output directory: into a new file
        (``O_EXCL``, never a planted name), owned and synced, renamed into place."""
        tmp = f".{name}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        dst = os.open(tmp, flags, 0o644, dir_fd=out)
        try:
            try:
                os.lseek(src, 0, os.SEEK_SET)
                with (
                    open(src, "rb", closefd=False) as source,
                    open(dst, "wb", closefd=False) as target,
                ):
                    shutil.copyfileobj(source, target, 1024 * 1024)
                os.fchmod(dst, 0o644)
                os.fchown(dst, *self._owner)
                os.fsync(dst)
            finally:
                os.close(dst)
            os.replace(tmp, name, src_dir_fd=out, dst_dir_fd=out)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=out)
            raise


def _read_sums(iso_fd: int | None) -> dict[str, str]:
    """``SHA256SUMS`` under ``iso_fd`` as file name -> digest, read without following a
    symlink; empty when absent or unreadable."""
    if iso_fd is None:
        return {}
    try:
        text = _read_small("SHA256SUMS", iso_fd)
    except OSError:
        return {}
    sums = {}
    for line in text.splitlines():
        digest, _, name = line.partition("  ")
        if _safe_name(name) and _DIGEST_RE.fullmatch(digest):
            sums[name] = digest
    return sums


def _entry(name: str, dir_fd: int | None) -> os.stat_result | None:
    """``name`` under ``dir_fd`` as it is -- a symlink is not followed -- None when absent
    or unreadable (a name too long, as ``Path.is_file`` would say)."""
    if dir_fd is None:
        return None
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None


def _latest_run(manifests: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    return max(
        manifests, key=lambda m: (str(m.get("started") or ""), str(m.get("run"))), default=None
    )


def _all_ok(manifest: Mapping[str, object]) -> bool:
    steps = _steps(manifest)
    return (
        manifest.get("status") == "ok"
        and bool(steps)
        and all(s.get("status") == "ok" for s in steps)
    )


def _artifact_names(manifest: Mapping[str, object]) -> tuple[list[tuple[str, str]], list[str]]:
    """``([(iso name, role)], [every artifact's name])`` the run recorded."""
    artifacts = manifest.get("artifacts")
    isos: list[tuple[str, str]] = []
    names: list[str] = []
    for art in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(art, dict) or not isinstance(art.get("path"), str):
            continue
        name, role = Path(art["path"]).name, str(art.get("role") or "")
        if not _safe_name(name):
            continue
        names.append(name)
        if role.startswith("iso"):
            isos.append((name, role))
    return isos, names


def _companions(iso: str, role: str) -> list[str]:
    """``iso``'s companion files; the packages list and SBOM are named after the zstd ISO."""
    names = [iso + suffix for suffix in _ISO_COMPANIONS]
    profile = role.partition(":")[2]
    if profile and profile != "zstd" and iso.endswith(f"-{profile}.iso"):
        base = iso.removesuffix(f"-{profile}.iso") + ".iso"
        names += [f"{base}.packages", f"{base}.spdx.json"]
    return names


def _worker_outcomes(
    wjob: WorkerJob,
    share: Sequence[ImageSpec],
    result: parallel.ChildResult | None,
    child_log: Path,
    estimates: Mapping[tuple[str, str], Estimate],
    options: BuildOptions,
    mover: _Mover,
) -> dict[str, Outcome]:
    """A worker share's outcomes from its results directory and the runs pulled into the
    host's runs (R4.6, R3.2); every ok image's ISO, its companions and the job's build
    bundle moved into the output directory (R4.2, R4.9, R6.3)."""
    results, job, node = wjob.results, wjob.job, wjob.worker
    job_log = results / f"{job}.log"
    log = job_log if job_log.is_file() else child_log
    exit_code = result.exit_code if result is not None else parallel.NOT_STARTED
    child_error = result.error if result is not None else "its child reported nothing"
    start_error = child_error if child_error not in (None, NO_RESULT_FILE) else ""
    quoted = _log_error(child_log) if exit_code != 0 else ""

    def failed(
        spec: ImageSpec, error: str, manifest: Mapping[str, object] | None = None
    ) -> Outcome:
        return Outcome(
            image=spec.key,
            node=node,
            ok=False,
            duration_s=0.0,
            estimate_s=_estimate_s(estimates, spec, node, manifest),
            log=log,
            error=error,
        )

    with contextlib.ExitStack() as stack:
        # every read goes through descriptors opened without following a symlink: the
        # user the child ran as owns the job's directory and may rearrange it (R6.1)
        rc: int | None = None
        leaf: int | None = None
        try:
            leaf = _open_results(results)
            stack.callback(os.close, leaf)
            rc = int(_read_small(f"{job}.rc", leaf).strip())
        except OSError, ValueError:
            rc = None
        if rc is None or leaf is None:
            # the job's end was never seen: refused, lost, or its pull failed (R3.2)
            reason = quoted or start_error or (_last_line(child_log) if exit_code != 0 else "")
            end = f"job {job} on {node} ended without {results / f'{job}.rc'} (exit {exit_code})"
            return {
                spec.key: failed(spec, f"{reason} ({end})" if reason else end) for spec in share
            }
        try:
            listed = _read_small(f"{job}.runs", leaf).split()
        except OSError:
            listed = []
        iso_dir = results / "iso"
        iso_fd: int | None = None
        try:
            iso_fd = _open_dir("iso", leaf)
            stack.callback(os.close, iso_fd)
        except FileNotFoundError:
            iso_fd = None  # nothing arrived: every ISO is missing
        except OSError as err:
            unusable = f"{iso_dir}: {_why(err)}"
            return {spec.key: failed(spec, unusable) for spec in share}
        return _collect_worker_results(
            wjob, share, listed, iso_dir, iso_fd, failed, log, quoted, estimates, options, mover
        )


def _collect_worker_results(
    wjob: WorkerJob,
    share: Sequence[ImageSpec],
    listed: Sequence[str],
    iso_dir: Path,
    iso_fd: int | None,
    failed: Callable[..., Outcome],
    log: Path,
    quoted: str,
    estimates: Mapping[tuple[str, str], Estimate],
    options: BuildOptions,
    mover: _Mover,
) -> dict[str, Outcome]:
    """The outcomes of a worker job whose end was seen, its results moved (R4.6, R4.2)."""
    job, node = wjob.job, wjob.worker
    run_ids = [run_id for run_id in listed if _safe_name(run_id)]
    manifests = [m for m in (_manifest(run_id) for run_id in run_ids) if m is not None]
    build_run = _latest_run([m for m in manifests if m.get("command") == "build"])
    worker_sums = _read_sums(iso_fd)
    note = f"; {quoted}" if quoted else ""
    outcomes: dict[str, Outcome] = {}
    for spec in share:
        step = next(
            (s for s in _steps(build_run or {}) if s.get("step") == f"assemble:{spec.image}"),
            None,
        )
        if build_run is None:
            outcomes[spec.key] = failed(
                spec, f"no build run of job {job} reached {config.runs_dir()}{note}"
            )
            continue
        if step is None or step.get("status") != "ok":
            why = (step or {}).get("error") or "no such step"
            outcomes[spec.key] = failed(
                spec, f"assemble:{spec.image} failed on {node}: {why}{note}"
            )
            continue
        runs = [
            m
            for m in manifests
            if m.get("command") == "assemble" and _image_key(_argv(m)) == spec.key
        ]
        done = _latest_run([m for m in runs if _all_ok(m)])
        if done is None:
            what = "did not end ok" if runs else f"did not reach {config.runs_dir()}"
            outcomes[spec.key] = failed(spec, f"the assemble run of {spec.key} {what}{note}")
            continue
        isos, artifacts = _artifact_names(done)
        # absent only: a symlink or anything else in its place is refused by the mover
        missing = [name for name, _ in isos if _entry(name, iso_fd) is None]
        if not isos or missing or iso_fd is None:
            what = ", ".join(missing) if missing else f"of run {done.get('run')}"
            outcomes[spec.key] = failed(spec, f"the ISO {what} did not reach {iso_dir}{note}", done)
            continue
        names = [name for iso, role in isos for name in (iso, *_companions(iso, role))]
        names += [*artifacts, f"latest-{spec.key}.txt"]
        present = [n for n in dict.fromkeys(names) if _entry(n, iso_fd) is not None]
        try:
            moved = mover.move(iso_dir, iso_fd, present, worker_sums)
        except OSError as err:
            outcomes[spec.key] = failed(spec, str(err), done)
            continue
        steps = _steps(done)
        duration = done.get("duration_s")
        outcomes[spec.key] = Outcome(
            image=spec.key,
            node=node,
            ok=True,
            duration_s=_seconds(duration)
            if duration is not None
            else sum(_seconds(s.get("duration_s")) for s in steps),
            estimate_s=_estimate_s(estimates, spec, node, done),
            log=log,
            isos=tuple(path for path in moved if path.name in {iso for iso, _ in isos}),
            # with --no-sbom nobody asked for one: "syft absent" would be untrue (R4.9)
            sbom=not options.sbom or any(n.endswith(".spdx.json") for n in present),
        )
    # the job's audit trail, named after the worker's build run
    for run_id in run_ids:
        bundle = f"{run_id}.build.tar.zst"
        if iso_fd is None or _entry(bundle, iso_fd) is None:
            continue
        try:
            mover.move(iso_dir, iso_fd, [bundle], worker_sums)
        except OSError as err:
            outcomes = {
                key: dataclasses.replace(o, ok=False, error=str(err)) if o.ok else o
                for key, o in outcomes.items()
            }
    return outcomes


def _argv(manifest: Mapping[str, object]) -> list[str]:
    argv = manifest.get("argv")
    return [str(a) for a in argv] if isinstance(argv, list) else []


def execute(
    plan: Plan,
    specs: Mapping[str, ImageSpec],
    estimates: Mapping[tuple[str, str], Estimate],
    *,
    options: BuildOptions,
    logs: Path,
    run_id: str,
    runner: Callable[..., list[parallel.ChildResult]] | None = None,
    host: str | None = None,
) -> list[Outcome]:
    """Run ``plan`` on every node at once; one :class:`Outcome` per planned image.

    The host's share is story 006's children -- one ``assemble`` each, as ``cli.build``
    passes them, or one ``build --serial`` child (R4.8) -- and each worker's share ONE
    ``shidashi worker job`` child, key ``@<worker>``, run as the invoking user (R6.1).
    Every child goes into ONE ``runner(argvs, logs=logs)`` call (R4.1; by default
    :func:`parallel.run_children`, which must run on the main thread for a signal to
    stop the build). ``host`` names the plan's host node -- by default
    ``socket.gethostname()``, the name :func:`discover` gives it; every other node of the
    plan is a worker.

    After the call a worker's outcomes come from its results directory and the runs
    pulled into the host's (R4.6, R3.2), its ISOs, their companions and its build
    bundle are moved into ``options.output_dir`` owned like it (R6.3), and their
    SHA256SUMS lines merged there by this process alone, after every child (R4.2). An
    unplaceable image is a failed outcome without a node (R1.7). No owner lock is taken:
    assembles only read the binhost. A signal raises :class:`Interrupted` with the
    host's outcomes, those of the worker jobs that had already ended (their results moved
    in as usual) and the worker jobs it stopped (R4.7).

    Under ``sudo`` this process is root and the worker results are the invoking user's:
    a worker whose name is no host name gets no child; ``worker-results/<worker>/`` stays
    root's and only the job's own directory is the user's; every file there is opened
    without following a symlink, and only a regular file is moved, chowned and hashed --
    through its own descriptor. The SHA256SUMS lines published are this process's own
    hashes; a worker line contradicting one fails its image.
    """
    run = runner if runner is not None else parallel.run_children
    here = host if host is not None else socket.gethostname()
    job = f"build-{run_id[-6:]}"
    worker.validate_job_name(job)

    planned = [*plan.assignments, *(k for k in plan.unplaceable if k not in plan.assignments)]
    order = [k for k in specs if k in planned] + [k for k in planned if k not in specs]
    outcomes: dict[str, Outcome] = {}
    host_share: list[ImageSpec] = []
    shares: dict[str, list[ImageSpec]] = {}
    for key in order:
        node = plan.assignments.get(key)
        spec = specs.get(key)
        if node is None or spec is None:
            error = plan.unplaceable.get(key) or f"no image spec for {key}"
            outcomes[key] = Outcome(
                image=key,
                node=node or "",
                ok=False,
                duration_s=0.0,
                estimate_s=0.0,
                log=None,
                error=error,
            )
        elif node == here:
            host_share.append(spec)
        else:
            shares.setdefault(node, []).append(spec)

    argvs = _host_argvs(host_share, options, logs) if host_share else {}
    jobs: dict[str, WorkerJob] = {}
    user: _InvokingUser | None = None
    refused: Exception | None = None
    try:
        user = _invoking_user() if shares else None
    except LookupError as err:  # an unknown SUDO_USER: no worker child may run as it
        refused = err
    for node, share in shares.items():
        try:
            if refused is not None:
                raise refused
            # a name that is no host name gets no directory and no child (R6.1)
            results = _results_dir(node, job)
            _prepare_results(results, job, user)
        except (OSError, LookupError, ValueError) as err:
            for spec in share:
                outcomes[spec.key] = Outcome(
                    image=spec.key,
                    node=node,
                    ok=False,
                    duration_s=0.0,
                    estimate_s=_estimate_s(estimates, spec, node, None),
                    log=None,
                    error=f"job {job} on {node} not started: {err}",
                )
            continue
        argvs[f"@{node}"] = _worker_argv(node, job, results, share, options, user)
        jobs[node] = WorkerJob(worker=node, job=job, results=results)

    started = time.time()
    try:
        children = run(argvs, logs=logs) if argvs else []
    except parallel.Interrupted as err:
        stopped = {r.image: r for r in err.results}
        outcomes.update(_host_outcomes(host_share, here, stopped, estimates, options, started))
        running: list[WorkerJob] = []
        ended: dict[str, WorkerJob] = {}
        for name, wjob in jobs.items():
            got = stopped.get(f"@{name}")
            if got is None or got.exit_code == parallel.NOT_STARTED:
                continue  # never started: nothing runs on that worker
            if got.error == _STOPPED:
                running.append(wjob)
            else:
                ended[name] = wjob  # its job had ended on its own before the signal
        outcomes.update(_collect_workers(ended, shares, stopped, logs, estimates, options))
        raise Interrupted(
            [outcomes[k] for k in order if k in outcomes], running, err.signum
        ) from err

    by_key = {r.image: r for r in children}
    outcomes.update(_host_outcomes(host_share, here, by_key, estimates, options, started))
    outcomes.update(_collect_workers(jobs, shares, by_key, logs, estimates, options))
    return [outcomes[k] for k in order]


def _collect_workers(
    jobs: Mapping[str, WorkerJob],
    shares: Mapping[str, Sequence[ImageSpec]],
    by_key: Mapping[str, parallel.ChildResult],
    logs: Path,
    estimates: Mapping[tuple[str, str], Estimate],
    options: BuildOptions,
) -> dict[str, Outcome]:
    """``jobs``' outcomes, their results moved into ``options.output_dir`` and their
    SHA256SUMS lines merged there by this process alone, after every child (R4.2)."""
    outcomes: dict[str, Outcome] = {}
    mover = _Mover(options.output_dir)
    try:
        for node, wjob in jobs.items():
            outcomes.update(
                _worker_outcomes(
                    wjob,
                    shares[node],
                    by_key.get(f"@{node}"),
                    logs / f"@{node}.log",
                    estimates,
                    options,
                    mover,
                )
            )
    finally:
        mover.close()
    if mover.sums:
        try:
            publish.update_sha256sums(options.output_dir, mover.sums)
        # PublishError: a corrupt SHA256SUMS, refused and left as it was (R4.12)
        except (OSError, publish.PublishError, locks.LockError, locks.LockBusy) as err:
            sums_error = f"merging SHA256SUMS of {options.output_dir}: {err}"
            outcomes = {
                key: dataclasses.replace(got, ok=False, error=sums_error) if got.ok else got
                for key, got in outcomes.items()
            }
    return outcomes
