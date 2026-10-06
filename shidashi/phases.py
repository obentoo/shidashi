"""Phases — phased build execution + layer cache (OVERVIEW §6.4 / §6.5).

PURE planning layer of story 003 (groups 3 + 4.1):

* **3.1** :func:`phase_target` / :func:`phase_emerge_argv` — the emerge target of each
  phase and the full argv (``--emptytree`` only in the base, ``-uDN`` in the other stages).
* **3.2** :func:`use_break_lines` / :func:`write_use_break` / :func:`clear_use_break`
  — the break-pass's transient ``package.use`` (I/O only against an on-disk rootfs,
  no root).
* **3.3** :func:`parse_built_atoms` — atoms built, from the ``emerge --verbose``
  output (reuses the matcher of :mod:`shidashi.resolve`).
* **3.4** :func:`fork_point` / :func:`trunk_phase_names` — the trunk reuse decision
  (only probes the filesystem) and the names of the trunk's phases.
* **4.1** :func:`snapshot_fork_point` / :func:`restore_fork_point` — capture and
  restore of the trunk as a tarball (atomic write via temp + ``os.replace``;
  I/O against an on-disk tree, no nspawn).

The privileged orchestration (``run_phase``/``settle_pass``/``run_phases``) of
story 003 task 5 runs ``emerge`` *inside* the container (nspawn) — it requires root and
is exercised by the host-gated integration tests. ``emerge`` failures (non-zero
exit) are wrapped in :class:`FactoryError`.
"""

import dataclasses
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

import pydantic

from shidashi import audit, config, progress, state
from shidashi.container import Container
from shidashi.flow import StagesFlow, active_flow
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from shidashi.resolve import _atom_from_ebuild_line, _iter_atom_lines, apply_portage
from shidashi.seed import ROOTFS_TAR_FLAGS
from shidashi.state import EmergePlanEntry, PhaseDiff

_USE_BREAK_FILE = ("etc", "portage", "package.use", "zz-shidashi-use-break")


class FactoryError(Exception):
    """Failure to build a phase/stage inside the container (OVERVIEW §6.4).

    Carries the ``phase`` where it happened (``None`` when not tied to a phase)
    and the captured ``emerge`` ``output`` (stdout+stderr) for diagnosis.

    Defined here (and not in :mod:`shidashi.factory`) to avoid a circular import:
    ``factory`` imports from ``phases`` (it orchestrates phases), and ``phases`` needs
    to raise this error; ``shidashi.factory`` re-exports the symbol.
    """

    def __init__(self, message: str, *, phase: str | None = None, output: str = "") -> None:
        super().__init__(message)
        self.phase = phase
        self.output = output


class CheckpointDecision(StrEnum):
    """The user's decision at a post-phase checkpoint of the interactive build (R2.2/R2.3).

    ``StrEnum`` (not ``(str, Enum)`` — UP042) whose members are worth their own name:
    ``CONTINUE`` goes on to the next phase, ``STOP`` breaks the loop without running the
    settle (R1.3), ``SHELL`` opens a shell in the container and presents the SAME
    checkpoint again. Defined here (and not in :mod:`shidashi.factory`) to avoid the
    circular import ``factory → phases``; ``shidashi.factory`` re-exports the symbol (Task 6).
    """

    CONTINUE = "CONTINUE"
    STOP = "STOP"
    SHELL = "SHELL"


class FailureDecision(StrEnum):
    """The user's decision on the failure of a phase of the interactive build (R3.1–R3.4).

    ``StrEnum`` (UP042): ``RETRY`` re-runs the SAME phase (same argv) and ``ABORT``
    persists the state and raises :class:`FactoryError`. There is NO option to skip a
    failed phase (R3.4). Defined here for the same circular-import reason as
    :class:`CheckpointDecision`; re-exported by :mod:`shidashi.factory` (Task 6).
    """

    RETRY = "RETRY"
    ABORT = "ABORT"


class PhaseResult(pydantic.BaseModel):
    """The result of running a single phase (OVERVIEW §6.4).

    *Frozen* value object: the phase that ran, the atoms built
    (:func:`parse_built_atoms` of the ``emerge`` output), the path of the fork-point
    snapshot when there is one (OVERVIEW §6.5; ``None`` when the phase does not
    materialize a fork point) and the raw ``emerge --verbose`` ``output`` (stdout+stderr)
    for the driver to compose the phase's diff WITHOUT re-running emerge (R4.1). ``output``
    defaults to ``""`` — it keeps story 003's construction, which does not pass it, valid.
    ``arbitrary_types_allowed`` admits :class:`~pathlib.Path`.
    """

    model_config = pydantic.ConfigDict(frozen=True, arbitrary_types_allowed=True)
    phase: Phase
    built_atoms: tuple[str, ...]
    snapshot: Path | None
    output: str = ""
    #: Installed from the generation's binpkgs rather than compiled.
    reused_atoms: tuple[str, ...] = ()


# --- 3.1 phase_target / phase_emerge_argv (PURE) -----------------------------


def phase_target(phase: Phase, recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Return the ``emerge`` target of a phase (R3.1/R3.2/R3.3, D24). Pure.

    - every STAGE phase → ``@world`` and then the stage's own sets. The base
      rebuilds it (``--emptytree``); a later stage updates it (``-uDN``), so
      that a package an earlier stage installed is rebuilt for THIS stage's USE
      even when it is outside the new sets' graph (measured on the real
      minimal: desktop left vim, kbd and fastfetch with the old USE without it);
    - a phase that is not a stage → ``phase.packages`` (the ``seat`` atoms).

    Nothing here depends on the phase's NAME. Two name-based conventions already
    cost dearly: ``apps`` → a literal ``@bentoo-apps`` (renaming the set left the phase
    pointing at nothing) and ``desktop`` → ``@<flavor>``. The relation is now
    data of the stage, and ``recipe`` stays in the signature only for compatibility.
    """
    del recipe  # the stage says it all; kept for the callers' signature
    sets = tuple("@" + name for name in phase.sets)
    if phase.emptytree or phase.stage:
        return ("@world", *sets)
    return sets or phase.packages


#: How the assembler installs an image: only binpkgs, and all of them -- the
#: stage3's own packages included (OVERVIEW §7). ``--binpkg-respect-use=y``
#: because --usepkgonly turns it OFF by default: emerge would then take any
#: instance of a package whatever its USE -- the cut one or the settled one.
#: ``--use-ebuild-visibility=y`` because --usepkgonly also ignores the ebuild
#: repositories: emerge would take any binpkg of the binhost, even a version the
#: pinned tree does not have (systemd-262 into an image pinned to a tree with
#: 261.3, 2026-10-06), and ignore the tree's masks. With it a binpkg is taken only
#: when its ebuild is in the pinned tree and visible.
#: The factory's binpkg check resolves with the same options, so the two
#: cannot drift apart.
ISO_EMERGE_OPTIONS = (
    "--usepkgonly",
    "--binpkg-respect-use=y",
    "--use-ebuild-visibility=y",
    "--emptytree",
)

#: The assembler's settle: the cut packages again, from their final binpkgs.
ISO_SETTLE_OPTIONS = (
    "--usepkgonly",
    "--binpkg-respect-use=y",
    "--use-ebuild-visibility=y",
    "--oneshot",
)

#: Where the binpkg check puts the stage3's vdb inside the build rootfs: the
#: ROOT its emerge resolves against, so it sees what the assembler sees.
_CHECK_ROOT = Path("var/tmp/shidashi-iso-root")


def image_targets(sets: tuple[str, ...]) -> tuple[str, ...]:
    """The emerge targets of an image made of ``sets``. Pure.

    ``@system`` plus the sets; ``@world`` when there are none (= ``@system`` +
    what the base seeded).
    """
    return ("@system", *(f"@{name}" for name in sets)) if sets else ("@world",)


def _stage_phases(recipe: ResolvedRecipe, stage: str) -> tuple[Phase, ...]:
    """The recipe's stage phases up to and including ``stage``. Pure."""
    chosen: list[Phase] = []
    for phase in recipe.phases:
        if not phase.stage:
            continue
        chosen.append(phase)
        if phase.stage == stage:
            return tuple(chosen)
    raise ValueError(f"stage {stage!r} is not in the recipe's chain")


