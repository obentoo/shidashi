"""Weekly update of a shipped image within its generation (D26).

A generation is built once from the verified stage3. Between generations an
image is UPDATED: its own fork point is restored and brought to the week's
pinned ::gentoo tree with ``emerge -uDN --changed-deps @world``, reusing every
binpkg of the generation (``--usepkg``) and building only what changed.

One thing an update never does is end its generation. The plan is resolved
first and judged by the generation's own ABI rules (story 016, D8): a gcc
release within its major, a glibc upgrade and any binutils change pass; a new
gcc major or a glibc downgrade is refused -- the answer is a new generation.
Source builds and binpkg installs of the toolchain are judged alike.
"""

import subprocess

from shidashi.bootstrap import natural_key
from shidashi.container import Container
from shidashi.generation import GenerationFingerprint, abi_differences
from shidashi.phases import FactoryError, PhaseResult, _category_pn, _run_emerge
from shidashi.recipe import Phase, ResolvedRecipe
from shidashi.resolve import _atom_from_ebuild_line
from shidashi.state import EmergePlanEntry

#: Packages whose version belongs to the generation (its fingerprint).
TOOLCHAIN = ("sys-devel/gcc", "sys-devel/binutils", "sys-libs/glibc")

#: The fingerprint field each toolchain package fills.
_FIELD = {"sys-devel/gcc": "gcc", "sys-devel/binutils": "binutils", "sys-libs/glibc": "glibc"}

#: Plan operations that keep the installed version: a rebuild is fine.
_SAME_VERSION = ("R", "r", "rR")

#: A plan line installs from source or from a binpkg (``--usepkg``).
_PLAN_PREFIXES = ("[ebuild", "[binary")


class ToolchainChangeError(FactoryError):
    """The update would change the toolchain: that ends a generation (D26)."""


def update_argv(recipe: ResolvedRecipe, *, pretend: bool = False) -> list[str]:
    """``emerge -uDN --changed-deps`` over @world and the image's sets. Pure."""
    return [
        "emerge",
        "--verbose",
        "--usepkg",
        "--update",
        "--deep",
        "--newuse",
        "--changed-deps",
        *(["--pretend"] if pretend else []),
        "@world",
        *(f"@{name}" for name in recipe.sets),
    ]


def toolchain_plan(output: str) -> tuple[EmergePlanEntry, ...]:
    """The toolchain entries of a plan, ``[ebuild`` and ``[binary`` lines alike. Pure.

    :func:`shidashi.phases.parse_emerge_plan` reads source builds only; an
    update resolves with ``--usepkg``, so a toolchain binpkg must be seen too.
    A lookalike (``gcc-config``) or a cross gcc is not the toolchain.
    """
    entries: list[EmergePlanEntry] = []
    for line in output.splitlines():
        stripped = line.strip()
        for prefix in _PLAN_PREFIXES:
            atom = _atom_from_ebuild_line(stripped, prefix)
            if atom is None:
                continue
            op_column = stripped[len(prefix) :].split("]", 1)[0].split()
            if op_column and _category_pn(atom) in TOOLCHAIN:
                entries.append(EmergePlanEntry(atom=atom, op=op_column[0]))
            break
    return tuple(entries)


def toolchain_changes(
    plan: tuple[EmergePlanEntry, ...], current: GenerationFingerprint
) -> dict[str, tuple[str, str]]:
    """The fields the plan would end the generation on, installed and planned. Pure.

    A same-version rebuild is skipped. A package planned more than once (a new
    gcc slot beside a patch in the old one) counts by its newest planned
    version, as :func:`shidashi.generation.installed_version` counts the
    newest installed slot.
    """
    planned: dict[str, str] = {}
    for entry in plan:
        cp = _category_pn(entry.atom)
        if cp not in _FIELD or entry.op in _SAME_VERSION:
            continue
        field, version = _FIELD[cp], entry.atom[len(cp) + 1 :]
        if field not in planned or natural_key(version) > natural_key(planned[field]):
            planned[field] = version
    return abi_differences(current, current.model_copy(update=planned))


def run_update(
    container: Container, recipe: ResolvedRecipe, *, current: GenerationFingerprint
) -> PhaseResult:
    """Resolve, refuse a plan that ends the generation, then update and clean up. PRIVILEGED.

    ``current`` is the image's fingerprint before the update. The pretend runs
    with ``check=False`` so that its output reaches the error: a plan that
    does not resolve is reported with the resolver's own message.
    """
    argv = update_argv(recipe, pretend=True)
    try:
        pretend = container.run(argv, check=False)
    except subprocess.CalledProcessError as exc:  # a runner that ignores check=False
        raise FactoryError(
            "update plan failed", phase="update", output=(exc.output or "") + (exc.stderr or "")
        ) from exc
    plan_output = pretend.stdout + pretend.stderr
    if pretend.exit_code != 0:
        raise FactoryError("the update does not resolve", phase="update", output=plan_output)
    changed = toolchain_changes(toolchain_plan(plan_output), current)
    if changed:
        detail = "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in changed.items())
        raise ToolchainChangeError(
            f"the update would change the toolchain's ABI ({detail}); that ends a generation (D26)",
            phase="update",
            output=plan_output,
        )
    built, output = _run_emerge(container, update_argv(recipe), phase="update")
    _, cleanup = _run_emerge(
        container, ["emerge", "--verbose", "--usepkg", "@preserved-rebuild"], phase="update"
    )
    stage = recipe.stages[-1] if recipe.stages else recipe.flavor
    return PhaseResult(
        phase=Phase(name="update", stage=stage),
        built_atoms=built,
        snapshot=None,
        output=output + cleanup,
    )
