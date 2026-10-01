"""Weekly update of a shipped image within its generation (D26).

A generation is built once from the verified stage3. Between generations an
image is UPDATED: its own fork point is restored and brought to the week's
pinned ::gentoo tree with ``emerge -uDN --changed-deps @world``, reusing every
binpkg of the generation (``--usepkg``) and building only what changed.

One thing an update never does is change the toolchain. gcc, binutils or glibc
moving under a built system is exactly the contamination a generation exists to
prevent (D26): the plan is resolved first, and an update that would upgrade any
of them is refused -- the answer is a new generation (a new stage3 pin).
"""

import subprocess

from shidashi.container import Container
from shidashi.phases import FactoryError, PhaseResult, _category_pn, _run_emerge, parse_emerge_plan
from shidashi.recipe import Phase, ResolvedRecipe
from shidashi.state import EmergePlanEntry

#: Packages whose version belongs to the generation (its fingerprint).
TOOLCHAIN = ("sys-devel/gcc", "sys-devel/binutils", "sys-libs/glibc")

#: Plan operations that keep the installed version: a rebuild is fine.
_SAME_VERSION = ("R", "r", "rR")


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


def toolchain_changes(plan: tuple[EmergePlanEntry, ...]) -> tuple[str, ...]:
    """The planned atoms that would change a toolchain package's version. Pure."""
    return tuple(
        entry.atom
        for entry in plan
        if _category_pn(entry.atom) in TOOLCHAIN and entry.op not in _SAME_VERSION
    )


def run_update(container: Container, recipe: ResolvedRecipe) -> PhaseResult:
    """Resolve, refuse a toolchain change, then update and clean up. PRIVILEGED.

    The pretend runs with ``check=False`` so that its output reaches the error:
    a plan that does not resolve is reported with the resolver's own message.
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
    changed = toolchain_changes(parse_emerge_plan(plan_output)[0])
    if changed:
        raise ToolchainChangeError(
            f"the update would change the toolchain ({' '.join(changed)}); a toolchain "
            "change ends a generation -- re-pin the stage3 and build a new one (D26)",
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