def shipped_sets(recipe: ResolvedRecipe, stage: str) -> tuple[str, ...]:
    """The sets of the image that ends at ``stage``: every stage's up to it. Pure.

    For the recipe's last stage this is ``recipe.sets``, what the assembler
    installs; for an earlier stage that ships (minimal, inside a flavor's
    recipe) it is that smaller image.
    """
    sets: list[str] = []
    for phase in _stage_phases(recipe, stage):
        sets.extend(s for s in phase.sets if s not in sets)
    return tuple(sets)


def image_cuts(recipe: ResolvedRecipe, stage: str | None = None) -> tuple[UseBreak, ...]:
    """Every cycle cut of the phases up to ``stage`` -- of the whole chain when
    ``stage`` is ``None`` (the assembler's image). Pure.

    The factory built each cut package twice -- cut in its stage, final in the
    settle -- and both binpkgs stay in the PKGDIR. A fresh stage3 has none of
    the cycle's packages installed, so the assembler meets every cycle again
    (ffmpeg -> libsdl2 -> pipewire[ffmpeg] -> ffmpeg stopped the first kde ISO,
    2026-09-29): it installs under the same cuts, then settles them.
    """
    cuts: list[UseBreak] = []
    phases = recipe.phases if stage is None else _stage_phases(recipe, stage)
    for phase in phases:
        cuts.extend(c for c in phase.use_break if c not in cuts)
    return tuple(cuts)


def binpkg_check_argv(recipe: ResolvedRecipe, stage: str, *, root: str) -> list[str]:
    """The ``emerge --pretend`` of the assembler's install, against ``root``. Pure."""
    return [
        "emerge",
        "--pretend",
        f"--root={root}",
        *ISO_EMERGE_OPTIONS,
        *image_targets(shipped_sets(recipe, stage)),
    ]


def binpkg_settle_check_argv(atoms: tuple[str, ...], *, root: str) -> list[str]:
    """The ``emerge --pretend`` of the assembler's settle. Pure.

    ``--nodeps``: the question is only whether each cut package has a binpkg
    with its final USE. Its dependencies were installed by the first pass,
    which a pretend does not do -- resolving them here would meet the very
    cycle the cut exists to avoid.
    """
    return ["emerge", "--pretend", f"--root={root}", *ISO_SETTLE_OPTIONS, "--nodeps", *atoms]


def _extract_vdb(tarball: Path, dest: Path) -> None:
    """Only ``var/db/pkg`` of a stage3 into ``dest`` (~6 s, ~50 MB). I/O."""
    dest.mkdir(parents=True, exist_ok=True)
    _tar(["-xpf", str(tarball), "-C", str(dest), "./var/db/pkg"])


def _seed_tarball(recipe: ResolvedRecipe) -> Path:
    """The cached stage3 the recipe's builds start from (never downloads)."""
    from shidashi.seed import fetch_stage3, load_pointer

    pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
    return fetch_stage3(pointer, cache_dir=config.cache_dir(), download=False)


def check_binpkgs(container: Container, recipe: ResolvedRecipe, stage: str) -> None:
    """Fail when the image ending at ``stage`` cannot be installed from binpkgs.

    PRIVILEGED. Replays the assembler's plan with ``--pretend``: the install
    under the image's cuts, then the settle of the cut packages, against a ROOT
    that holds only the stage3's vdb (what the assembler starts from).

    It catches a package with no binpkg (ccache, from the bootstrap, F75) and a
    binpkg whose USE no longer matches. It does NOT reliably catch a dependency
    cycle (F76): the resolver here is the IMAGE's Portage, while the assembler
    runs the STAGE3's. Measured on 2026-09-29: portage-3.0.82.2 orders the
    ffmpeg -> libsdl2 -> pipewire cycle with no cut, 3.0.81.3 refuses it -- with
    and without --root. A cycle is caught when this resolver also refuses it.
    """
    root = container.rootfs / _CHECK_ROOT
    inside = "/" + _CHECK_ROOT.as_posix()
    cuts = image_cuts(recipe, stage)
    phase = f"{stage}:binpkgs"
    try:
        shutil.rmtree(root, ignore_errors=True)
        _extract_vdb(_seed_tarball(recipe), root)
        write_cuts(container.rootfs, cuts)
        try:
            container.run(binpkg_check_argv(recipe, stage, root=inside), check=True)
            clear_use_break(container.rootfs)
            atoms = tuple(sorted({c.atom for c in cuts}))
            if atoms:
                container.run(binpkg_settle_check_argv(atoms, root=inside), check=True)
        except subprocess.CalledProcessError as exc:
            output = (exc.output or "") + (exc.stderr or "")
            missing = re.findall(r'no binary packages to satisfy "([^"]+)"', output)
            if missing:
                detail = f": no binpkg for {', '.join(missing)}"
            elif "circular dependencies" in output:
                detail = ": a dependency cycle with no cut (use_break) in the chain"
            else:
                detail = ""
            raise FactoryError(
                f"the {stage} image cannot be installed from binpkgs{detail}. The stage's "
                "fork point is saved; fix it before assembling.",
                phase=phase,
                output=output,
            ) from exc
    finally:
        clear_use_break(container.rootfs)
        shutil.rmtree(root, ignore_errors=True)


def stages_flow() -> StagesFlow:
    """The ``stages`` part of the flow in force (``variants/flow.yaml``)."""
    return active_flow(config.variants_dir()).stages


def phase_emerge_argv(
    phase: Phase, recipe: ResolvedRecipe, *, emptytree: bool, flow: StagesFlow | None = None
) -> list[str]:
    """Build the ``emerge`` argv for a phase (R3.1, D24). Pure.

    The mode comes from the stage, not from the phase's name:

    - the base (``phase.emptytree``) → ``--emptytree`` when ``emptytree`` is
      true: the only full rebuild, which "cooks" the stage3;
    - every stage after it → ``--update --deep --newuse``: recompiles only what
      that stage's configuration changes (the graphical USE, in desktop) and
      installs its sets. Also the base when ``emptytree`` is false;
    - a phase without a stage (openrc's ``seat``) → only its atoms.

    ``--usepkg`` always: a binpkg of the same package, version and USE is
    reused. Safe only because the PKGDIR belongs to ONE generation -- the
    fingerprint check (:mod:`shidashi.generation`) runs before the first phase,
    since Portage itself never compares CFLAGS or the toolchain (D26).
    """
    emerge = (flow or stages_flow()).emerge
    if phase.emptytree and emptytree:
        mode: tuple[str, ...] = emerge.rebuild
    elif phase.stage:
        mode = emerge.update
    else:
        mode = ()
    target = phase_target(phase, recipe)
    if phase.stage and not phase.emptytree and not emerge.world:
        target = tuple(t for t in target if t != "@world")
    return ["emerge", *emerge.options, *mode, *target]


