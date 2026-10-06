"""Factory — Package Factory: builds binpkgs from a recipe (OVERVIEW §6).

Orchestrates, for a :class:`~shidashi.recipe.ResolvedRecipe`, the multi-instance build
of binpkgs in a clean container per flavor (OVERVIEW §6.1–§6.3): seed/extraction of the
stage3 (or reuse of a trunk fork point), overlaying of the portage layers
+ installation of the sets, mounting of the binds (repos RO; PKGDIR/ccache/sccache/DISTDIR
RW over the fixed paths of ``make.conf``) and execution of the phases (OVERVIEW §6.4)
followed by the settle-pass. The privilege guard (root) is the **first** thing
:meth:`Factory.build` does — Shidashi never escalates privileges on its own (R8.1).

The privileged execution symbols are imported at **module level**
(``fetch_stage3``/``extract_stage3``/``bind_repos``/``apply_portage``) so that the
tests can monkeypatch them in ``shidashi.factory`` and :meth:`build` sees them.
:class:`FactoryError` is defined in :mod:`shidashi.phases` (avoiding the import cycle
``factory`` → ``phases``) and **re-exported** here.
"""

import os
import re
import shutil
import time
from collections.abc import Mapping
from pathlib import Path

import pydantic

from shidashi import audit, config, isacheck, state
from shidashi.bootstrap import BootstrapResult, run_bootstrap
from shidashi.container import Container
from shidashi.generation import FINGERPRINT_FILE, check_or_record, fingerprint
from shidashi.phases import (
    CheckpointDecision,
    CheckpointHook,
    FactoryError,
    FailureDecision,
    FailureHook,
    PhaseHook,
    PhaseResult,
    attach_packages,
    fork_point,
    latest_resumable,
    plan_phase_run,
    restore_fork_point,
    run_phases,
    run_phases_stepwise,
    snapshot_fork_point,
    stage_fork_point_path,
)
from shidashi.recipe import ResolvedRecipe
from shidashi.resolve import apply_portage, apply_rootfs, bind_repos, install_sets
from shidashi.seed import Stage3Pointer, extract_stage3, fetch_stage3, load_pointer
from shidashi.state import PhaseDiff
from shidashi.tree import load_pin_id, pinned_repos
from shidashi.update import run_update

__all__ = [
    "CheckpointDecision",
    "Factory",
    "FactoryError",
    "FactoryResult",
    "FailureDecision",
    "StaleStateError",
]

# Fixed container paths, defined by the base ``make.conf`` (OVERVIEW §6.3):
# the Factory picks the *host-side* directories (under ``cache_dir()``) and bind-mounts them
# over these fixed targets — ``make.conf`` is not edited.
_PKGDIR_DST = Path("/var/cache/binpkgs")
_CCACHE_DST = Path("/var/cache/ccache")
_SCCACHE_DST = Path("/var/cache/sccache")
_DISTDIR_DST = Path("/var/cache/distfiles")


