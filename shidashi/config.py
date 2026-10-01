"""Path resolution for Shidashi's ``variants/`` tree.

This module finds the ``variants/`` directory (overridable through the
``SHIDASHI_VARIANTS_DIR`` environment variable) and resolves the paths of each
axis (``arch``/``flavor``/``init``) and of the ``base`` fragment. It does not
parse YAML -- that is the job of :mod:`shidashi.recipe`, whose loaders take an
explicit ``Path``. It stays pure: no global mutable state; the environment
variable is read on every call so that tests can ``monkeypatch`` it.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shidashi.recipe import ResolvedRecipe

from shidashi.recipe import ResolvedRecipe

_ENV_VAR = "SHIDASHI_VARIANTS_DIR"
_SCRATCH_ENV = "SHIDASHI_SCRATCH"
_CACHE_ENV = "SHIDASHI_CACHE"
_SEEDS_ENV = "SHIDASHI_SEEDS_DIR"
_CATALYST_ENV = "SHIDASHI_CATALYST_DIR"
_RUNS_ENV = "SHIDASHI_RUNS"


class UnknownAxisError(Exception):
    """Unknown axis/value while resolving a ``variants/`` directory (R1.4).

    Carries the queried ``axis``, the missing ``name`` and the list of
    ``available`` names (valid names for that axis). The message embeds the
    available names so that the CLI (a later task) can show them to the user.
    """

    def __init__(self, axis: str, name: str, available: list[str]) -> None:
        self.axis = axis
        self.name = name
        self.available = available
        disponiveis = ", ".join(available) if available else "(none)"
        super().__init__(f"unknown value {name!r} for axis {axis!r}; available: {disponiveis}")


def variants_dir() -> Path:
    """Return the ``variants/`` directory (R6.1).

    If ``SHIDASHI_VARIANTS_DIR`` is set, use it; otherwise locate ``variants/``
    relative to the package: the project root is the parent directory of the
    ``shidashi`` package and ``variants/`` lives at ``<root>/variants``.
    """
    override = os.environ.get(_ENV_VAR)
    if override:
        return Path(override)
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "variants"


def available_names(axis: str) -> list[str]:
    """Sorted list of the subdirectories under ``variants_dir()/axis``.

    Each subdirectory stands for one value of the axis. Return an empty list if
    the axis directory does not exist.
    """
    axis_root = variants_dir() / axis
    if not axis_root.is_dir():
        return []
    return sorted(entry.name for entry in axis_root.iterdir() if entry.is_dir())


def axis_dir(axis: str, name: str) -> Path:
    """Return ``variants_dir()/axis/name`` (R1.3).

    Raise :class:`UnknownAxisError` (with the available names) if the resolved
    directory does not exist.
    """
    candidate = variants_dir() / axis / name
    if not candidate.is_dir():
        raise UnknownAxisError(axis, name, available_names(axis))
    return candidate


def recipe_path(axis: str, name: str) -> Path:
    """Return ``axis_dir(axis, name)/"recipe.yaml"`` (R1.3).

    First checks that the axis directory exists, via :func:`axis_dir`.
    """
    return axis_dir(axis, name) / "recipe.yaml"


def base_path() -> Path:
    """Return ``variants_dir()/"base"/"recipe.yaml"`` (R1.3)."""
    return variants_dir() / "base" / "recipe.yaml"


def stage_path(name: str) -> Path:
    """The YAML of a stage (D24): where each kind of stage lives.

    - ``base``, ``minimal``, ``desktop`` → ``variants/<name>/recipe.yaml``;
    - a flavor → ``variants/flavor/<name>/recipe.yaml``.

    Every stage is a ``recipe.yaml``, like every axis value: the place of a
    stage's ``exclude:`` is the same wherever the stage sits in the chain.

    An unknown name raises :class:`UnknownAxisError` listing the targets.
    """
    if name == "base":
        return base_path()
    core = variants_dir() / name / "recipe.yaml"
    if name in ("minimal", "desktop") and core.is_file():
        return core
    flavor = variants_dir() / "flavor" / name / "recipe.yaml"
    if flavor.is_file():
        return flavor
    raise UnknownAxisError("target", name, target_names())


def target_names() -> list[str]:
    """The images one can build: ``minimal`` and every flavor, in chain order."""
    return ["minimal", *available_names("flavor")]


def stage_names() -> list[str]:
    """Every stage of the chains, base first: the images plus ``base`` and
    ``desktop``, the stages they grow from without being images themselves."""
    from shidashi.recipe import CORE_STAGES

    return [*CORE_STAGES, *available_names("flavor")]


def load_recipe(arch: str, target: str, init: str, *, any_stage: bool = False) -> ResolvedRecipe:
    """Load the whole chain for ``target`` and merge it with ``arch`` and ``init``.

    The one entry point for "give me the recipe of this image": CLI, lab sync
    and tests all go through it, so the chain is walked in exactly one place.
    ``any_stage`` also accepts ``base`` and ``desktop``: stages, not images, so
    only read-only views (``shidashi world desktop``) ask for them.
    """
    from shidashi.recipe import load_arch, load_base, load_chain, load_init, merge

    if target not in (stage_names() if any_stage else target_names()):
        raise UnknownAxisError("target", target, target_names())
    return merge(
        load_base(base_path()),
        load_arch(recipe_path("arch", arch)),
        load_chain(target, stage_path),
        load_init(recipe_path("init", init)),
    )


def kits_dir() -> Path:
    """Return ``variants_dir()/"kits"``: the library of ALL sets (D25).

    It is neither an axis nor a layer: it has no ``portage/`` and no recipe. The
    layers (base, flavor, …) only DECLARE which sets they install; the content
    lives here, in ``kits/<category>/<set>``. The categories are for people only.
    """
    return variants_dir() / "kits"


def scratch_dir() -> Path:
    """Return the scratch directory of the *pretend* flow (R6.2).

    Honors ``SHIDASHI_SCRATCH`` (read on every call, like :func:`variants_dir`);
    when unset, uses the default ``/var/tmp/shidashi-pretend``. All ephemeral
    resolution state (the seeded rootfs) is confined here.
    """
    override = os.environ.get(_SCRATCH_ENV)
    if override:
        return Path(override)
    return Path("/var/tmp/shidashi-pretend")


def cache_dir() -> Path:
    """Return the cache directory for downloaded stage3 tarballs (R2.5).

    Honors ``SHIDASHI_CACHE`` (read on every call); default ``/var/cache/shidashi``.
    The verified tarball is kept here for reuse across runs.
    """
    override = os.environ.get(_CACHE_ENV)
    if override:
        return Path(override)
    return Path("/var/cache/shidashi")


def runs_dir() -> Path:
    """Where each run's audit trail lives (:mod:`shidashi.audit`): one directory per run.

    Honors ``SHIDASHI_RUNS`` (read on every call); default ``/var/log/shidashi/runs``.
    Kept apart from scratch and cache, which are wiped or reused: an audit trail
    outlives the builds it describes.
    """
    override = os.environ.get(_RUNS_ENV)
    if override:
        return Path(override)
    return Path("/var/log/shidashi/runs")


def seeds_dir() -> Path:
    """Return the repo's ``seeds/`` directory (the pinned pointer) (R2.1).

    Honors ``SHIDASHI_SEEDS_DIR`` (read on every call); when unset, resolves
    ``seeds/`` relative to the project root (same resolution as
    :func:`variants_dir`: the parent directory of the ``shidashi`` package).
    """
    override = os.environ.get(_SEEDS_ENV)
    if override:
        return Path(override)
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "seeds"


def build_root() -> Path:
    """Return the root of the build rootfs trees (R5.1/R8.2): ``scratch_dir()/build``.

    Each flavor/arch assembles its ephemeral rootfs under this directory.
    Inherits the ``SHIDASHI_SCRATCH`` override (read per call) from
    :func:`scratch_dir`.
    """
    return scratch_dir() / "build"


def pkgdir(arch: str, generation: str | None = None) -> Path:
    """Return the per-arch binary package ``PKGDIR`` (R6.2): ``cache_dir()/binpkgs/<arch>``.

    Partitioned by ``arch`` so that microarchitecture variants (``v3``,
    ``znver5``, …) do not share incompatible binpkgs. Inherits the
    ``SHIDASHI_CACHE`` override (read per call) from :func:`cache_dir`.

    With ``generation`` -- the pinned stage3's snapshot -- one more level:
    ``binpkgs/<arch>/<generation>``. A new stage3 pin therefore starts an empty
    PKGDIR, and nothing is reused across generations but distfiles and ccache
    (D26). The generation fingerprint guards what this layout cannot see.
    """
    base = cache_dir() / "binpkgs" / arch
    return base / generation if generation is not None else base


def catalyst_dir(arch: str) -> Path:
    """Return the per-arch Catalyst storedir/output directory (story 005).

    Partitioned by ``arch`` (like :func:`pkgdir`) so that stage3 tarballs of
    different microarchitectures do not collide. Honors the dedicated
    ``SHIDASHI_CATALYST_DIR`` override (read per call); when unset, uses
    ``cache_dir()/catalyst`` -- thus inheriting the ``SHIDASHI_CACHE`` override.
    """
    override = os.environ.get(_CATALYST_ENV)
    base = Path(override) if override else cache_dir() / "catalyst"
    return base / arch


def catalyst_spec_dir(arch: str) -> Path:
    """Return the per-arch directory of ephemeral Catalyst specs (story 005).

    The stage1/2/3 specs are regenerated on every build, so they live under
    scratch: ``scratch_dir()/catalyst/<arch>``. Inherits the ``SHIDASHI_SCRATCH``
    override (read per call) from :func:`scratch_dir`.
    """
    return scratch_dir() / "catalyst" / arch


def ccache_dir() -> Path:
    """Return the shared ``ccache`` directory (R6.2): ``cache_dir()/ccache``.

    Shared across flavors/archs (C/C++ compilation cache). Inherits the
    ``SHIDASHI_CACHE`` override (read per call).
    """
    return cache_dir() / "ccache"


def sccache_dir() -> Path:
    """Return the shared ``sccache`` directory (R6.2): ``cache_dir()/sccache``.

    Shared across flavors/archs (Rust compilation cache). Inherits the
    ``SHIDASHI_CACHE`` override (read per call).
    """
    return cache_dir() / "sccache"


def distdir() -> Path:
    """Return the shared ``DISTDIR`` (R6.2): ``cache_dir()/distfiles``.

    Shared across flavors/archs (downloaded source tarballs). Inherits the
    ``SHIDASHI_CACHE`` override (read per call).
    """
    return cache_dir() / "distfiles"


def fork_points_dir() -> Path:
    """Return the *fork points* directory (R6.2): ``cache_dir()/fork-points``.

    Holds the fork markers between build stages. Inherits the
    ``SHIDASHI_CACHE`` override (read per call).
    """
    return cache_dir() / "fork-points"


def state_dir() -> Path:
    """Return the persisted build state directory (R6.1): ``cache_dir()/state``.

    Each build's progress state lives here, under the cache, so that it survives
    the teardown of the ephemeral rootfs. Inherits the ``SHIDASHI_CACHE`` override
    (read per call).
    """
    return cache_dir() / "state"


def build_state_path(recipe: ResolvedRecipe) -> Path:
    """Return the state path of a recipe (R6.1): ``state_dir()/<key>.json``.

    The ``<arch>-<flavor>-<init>`` key mirrors the rootfs/fork-point convention,
    isolating progress per variant. Inherits the ``SHIDASHI_CACHE`` override (read
    per call) from :func:`state_dir`.
    """
    return state_dir() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}.json"


def build_log_path(recipe: ResolvedRecipe) -> Path:
    """Where a factory run streams its container output: ``scratch_dir()/logs/<key>.log``.

    Appended to across runs (each command is stamped), and kept outside the
    rootfs so that a discarded or restored rootfs does not take it along.
    """
    return scratch_dir() / "logs" / f"{recipe.arch}-{recipe.flavor}-{recipe.init}.log"