# --- 3.2 the break-pass's transient package.use ------------------------------


def use_break_lines(phase: Phase) -> tuple[str, ...]:
    """Render the ``package.use`` lines of the phase's cycle breaks (R4.1). Pure.

    One line per :class:`~shidashi.recipe.UseBreak`: ``"<atom> <±flag>"`` where the
    sign is ``""`` (enables) when ``enable`` is true and ``"-"``
    (disables) otherwise. A phase without breaks → empty tuple.
    """
    return cut_lines(phase.use_break)


def cut_lines(cuts: tuple[UseBreak, ...]) -> tuple[str, ...]:
    """The ``package.use`` lines of ``cuts``. Pure."""
    return tuple(f"{ub.atom} {'' if ub.enable else '-'}{ub.flag}" for ub in cuts)


def write_use_break(rootfs: Path, phase: Phase) -> Path | None:
    """Write the break-pass's transient ``package.use`` (R4.1/R4.4).

    Writes the lines of :func:`use_break_lines` to
    ``${rootfs}/etc/portage/package.use/zz-shidashi-use-break`` (creating the
    parent directories) and returns the path written. When the phase has no breaks,
    nothing is written and ``None`` is returned. Filesystem I/O only — no root.
    """
    return write_cuts(rootfs, phase.use_break)