class FactoryResult(pydantic.BaseModel):
    """The result of one Factory run (OVERVIEW §6).

    *Frozen* value object (pydantic v2, ``extra="forbid"``;
    ``arbitrary_types_allowed`` admits :class:`~pathlib.Path`). Carries the
    ``pkgdir`` produced, the compiled ``built_atoms``, the names of the ``phases``
    that ran, the materialized/reused ``fork_point`` (``None`` when there is none),
    ``fork_point_reused`` (trunk reuse) and the settle-pass's ``settle_atoms``.

    The interactive build's fields (story 004) are **defaulted** to keep story 003's
    construction (without them) valid despite ``extra="forbid"`` (R8.2):
    ``stopped_at`` is the label where a stepwise run stopped early (``--until``/STOP) or
    ``None`` when it ran to the end; ``phase_diffs`` the per-phase history of
    :class:`~shidashi.state.PhaseDiff` and ``completed_phases`` the names of the
    phases already closed — both read from the state persisted by the stepwise run.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    pkgdir: Path
    built_atoms: tuple[str, ...]
    phases: tuple[str, ...]
    fork_point: Path | None
    fork_point_reused: bool
    settle_atoms: tuple[str, ...]
    stopped_at: str | None = None
    phase_diffs: tuple[PhaseDiff, ...] = ()
    completed_phases: tuple[str, ...] = ()
    #: Binaries carrying instructions this build host cannot execute (R9.x).
    #: Non-empty is a WARNING, not a failure: the binpkgs are valid FOR THE
    #: TARGET, they simply cannot be test-run or smoke-tested here. Always empty
    #: when target and host share an ISA, which is the common case.
    unrunnable_here: tuple[str, ...] = ()
    #: Installed from the generation's binpkgs instead of compiled (--usepkg).
    reused_atoms: tuple[str, ...] = ()
    #: The toolchain bootstrap this build ran over a fresh stage3; ``None`` when
    #: it resumed from the bootstrap checkpoint or from a stage fork point.
    bootstrap: BootstrapResult | None = None


class StaleStateError(FactoryError):
    """Persisted build state that is stale against the current snapshot/recipe (R6.3).

    Raised by :meth:`Factory.build_stepwise` when :func:`shidashi.state.is_stale`
    reports a divergence (the stage3 snapshot or the recipe hash changed) and neither
    ``--reset`` nor ``--force-resume`` was passed — the stepwise run NEVER proceeds
    silently over stale progress. Subclass of :class:`FactoryError`
    (carries the same ``phase``/``output``); the CLI (Task 7) catches it to emit a
    prompt/diagnosis and exit with code 1, telling it apart from an emerge failure.
    """


def _require_root() -> None:
    """Privilege guard (R8.1): raises :class:`FactoryError` if not root.

    The first thing :meth:`Factory.build` and :meth:`Factory.build_stepwise`
    call — **before** any fetch/extraction/state I/O. Shidashi never
    escalates privileges on its own; the message is actionable and mentions ``root``.
    """
    if os.geteuid() != 0:
        raise FactoryError(
            "shidashi factory requires root (systemd-nspawn + stage3 extraction); "
            "run it as root — Shidashi does not escalate privileges on its own"
        )


def _fresh_seed(rootfs: Path, pointer: Stage3Pointer, *, download: bool) -> None:
    """Seed a **fresh** rootfs from the ``pointer``'s stage3 (R1.4/R8.2).

    :func:`shidashi.seed.fetch_stage3` (cache under :func:`shidashi.config.cache_dir`)
    followed by :func:`shidashi.seed.extract_stage3` (which already creates ``rootfs``).

    PRIVILEGED sub-step shared by the fresh path of
    :func:`_seed_or_restore` and by the "no state" case of :meth:`Factory.build_stepwise`;
    ``fetch_stage3``/``extract_stage3`` are module globals (monkeypatchable in the tests).
    """
    tarball = fetch_stage3(pointer, cache_dir=config.cache_dir(), download=download)
    extract_stage3(tarball, rootfs)


def bootstrap_fork_point_path(
    recipe: ResolvedRecipe, *, snapshot: str, pins: str, fork_points_dir: Path
) -> Path:
    """Where the bootstrap checkpoint lives: the stage3 with its toolchain rebuilt.

    ``<arch>-<init>-<snapshot>-<pins>-bootstrap.tar``, beside the stage fork
    points and keyed like them (no target): every image of one arch × init
    starts here. The arch is in the key because the toolchain is built with the
    arch's CFLAGS; the pin id because it is built from the pinned tree, and a
    toolchain of an older pin must never be restored under a newer one (D7).
    """
    return fork_points_dir / f"{recipe.arch}-{recipe.init}-{snapshot}-{pins}-bootstrap.tar"


def update_source(
    recipe: ResolvedRecipe, target: str, *, snapshot: str, pins: str, fork_points_dir: Path
) -> Path | None:
    """The image fork point an update starts from (D10, R8.9, R8.10). Probes only.

    The current pin's key when it exists; else the newest image of an older
    pin of the same stage3 snapshot -- newest pin date, then newest mtime; a
    date later than the current pin's is never taken; else the pre-fix key
    (no pin id); else ``None``. An update is the one reader that crosses pins:
    it exists to bring an older pin's image to the current one, and it writes
    the result under the current key, never over its source.
    """
    current = stage_fork_point_path(
        recipe, target, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir
    )
    if current.exists():
        return current
    today = re.fullmatch(r"p(\d{8})\.[0-9a-f]{8}", pins)
    if today is None:
        raise FactoryError(f"not a pin id: {pins!r}", phase="update")
    key = re.compile(
        re.escape(f"{recipe.arch}-{recipe.init}-{snapshot}-")
        + r"p(\d{8})\.[0-9a-f]{8}"
        + re.escape(f"-{target}.tar")
    )
    older: list[tuple[str, float, Path]] = []
    if fork_points_dir.is_dir():
        for path in fork_points_dir.iterdir():
            match = key.fullmatch(path.name)
            if match and match.group(1) <= today.group(1) and path.is_file():
                older.append((match.group(1), path.stat().st_mtime, path))
    if older:
        return max(older, key=lambda found: (found[0], found[1]))[2]
    pre_fix = fork_points_dir / f"{recipe.arch}-{recipe.init}-{snapshot}-{target}.tar"
    return pre_fix if pre_fix.exists() else None


def _generation_recheck(pkgdir: Path, rootfs: Path, recipe: ResolvedRecipe) -> PhaseHook:
    """The re-check run after every phase's emerge, as an audited ``generation`` step (D5).

    Recomputes the rootfs's fingerprint and checks it against the PKGDIR's,
    naming the phase. A refusal (:class:`GenerationMismatchError`), an
    unreadable fingerprint (:class:`FactoryError`) or an ``OSError`` propagates:
    the caller's failure path keeps the rootfs.
    """

    def recheck(phase: str) -> None:
        with audit.current().step("generation", after=phase) as step:
            current = fingerprint(rootfs, recipe)
            check_or_record(pkgdir, current, after_phase=phase)
            step.add(fingerprint=current.model_dump())

    return recheck


def _bootstrap(
    container: Container,
    recipe: ResolvedRecipe,
    *,
    pkgdir: Path,
    snapshot: str,
    pins: str,
    fork_points_dir: Path,
) -> BootstrapResult:
    """Run the toolchain bootstrap and checkpoint it (BOOTSTRAP-PROCESS §5, items 1-2).

    The checkpoint is what a failed base build restores to -- not the raw stage3,
    which would redo the ~15 min of toolchain first. The bootstrap's
    ``check-generation`` step runs the fingerprint check as soon as the toolchain
    is final, before the steps that write binpkgs (ccache and its dependencies,
    which no later stage rebuilds).
    """
    rootfs = container.rootfs
    result = run_bootstrap(
        container, on_generation=lambda: check_or_record(pkgdir, fingerprint(rootfs, recipe))
    )
    dest = bootstrap_fork_point_path(
        recipe, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    snapshot_fork_point(container.rootfs, dest)
    return result


def _restore_into(tarball: Path, rootfs: Path) -> None:
    shutil.rmtree(rootfs, ignore_errors=True)
    rootfs.mkdir(parents=True, exist_ok=True)
    restore_fork_point(tarball, rootfs)


def _seed_or_restore(
    recipe: ResolvedRecipe,
    rootfs: Path,
    pointer: Stage3Pointer,
    *,
    snapshot: str,
    pins: str,
    fork_points_dir: Path,
    download: bool,
) -> tuple[str | None, Path, bool, bool]:
    """Decide between reusing the trunk fork point and a fresh seed (R5.1/R5.2/R8.2).

    *Seed-or-restore* block extracted from :meth:`Factory.build` WITHOUT a change in
    behavior (R8.2): if :func:`shidashi.phases.fork_point` finds the trunk pinned
    for ``snapshot`` and ``pins``, it restores it into a clean rootfs and returns
    ``(resume_at, fork_point_path, True, True)`` where ``resume_at`` is the phase of the
    restored stage; otherwise it restores the bootstrap checkpoint, if it exists, or runs
    :func:`_fresh_seed` -- ``(None, <key of the first stage>, False,
    bootstrapped)``. The last flag says whether the toolchain bootstrap is
    already in the rootfs; ``False`` means the caller must run it. PRIVILEGED.
    """
    found = fork_point(recipe, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir)
    if found is not None:
        # the deepest STAGE already built for this arch × init, by any image
        # (D24, F70): resume right after it
        phase, existing = found
        _restore_into(existing, rootfs)
        return phase.name, existing, True, True
    first_stage = next((p.stage for p in recipe.phases if p.stage), "base")
    fork_point_path = stage_fork_point_path(
        recipe, first_stage, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir
    )
    checkpoint = bootstrap_fork_point_path(
        recipe, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir
    )
    if checkpoint.exists():
        _restore_into(checkpoint, rootfs)
        return None, fork_point_path, False, True
    # a fresh seed is a fresh ROOTFS: a failed --keep run leaves its tree behind,
    # and a stage3 extracted over it would inherit whatever that run broke
    shutil.rmtree(rootfs, ignore_errors=True)
    _fresh_seed(rootfs, pointer, download=download)
    return None, fork_point_path, False, False


def _phases_through(recipe: ResolvedRecipe, name: str | None) -> tuple[str, ...]:
    """Names of the phases up to and including ``name``; ``()`` for ``None``. Pure."""
    names: list[str] = []
    if name is None:
        return ()
    for phase in recipe.phases:
        names.append(phase.name)
        if phase.name == name:
            break
    return tuple(names)


def _entry_layers(recipe: ResolvedRecipe, *, done: tuple[str, ...]) -> tuple[str, ...] | None:
    """The layers of the first phase still to run, skipping ``done`` (D24). Pure.

    The configuration grows along the chain and ``run_phase`` re-applies it per
    phase; before the container opens, only what the FIRST phase needs goes in
    -- applying every layer here would leave the flavor's package.use in place
    while the base is still being built. ``None`` means "all of them": a recipe
    whose phases carry no layers (built directly, as in the tests).
    """
    for phase in recipe.phases:
        if phase.name not in done and phase.layers:
            return phase.layers
    return None


def _prepare_portage(
    rootfs: Path, recipe: ResolvedRecipe, *, layers: tuple[str, ...] | None = None
) -> None:
    """Overlay the portage layers and install the recipe's sets (R6.4/R8.2).

    *Portage-apply* block extracted from :meth:`Factory.build` WITHOUT a change in
    behavior (R8.2): :func:`shidashi.resolve.apply_portage` (layers under
    :func:`shidashi.config.variants_dir`) followed by :meth:`Factory._install_sets`.
    Shared by :meth:`Factory.build` and :meth:`Factory.build_stepwise`.

    The layers' ``rootfs/`` trees go first (:func:`shidashi.resolve.apply_rootfs`):
    the bootstrap's ``locale-gen`` reads the curated ``/etc/locale.gen``.
    """
    apply_rootfs(rootfs, recipe, variants_dir=config.variants_dir(), layers=layers)
    apply_portage(rootfs, recipe, variants_dir=config.variants_dir(), layers=layers)
    Factory._install_sets(rootfs, recipe)


def _build_binds(
    recipe: ResolvedRecipe,
    *,
    pkgdir: Path,
    repos: Mapping[str, Path],
    repos_conf_dir: Path | None = None,
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """Build the container's RO (repos) and RW (PKGDIR/caches) binds (R6.2/R6.3/R7.2). Pure.

    - ``binds_ro`` = :func:`shidashi.resolve.bind_repos` over ``repos_conf_dir`` (the
      resolved rootfs's ``repos.conf``; the pinned repos, bound RO).
    - ``binds_rw`` maps the *host-side* directories (under ``cache_dir()``) over the
      fixed targets of the container's ``make.conf``: ``pkgdir`` → ``/var/cache/binpkgs``,
      ``ccache_dir()`` → ``/var/cache/ccache``, ``sccache_dir()`` → ``/var/cache/sccache``,
      ``distdir()`` → ``/var/cache/distfiles``. Binpkgs and caches persist on the host.

    ``repos_conf_dir`` is optional to keep the test call (which monkeypatches
    ``bind_repos``) trivial; :meth:`Factory.build` passes the rootfs's real
    ``repos.conf``. ``bind_repos`` is resolved via the module global (monkeypatchable).
    ``repos`` are the pinned repositories by name (D26: the ::gentoo snapshot
    and the overlays' commits); the host's own repos are never bound.
    """
    binds_ro = bind_repos(repos_conf_dir if repos_conf_dir is not None else Path(), pinned=repos)
    binds_rw: list[tuple[Path, Path]] = [
        (pkgdir, _PKGDIR_DST),
        (config.ccache_dir(), _CCACHE_DST),
        (config.sccache_dir(), _SCCACHE_DST),
        (config.distdir(), _DISTDIR_DST),
    ]
    return binds_ro, binds_rw


def portage_ids(rootfs: Path) -> tuple[int, int] | None:
    """The ``portage`` uid and gid of the ROOTFS (not the host's). Pure I/O.

    ``None`` when either file lacks the entry. Read from the image because the
    host may have no portage user, or another id for it.
    """

    def _lookup(path: Path) -> int | None:
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split(":")
            if len(fields) > 2 and fields[0] == "portage" and fields[2].isdigit():
                return int(fields[2])
        return None

    uid = _lookup(rootfs / "etc" / "passwd")
    gid = _lookup(rootfs / "etc" / "group")
    return None if uid is None or gid is None else (uid, gid)


def _ensure_bind_dirs(binds_rw: list[tuple[Path, Path]], *, rootfs: Path | None = None) -> None:
    """Create the host-side directories of the RW binds before the nspawn.

    ``systemd-nspawn`` requires the *source* of each ``--bind=`` to exist on the host;
    without this the spawn aborts with ``Failed to clone …``. It stays outside
    :func:`_build_binds` to preserve the purity (and the unit test) of that assembly.

    With ``rootfs``, the ccache directory is also handed to the image's
    ``portage`` user: compiles run under ``userpriv``, and a root-owned cache
    fails the first one (BOOTSTRAP-PROCESS §5, item 7).
    """
    for src, _dst in binds_rw:
        src.mkdir(parents=True, exist_ok=True)
    ids = portage_ids(rootfs) if rootfs is not None else None
    if ids is None:
        return
    for src, dst in binds_rw:
        if dst == _CCACHE_DST:
            os.chown(src, *ids)


class Factory:
    """Builds the stage4 (binpkgs) of a resolved recipe (OVERVIEW §6).

    Takes the already resolved recipe and the output ``pkgdir`` (host-side PKGDIR) and
    orchestrates the phased build in a clean, **non-ephemeral** container (the rootfs
    persists for fork-point reuse and debugging — R8.4).
    """

    def __init__(self, recipe: ResolvedRecipe, pkgdir: Path) -> None:
        self.recipe = recipe
        self.pkgdir = pkgdir

    def build(
        self,
        *,
        emptytree: bool = True,
        download: bool = True,
        keep: bool = False,
        stop_after: str | None = None,
    ) -> FactoryResult:
        """Compile the recipe's binpkgs in an nspawn container (OVERVIEW §6, R1.1/R8.x).

        Order (see the design's Sequence):

        1. **Root guard** (R8.1): if not root, raises an actionable :class:`FactoryError`
           **before any work** — Shidashi does not escalate privileges.
        2. Resolves the ``snapshot`` from the stage3 pointer (``seed.load_pointer``).
           If :func:`shidashi.phases.fork_point` finds the pinned trunk, restores it into the
           rootfs and resumes after the trunk's last phase (``resume_at``,
           ``fork_point_reused=True``); else the bootstrap checkpoint, when it
           exists; else ``fetch_stage3`` + ``extract_stage3`` into a fresh rootfs.
        3. ``apply_rootfs`` + ``apply_portage`` (layers) + installs ``recipe.sets``
           into ``/etc/portage/sets/``.
        4. Builds the binds (:func:`_build_binds`, ccache owned by the image's
           ``portage``) and opens a **non-ephemeral** :class:`Container`.
        5. Over a fresh stage3 only: the toolchain bootstrap
           (:func:`shidashi.bootstrap.run_bootstrap`), then its checkpoint. Then
           the generation fingerprint is recorded in, or checked against, the
           PKGDIR (:func:`shidashi.generation.check_or_record`, D26).
        6. :func:`shidashi.phases.run_phases` (phases + settle-pass), with the
           fingerprint re-checked after every phase's emerge (:func:`_generation_recheck`).
        7. Builds the :class:`FactoryResult`. On success and without ``keep``, removes the
           build rootfs; on failure or ``keep``, preserves it (R8.4). An ``emerge``
           failure already comes up as a :class:`FactoryError` from ``run_phase``/
           ``settle_pass`` and propagates.
        """
        _require_root()
        run = audit.current()

        recipe = self.recipe
        rootfs = config.build_root() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}"

        pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
        snapshot = pointer.snapshot
        # restore points are keyed by the repository pins too (D7); an
        # unreadable pin file refuses here, before any fetch
        pins = load_pin_id(config.seeds_dir())
        fork_points_dir = config.fork_points_dir()
        with run.step("seed") as step:
            # first: a pin that is missing or inside the cooldown refuses the build
            # before any seed is extracted (D26)
            repos = pinned_repos(
                seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
            )
            resume_at, fork_point_path, fork_point_reused, bootstrapped = _seed_or_restore(
                recipe,
                rootfs,
                pointer,
                snapshot=snapshot,
                pins=pins,
                fork_points_dir=fork_points_dir,
                download=download,
            )
            entry = _entry_layers(recipe, done=_phases_through(recipe, resume_at))
            _prepare_portage(rootfs, recipe, layers=entry)
            step.add(
                resume_at=resume_at,
                fork_point=str(fork_point_path) if fork_point_path else None,
                fork_point_reused=fork_point_reused,
                bootstrapped=bootstrapped,
                layers=list(entry) if entry is not None else None,
                pins=pins,
            )

        binds_ro, binds_rw = _build_binds(
            recipe,
            pkgdir=self.pkgdir,
            repos_conf_dir=rootfs / "etc" / "portage" / "repos.conf",
            repos=repos,
        )
        _ensure_bind_dirs(binds_rw, rootfs=rootfs)

        keep_rootfs = keep
        bootstrap: BootstrapResult | None = None
        try:
            with Container(
                rootfs,
                ephemeral=False,
                binds=binds_ro,
                binds_rw=binds_rw,
                log=config.build_log_path(recipe),
            ) as container:
                if not bootstrapped:
                    with run.step("bootstrap") as step:
                        bootstrap = _bootstrap(
                            container,
                            recipe,
                            pkgdir=self.pkgdir,
                            snapshot=snapshot,
                            pins=pins,
                            fork_points_dir=fork_points_dir,
                        )
                        step.add(
                            binutils=bootstrap.binutils,
                            gcc=bootstrap.gcc,
                            locales_before=bootstrap.locales_before,
                            locales_after=bootstrap.locales_after,
                        )
                # before any emerge can reuse a binpkg (D26)
                with run.step("generation") as step:
                    generation_print = fingerprint(rootfs, recipe)
                    recorded = check_or_record(self.pkgdir, generation_print)
                    step.add(recorded=recorded, fingerprint=generation_print.model_dump())
                with run.step("stages"):
                    results = run_phases(
                        container,
                        recipe,
                        emptytree=emptytree,
                        resume_at=resume_at,
                        snapshot=snapshot,
                        pins=pins,
                        fork_points_dir=fork_points_dir,
                        stop_after=stop_after,
                        on_phase_emerged=_generation_recheck(self.pkgdir, rootfs, recipe),
                    )
        except BaseException:
            keep_rootfs = True  # preserves the rootfs for debugging on failure (R8.4)
            raise

        phase_names = tuple(r.phase.name for r in results if r.phase.name != "settle")
        built_atoms: tuple[str, ...] = ()
        settle_atoms: tuple[str, ...] = ()
        for r in results:
            if r.phase.name == "settle":
                settle_atoms += r.built_atoms  # one settle per shipped stage (D24)
            else:
                built_atoms += r.built_atoms
        reused_atoms = tuple(a for r in results for a in r.reused_atoms)

        # ISA gap check (R9.x). Scans the ROOTFS, not pkgdir: binpkgs are
        # compressed .gpkg.tar archives objdump cannot read, while the rootfs
        # holds the same binaries already unpacked. It must therefore run before
        # the rootfs is discarded below.
        #
        # Deliberately NOT fatal. The build succeeded and the binpkgs are correct
        # for the target; the finding means only that THIS host cannot execute
        # them, so test suites and ISO smoke tests have to happen elsewhere.
        # Failing here would discard hours of correct work over a fact about the
        # build machine.
        with run.step("isa-check") as step:
            unrunnable = tuple(
                str(f.path.relative_to(rootfs)) for f in isacheck.check_rootfs(rootfs, recipe.arch)
            )
            step.add(unrunnable=len(unrunnable))
        run.metric("packages.built", len(built_atoms))
        run.metric("packages.reused", len(reused_atoms))
        run.metric("packages.settled", len(settle_atoms))

        result = FactoryResult(
            pkgdir=self.pkgdir,
            built_atoms=built_atoms,
            phases=phase_names,
            fork_point=fork_point_path,
            fork_point_reused=fork_point_reused,
            settle_atoms=settle_atoms,
            unrunnable_here=unrunnable,
            bootstrap=bootstrap,
            reused_atoms=reused_atoms,
        )

        if not keep_rootfs:
            with run.step("cleanup"):
                shutil.rmtree(rootfs, ignore_errors=True)
        return result

    def update(self, *, download: bool = True, keep: bool = False) -> FactoryResult:
        """Bring a shipped image to the week's pinned tree, within its generation (D26).

        1. Root guard; the pinned ::gentoo tree (its cooldown refuses early).
        2. An image fork point must exist (:func:`update_source`: the current
           pin's, else an older pin's or the pre-fix key, D10) -- an update
           updates a built image, it never builds one -- and so must the generation's
           fingerprint in the PKGDIR: an empty PKGDIR is a new generation, which
           is a full build, not an update.
        3. Restore it; apply the full configuration (every layer) and the sets.
        4. In the container: the fingerprint must still match, then
           :func:`shidashi.update.run_update` -- plan, refuse a plan that ends
           the generation, ``-uDN --changed-deps`` with ``--usepkg``,
           ``@preserved-rebuild`` -- then the fingerprint is re-checked.
        5. Write the updated image under the current pin's key -- never over
           the source, which may be an older pin's image (D10).
        """
        _require_root()

        recipe = self.recipe
        rootfs = config.build_root() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}"
        snapshot = load_pointer(recipe.init, seeds_dir=config.seeds_dir()).snapshot
        pins = load_pin_id(config.seeds_dir())
        fork_points_dir = config.fork_points_dir()
        repos = pinned_repos(
            seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
        )

        target = recipe.stages[-1] if recipe.stages else recipe.flavor
        image = stage_fork_point_path(
            recipe, target, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir
        )
        source = update_source(
            recipe, target, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir
        )
        if source is None:
            raise FactoryError(
                f"nothing to update: {image.name} does not exist, nor an image of an older "
                "pin -- build the image first",
                phase="update",
            )
        if not (self.pkgdir / FINGERPRINT_FILE).is_file():
            raise FactoryError(
                f"{self.pkgdir} has no generation fingerprint: an update continues a "
                "generation, it never starts one -- run a full build",
                phase="update",
            )

        run = audit.current()
        with run.step("restore", fork_point=str(source)):
            _restore_into(source, rootfs)
            _prepare_portage(rootfs, recipe)

        binds_ro, binds_rw = _build_binds(
            recipe,
            pkgdir=self.pkgdir,
            repos_conf_dir=rootfs / "etc" / "portage" / "repos.conf",
            repos=repos,
        )
        _ensure_bind_dirs(binds_rw, rootfs=rootfs)

        keep_rootfs = keep
        try:
            with Container(
                rootfs,
                ephemeral=False,
                binds=binds_ro,
                binds_rw=binds_rw,
                log=config.build_log_path(recipe),
            ) as container:
                with run.step("generation"):
                    current = fingerprint(rootfs, recipe)
                    check_or_record(self.pkgdir, current)
                with run.step("update") as step:
                    since = int(time.time())
                    result = run_update(container, recipe, current=current)
                    step.add(built=len(result.built_atoms))
                    attach_packages(rootfs, "update", since=since, built=result.built_atoms)
                # what the plan check could not foresee (a toolchain pulled in by
                # @preserved-rebuild): refused before the updated image is written
                _generation_recheck(self.pkgdir, rootfs, recipe)("update")
                with run.step("fork-point", path=str(image)):
                    snapshot_fork_point(rootfs, image)
        except BaseException:
            keep_rootfs = True  # preserved for debugging, as in build()
            raise

        if not keep_rootfs:
            shutil.rmtree(rootfs, ignore_errors=True)
        return FactoryResult(
            pkgdir=self.pkgdir,
            built_atoms=result.built_atoms,
            phases=("update",),
            fork_point=image,
            fork_point_reused=True,
            settle_atoms=(),
        )

    def build_stepwise(
        self,
        *,
        until: str | None = None,
        interactive: bool = False,
        emptytree: bool = True,
        download: bool = True,
        reset: bool = False,
        force_resume: bool = False,
        on_checkpoint: CheckpointHook | None = None,
        on_failure: FailureHook | None = None,
    ) -> FactoryResult:
        """Build the binpkgs step by step, with resume/checkpoints (OVERVIEW §6, R1.x/R2.x/R6.x).

        Interactive/resumable variant of :meth:`build`. Order:

        1. **Root guard** (R8.1, :func:`_require_root`) — the FIRST thing, before
           any fetch/extraction/state I/O; then an invalid ``until`` is refused
           (:func:`shidashi.phases.plan_phase_run`), still before any work.
        2. Resolves ``snapshot`` (stage3 pointer) and ``recipe_hash``; the path of the
           persisted state is :func:`shidashi.config.build_state_path`.
        3. ``reset`` (R6.4): clears the persisted state and removes the rootfs, starting
           over from scratch. Otherwise it loads the state: if it exists and is stale
           (:func:`shidashi.state.is_stale`) and there is no ``force_resume`` → raises
           :class:`StaleStateError` (R6.3) — NEVER proceeds over stale progress.
        4. **Seed-or-restore** in three cases: (a) there are completed phases →
           :func:`shidashi.phases.latest_resumable` + :func:`restore_fork_point` (if the
           tarball is gone/corrupted, raises :class:`FactoryError` and KEEPS the rootfs);
           (b) ``seed_done`` but no phases (e.g. an earlier ``--until seed``) → reuses the
           persistent rootfs AS-IS (R1.4); (c) no state/``reset`` → fresh seed
           (:func:`_fresh_seed`), marks ``seed_done`` and persists; if ``interactive``,
           the ``"seed"`` checkpoint honoring CONTINUE/STOP/SHELL (R2.5).
        5. :func:`_prepare_portage` (layers + sets).
        6. Opens a persistent **non-ephemeral** :class:`Container` (RO/RW binds).
        7. :func:`shidashi.phases.run_phases_stepwise` (resume, checkpoints, retry,
           per-phase snapshot, per-phase persistence).
        8. Builds the extended :class:`FactoryResult` (``stopped_at``/``phase_diffs``/
           ``completed_phases`` read from the persisted state).
        9. **Teardown: NEVER auto-deletes the rootfs** — stop, completion and failure ALL
           keep it (R1.6); only ``reset`` (step 3) removes it. An ``emerge`` failure
           comes up as a :class:`FactoryError` (state already persisted by the stepwise run,
           rootfs kept) and propagates → CLI exit 1.
        """
        _require_root()

        recipe = self.recipe
        # an invalid --until is refused before any fetch, seed or state write
        plan_phase_run(recipe, completed=(), until=until)
        rootfs = config.build_root() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}"
        pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
        snapshot = pointer.snapshot
        pins = load_pin_id(config.seeds_dir())
        fork_points_dir = config.fork_points_dir()
        state_path = config.build_state_path(recipe)
        rh = state.recipe_hash(recipe)
        repos = pinned_repos(
            seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
        )

        if reset:
            state.clear_state(state_path)
            shutil.rmtree(rootfs, ignore_errors=True)
            loaded: state.BuildState | None = None
        else:
            loaded = state.load_state(state_path)
            if (
                loaded is not None
                and state.is_stale(loaded, snapshot=snapshot, pins=pins, recipe_hash=rh)
                and not force_resume
            ):
                raise StaleStateError(
                    f"stale build state for {recipe.arch}-{recipe.flavor}-"
                    f"{recipe.init} (snapshot, pins or recipe changed); run with --reset to "
                    "start over from scratch or --force-resume to resume anyway"
                )

        completed = loaded.completed_phases if loaded is not None else ()
        seed_done = loaded.seed_done if loaded is not None else False

        stopped_at_seed = self._seed_or_restore_stepwise(
            recipe,
            rootfs,
            pointer,
            snapshot=snapshot,
            pins=pins,
            recipe_hash=rh,
            fork_points_dir=fork_points_dir,
            state_path=state_path,
            completed=completed,
            seed_done=seed_done,
            interactive=interactive,
            download=download,
            on_checkpoint=on_checkpoint,
        )
        if stopped_at_seed:
            return self._assemble_result(state_path, stopped_at="seed", results=())

        seeded = state.load_state(state_path)
        needs_bootstrap = not completed and (seeded is None or not seeded.bootstrap_done)

        _prepare_portage(rootfs, recipe, layers=_entry_layers(recipe, done=completed))

        binds_ro, binds_rw = _build_binds(
            recipe,
            pkgdir=self.pkgdir,
            repos_conf_dir=rootfs / "etc" / "portage" / "repos.conf",
            repos=repos,
        )
        _ensure_bind_dirs(binds_rw, rootfs=rootfs)

        # Teardown rule (R1.6): the stepwise run NEVER auto-deletes the rootfs — stop,
        # completion and failure ALL keep it; only ``reset`` (above) removes it. That is why
        # there is NO removal clause here, and an emerge failure propagates with the
        # state already persisted by run_phases_stepwise and the rootfs intact (R3.5).
        bootstrap: BootstrapResult | None = None
        with Container(
            rootfs,
            ephemeral=False,
            binds=binds_ro,
            binds_rw=binds_rw,
            log=config.build_log_path(recipe),
        ) as container:
            run = audit.current()
            if needs_bootstrap:
                with run.step("bootstrap"):
                    bootstrap = _bootstrap(
                        container,
                        recipe,
                        pkgdir=self.pkgdir,
                        snapshot=snapshot,
                        pins=pins,
                        fork_points_dir=fork_points_dir,
                    )
                if seeded is not None:
                    state.save_state(
                        state_path, seeded.model_copy(update={"bootstrap_done": True, "pins": pins})
                    )
            # before any emerge can reuse a binpkg (D26)
            with run.step("generation"):
                check_or_record(self.pkgdir, fingerprint(rootfs, recipe))
            results = run_phases_stepwise(
                container,
                recipe,
                emptytree=emptytree,
                completed=completed,
                until=until,
                snapshot=snapshot,
                pins=pins,
                fork_points_dir=fork_points_dir,
                state_path=state_path,
                on_checkpoint=on_checkpoint,
                on_failure=on_failure,
                on_phase_emerged=_generation_recheck(self.pkgdir, rootfs, recipe),
            )

        return self._assemble_result(
            state_path,
            stopped_at=self._stopped_label(
                results,
                until=until,
                final_phase=recipe.phases[-1].name if recipe.phases else None,
            ),
            results=results,
            bootstrap=bootstrap,
        )

    def _seed_or_restore_stepwise(
        self,
        recipe: ResolvedRecipe,
        rootfs: Path,
        pointer: Stage3Pointer,
        *,
        snapshot: str,
        pins: str,
        recipe_hash: str,
        fork_points_dir: Path,
        state_path: Path,
        completed: tuple[str, ...],
        seed_done: bool,
        interactive: bool,
        download: bool,
        on_checkpoint: CheckpointHook | None,
    ) -> bool:
        """Resolve the stepwise seed-or-restore in its three cases (R1.4/R2.5/R5.2/R6.4).

        Returns ``True`` if an interactive ``"seed"`` checkpoint asked for STOP (the
        caller returns early, rootfs kept); ``False`` if the build should go on.

        * (a) non-empty ``completed`` → :func:`shidashi.phases.latest_resumable` and, if there
          is a per-phase snapshot, :func:`restore_fork_point` into a clean rootfs. If the
          restore raises (tarball gone/corrupted — ``latest_resumable`` only probed
          ``.exists()``), wraps it in :class:`FactoryError` and KEEPS the rootfs
          (Reviewer #8) — it does not go on with an undefined rootfs.
        * (b) ``seed_done`` without completed phases → reuses the persistent rootfs AS-IS,
          with no re-fetch/extract and no restore (R1.4 — Reviewer #5).
        * (c) no state → :func:`_fresh_seed`, marks ``seed_done`` and persists; if
          ``interactive`` presents the ``"seed"`` checkpoint (CONTINUE/STOP/SHELL,
          R2.5) — at the seed the Container is not open yet, so SHELL opens a
          transient shell over the persistent rootfs.
        """
        if completed:
            phase, path = latest_resumable(
                recipe,
                snapshot=snapshot,
                pins=pins,
                completed=completed,
                fork_points_dir=fork_points_dir,
            )
            if path is not None:
                shutil.rmtree(rootfs, ignore_errors=True)
                rootfs.mkdir(parents=True, exist_ok=True)
                try:
                    restore_fork_point(path, rootfs)
                except Exception as err:  # tarball gone/corrupted between the probe and the use
                    raise FactoryError(
                        f"failed to restore the snapshot of phase {phase!r} from {path}: {err}; "
                        "the rootfs was kept — run with --reset to start over from scratch"
                    ) from err
            return False

        if seed_done:
            # (b) seed already done by an earlier --until seed, with no completed phases:
            # reuse the persistent rootfs as it is — neither re-seed nor restore (R1.4).
            return False

        # (c) no state. The bootstrap checkpoint, when one exists, replaces
        # the raw stage3 AND the ~15 min toolchain rebuild on top of it.
        checkpoint = bootstrap_fork_point_path(
            recipe, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir
        )
        if checkpoint.exists():
            _restore_into(checkpoint, rootfs)
            state.save_state(
                state_path,
                state.BuildState(
                    arch=recipe.arch,
                    flavor=recipe.flavor,
                    init=recipe.init,
                    snapshot=snapshot,
                    pins=pins,
                    recipe_hash=recipe_hash,
                    seed_done=True,
                    bootstrap_done=True,
                ),
            )
            return self._seed_checkpoint(rootfs, recipe, on_checkpoint) if interactive else False

        # Fresh seed, and persist the seed_done milestone.
        shutil.rmtree(rootfs, ignore_errors=True)  # no state: nothing to keep
        _fresh_seed(rootfs, pointer, download=download)
        state.save_state(
            state_path,
            state.BuildState(
                arch=recipe.arch,
                flavor=recipe.flavor,
                init=recipe.init,
                snapshot=snapshot,
                pins=pins,
                recipe_hash=recipe_hash,
                seed_done=True,
            ),
        )
        if interactive:
            return self._seed_checkpoint(rootfs, recipe, on_checkpoint)
        return False

    @staticmethod
    def _seed_checkpoint(
        rootfs: Path, recipe: ResolvedRecipe, on_checkpoint: CheckpointHook | None
    ) -> bool:
        """Present the ``"seed"`` checkpoint and return ``True`` if the user asked for STOP (R2.5).

        Without ``on_checkpoint`` ⇒ auto-CONTINUE (returns ``False``). Otherwise it
        asks the hook with a base :class:`~shidashi.state.PhaseDiff` (phase ``"seed"``,
        no atoms): CONTINUE goes on (``False``); STOP breaks (``True``); SHELL
        opens a transient shell over the persistent rootfs (the build's Container
        is not open yet at the seed) and presents the SAME checkpoint again.
        """
        if on_checkpoint is None:
            return False
        diff = PhaseDiff(phase="seed", built=())
        while True:
            decision = on_checkpoint("seed", diff)
            if decision is CheckpointDecision.CONTINUE:
                return False
            if decision is CheckpointDecision.STOP:
                return True
            Container(rootfs, ephemeral=False).shell()

    def _assemble_result(
        self,
        state_path: Path,
        *,
        stopped_at: str | None,
        results: tuple[PhaseResult, ...],
        bootstrap: BootstrapResult | None = None,
    ) -> FactoryResult:
        """Build the extended :class:`FactoryResult` by reading the persisted state (R6.1).

        ``phase_diffs``/``completed_phases`` come from the re-read
        :class:`~shidashi.state.BuildState` (the source of truth of the progress, persisted
        per phase); without a state they fall back to ``()``. ``built_atoms``/``settle_atoms``/
        ``phases`` are derived from the phases actually run in ``results`` (the settle only
        appears when it ran), mirroring :meth:`build`.
        """
        persisted = state.load_state(state_path)
        phase_diffs = persisted.phase_diffs if persisted is not None else ()
        completed_phases = persisted.completed_phases if persisted is not None else ()

        phase_names = tuple(r.phase.name for r in results if r.phase.name != "settle")
        built_atoms: tuple[str, ...] = ()
        settle_atoms: tuple[str, ...] = ()
        for r in results:
            if r.phase.name == "settle":
                settle_atoms += r.built_atoms  # one settle per shipped stage (D24)
            else:
                built_atoms += r.built_atoms

        return FactoryResult(
            pkgdir=self.pkgdir,
            built_atoms=built_atoms,
            phases=phase_names,
            fork_point=None,
            fork_point_reused=False,
            settle_atoms=settle_atoms,
            stopped_at=stopped_at,
            phase_diffs=phase_diffs,
            completed_phases=completed_phases,
            bootstrap=bootstrap,
            reused_atoms=tuple(a for r in results for a in r.reused_atoms),
        )

    @staticmethod
    def _stopped_label(
        results: tuple[PhaseResult, ...], *, until: str | None, final_phase: str | None
    ) -> str | None:
        """The label where the stepwise run stopped: the last real phase, when it was
        not the final one.

        ``None`` (ran to the end) when the last REAL phase run is the recipe's
        final phase. The criterion used to be "there was a settle", which stopped
        holding with one settle per shipped stage (D24): minimal is settled
        halfway through kde's path. With no real phase run (everything was already
        completed), returns ``until``.
        """
        real = [r.phase.name for r in results if r.phase.name != "settle"]
        if real:
            return None if real[-1] == final_phase else real[-1]
        return until

    @staticmethod
    def _install_sets(rootfs: Path, recipe: ResolvedRecipe) -> None:
        """Install the recipe's sets (R6.4). Delegates to :func:`resolve.install_sets`.

        The Factory and the Assembler consume the SAME set curation, and for a long
        time each had its own COPY of the logic -- "mirrors the other", the
        docstring said. The two diverged as soon as one was fixed: the Assembler
        went on without resolving ``@refs`` and without applying ``exclude``, assembling ISOs
        from broken sets. Now there is a single implementation.
        """
        install_sets(rootfs, recipe)
