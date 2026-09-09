"""ISA gap check — can this target's binaries run on this build host? (R9.x)

A target may legitimately emit instructions the build host cannot execute. That
is not a configuration mistake: `arrowlake` and `znver5` are different CPUs and
neither ISA contains the other. It matters because the build host runs test
suites, code generators and `pkg_config` helpers out of packages it has just
compiled -- and an unexecutable instruction there is a SIGILL, not a slowdown.

The check is STATIC, by design. Emulation proves that the code paths a test
happens to exercise work; disassembling proves the instruction is absent from
the binary altogether, including paths no test would reach. It is also cheaper:
one objdump per binary, no emulator to install.

Measured on this host (Zen 5) against -march=arrowlake: gcc emits AVX-IFMA,
AVX-VNNI-INT8, AVX-NE-CONVERT, CMPccXADD and Key Locker, none of which Zen 5
implements. QEMU 11.1.1 does not emulate them either (TCG raises SIGILL even
with -cpu max), which is why this module exists rather than a qemu wrapper.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Instructions an Arrow Lake target can emit that a Zen 5 host cannot execute.
#:
#: The IFMA entry is the subtle one. Zen 5 HAS AVX512-IFMA, whose mnemonics are
#: identical (`vpmadd52luq`/`vpmadd52huq`) but EVEX-encoded; Arrow Lake has the
#: VEX-encoded AVX-IFMA, which Zen 5 lacks. objdump prints "{vex}" before the
#: VEX form, so matching on the bare mnemonic would flag every AVX-512 binary.
_ARROWLAKE_BEYOND_ZEN5 = (
    r"\{vex\}[ \t]+vpmadd52"  # AVX-IFMA (VEX form only)
    r"|vpdpb(ss|su|us|uu)d|vpdpw(sud|usd|uud)"  # AVX-VNNI-INT8 / INT16
    r"|vbcstne(bf162ps|sh2ps)"  # AVX-NE-CONVERT
    r"|vcvtne[eo](bf16|ph)2ps"  # AVX-NE-CONVERT
    r"|cmp[a-z]+xadd"  # CMPccXADD
    r"|encodekey(128|256)|aes(enc|dec)(wide)?(128|256)kl"  # Key Locker
    r"|\bserialize\b|\bumonitor\b|\bumwait\b|\btpause\b"  # SERIALIZE / WAITPKG
)

#: (target arch, host arch) -> pattern of instructions the host cannot run.
#: A pair that is absent means "no known gap"; a target always runs on itself.
ISA_GAPS: dict[tuple[str, str], str] = {
    ("arrowlake", "znver5"): _ARROWLAKE_BEYOND_ZEN5,
    ("arrowlake", "v3"): _ARROWLAKE_BEYOND_ZEN5,
}


@dataclass(frozen=True)
class Finding:
    """One offending instruction, with enough context to act on it."""

    path: Path
    line: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.path}: {self.line.strip()}"


class IsaCheckError(Exception):
    """objdump is missing, or could not read a binary it was pointed at."""


def scan_disassembly(text: str, pattern: str) -> list[str]:
    """Return the disassembly lines that match `pattern`.

    Pure and therefore testable without objdump or any binary on disk.
    """
    rx = re.compile(pattern, re.IGNORECASE)
    return [line for line in text.splitlines() if rx.search(line)]


def gap_pattern(target: str, host: str) -> str | None:
    """The instruction pattern `host` cannot execute from `target`, if any."""
    if target == host:
        return None
    return ISA_GAPS.get((target, host))


def disassemble(path: Path) -> str:
    """Disassemble one ELF file. Raises IsaCheckError if objdump is unusable."""
    objdump = shutil.which("objdump")
    if objdump is None:
        raise IsaCheckError("objdump not found; install sys-devel/binutils")
    proc = subprocess.run(
        [objdump, "-d", "--no-show-raw-insn", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise IsaCheckError(f"objdump failed on {path}: {proc.stderr.strip()}")
    return proc.stdout


#: Directories inside a rootfs worth scanning. Everything executable lives in
#: one of these; scanning the whole tree would cost an objdump per file for
#: thousands of non-ELF files with nothing to gain.
_BINARY_DIRS = ("bin", "sbin", "usr/bin", "usr/sbin", "usr/lib", "usr/lib64", "usr/libexec")


def host_arch() -> str | None:
    """The arch name gcc picks for this machine, e.g. "znver5". None if unknown.

    Uses `-march=native`, which is the one place that knows what this CPU is
    without parsing /proc/cpuinfo by hand.
    """
    gcc = shutil.which("gcc")
    if gcc is None:
        return None
    proc = subprocess.run(
        [gcc, "-march=native", "-Q", "--help=target"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "-march=":
            return parts[1]
    return None


def iter_binaries(rootfs: Path) -> list[Path]:
    """ELF files under the usual executable directories of `rootfs`."""
    found: list[Path] = []
    for rel in _BINARY_DIRS:
        base = rootfs / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    with path.open("rb") as fh:
                        if fh.read(4) == b"\x7fELF":
                            found.append(path)
                except OSError:
                    continue
    return found


def check_rootfs(rootfs: Path, target: str, host: str | None = None) -> list[Finding]:
    """Scan a built rootfs for instructions this build host cannot execute.

    `host` defaults to whatever :func:`host_arch` reports. Returns an empty list
    when the host is unknown or when the pair has no declared gap -- the check
    is an early warning, never a reason to fail a build that already succeeded.
    """
    resolved_host = host if host is not None else host_arch()
    if resolved_host is None or gap_pattern(target, resolved_host) is None:
        return []
    return check_paths(iter_binaries(rootfs), target, resolved_host)


def check_paths(paths: list[Path], target: str, host: str) -> list[Finding]:
    """Findings across `paths`. Empty list means every binary is host-runnable.

    An empty list is also what you get when there is no known gap between the
    two arches -- callers should treat "no gap" and "gap, nothing found" the
    same way, because both mean the binaries are safe to execute here.
    """
    pattern = gap_pattern(target, host)
    if pattern is None:
        return []
    findings: list[Finding] = []
    for path in paths:
        for line in scan_disassembly(disassemble(path), pattern):
            findings.append(Finding(path=path, line=line))
    return findings