def write_cuts(rootfs: Path, cuts: tuple[UseBreak, ...]) -> Path | None:
    """Write ``cuts`` as the transient ``package.use`` file; ``None`` when empty. I/O."""
    lines = cut_lines(cuts)
    if not lines:
        return None
    target = rootfs.joinpath(*_USE_BREAK_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def clear_use_break(rootfs: Path) -> None:
    """Remove the break-pass's transient ``package.use`` if present (R4.4).

    Idempotent: clearing when the file no longer exists is a no-op (does not raise).
    """
    rootfs.joinpath(*_USE_BREAK_FILE).unlink(missing_ok=True)


# --- 3.3 parsing of built atoms ----------------------------------------------


def parse_reused_atoms(emerge_output: str) -> tuple[str, ...]:
    """The ``cat/pkg-version`` of the ``[binary ...]`` lines: installed from a binpkg. Pure.

    Kept apart from :func:`parse_built_atoms` (compiled): a stage served wholly
    from the generation's binpkgs used to report nothing at all.
    """
    atoms = (_atom_from_ebuild_line(line.strip(), "[binary") for line in emerge_output.splitlines())
    return tuple(a for a in atoms if a is not None)


def parse_built_atoms(emerge_output: str) -> tuple[str, ...]:
    """Extract the ``cat/pkg-version`` atoms from an ``emerge --verbose`` output (R3.4). Pure.

    Reuses the shared matcher :func:`shidashi.resolve._iter_atom_lines` (the same
    ``[ebuild ...]`` line matching as :func:`shidashi.resolve.parse_packages`).
    Output without ``[ebuild ...]`` lines → empty tuple.
    """
    return tuple(_iter_atom_lines(emerge_output))


# --- 3.4 fork-point decision (PURE, only probes the filesystem) --------------


def _variant_key(recipe: ResolvedRecipe) -> str:
    """Per-variant key prefix: ``<arch>-<flavor>-<init>`` (R5.1/R5.2). Pure.

    Component shared by the fork-point keys (trunk and per-phase) and the build
    state (:func:`shidashi.config.build_state_path`), isolating the build per variant.
    """
    return f"{recipe.arch}-{recipe.flavor}-{recipe.init}"


def stage_fork_point_path(
    recipe: ResolvedRecipe, stage: str, *, snapshot: str, pins: str, fork_points_dir: Path
) -> Path:
    """Where the fork point of one STAGE lives (D24, F70). Pure.

    ``<arch>-<init>-<snapshot>-<pins>-<stage>.tar`` -- with no target in it, so
    that the fork point of ``base``, ``minimal`` or ``desktop`` built on the way
    to one image is found by every other image of the same arch × init. The
    old key carried the flavor, so the trunk was never shared between flavors.
    ``pins`` (:func:`shidashi.tree.pin_id`) keeps a tree built from one
    repository pin from being restored under another (story 016, D7).
    """
    return fork_points_dir / f"{recipe.arch}-{recipe.init}-{snapshot}-{pins}-{stage}.tar"


def fork_point(
    recipe: ResolvedRecipe, *, snapshot: str, pins: str, fork_points_dir: Path
) -> tuple[Phase, Path] | None:
    """The deepest stage fork point on disk BEFORE the target (D24). Probes only.

    Walks the chain backwards from the stage before the target and returns
    the first whose tarball exists, with its phase -- the point to resume
    after. The target's own stage is never restored: asking for an image is
    asking to build its last stage. ``None`` when nothing is reusable.
    """
    stage_phases = [p for p in recipe.phases if p.stage]
    for phase in reversed(stage_phases[:-1]):
        path = stage_fork_point_path(
            recipe, phase.stage, snapshot=snapshot, pins=pins, fork_points_dir=fork_points_dir
        )
        if path.exists():
            return phase, path
    return None


def pending_breaks(recipe: ResolvedRecipe, *, through: str | None) -> tuple[UseBreak, ...]:
    """The cycle cuts still in force after phase ``through`` (D24). Pure.

    A shipped stage settles every cut accumulated since the previous settle, so
    what is pending is the cuts of the phases after the last shipped one, up to
    and including ``through``. Resuming from a fork point needs it: the base's
    fork point carries its cuts unsettled, and minimal's settle must undo them.
    """
    pending: tuple[UseBreak, ...] = ()
    if through is None:
        return pending
    for phase in recipe.phases:
        pending = () if phase.ships else pending + phase.use_break
        if phase.name == through:
            return pending
    return pending


def trunk_phase_names(recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Names of the *trunk*'s phases: up to and including the base's (R5.4, D24). Pure.

    The trunk is what every image of the same arch × init shares: the seed, the
    phases the ``init`` prepends (``seat``) and the base, the only full
    rebuild. It is fork point 1 of the ``base → minimal → desktop → flavor`` tree.
    """
    names: list[str] = []
    for phase in recipe.phases:
        names.append(phase.name)
        if phase.emptytree:
            break
    return tuple(names)


# --- 3.1 (story 004) parse_emerge_plan (PURE) --------------------------------


def _clean_use_flag(token: str) -> str:
    """Normalize a USE-delta token to the bare flag name. Pure.

    Removes the outer parentheses, the ``-`` disabling sign and the change markers
    ``%``/``*`` (in any combination), returning only the flag name
    (e.g. ``(sound%)`` → ``sound``; ``-wayland*`` → ``wayland``;
    ``(rsync-verify%*)`` → ``rsync-verify``).
    """
    return token.strip("()").lstrip("-").rstrip("%*")


def _use_changes_from_segment(stripped: str) -> tuple[str, ...]:
    """Extract the USE deltas from the ``USE="..."`` segment of an ``[ebuild]`` line. Pure.

    Reads only the quoted content of the first ``USE="..."`` and returns the
    *changed* flags — those marked by ``()``/``%``/``*`` (default changed, changed since
    the last build, asterisk). Flags without a marker (e.g. ``X``, ``vulkan``) are
    current state, not a delta, and are ignored. No ``USE`` segment or no
    marked flags → empty tuple.
    """
    match = re.search(r'USE="([^"]*)"', stripped)
    if match is None:
        return ()
    changes: list[str] = []
    for token in match.group(1).split():
        if "(" in token or "%" in token or "*" in token:
            flag = _clean_use_flag(token)
            if flag:
                changes.append(flag)
    return tuple(changes)


def parse_emerge_plan(
    output: str,
) -> tuple[tuple[EmergePlanEntry, ...], tuple[str, ...]]:
    """Parse an ``emerge --verbose`` output into plan entries + blockers (R4.1/R4.3). Pure.

    Walks the ``[ebuild ...]`` lines reusing the shared matching core
    :func:`shidashi.resolve._atom_from_ebuild_line` (the same atom as
    :func:`parse_built_atoms`), reading from each one: the operation column (the token
    right after ``[ebuild`` — ``N``/``R``/``rR``/``U``/``D``/``r``/``NS``/``UD``) into
    :attr:`~shidashi.state.EmergePlanEntry.op` and the USE deltas of the
    ``USE="..."`` segment (:func:`_use_changes_from_segment`) into ``use_changes``.
    ``[blocks B ...]`` lines are collected (raw, stripped) in the second tuple. Output
    without a merge (e.g. ``"Nothing to merge"``) → ``((), ())``. Does NOT do I/O nor run
    emerge — it operates on the output already captured.
    """
    entries: list[EmergePlanEntry] = []
    blockers: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("[blocks"):
            blockers.append(stripped)
            continue
        atom = _atom_from_ebuild_line(stripped)
        if atom is None:
            continue
        # op column = tokens between ``[ebuild`` and ``]`` (e.g. ``N``, ``rR``);
        # _atom_from_ebuild_line already guaranteed the prefix and the presence of ``]``.
        op_column = stripped[len("[ebuild") :].split("]", 1)[0].split()
        if not op_column:
            continue
        entries.append(
            EmergePlanEntry(
                atom=atom,
                op=op_column[0],
                use_changes=_use_changes_from_segment(stripped),
            )
        )
    return tuple(entries), tuple(blockers)


# --- 3.2 (story 004) compute_phase_diff (PURE) -------------------------------


def _category_pn(atom: str) -> str:
    """Reduce ``cat/pkg-version`` to the ``cat/pkg`` identifier (version-stripped). Pure.

    Removes the version suffix from the package name — everything from the last ``-``
    followed by a digit (covers ``-rN`` revisions, which are part of the version). So
    ``media-libs/mesa-24.0.7`` and ``media-libs/mesa-24.0.5`` both collapse into
    ``media-libs/mesa``, allowing rebuilds to be matched by category/PN
    regardless of the version. Atoms without a version component are returned
    unchanged.
    """
    return re.sub(r"-\d.*$", "", atom)


def compute_phase_diff(
    phase: str,
    plan_entries: tuple[EmergePlanEntry, ...],
    blockers: tuple[str, ...],
    *,
    prior_atoms: tuple[str, ...],
) -> PhaseDiff:
    """Classify a phase's plan into a :class:`~shidashi.state.PhaseDiff` (R4.1/R4.2). Pure.

    From the entries of :func:`parse_emerge_plan` it composes the diff of phase
    ``phase``:

    * ``built`` — the entries' atoms, in order;
    * ``unexpected_rebuilds`` — entries with op ``R``/``rR`` whose category/PN
      identifier (:func:`_category_pn`, *version-stripped*) is already in
      ``prior_atoms`` (a phase rebuilding what an earlier phase already
      built, R4.2) — records the entry's atom (with version);
    * ``use_changes`` — every flag of the entries that carry ``use_changes``,
      flattened in order;
    * ``blockers`` — passthrough of the ``blockers`` argument.

    Does NOT do I/O nor run emerge.
    """
    prior_pn = {_category_pn(atom) for atom in prior_atoms}
    built = tuple(entry.atom for entry in plan_entries)
    unexpected_rebuilds = tuple(
        entry.atom
        for entry in plan_entries
        if entry.op in ("R", "rR") and _category_pn(entry.atom) in prior_pn
    )
    use_changes = tuple(flag for entry in plan_entries for flag in entry.use_changes)
    return PhaseDiff(
        phase=phase,
        built=built,
        unexpected_rebuilds=unexpected_rebuilds,
        use_changes=use_changes,
        blockers=blockers,
    )


# --- 2.1 (story 004) checkpoint_sequence / plan_phase_run (PURE) -------------


def checkpoint_sequence(recipe: ResolvedRecipe) -> tuple[str, ...]:
    """The build's checkpoint sequence: ``seed`` + phases + ``settle`` (R2.5). Pure.

    ``seed`` is checkpoint 0 (the seeded stage3, before any phase) and
    ``settle`` the final checkpoint (the USE-reconciliation settle-pass). Both are
    checkpoint/``--until`` labels only — NEVER members of
    ``completed_phases``/``phase_diffs``/per-phase snapshots, which track only the
    recipe's real phases.
    """
    return ("seed", *(p.name for p in recipe.phases), "settle")


def plan_phase_run(
    recipe: ResolvedRecipe, *, completed: tuple[str, ...], until: str | None
) -> tuple[Phase, ...]:
    """The plan of phases to run from the resume point up to ``until`` (R1.1/R1.2/R1.4/R1.5). Pure.

    Starts from ``recipe.phases``, drops every phase whose name is in ``completed``
    (resume skips what was already built) and, when ``until`` is not ``None``, stops
    **after** the phase named by ``until`` (inclusive). ``until="seed"`` ⇒ an empty
    plan (only seed, no phase). An invalid ``until`` — outside
    ``{"seed"} ∪ {phase names}`` — raises :class:`ValueError` whose message
    **lists the valid names** (including ``"seed"``); the CLI maps that error to
    exit 1. ``seed`` and ``settle`` are checkpoint labels, not phases: they never
    enter ``completed`` nor the returned plan.
    """
    phase_names = tuple(p.name for p in recipe.phases)
    valid = ("seed", *phase_names)
    if until is not None and until not in valid:
        raise ValueError(f"--until {until!r} is invalid; valid values: {', '.join(valid)}")
    plan: list[Phase] = []
    for phase in recipe.phases:
        if phase.name in completed:
            continue
        plan.append(phase)
        if phase.name == until:
            break
    if until == "seed":
        return ()
    return tuple(plan)


# --- 2.2 (story 004) phase_snapshot_path / latest_resumable (PURE) -----------


def phase_snapshot_path(
    recipe: ResolvedRecipe, *, snapshot: str, pins: str, phase: str, fork_points_dir: Path
) -> Path:
    """Path of the per-phase snapshot under ``fork_points_dir`` (R5.1/R5.2). Pure.

    The key is ``<arch>-<flavor>-<init>-<snapshot>-<pins>-<phase>.tar`` (``pins``
    per story 016, D7) — DISTINCT from the key
    of story 003's trunk fork point (:func:`fork_point`, which omits ``phase``):
    each completed phase materializes its own snapshot for a granular resume. It
    neither probes nor writes anything — it only composes the path.
    """
    return fork_points_dir / f"{_variant_key(recipe)}-{snapshot}-{pins}-{phase}.tar"


def latest_resumable(
    recipe: ResolvedRecipe,
    *,
    snapshot: str,
    pins: str,
    completed: tuple[str, ...],
    fork_points_dir: Path,
) -> tuple[str | None, Path | None]:
    """The last completed phase with a snapshot on disk, and its path (R5.2). Pure.

    Walks the completed phases in the order of ``recipe.phases`` (not in the order of
    ``completed``) and returns the LAST one whose :func:`phase_snapshot_path` exists on
    disk, together with the path — the resume's restore point. If no completed phase
    has a snapshot on disk it returns ``(None, None)``. Only probes the
    filesystem.
    """
    found: tuple[str, Path] | None = None
    for phase in recipe.phases:
        if phase.name not in completed:
            continue
        candidate = phase_snapshot_path(
            recipe,
            snapshot=snapshot,
            pins=pins,
            phase=phase.name,
            fork_points_dir=fork_points_dir,
        )
        if candidate.exists():
            found = (phase.name, candidate)
    if found is None:
        return (None, None)
    return found


# --- 4.1 fork-point snapshot / restore (tarball, on-disk I/O) ----------------


def snapshot_fork_point(rootfs: Path, dest: Path) -> Path:
    """Capture ``rootfs`` into a tarball at ``dest`` and return ``dest`` (R5.1/R5.2).

    Atomic write: the tar is first written to a temporary file that is a sibling of
    ``dest`` (same directory, hence same filesystem) and only then promoted via
    :func:`os.replace`. A failure during the write removes the partial temp. The
    content sits at the root of the tar (``-C rootfs .``), so that
    :func:`restore_fork_point` rebuilds it directly under another directory.

    GNU tar with :data:`shidashi.seed.ROOTFS_TAR_FLAGS`: every mode bit and the
    xattrs (file capabilities) survive, which Python's ``tarfile`` did not.
    """
    tmp = dest.with_name(f".{dest.name}.tmp")
    progress.current().note(f"writing {dest.name}")
    try:
        _tar(["--create", "--file", str(tmp), "--directory", str(rootfs), *ROOTFS_TAR_FLAGS, "."])
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def restore_fork_point(tarball: Path, rootfs: Path) -> None:
    """Extract ``tarball`` into ``rootfs`` (R5.1/R5.2), modes and xattrs intact."""
    progress.current().note(f"restoring {tarball.name}")
    _tar(["--extract", "--file", str(tarball), "--directory", str(rootfs), *ROOTFS_TAR_FLAGS])


def _tar(args: list[str]) -> None:
    result = subprocess.run(["tar", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise FactoryError(f"tar {args[0]} failed: {result.stderr.strip()}", phase="fork-point")


# --- privileged orchestration (story 003 task 5) -----------------------------


def _run_emerge(
    container: Container, argv: list[str], *, phase: str
) -> tuple[tuple[str, ...], str]:
    """Run an ``emerge`` in the container; return ``(atoms, raw output)`` (R3.4/R8.3).

    Wraps a non-zero ``emerge`` (``CalledProcessError``) in a
    :class:`FactoryError` carrying ``phase`` and the captured output
    (stdout+stderr). On success it returns the tuple of :func:`parse_built_atoms`
    TOGETHER with the raw ``stdout+stderr`` output — the stepwise driver reuses that output
    to compose the phase's diff (:func:`parse_emerge_plan`/:func:`compute_phase_diff`)
    WITHOUT re-running emerge (R4.1).
    """
    try:
        result = container.run(argv, check=True)
    except subprocess.CalledProcessError as exc:
        output = (exc.output or "") + (exc.stderr or "")
        raise FactoryError(f"emerge failed in phase {phase!r}", phase=phase, output=output) from exc
    raw_output = result.stdout + result.stderr
    return parse_built_atoms(raw_output), raw_output


# --- module-rebuild (F83) --------------------------------------------------------
#
# A binpkg of an out-of-tree module (nvidia-drivers, r8168...) carries modules for
# the kernel it was BUILT against. Portage reuses it while `virtual/dist-kernel`'s
# subslot is unchanged -- and gentoo-kernel-7.2.6 and gentoo-kernel-bin-7.2.6 share
# it, though their modules live in different directories (7.2.6-gentoo-dist vs
# 7.2.6-gentoo-dist-bin). After the swap the image had its modules for a kernel it
# did not have (2026-10-01). This step finds such directories and rebuilds their
# owners from source, against the kernel actually installed.

#: The rootfs's modules directory (``/lib`` is a symlink to ``usr/lib``).
_MODULES = Path("usr") / "lib" / "modules"
_VDB = Path("var") / "db" / "pkg"


def stale_module_dirs(rootfs: Path) -> tuple[str, ...]:
    """Kernel-version directories under /lib/modules with no kernel in them. I/O.

    A kernel's directory holds its image (``vmlinuz``, where kernel-install and
    the dist-kernels put it); one without is left by modules built for a kernel
    that is not installed. ``()`` when no kernel is installed at all: nothing to
    compare against yet.
    """
    root = rootfs / _MODULES
    if not root.is_dir():
        return ()
    dirs = sorted(d for d in root.iterdir() if d.is_dir())
    kernels = {d.name for d in dirs if (d / "vmlinuz").exists() or (d / "vmlinuz").is_symlink()}
    if not kernels:
        return ()
    return tuple(d.name for d in dirs if d.name not in kernels)


def module_owners(rootfs: Path, kernel_versions: Sequence[str]) -> tuple[str, ...]:
    """The packages (``category/package-version``) that installed files under
    those /lib/modules directories, read from the vdb's CONTENTS. I/O."""
    if not kernel_versions:
        return ()
    prefixes = tuple(
        f"{base}/modules/{kv}/" for kv in kernel_versions for base in ("/lib", "/usr/lib")
    )
    owners: set[str] = set()
    vdb = rootfs / _VDB
    for contents in sorted(vdb.glob("*/*/CONTENTS")):
        for line in contents.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0] in ("obj", "sym") and parts[1].startswith(prefixes):
                owners.add(f"{contents.parent.parent.name}/{contents.parent.name}")
                break
    return tuple(sorted(owners))


def module_rebuild(container: Container, *, phase: str) -> dict[str, object]:
    """Rebuild, from source, every package with modules for a kernel that is not
    installed; drop the directories left empty of packages. PRIVILEGED.

    Built WITH buildpkg: the new binpkg instance is the one later builds and the
    assembler pick. Fails when a stale directory still has an owner afterwards.
    """
    rootfs = container.rootfs
    stale = stale_module_dirs(rootfs)
    if not stale:
        return {"stale": []}
    owners = module_owners(rootfs, stale)
    if owners:
        argv = ["emerge", "--oneshot", "--verbose", "--usepkg=n", *(f"={o}" for o in owners)]
        _run_emerge(container, argv, phase=phase)
    left = module_owners(rootfs, stale)
    if left:
        raise FactoryError(
            f"modules for a kernel that is not installed ({', '.join(stale)}) are still "
            f"owned by {', '.join(left)} after the rebuild",
            phase=phase,
        )
    # what remains is depmod's output for a kernel that is gone: no package owns it
    container.run(["rm", "-rf", *(f"/{_MODULES}/{kv}" for kv in stale)], check=True)
    return {"stale": list(stale), "rebuilt": list(owners)}


# --- perl-rebuild -----------------------------------------------------------------

_PERL5 = Path("usr") / "lib64" / "perl5"
_PERL_VERSION = re.compile(r"5\.\d+")


def installed_perl(rootfs: Path) -> str | None:
    """The installed perl's ``major.minor`` (``5.44``), from the vdb; ``None`` without one. I/O."""
    for entry in sorted((rootfs / _VDB / "dev-lang").glob("perl-[0-9]*")):
        match = re.fullmatch(r"perl-(5\.\d+)\..*", entry.name)
        if match:
            return match.group(1)
    return None


def stale_perl_dirs(rootfs: Path) -> tuple[str, ...]:
    """The ``/usr/lib64/perl5`` version directories of a perl that is not installed. I/O.

    Perl keeps modules per ``major.minor`` (``vendor_perl/5.42``): after an
    upgrade, a module nothing rebuilt sits where the new perl never looks. The
    base's ``--emptytree @world`` rebuilds the world; the stage3's build-only
    leftovers stay -- Locale-gettext, which help2man loads, broke the toolbox's
    grub that way (2026-10-01). ``()`` without a perl: nothing to compare against.
    """
    current = installed_perl(rootfs)
    if current is None:
        return ()
    stale: list[str] = []
    for base in (_PERL5, _PERL5 / "vendor_perl"):
        root = rootfs / base
        if root.is_dir():
            stale.extend(
                f"/{base / d.name}"
                for d in sorted(root.iterdir())
                if d.is_dir() and _PERL_VERSION.fullmatch(d.name) and d.name != current
            )
    return tuple(stale)


def perl_owners(rootfs: Path, dirs: Sequence[str]) -> tuple[str, ...]:
    """The packages that installed files under those perl directories (vdb CONTENTS). I/O."""
    if not dirs:
        return ()
    prefixes = tuple(f"{d}/" for d in dirs)
    owners: set[str] = set()
    for contents in sorted((rootfs / _VDB).glob("*/*/CONTENTS")):
        for line in contents.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0] in ("obj", "sym") and parts[1].startswith(prefixes):
                owners.add(f"{contents.parent.parent.name}/{contents.parent.name}")
                break
    return tuple(sorted(owners))


