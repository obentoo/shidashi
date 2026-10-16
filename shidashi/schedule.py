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
  build's exit code (R1.5, R3.1, R4.4, R4.5, R4.9).

Every input of the planner is data, so its decision is reproducible from the audit trail.
The library raises, it does not log -- the command reports.
"""

import dataclasses
import itertools
import json
import os
import pwd
import socket
import statistics
import threading
import time
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from shidashi import assembler, checkpoint, config, isaguard, remote, worker, workers, world

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
        return len(world.current_atoms(recipe, config.variants_dir()))
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


def invoking_user_workers_dir() -> Path:
    """The workers directory of the user who invoked the build (R6.1).

    Under ``sudo`` (effective uid 0 with ``SUDO_USER`` set) it is ``SUDO_USER``'s:
    ``$XDG_DATA_HOME`` when set, else their ``~/.local/share`` from ``pwd.getpwnam``, plus
    ``shidashi/worker`` -- never root's, whose registry is empty and key absent. Otherwise
    :func:`config.workers_dir`. Raises :class:`LookupError` when ``SUDO_USER`` names no
    user of this system.
    """
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return config.workers_dir()
    try:
        home = Path(pwd.getpwnam(user).pw_dir)
    except KeyError as err:
        raise LookupError(f"SUDO_USER={user!r} is not a user of this system") from err
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else home / ".local" / "share"
    return base / "shidashi" / "worker"


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


def _probe(entry: workers.WorkerEntry, workers_dir: Path, timeout: float) -> worker.WorkerStatus:
    """Story 010's status of ``entry``, wherever it is now -- as ``cli._on_worker`` finds it.

    The recorded address, else the one the worker answers from over mDNS. When the
    RECORDED address does not answer, the worker is looked up by name once and probed
    once more there; a changed host key is never retried. Raises as ``worker.status``
    and :func:`remote.resolve_address` do.
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
    try:
        where = remote.resolve_address(entry.model_copy(update={"address": None}))
    except remote.RemoteUnreachable as lookup:
        reason = f"{' '.join(failure.split())}; {lookup}"
        raise remote.RemoteUnreachable(entry.name, None, reason) from lookup
    return worker.status(_remote(entry, where, workers_dir), timeout=timeout)


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
    lock = threading.Lock()

    def probe(entry: workers.WorkerEntry) -> None:
        try:
            node = _worker_node(entry.name, _probe(entry, workers_dir, timeout))
        except (AttributeError, TypeError, ValueError) as err:
            node = _unreachable(entry.name, f"malformed status: {err}")
        except Exception as err:  # RemoteUnreachable, HostKeyMismatch, UnknownFlag, OSError
            node = _unreachable(entry.name, str(err) or type(err).__name__)
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
        return [found.get(name) or _unreachable(name, late) for name in sorted(entries)]


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
) -> Plan:
    """The assignment of ``images`` to ``nodes`` with the smallest makespan.

    A node is a candidate for an image when it is reachable and its CPU runs the image's
    arch (R1.2, R3.1, R3.6); an image without a candidate is unplaceable (R1.7).
    Exhaustive while ``reachable nodes ** placeable images <= 4096`` (an offline worker or
    an image nobody runs must not push a small search into greedy), greedy above it; ties go to
    fewer trunks installed, then more work on the host, then the lowest assignment
    (R1.4) -- the result does not depend on the order of ``nodes``.
    """
    reachable = sorted((n for n in nodes if n.reachable), key=lambda n: n.name)
    skipped = {n.name: n.note or "unreachable" for n in nodes if not n.reachable}
    candidates = {
        image.key: [n for n in reachable if image.arch in n.runnable_arches] for image in images
    }
    unplaceable = {i.key: f"no node runs {i.arch}" for i in images if not candidates[i.key]}
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
            isos = " ".join(iso.name for iso in outcome.isos) or "no ISO"
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
