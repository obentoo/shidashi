"""Per-microarchitecture stage3 generation via Catalyst (story 005).

Separates the **pure** logic (generating the text of the stage1/2/3 specs from the
resolved recipe — :func:`render_specs`) from the **privileged** execution (invoking
``catalyst`` by shelling out, computing the tarball's SHA-512 — Task 4), in the same
idiom as ``seed.py``. The pure part is unit-tested on non-Gentoo CI; the invocation
of ``catalyst`` is isolated in a monkeypatchable helper.

The microarchitecture tuning (``-march`` etc.) does NOT come in through Catalyst's
``subarch`` (there is no ``znver5`` subarch upstream) but through ``portage_confdir`` —
which reuses the already existing ``variants/arch/<arch>/portage``. The ``subarch`` stays at
the generic ``amd64`` baseline.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

from shidashi.recipe import ResolvedRecipe

# baseline subarch (the specific march comes via portage_confdir, not through the subarch)
_SUBARCH = "amd64"
# compression of the produced tarball — matches the generic seed's `.tar.xz`
_COMPRESSION = "xz"
# stable key order in the spec text (determinism, R2.6)
_KEY_ORDER = (
    "subarch",
    "target",
    "rel_type",
    "profile",
    "version_stamp",
    "snapshot_treeish",
    "source_subpath",
    "portage_confdir",
    "compression_mode",
)


class CatalystError(Exception):
    """Failure to generate specs or to run Catalyst (story 005).

    Raised for a missing required input in :func:`render_specs` and, in
    Task 4, for an unavailable ``catalyst``, a non-zero exit or a SHA-512
    mismatch of the produced tarball.
    """


def _built_subpath(rel_type: str, stage_n: int, version_stamp: str) -> str:
    """Subpath of the built stage under Catalyst's storedir.

    Catalyst convention: ``<rel_type>/stage<N>-<subarch>-<version_stamp>``.
    Used to chain ``source_subpath`` (stage2 starts from stage1, etc.).
    """
    return f"{rel_type}/stage{stage_n}-{_SUBARCH}-{version_stamp}"


def _render_one(fields: dict[str, str]) -> str:
    """Format a spec as ``key: value`` lines in a stable order."""
    return "".join(f"{key}: {fields[key]}\n" for key in _KEY_ORDER)


def render_specs(
    recipe: ResolvedRecipe,
    *,
    seed_subpath: str,
    version_stamp: str,
    snapshot_treeish: str,
    confdir: Path,
) -> dict[str, str]:
    """Generate the stage1/2/3 specs from the resolved recipe (R2.1–R2.6). Pure.

    Maps ``recipe`` + stamps → ``{"stage1": <text>, "stage2": ..., "stage3":
    ...}``. Every variable input is a parameter — no clock and no
    randomness — so the same inputs produce byte-identical text
    (R2.6). ``source_subpath`` is chained: stage1 starts from ``seed_subpath`` (the
    generic bootstrap seed, R2.2), stage2 from the built stage1 and stage3 from
    stage2 (R2.3). ``portage_confdir`` reuses the arch's portage directory (R2.4) and
    ``rel_type`` derives from the arch, with ``subarch`` at the ``amd64`` baseline (R2.5).

    Raises :class:`CatalystError` if a required input is empty.
    """
    for name, value in (
        ("seed_subpath", seed_subpath),
        ("version_stamp", version_stamp),
        ("snapshot_treeish", snapshot_treeish),
    ):
        if not value:
            raise CatalystError(
                f"required input {name!r} is empty while generating the catalyst specs"
            )

    rel_type = f"shidashi/{recipe.arch}"
    common = {
        "subarch": _SUBARCH,
        "rel_type": rel_type,
        "profile": recipe.profile,
        "version_stamp": version_stamp,
        "snapshot_treeish": snapshot_treeish,
        "portage_confdir": str(confdir),
        "compression_mode": _COMPRESSION,
    }
    sources = {
        "stage1": seed_subpath,
        "stage2": _built_subpath(rel_type, 1, version_stamp),
        "stage3": _built_subpath(rel_type, 2, version_stamp),
    }
    return {
        target: _render_one({**common, "target": target, "source_subpath": source})
        for target, source in sources.items()
    }


# --- privileged invocation + orchestration (Task 4) --------------------------

# tarball suffixes recognized when deriving the bootstrap seed's subpath
_ARCHIVE_SUFFIXES = (".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst", ".tar")


def _seed_subpath(seed: Path) -> str:
    """Subpath of the bootstrap seed for stage1's ``source_subpath``.

    Derived from the generic tarball's name without the archive suffix (Catalyst
    references stages without an extension under the storedir).
    """
    name = seed.name
    for suffix in _ARCHIVE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _stage3_tarball(output_dir: Path, version_stamp: str) -> Path:
    """Path of the stage3 that Catalyst must produce (naming convention)."""
    return output_dir / f"stage3-{_SUBARCH}-{version_stamp}.tar.xz"


def _sha512_file(path: Path) -> str:
    """Hex SHA-512 of ``path``, read in chunks (identical to seed.verify_digest)."""
    h = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_catalyst(spec: Path) -> None:
    """Run ``catalyst -f <spec>`` on the host (privileged). Isolated for monkeypatching.

    Never ignores the return code: a non-zero exit raises :class:`CatalystError`
    naming the spec (and therefore the stage) that failed, mirroring
    ``seed.verify_signature``.
    """
    try:
        result = subprocess.run(
            ["catalyst", "-f", str(spec)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:
        raise CatalystError(f"failed to run catalyst -f {spec.name}: {err}") from err
    if result.returncode != 0:
        raise CatalystError(f"catalyst failed on {spec.name}:\n{result.stderr.strip()}")


def build_stage3_catalyst(
    recipe: ResolvedRecipe,
    generic_seed: Path,
    *,
    version_stamp: str,
    snapshot_treeish: str,
    confdir: Path,
    scratch_dir: Path,
    output_dir: Path,
    expected_sha512: str = "",
) -> tuple[Path, str]:
    """Build the per-microarchitecture stage3 via Catalyst (R3.1–R3.5, R4.1, R4.3).

    A *thin* orchestrator over :func:`render_specs` and :func:`_run_catalyst`:

    1. Fails early (before any build) if ``catalyst`` is not on the
       ``PATH`` — :class:`CatalystError` naming ``dev-util/catalyst`` (R3.3).
    2. Generates the specs and writes them to ``scratch_dir``; invokes ``catalyst`` once
       per spec in the order stage1→stage2→stage3 (R3.2). A non-zero exit
       propagates and aborts without moving on to the following stages (R3.4).
    3. Locates the produced stage3 in ``output_dir`` (clear error if missing) and
       computes its SHA-512 (R4.1/R3.5).
    4. If ``expected_sha512`` is given and does not match, raises
       :class:`CatalystError` instead of returning diverging bytes (R4.3).

    Returns ``(tarball, sha512)``. The privileged placement of ``generic_seed``
    in Catalyst's storedir (the bootstrap ``--seed``) and the real ``catalyst`` are
    host-gated; here ``generic_seed`` defines stage1's ``source_subpath``.
    """
    if shutil.which("catalyst") is None:
        raise CatalystError(
            "catalyst unavailable on the host; install dev-util/catalyst for seed_source=catalyst"
        )

    specs = render_specs(
        recipe,
        seed_subpath=_seed_subpath(generic_seed),
        version_stamp=version_stamp,
        snapshot_treeish=snapshot_treeish,
        confdir=confdir,
    )
    scratch_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    for target in ("stage1", "stage2", "stage3"):  # seed chain order (R3.2)
        spec_path = scratch_dir / f"{target}.spec"
        spec_path.write_text(specs[target], encoding="utf-8")
        _run_catalyst(spec_path)  # propagates CatalystError, aborting (R3.4)

    tarball = _stage3_tarball(output_dir, version_stamp)
    if not tarball.is_file():
        raise CatalystError(f"catalyst did not produce the expected stage3: {tarball}")

    sha512 = _sha512_file(tarball)
    if expected_sha512 and sha512 != expected_sha512:
        raise CatalystError(
            f"sha512 mismatch for the built stage3 {tarball.name}: "
            f"expected {expected_sha512}, got {sha512}"
        )
    return tarball, sha512