def perl_rebuild(container: Container, *, phase: str) -> dict[str, object]:
    """Rebuild, from source, every package with modules for a perl that is not
    installed; drop the directories they leave. PRIVILEGED.

    Runs BEFORE the stage's emerge: a stage that builds something whose build
    loads such a module (grub → help2man → Locale::gettext) would fail before
    any later step could repair it. Built WITH buildpkg, like module-rebuild.
    """
    rootfs = container.rootfs
    stale = stale_perl_dirs(rootfs)
    if not stale:
        return {"stale": []}
    owners = perl_owners(rootfs, stale)
    if owners:
        argv = ["emerge", "--oneshot", "--verbose", "--usepkg=n", *(f"={o}" for o in owners)]
        _run_emerge(container, argv, phase=phase)
    left = perl_owners(rootfs, stale)
    if left:
        raise FactoryError(
            f"modules for a perl that is not installed ({', '.join(stale)}) are still "
            f"owned by {', '.join(left)} after the rebuild",
            phase=phase,
        )
    # what remains (packlists, empty directories) belongs to no package
    container.run(["rm", "-rf", *stale], check=True)
    return {"stale": list(stale), "rebuilt": list(owners)}


def run_phase(
    container: Container, recipe: ResolvedRecipe, phase: Phase, *, emptytree: bool
) -> PhaseResult:
    """Run one phase (one ordered ``emerge``) inside the container (R3.1/R3.4/R4.1).

    PRIVILEGED (``emerge`` runs inside the nspawn). Writes the phase's transient
    break-pass ``package.use`` (:func:`write_use_break`), runs
    ``emerge --verbose`` with the target(s) of :func:`phase_emerge_argv`
    (``--emptytree`` only in the base, ``-uDN`` in the following stages) and returns a
    :class:`PhaseResult` with the atoms of :func:`parse_built_atoms`
    (``snapshot=None`` — the fork point is materialized by :func:`run_phases`).
    An ``emerge`` with a non-zero exit (``CalledProcessError``) is wrapped in a
    :class:`FactoryError` carrying the phase name and the captured output (R8.3).
    """
    if not phase_target(phase, recipe):
        # A phase without a target is a NO-OP, in the same spirit as settle_pass with empty
        # breaks: no `emerge` is run. Today only a phase that is not a stage
        # (openrc's `seat`) without atoms would land here; a stage always has @world.
        return PhaseResult(phase=phase, built_atoms=(), snapshot=None, output="")
    flow = stages_flow()
    before, _after = flow.split()
    built: tuple[str, ...] = ()
    output = ""
    run = audit.current()
    # the steps up to the emerge, in the order variants/flow.yaml declares them
    for kind in before:
        with run.step(kind) as step:
            if kind == "apply-config" and phase.layers:
                # The configuration in force for THIS stage (D24). Layers only grow
                # along the chain, so re-applying is additive.
                apply_portage(
                    container.rootfs,
                    recipe,
                    variants_dir=config.variants_dir(),
                    layers=phase.layers,
                )
                step.add(layers=list(phase.layers))
            elif kind == "write-cuts":
                write_use_break(container.rootfs, phase)
                step.add(cuts=list(use_break_lines(phase)))
            elif kind == "perl-rebuild":
                step.add(**perl_rebuild(container, phase=phase.name))
            elif kind == "emerge-stage":
                argv = phase_emerge_argv(phase, recipe, emptytree=emptytree, flow=flow)
                since = int(time.time())
                built, output = _run_emerge(container, argv, phase=phase.name)
                reused = parse_reused_atoms(output)
                step.add(argv=argv, built=len(built), reused=len(reused))
                attach_packages(
                    container.rootfs,
                    phase.stage or phase.name,
                    since=since,
                    built=built,
                    reused=reused,
                )
    return PhaseResult(
        phase=phase,
        built_atoms=built,
        snapshot=None,
        output=output,
        reused_atoms=parse_reused_atoms(output),
    )


