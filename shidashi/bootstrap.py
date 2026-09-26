"""Toolchain bootstrap of a fresh stage3 -- BOOTSTRAP-PROCESS.md §1, steps 0-5 + P.

A stage3 ships the toolchain it was built with. Before the base stage rebuilds
everything with ``--emptytree``, the lab brings that toolchain to the tree's
current versions, in dependency order, and switches to each new one BY NAME:

====  ==========================================================================
0     ``locale-gen`` from the curated ``/etc/locale.gen``; select the locale
1     ``linux-headers`` + ``binutils``; ``binutils-config <newest>``
2     ``gcc`` (it lands in a NEW slot, F8); ``gcc-config <newest>``
3     ``dev-build/libtool`` (the category moved from ``sys-devel``)
4     ``glibc``; ``locale -a`` must not shrink (it is built -compile-locales)
5     ``@preserved-rebuild``
P     ``dev-util/ccache`` -- Portage refuses ``FEATURES=ccache`` without it
====  ==========================================================================

Every emerge is ``--oneshot`` (the world file must end EMPTY: whatever enters it
would be rebuilt by the base and shipped) and runs with
``FEATURES="-buildpkg -ccache"`` (D22): the base ``make.conf`` turns both ON for
the world build, and only the environment can turn them off without touching
the ``package.env/toolchain`` exclusion (F65). The environment goes in the argv
(``env FEATURES=… emerge …``) because ``systemd-nspawn`` does not pass the
host's environment into the container.

The container is duck-typed (``.rootfs`` + ``.run``) so each step is testable
with a fake; the real one is :class:`shidashi.container.Container`.
"""

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

import pydantic

from shidashi.container import CommandResult
from shidashi.phases import FactoryError

#: Turned off for every bootstrap emerge (D22, F65).
BOOTSTRAP_FEATURES = "-buildpkg -ccache"

#: The system locale, selected by NAME with the exact spelling of locale.gen
#: (``en_US.utf8`` would make eselect list a phantom second target).
LOCALE = "en_US.UTF-8"

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


def _natural_key(name: str) -> tuple[tuple[int, int | str], ...]:
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
    return max(names, key=_natural_key)


def emerge_argv(*targets: str) -> list[str]:
    """``emerge --oneshot`` with the bootstrap's FEATURES, as an argv. Pure."""
    return ["env", f"FEATURES={BOOTSTRAP_FEATURES}", "emerge", "--oneshot", *targets]


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


def run_bootstrap(container: _Runner) -> BootstrapResult:
    """Run steps 0-5 + P in ``container`` over a fresh stage3. PRIVILEGED.

    The layered ``make.conf`` and the layers' ``rootfs/`` must already be in
    place (``CFLAGS``, ``ACCEPT_KEYWORDS`` and the curated ``locale.gen`` are the
    recipe's). Raises :class:`BootstrapError` naming the step on the first
    failure, with the transcript so far.
    """
    rootfs = container.rootfs
    log = _Log(container)

    # 0 -- locale. Checked before anything runs: eselect by a name that
    # locale-gen never generated fails far from the cause.
    if LOCALE not in active_locales(rootfs):
        raise log.fail("locale", f"{LOCALE} is not active in /{_LOCALE_GEN}")
    log.run("locale", ["locale-gen"])
    log.run("locale", ["eselect", "locale", "set", LOCALE])
    log.run("locale", ["env-update"])

    # 1 -- kernel headers and binutils, then switch to the newest binutils
    log.run("binutils", emerge_argv("sys-kernel/linux-headers", "sys-devel/binutils"))
    binutils = newest_profile(rootfs / _ENV_D / "binutils")
    log.run("binutils", ["binutils-config", binutils])
    log.run("binutils", ["env-update"])

    # 2 -- gcc lands in a new slot; merging does not switch to it (F8)
    log.run("gcc", emerge_argv("sys-devel/gcc"))
    gcc = newest_profile(rootfs / _ENV_D / "gcc")
    log.run("gcc", ["gcc-config", gcc])
    log.run("gcc", ["env-update"])

    # 3 -- libtool, rebuilt against the new gcc
    log.run("libtool", emerge_argv("dev-build/libtool"))

    # 4 -- glibc; -compile-locales means an upgrade can drop locale-archive
    before = _count_locales(log, "glibc")
    log.run("glibc", emerge_argv("sys-libs/glibc"))
    after = _count_locales(log, "glibc")
    if after < before:
        raise log.fail(
            "glibc", f"glibc upgrade lost locales: locale -a went {before} -> {after}"
        )

    # 5 -- whatever still links against a replaced library
    log.run("preserved", emerge_argv("@preserved-rebuild"))

    # P -- the ccache binary, while FEATURES still says -ccache
    log.run("ccache", emerge_argv("dev-util/ccache"))

    leftover = world_entries(rootfs)
    if leftover:
        raise log.fail(
            "world", f"the bootstrap left {len(leftover)} world entries: {' '.join(leftover)}"
        )

    return BootstrapResult(
        steps=("locale", "binutils", "gcc", "libtool", "glibc", "preserved", "ccache"),
        binutils=binutils,
        gcc=gcc,
        locales_before=before,
        locales_after=after,
        output="".join(log.parts),
    )
