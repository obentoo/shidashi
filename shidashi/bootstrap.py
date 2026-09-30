"""Toolchain bootstrap of a fresh stage3 -- the executor of flow.yaml's ``bootstrap``.

A stage3 ships the toolchain it was built with. Before the base stage rebuilds
everything with ``--emptytree``, the bootstrap brings that toolchain to the
pinned tree's versions, in dependency order, switching to each new one BY NAME.
The steps -- locale, headers + binutils, gcc, libtool, glibc, preserved-rebuild,
the generation check, ccache, and the empty-world check -- and their order are in
``variants/flow.yaml`` (BOOTSTRAP-PROCESS.md §1); this module executes the step
kinds :mod:`shidashi.flow` defines.

The container is duck-typed (``.rootfs`` + ``.run``) so each step is testable
with a fake; the real one is :class:`shidashi.container.Container`.
"""

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import pydantic

from shidashi.container import CommandResult
from shidashi.flow import (
    AssertWorldEmptyStep,
    BootstrapFlow,
    CheckGenerationStep,
    EmergeStep,
    LocaleStep,
    SelectToolchainStep,
    load_flow,
)
from shidashi.phases import FactoryError

_WORLD = Path("var/lib/portage/world")
_LOCALE_GEN = Path("etc/locale.gen")
_ENV_D = Path("etc/env.d")


class BootstrapError(FactoryError):
    """A bootstrap step failed; ``phase`` names the step (``bootstrap:<step>``)."""


class _Runner(Protocol):
    rootfs: Path

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult: ...


class BootstrapResult(pydantic.BaseModel):
    """What the bootstrap left in place, for the report and the fingerprint."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")
    steps: tuple[str, ...]
    binutils: str
    gcc: str
    locales_before: int
    locales_after: int
    output: str


def natural_key(name: str) -> tuple[tuple[int, int | str], ...]:
    """``sort -V``-like key: digit runs compare as numbers (``-9`` < ``-10``)."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", name)
        if part
    )


def newest_profile(env_dir: Path) -> str:
    """The newest toolchain profile in ``/etc/env.d/{binutils,gcc}`` (F8, F9). Pure.

    One file per installed slot, plus ``config-<chost>`` which records the
    active one and is skipped. Reading the directory avoids ``*-config -l``,
    whose indices shift between runs and whose output carries ANSI colour even
    when redirected.
    """
    names = [
        p.name for p in env_dir.iterdir() if p.is_file() and not p.name.startswith("config-")
    ]
    if not names:
        raise BootstrapError(f"no toolchain profile in {env_dir}", phase="bootstrap")
    return max(names, key=natural_key)


def emerge_argv(*targets: str, env: Mapping[str, str]) -> list[str]:
    """``env K=V ... emerge --oneshot <targets>`` as an argv. Pure.

    The environment goes in the argv because systemd-nspawn does not pass the
    host's environment into the container.
    """
    return ["env", *(f"{k}={v}" for k, v in env.items()), "emerge", "--oneshot", *targets]


def world_entries(rootfs: Path) -> tuple[str, ...]:
    """The atoms in the rootfs's world file (``()`` when it is absent). Pure I/O."""
    path = rootfs / _WORLD
    if not path.is_file():
        return ()
    return tuple(line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip())


def active_locales(rootfs: Path) -> tuple[str, ...]:
    """The uncommented entries of the rootfs's ``locale.gen`` (first field). Pure I/O."""
    path = rootfs / _LOCALE_GEN
    if not path.is_file():
        return ()
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if fields:
            entries.append(fields[0])
    return tuple(entries)


class _Log:
    """Runs the steps' commands and keeps one transcript for the result/error."""

    def __init__(self, container: _Runner) -> None:
        self.container = container
        self.parts: list[str] = []

    def run(self, step: str, argv: list[str]) -> str:
        self.parts.append(f"### [{step}] {' '.join(argv)}\n")
        try:
            result = self.container.run(argv, check=True)
        except subprocess.CalledProcessError as exc:
            output = (exc.output or "") + (exc.stderr or "")
            self.parts.append(output)
            raise BootstrapError(
                f"bootstrap step {step!r} failed: {' '.join(argv)}",
                phase=f"bootstrap:{step}",
                output="".join(self.parts),
            ) from exc
        out = result.stdout + result.stderr
        self.parts.append(out)
        return out

    def fail(self, step: str, message: str) -> BootstrapError:
        self.parts.append(f"!!! {message}\n")
        return BootstrapError(message, phase=f"bootstrap:{step}", output="".join(self.parts))


def _count_locales(log: _Log, step: str) -> int:
    return len([line for line in log.run(step, ["locale", "-a"]).splitlines() if line.strip()])


def run_bootstrap(
    container: _Runner,
    flow: BootstrapFlow | None = None,
    *,
    on_generation: Callable[[], object] | None = None,
) -> BootstrapResult:
    """Run the bootstrap steps of ``variants/flow.yaml`` over a fresh stage3. PRIVILEGED.

    ``flow`` defaults to the repository's (:func:`shidashi.flow.load_flow`). The
    layered ``make.conf`` and the layers' ``rootfs/`` must already be in place.
    ``on_generation`` runs at the ``check-generation`` step -- the factory's
    fingerprint check (D26); a flow with that step and no callback fails there.
    Raises :class:`BootstrapError` naming the step (``bootstrap:<name>``) on the
    first failure, with the transcript so far.
    """
    if flow is None:
        from shidashi import config

        flow = load_flow(config.variants_dir()).bootstrap
    rootfs = container.rootfs
    log = _Log(container)
    selected: dict[str, str] = {}
    before = after = 0
    names: list[str] = []

    for step in flow.steps:
        if step.name not in names:
            names.append(step.name)
        if isinstance(step, LocaleStep):
            # checked before anything runs: eselect by a name that locale-gen
            # never generated fails far from the cause
            if step.locale not in active_locales(rootfs):
                raise log.fail(step.name, f"{step.locale} is not active in /{_LOCALE_GEN}")
            log.run(step.name, ["locale-gen"])
            log.run(step.name, ["eselect", "locale", "set", step.locale])
            log.run(step.name, ["env-update"])
        elif isinstance(step, EmergeStep):
            if step.keep_locales:
                before = _count_locales(log, step.name)
            log.run(step.name, emerge_argv(*step.atoms, env=flow.step_env(step)))
            if step.keep_locales:
                after = _count_locales(log, step.name)
                if after < before:
                    raise log.fail(
                        step.name,
                        f"{' '.join(step.atoms)} lost locales: locale -a went {before} -> {after}",
                    )
        elif isinstance(step, SelectToolchainStep):
            profile = newest_profile(rootfs / _ENV_D / step.tool)
            log.run(step.name, [f"{step.tool}-config", profile])
            log.run(step.name, ["env-update"])
            selected[step.tool] = profile
        elif isinstance(step, CheckGenerationStep):
            if on_generation is None:
                raise log.fail(step.name, "check-generation has no fingerprint check to run")
            log.parts.append(f"### [{step.name}] generation fingerprint\n")
            on_generation()
        elif isinstance(step, AssertWorldEmptyStep):
            leftover = world_entries(rootfs)
            if leftover:
                raise log.fail(
                    step.name,
                    f"the bootstrap left {len(leftover)} world entries: {' '.join(leftover)}",
                )

    return BootstrapResult(
        steps=tuple(names),
        binutils=selected.get("binutils", ""),
        gcc=selected.get("gcc", ""),
        locales_before=before,
        locales_after=after,
        output="".join(log.parts),
    )