def attach_packages(
    rootfs: Path,
    label: str,
    *,
    since: int,
    built: Sequence[str] = (),
    reused: Sequence[str] = (),
) -> None:
    """``packages-<label>.json``: the rootfs's packages after this emerge, with the
    ones it merged marked built or binpkg and timed from emerge.log (audit)."""
    run = audit.current()
    if run.root is None:
        return
    try:
        text = (rootfs / "var" / "log" / "emerge.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    run.attach(
        f"packages-{label}",
        audit.harvest_packages(
            rootfs, merges=audit.parse_emerge_log(text, since=since), built=built, reused=reused
        ),
    )


def is_installed(rootfs: Path, cp: str) -> bool:
    """Whether ``category/package`` has an entry in the rootfs's vdb. Pure I/O.

    Matches ``<name>-<digit>`` so that ``python`` is not taken for
    ``python-exec``.
    """
    category, name = cp.split("/", 1)
    vdb = rootfs / "var" / "db" / "pkg" / category
    if not vdb.is_dir():
        return False
    prefix = f"{name}-"
    return any(
        d.name.startswith(prefix) and d.name[len(prefix) :][:1].isdigit() for d in vdb.iterdir()
    )


def settle_pass(
    container: Container, recipe: ResolvedRecipe, breaks: tuple[UseBreak, ...], *, stage: str = ""
) -> PhaseResult:
    """Settle-pass: re-emerge the broken atoms with the final USE (R4.2/R4.3/R4.4).

    PRIVILEGED. When ``breaks`` is empty it is a **no-op**: returns a
    :class:`PhaseResult` of phase ``settle`` with no atoms and **without** calling
    ``container.run`` (no ``emerge``; R4.4). Otherwise it removes the break-pass's
    transient ``package.use`` (:func:`clear_use_break`) and re-emerges
    the breaks' distinct atoms (sorted) with ``--newuse --oneshot`` to
    rebuild them with the definitive USE. An ``emerge`` failure (non-zero) is wrapped in
    :class:`FactoryError` (``phase="settle"``).
    """
    settle = Phase(name="settle", stage=stage)
    if not breaks:
        return PhaseResult(phase=settle, built_atoms=(), snapshot=None)
    clear_use_break(container.rootfs)
    # Only what is INSTALLED has a cut to undo. A cut declared by the base can
    # name a package the shipped image does not contain -- pipewire is cut in the
    # base, but minimal has no audio server (D24) -- and `--oneshot` on it would
    # install it (first real run, 2026-09-27).
    atoms = sorted({b.atom for b in breaks if is_installed(container.rootfs, b.atom)})
    if not atoms:
        return PhaseResult(phase=settle, built_atoms=(), snapshot=None)
    built, output = _run_emerge(
        container, ["emerge", *stages_flow().settle.options, *atoms], phase="settle"
    )
    return PhaseResult(phase=settle, built_atoms=built, snapshot=None, output=output)


