"""Tests for the ISA gap check (shidashi/isacheck.py)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from shidashi.isacheck import (
    ISA_GAPS,
    IsaCheckError,
    check_paths,
    disassemble,
    gap_pattern,
    scan_disassembly,
)

_ARROWLAKE_ON_ZEN5 = ISA_GAPS[("arrowlake", "znver5")]


# --- gap_pattern -------------------------------------------------------------


def test_no_gap_against_itself() -> None:
    assert gap_pattern("znver5", "znver5") is None


def test_no_gap_when_pair_unknown() -> None:
    assert gap_pattern("v3", "znver5") is None


def test_gap_is_known_for_arrowlake_on_zen5() -> None:
    assert gap_pattern("arrowlake", "znver5") is not None


# --- scan_disassembly --------------------------------------------------------


def test_flags_vex_encoded_ifma() -> None:
    line = "    1089:\t{vex} vpmadd52luq %ymm2,%ymm1,%ymm0"
    assert scan_disassembly(line, _ARROWLAKE_ON_ZEN5) == [line]


def test_does_not_flag_evex_ifma_which_zen5_has() -> None:
    """AVX512-IFMA shares the mnemonic but Zen 5 implements it.

    Matching the bare mnemonic would condemn every AVX-512 binary; only the
    VEX-encoded form, which objdump prefixes with "{vex}", is a real gap.
    """
    line = "    10a1:\tvpmadd52luq %zmm2,%zmm1,%zmm0{%k1}"
    assert scan_disassembly(line, _ARROWLAKE_ON_ZEN5) == []


@pytest.mark.parametrize(
    "insn",
    [
        "vpdpbssd %ymm2,%ymm1,%ymm0",  # AVX-VNNI-INT8
        "vpdpwsud %ymm2,%ymm1,%ymm0",  # AVX-VNNI-INT16
        "vbcstnebf162ps (%rax),%ymm0",  # AVX-NE-CONVERT
        "vcvtneebf162ps (%rax),%ymm0",  # AVX-NE-CONVERT
        "cmpbexadd %r12,%r13,(%rax)",  # CMPccXADD
        "encodekey128 %eax,%eax",  # Key Locker
        "aesenc128kl (%rax),%xmm0",  # Key Locker
        "serialize",  # SERIALIZE
        "tpause %eax",  # WAITPKG
    ],
)
def test_flags_each_arrowlake_only_family(insn: str) -> None:
    assert scan_disassembly(f"  4011:\t{insn}", _ARROWLAKE_ON_ZEN5)


@pytest.mark.parametrize(
    "insn",
    [
        "vpaddd %ymm2,%ymm1,%ymm0",  # plain AVX2, both CPUs have it
        "vaesenc %ymm2,%ymm1,%ymm0",  # VAES, Zen 5 has it
        "sha256rnds2 %xmm1,%xmm0",  # SHA-NI, Zen 5 has it
        "mov %rax,%rbx",
    ],
)
def test_does_not_flag_instructions_zen5_can_run(insn: str) -> None:
    assert scan_disassembly(f"  4011:\t{insn}", _ARROWLAKE_ON_ZEN5) == []


# --- check_paths -------------------------------------------------------------


def test_check_paths_returns_empty_without_a_known_gap(tmp_path: Path) -> None:
    """No gap means nothing is scanned -- and no objdump call is made."""
    assert check_paths([tmp_path / "nonexistent"], "znver5", "znver5") == []


# --- integration: needs a real toolchain -------------------------------------

_HAS_TOOLCHAIN = shutil.which("gcc") is not None and shutil.which("objdump") is not None


@pytest.mark.skipif(not _HAS_TOOLCHAIN, reason="needs gcc and objdump")
def test_detects_avx_ifma_in_a_real_binary(tmp_path: Path) -> None:
    src = tmp_path / "ifma.c"
    src.write_text(
        "#include <immintrin.h>\n"
        "int main(void){\n"
        "  __m256i a=_mm256_set1_epi64x(3),b=_mm256_set1_epi64x(5),"
        "c=_mm256_setzero_si256();\n"
        "  __m256i r=_mm256_madd52lo_epu64(c,a,b);\n"
        "  return _mm256_extract_epi64(r,0)!=15;\n}\n",
        encoding="utf-8",
    )
    binary = tmp_path / "ifma"
    build = subprocess.run(
        ["gcc", "-march=arrowlake", "-O2", str(src), "-o", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f"gcc cannot target arrowlake here: {build.stderr.strip()[:120]}")

    findings = check_paths([binary], "arrowlake", "znver5")
    assert findings, "AVX-IFMA should have been flagged"
    assert "vpmadd52" in findings[0].line


@pytest.mark.skipif(not _HAS_TOOLCHAIN, reason="needs gcc and objdump")
def test_plain_arrowlake_binary_is_clean(tmp_path: Path) -> None:
    """-march=arrowlake alone does not make a binary unrunnable.

    Most packages never touch the exclusive extensions, which is exactly why a
    static scan is worth doing instead of assuming the worst.
    """
    src = tmp_path / "plain.c"
    src.write_text("int main(void){return 0;}\n", encoding="utf-8")
    binary = tmp_path / "plain"
    build = subprocess.run(
        ["gcc", "-march=arrowlake", "-O2", str(src), "-o", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip("gcc cannot target arrowlake here")
    assert check_paths([binary], "arrowlake", "znver5") == []


@pytest.mark.skipif(shutil.which("objdump") is None, reason="needs objdump")
def test_disassemble_raises_on_a_non_binary(tmp_path: Path) -> None:
    junk = tmp_path / "not-elf.txt"
    junk.write_text("hello\n", encoding="utf-8")
    with pytest.raises(IsaCheckError):
        disassemble(junk)


# --- host_arch / iter_binaries / check_rootfs --------------------------------


@pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")
def test_host_arch_returns_a_march_name() -> None:
    from shidashi.isacheck import host_arch

    arch = host_arch()
    assert arch is None or (isinstance(arch, str) and arch and " " not in arch)


def test_iter_binaries_finds_elf_and_skips_the_rest(tmp_path: Path) -> None:
    from shidashi.isacheck import iter_binaries

    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "etc").mkdir()
    elf = tmp_path / "usr" / "bin" / "real"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 32)
    (tmp_path / "usr" / "bin" / "script.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "etc" / "conf").write_bytes(b"\x7fELF")  # outside the scanned dirs

    assert iter_binaries(tmp_path) == [elf]


def test_check_rootfs_is_quiet_without_a_known_gap(tmp_path: Path) -> None:
    """znver5 on znver5 has no gap, so nothing is disassembled at all."""
    from shidashi.isacheck import check_rootfs

    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "usr" / "bin" / "x").write_bytes(b"\x7fELF" + b"\x00" * 32)
    assert check_rootfs(tmp_path, "znver5", host="znver5") == []


def test_check_rootfs_is_quiet_when_host_is_unknown(tmp_path: Path) -> None:
    from shidashi.isacheck import check_rootfs

    assert check_rootfs(tmp_path, "arrowlake", host="some-unknown-cpu") == []


@pytest.mark.skipif(not _HAS_TOOLCHAIN, reason="needs gcc and objdump")
def test_check_rootfs_finds_an_offending_binary(tmp_path: Path) -> None:
    """End to end: a rootfs-shaped tree with one AVX-IFMA binary in usr/bin."""
    from shidashi.isacheck import check_rootfs

    binroot = tmp_path / "usr" / "bin"
    binroot.mkdir(parents=True)
    src = tmp_path / "ifma.c"
    src.write_text(
        "#include <immintrin.h>\n"
        "int main(void){\n"
        "  __m256i a=_mm256_set1_epi64x(3),b=_mm256_set1_epi64x(5),"
        "c=_mm256_setzero_si256();\n"
        "  return _mm256_extract_epi64(_mm256_madd52lo_epu64(c,a,b),0)!=15;\n}\n",
        encoding="utf-8",
    )
    build = subprocess.run(
        ["gcc", "-march=arrowlake", "-O2", str(src), "-o", str(binroot / "ifma")],
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip("gcc cannot target arrowlake here")

    findings = check_rootfs(tmp_path, "arrowlake", host="znver5")
    assert [f.path.name for f in findings] == ["ifma"]
