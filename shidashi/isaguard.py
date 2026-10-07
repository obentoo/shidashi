"""The CPU guard: can this CPU run binaries built for an arch? (story 010, R4)

An arch declares the ``CPU_FLAGS_X86`` it compiles for (its ``portage/make.conf``); a CPU
reports its own in ``/proc/cpuinfo`` -- under different names for some of them
(``sse3`` is ``pni``, ``pclmul`` is ``pclmulqdq``, ``sha`` is ``sha_ni``, ``fma3`` is
``fma``). :data:`CPUINFO_NAME` lists every flag of every arch explicitly: a flag it
does not know is refused (:class:`UnknownFlag`), never guessed, and names are compared
whole, so ``avx2`` never satisfies ``avx``.
"""

import functools
from collections.abc import Sequence
from pathlib import Path

from shidashi import config
from shidashi.recipe import load_arch

#: ``CPU_FLAGS_X86`` name → ``/proc/cpuinfo`` name, for every flag of every arch.
CPUINFO_NAME: dict[str, str] = {
    "aes": "aes",
    "avx": "avx",
    "avx2": "avx2",
    "avx512_bf16": "avx512_bf16",
    "avx512_bitalg": "avx512_bitalg",
    "avx512_vbmi2": "avx512_vbmi2",
    "avx512_vnni": "avx512_vnni",
    "avx512_vp2intersect": "avx512_vp2intersect",
    "avx512_vpopcntdq": "avx512_vpopcntdq",
    "avx512bw": "avx512bw",
    "avx512cd": "avx512cd",
    "avx512dq": "avx512dq",
    "avx512f": "avx512f",
    "avx512ifma": "avx512ifma",
    "avx512vbmi": "avx512vbmi",
    "avx512vl": "avx512vl",
    "avx_vnni": "avx_vnni",
    "bmi1": "bmi1",
    "bmi2": "bmi2",
    "f16c": "f16c",
    "fma3": "fma",
    "gfni": "gfni",
    "mmx": "mmx",
    "mmxext": "mmxext",
    "pclmul": "pclmulqdq",
    "popcnt": "popcnt",
    "rdrand": "rdrand",
    "sha": "sha_ni",
    "sse": "sse",
    "sse2": "sse2",
    "sse3": "pni",
    "sse4_1": "sse4_1",
    "sse4_2": "sse4_2",
    "sse4a": "sse4a",
    "ssse3": "ssse3",
    "vaes": "vaes",
    "vpclmulqdq": "vpclmulqdq",
}

#: The arches, most demanding first: the first one a CPU runs is its best target.
ARCH_ORDER: tuple[str, ...] = ("znver5", "arrowlake", "v3")

#: The commands that run an arch's binaries, and so are guarded.
_CPU_COMMANDS = frozenset({"factory", "assemble", "build", "pretend"})


class UnknownFlag(Exception):
    """An arch requires a flag this guard cannot translate to a ``/proc/cpuinfo`` name."""

    def __init__(self, arch: str, flag: str) -> None:
        self.arch, self.flag = arch, flag
        super().__init__(
            f"{arch}: CPU_FLAGS_X86 flag {flag!r} has no /proc/cpuinfo name in "
            "shidashi.isaguard.CPUINFO_NAME; add it there before sending this arch to a worker"
        )


def required_flags(arch: str) -> tuple[str, ...]:
    """The arch's ``CPU_FLAGS_X86``, as its fragment declares them."""
    return tuple(load_arch(config.recipe_path("arch", arch)).cpu_flags_x86)


def missing(arch: str, worker_flags: Sequence[str]) -> tuple[str, ...]:
    """The arch's flags (``CPU_FLAGS_X86`` names) that ``worker_flags`` lacks; empty
    when the CPU runs the arch. Raises :class:`UnknownFlag` for an untranslatable flag."""
    have = set(worker_flags)
    lacking: list[str] = []
    for flag in required_flags(arch):
        name = CPUINFO_NAME.get(flag)
        if name is None:
            raise UnknownFlag(arch, flag)
        if name not in have:
            lacking.append(flag)
    return tuple(lacking)


def runnable(worker_flags: Sequence[str]) -> tuple[str, ...]:
    """The arches this CPU runs, in :data:`ARCH_ORDER`."""
    return tuple(arch for arch in ARCH_ORDER if not missing(arch, worker_flags))


def max_target(worker_flags: Sequence[str]) -> str | None:
    """The most demanding arch this CPU runs, or None."""
    arches = runnable(worker_flags)
    return arches[0] if arches else None


@functools.cache
def _value_options(command: str) -> frozenset[str]:
    """The option names of a Shidashi command that take a value (from its Typer params)."""
    import typer.core
    import typer.main

    from shidashi.cli import app

    commands = getattr(typer.main.get_command(app), "commands", {})
    cmd = commands.get(command)
    names: set[str] = set()
    for param in cmd.params if cmd is not None else ():
        if isinstance(param, typer.core.TyperOption) and not (param.is_flag or param.count):
            names.update(param.opts)
            names.update(param.secondary_opts)
    return frozenset(names)


def job_target(args: Sequence[str]) -> tuple[str, str] | None:
    """``(arch, init)`` of a command that runs an arch's binaries; None for any other.

    The positionals after the command, skipping every option and, for an option that
    takes one, its value. ``init`` is the first later positional naming an init, else
    ``systemd``.
    """
    if not args or args[0] not in _CPU_COMMANDS:
        return None
    takes_value = _value_options(args[0])
    positionals: list[str] = []
    rest = iter(args[1:])
    for arg in rest:
        if arg.startswith("-") and arg != "-":
            if "=" not in arg and arg in takes_value:
                next(rest, None)
            continue
        positionals.append(arg)
    if not positionals:
        return None
    inits = set(config.available_names("init"))
    init = next((p for p in positionals[1:] if p in inits), "systemd")
    return positionals[0], init


def local_cpu_flags(cpuinfo: Path = Path("/proc/cpuinfo")) -> tuple[str, ...]:
    """This machine's CPU flags: the first ``flags`` line of ``cpuinfo``; () without one."""
    try:
        text = cpuinfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "flags":
            return tuple(value.split())
    return ()