def _audited_settle(
    container: Container, recipe: ResolvedRecipe, breaks: tuple[UseBreak, ...], stage: str
) -> PhaseResult:
    """:func:`settle_pass` as an audited step, with the atoms it rebuilt."""
    with audit.current().step("settle", cuts=len(breaks)) as step:
        result = settle_pass(container, recipe, breaks, stage=stage)
        step.add(atoms=list(result.built_atoms))
    return result


def _audited_snapshot(rootfs: Path, dest: Path) -> None:
    """:func:`snapshot_fork_point` as an audited step, with the tarball's size.

    Size only: hashing a 25 GB fork point would add minutes to every stage.
    """
    with audit.current().step("fork-point", path=str(dest)) as step:
        snapshot_fork_point(rootfs, dest)
        step.add(size_bytes=dest.stat().st_size if dest.is_file() else None)


def run_phases(
    container: Container,
    recipe: ResolvedRecipe,
    *,
    emptytree: bool,
    resume_at: str | None = None,
    snapshot: str,
    pins: str,
    fork_points_dir: Path,
    stop_after: str | None = None,
) -> tuple[PhaseResult, ...]:
    """Orchestrate the chain of stages in order (R3.x/R4.x/R5.x, D24).

    PRIVILEGED. When ``resume_at`` is given (a restored fork point), the
    phases up to and including it are skipped and the cuts still in force there
    (:func:`pending_breaks`) stay pending. For each remaining phase:

    1. :func:`run_phase` -- applies the stage's layers, the cuts and the emerge;
    2. if the stage SHIPS (``ships``), :func:`settle_pass` undoes the cuts
       accumulated since the last settle -- the image is settled here, and what
       comes after starts from it settled;
    3. if the phase belongs to a stage, writes its fork point
       (:func:`stage_fork_point_path`), after the settle.

    Returns the :class:`PhaseResult` in order, each settle right after its stage.

    ``stop_after`` names a STAGE: the run ends right after that stage's fork
    point (and its settle, when it ships). The next run without it resumes from
    that fork point -- e.g. build the desktop stage alone before a flavor.
    """
    pending = pending_breaks(recipe, through=resume_at)
    _before, after = stages_flow().split()
    skipping = resume_at is not None
    results: list[PhaseResult] = []
    for phase in recipe.phases:
        if skipping:
            if phase.name == resume_at:
                skipping = False
            continue
        with audit.current().step(
            f"stage:{phase.stage or phase.name}", ships=phase.ships, sets=list(phase.sets)
        ):
            results.append(run_phase(container, recipe, phase, emptytree=emptytree))
            pending += phase.use_break
            # the steps after the emerge, in the order variants/flow.yaml declares them
            for kind in after:
                if kind == "module-rebuild":
                    with audit.current().step("module-rebuild") as step:
                        step.add(**module_rebuild(container, phase=phase.name))
                elif kind == "settle" and phase.ships:
                    results.append(_audited_settle(container, recipe, pending, phase.stage))
                    pending = ()
                elif kind == "snapshot" and phase.stage:
                    _audited_snapshot(
                        container.rootfs,
                        stage_fork_point_path(
                            recipe,
                            phase.stage,
                            snapshot=snapshot,
                            pins=pins,
                            fork_points_dir=fork_points_dir,
                        ),
                    )
                elif kind == "check-binpkgs" and phase.ships:
                    with audit.current().step("check-binpkgs"):
                        check_binpkgs(container, recipe, phase.stage)
        if stop_after is not None and phase.stage == stop_after:
            break
    return tuple(results)


# --- 5.1/5.2 (story 004) interactive stepwise orchestration ------------------


CheckpointHook = Callable[[str, PhaseDiff], CheckpointDecision]
FailureHook = Callable[[str, Exception], FailureDecision]


@dataclasses.dataclass
class _RunState:
    """Mutable state accumulated across the phases of the stepwise build (R4.1/R6.1).

    Holds the current progress — ``completed`` (names of phases already closed),
    ``phase_diffs`` (per-phase diff), ``accumulated_breaks`` (accumulated cycle
    breaks) and ``prior_atoms`` (every atom built so far, the basis of
    :func:`compute_phase_diff`'s ``prior_atoms``) — and knows how to persist itself via
    :meth:`persist` (module-qualified ``state.save_state``; ``OSError`` propagates).
    Isolating the state in an object avoids capturing loop variables in a
    persistence closure on the ABORT path.
    """

    recipe: ResolvedRecipe
    state_path: Path
    snapshot: str
    completed: tuple[str, ...]
    #: The pin id the build runs under; persisted so that a resume under the
    #: same pins is not stale (story 016, R6.12).
    pins: str = ""
    phase_diffs: tuple[PhaseDiff, ...] = ()
    accumulated_breaks: tuple[UseBreak, ...] = ()
    prior_atoms: tuple[str, ...] = ()

    def record(self, phase: Phase, diff: PhaseDiff, built_atoms: tuple[str, ...]) -> None:
        """Incorporate a finished phase: name, diff, breaks and atoms built."""
        self.completed += (phase.name,)
        self.phase_diffs += (diff,)
        self.accumulated_breaks += phase.use_break
        self.prior_atoms += built_atoms

    def persist(self) -> None:
        """Persist the current :class:`~shidashi.state.BuildState` (R6.1).

        An ``OSError`` propagates."""
        state.save_state(
            self.state_path,
            state.BuildState(
                arch=self.recipe.arch,
                flavor=self.recipe.flavor,
                init=self.recipe.init,
                snapshot=self.snapshot,
                pins=self.pins,
                recipe_hash=state.recipe_hash(self.recipe),
                seed_done=True,
                # phases only ever run after the toolchain bootstrap
                bootstrap_done=True,
                completed_phases=self.completed,
                accumulated_breaks=self.accumulated_breaks,
                phase_diffs=self.phase_diffs,
            ),
        )


