"""Shidashi build state: progress/diff models + persistence (story 004).

Defines the *frozen* pydantic v2 models (``extra="forbid"``, ``tuple`` collections,
the ``_STRICT`` idiom from :mod:`shidashi.recipe`) that record the progress of a
phased build — emerge plan entries, the per-phase diff (atoms built, USE changes,
unexpected rebuilds, blockers) and the aggregate ``BuildState`` (R4.1/R4.4).

Persistence (R6.1/R6.2/R6.4) is pure I/O against an explicit ``Path``: the state
is serialized as JSON under the cache (it survives the rootfs teardown), written
atomically (sibling temp + ``os.replace``), read back with a clear error when
corrupted, cleared idempotently and compared against the recipe's
``snapshot``/``hash`` to detect staleness. Uses only the stdlib (``hashlib``/``os``/
``tempfile``) plus pydantic.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from shidashi.recipe import ResolvedRecipe, UseBreak

_STRICT = ConfigDict(frozen=True, extra="forbid")


class StateError(Exception):
    """Failure to load a persisted build state (R6.1).

    Raised by :func:`load_state` when the file exists but is corrupted
    (invalid JSON or not conforming to the :class:`BuildState` schema). Never
    raised for a missing file — that case returns ``None``.
    """


class EmergePlanEntry(BaseModel):
    """One entry of the emerge plan (``--pretend`` output) (R4.4).

    Frozen pydantic with ``extra="forbid"``. ``atom`` is the package/version, ``op``
    the Portage operation code (``N`` new, ``R`` rebuild, ``U`` upgrade, …) and
    ``use_changes`` the USE flags changed for that atom (empty by default).
    """

    model_config = _STRICT
    atom: str
    op: str
    use_changes: tuple[str, ...] = ()


class PhaseDiff(BaseModel):
    """Observed diff of a build phase (R4.1/R4.4).

    Frozen pydantic with ``extra="forbid"``. Records, for the phase ``phase``: the
    atoms actually ``built``, the ``unexpected_rebuilds`` (rebuilds outside the
    plan), the applied ``use_changes`` and the ``blockers`` found. All
    collections are ``tuple`` and empty by default (except ``built``).
    """

    model_config = _STRICT
    phase: str
    built: tuple[str, ...]
    unexpected_rebuilds: tuple[str, ...] = ()
    use_changes: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


class BuildState(BaseModel):
    """Aggregate, persisted state of a phased build (R4.1/R4.4/R6.1).

    Frozen pydantic with ``extra="forbid"``. Identifies the build by the
    ``arch``/``flavor``/``init`` key plus the stage3 ``snapshot`` and the
    ``recipe_hash`` of the resolved recipe (both compared in :func:`is_stale`).
    ``seed_done`` marks the extraction as finished; ``completed_phases`` the phases
    already closed; ``accumulated_breaks`` the accumulated
    :class:`~shidashi.recipe.UseBreak`; and ``phase_diffs`` the per-phase history of
    :class:`PhaseDiff`.
    """

    model_config = _STRICT
    arch: str
    flavor: str
    init: str
    snapshot: str
    recipe_hash: str
    seed_done: bool = False
    # Unused since the Catalyst seed was removed; always empty. Kept only so
    # older state files (which carry it) still load under extra="forbid".
    seed_sha512: str = ""
    #: The toolchain bootstrap ran over the seed (BOOTSTRAP-PROCESS §5). Additive
    #: and defaulted, like seed_sha512: older state files still load.
    bootstrap_done: bool = False
    completed_phases: tuple[str, ...] = ()
    accumulated_breaks: tuple[UseBreak, ...] = ()
    phase_diffs: tuple[PhaseDiff, ...] = ()


def recipe_hash(recipe: ResolvedRecipe) -> str:
    """Return the hex SHA-256 of the resolved recipe's canonical JSON (R6.2).

    Stable (same recipe → same hash) and sensitive to any change of a recipe
    field, since it derives from ``recipe.model_dump_json()`` (pydantic v2's
    deterministic serialization). Used by :func:`is_stale` to detect a recipe
    that diverges from the persisted one.
    """
    payload = recipe.model_dump_json().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def save_state(path: Path, state: BuildState) -> None:
    """Write ``state`` to ``path`` as JSON, atomically (R6.1).

    First writes to a *sibling* temporary file (same directory, so that
    ``os.replace`` is atomic on the same filesystem) and only then renames it over
    ``path``. On success it leaves **no** partial file: the temp either became the
    final file (``replace``) or was removed. I/O errors (:class:`OSError`) propagate
    for the caller to surface them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump_json()
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def load_state(path: Path) -> BuildState | None:
    """Read and return the :class:`BuildState` persisted at ``path`` (R6.1).

    Returns ``None`` when the file is missing (expected case: a new build).
    If the file exists but is corrupted — invalid JSON or not conforming to the
    schema — raises :class:`StateError` with a clear message (never silently
    returns ``None`` for a malformed file).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return BuildState.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError) as err:
        raise StateError(f"corrupted build state at {path}: {err}") from err


def clear_state(path: Path) -> None:
    """Remove the state persisted at ``path``, idempotently (R6.4).

    Ignores a missing file (``missing_ok=True``): calling it repeatedly — or
    on a build without state — never raises. Supports the reset/clear of the
    interactive flow.
    """
    path.unlink(missing_ok=True)


def is_stale(state: BuildState, *, snapshot: str, recipe_hash: str) -> bool:
    """Tell whether ``state`` is stale against the current ``snapshot``/``recipe_hash`` (R6.2).

    Stale (``True``) when the stage3 ``snapshot`` changed **or** the recipe's
    ``recipe_hash`` changed relative to the persisted one — in both cases the
    saved progress can no longer be reused. The ``recipe_hash`` parameter is
    keyword-only and deliberately shadows the name of the :func:`recipe_hash` function.
    """
    return state.snapshot != snapshot or state.recipe_hash != recipe_hash