def _run_phase_retrying(
    container: Container,
    recipe: ResolvedRecipe,
    phase: Phase,
    *,
    emptytree: bool,
    on_failure: FailureHook | None,
    on_abort: Callable[[], None],
) -> PhaseResult:
    """Run a phase via :func:`run_phase` in a retry loop driven by ``on_failure``.

    On a failure (``FactoryError`` — which :func:`run_phase` raises wrapping the
    emerge's ``CalledProcessError``): without ``on_failure`` it re-raises (the
    non-interactive ``--until`` path; the state of the earlier phases is already persisted and
    the rootfs is kept → exit 1, R3.5). With ``on_failure``, it asks
    ``on_failure(phase.name, err)``: ``RETRY`` re-runs the SAME phase (same argv —
    a new loop); ``ABORT`` calls ``on_abort`` (persist the state of the earlier
    phases) and raises the :class:`FactoryError`. NEVER skips a failed phase (R3.4).
    """
    while True:
        try:
            return run_phase(container, recipe, phase, emptytree=emptytree)
        except (FactoryError, subprocess.CalledProcessError) as err:
            if on_failure is None:
                raise
            if on_failure(phase.name, err) is FailureDecision.RETRY:
                continue
            on_abort()
            if isinstance(err, FactoryError):
                raise
            raise FactoryError(f"build aborted in phase {phase.name!r}", phase=phase.name) from err


def run_phases_stepwise(
    container: Container,
    recipe: ResolvedRecipe,
    *,
    emptytree: bool,
    completed: tuple[str, ...],
    until: str | None,
    snapshot: str,
    pins: str,
    fork_points_dir: Path,
    state_path: Path,
    on_checkpoint: CheckpointHook | None = None,
    on_failure: FailureHook | None = None,
) -> tuple[PhaseResult, ...]:
    """Orchestrate the build's phases step by step, with checkpoints and retry
    (R1.x/R2.x/R3.x/R5.x).

    PRIVILEGED. Iterates the plan of :func:`plan_phase_run` (resuming from
    ``completed``, stopping after ``until`` inclusive). Per phase:

    * runs it via :func:`run_phase` in a retry loop (:func:`_run_phase_retrying`):
      without ``on_failure`` a failure propagates with the earlier state persisted and the
      rootfs kept (R3.5); with ``on_failure``, ``RETRY`` re-runs the same phase and
      ``ABORT`` persists and raises (R3.1–R3.3); never skips (R3.4);
    * composes the diff via :func:`compute_phase_diff` from the phase's captured
      output (without re-running emerge), with ``prior_atoms`` = every atom of the
      earlier phases (R4.1/R4.2);
    * captures the per-phase fork point at :func:`phase_snapshot_path` via
      :func:`snapshot_fork_point` (R5.1/R5.2);
    * accumulates ``completed``/``phase_diffs``/breaks and persists the
      :class:`~shidashi.state.BuildState` via ``state.save_state`` (module-qualified
      so it can be monkeypatched; ``OSError`` propagates — a build that cannot
      record progress fails loudly);
    * asks ``on_checkpoint(phase.name, diff)`` (``None`` ⇒ auto-CONTINUE) and
      honors the :class:`CheckpointDecision`: ``CONTINUE`` goes on; ``STOP`` breaks the
      loop before the next phase (R1.3/R2.3); ``SHELL`` opens ``container.shell()`` and
      presents the SAME checkpoint again.

    Each SHIPPED stage (``ships``) is settled right after its phase
    (D24): :func:`settle_pass` undoes the cuts accumulated since the last
    settle, and that phase's snapshot and checkpoint already see the settled image.
    A STOP breaks before the next phase, never in the middle of an image. On
    resume, the cuts still pending come from :func:`pending_breaks`. Returns the
    :class:`PhaseResult` that ran, each settle right after its stage.
    """
    plan = plan_phase_run(recipe, completed=completed, until=until)
    _before, after = stages_flow().split()
    last_done = next((p.name for p in reversed(recipe.phases) if p.name in completed), None)

    run = _RunState(
        recipe=recipe,
        state_path=state_path,
        snapshot=snapshot,
        completed=completed,
        pins=pins,
        accumulated_breaks=pending_breaks(recipe, through=last_done),
    )
    results: list[PhaseResult] = []

    for phase in plan:
        result = _run_phase_retrying(
            container,
            recipe,
            phase,
            emptytree=emptytree,
            on_failure=on_failure,
            on_abort=run.persist,
        )
        results.append(result)

        entries, blockers = parse_emerge_plan(result.output)
        diff = compute_phase_diff(phase.name, entries, blockers, prior_atoms=run.prior_atoms)
        run.record(phase, diff, result.built_atoms)

        # the steps after the emerge, in the order variants/flow.yaml declares them
        for kind in after:
            if kind == "module-rebuild":
                with audit.current().step("module-rebuild") as step:
                    step.add(**module_rebuild(container, phase=phase.name))
            elif kind == "settle" and phase.ships:
                results.append(
                    _audited_settle(container, recipe, run.accumulated_breaks, phase.stage)
                )
                run.accumulated_breaks = ()
            elif kind == "snapshot":
                _audited_snapshot(
                    container.rootfs,
                    phase_snapshot_path(
                        recipe,
                        snapshot=snapshot,
                        pins=pins,
                        phase=phase.name,
                        fork_points_dir=fork_points_dir,
                    ),
                )
            elif kind == "check-binpkgs" and phase.ships:
                with audit.current().step("check-binpkgs"):
                    check_binpkgs(container, recipe, phase.stage)
        run.persist()

        if _checkpoint_decision(on_checkpoint, container, phase.name, diff) is (
            CheckpointDecision.STOP
        ):
            break

    return tuple(results)


def _checkpoint_decision(
    on_checkpoint: CheckpointHook | None,
    container: Container,
    phase_name: str,
    diff: PhaseDiff,
) -> CheckpointDecision:
    """Resolve the post-phase checkpoint decision, honoring ``SHELL`` (R2.2/R2.3).

    Without ``on_checkpoint`` ⇒ auto-``CONTINUE``. Otherwise it asks the hook; on a
    ``SHELL`` decision it opens ``container.shell()`` and presents the SAME checkpoint again
    (calls the hook again), repeating until a terminal ``CONTINUE``/``STOP`` decision.
    """
    if on_checkpoint is None:
        return CheckpointDecision.CONTINUE
    while True:
        decision = on_checkpoint(phase_name, diff)
        if decision is not CheckpointDecision.SHELL:
            return decision
        container.shell()
