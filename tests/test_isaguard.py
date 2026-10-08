"""The CPU guard (shidashi/isaguard.py): can this worker's CPU run this arch? (R4)"""

import shutil
from pathlib import Path

import pytest

from shidashi import config, isaguard
from shidashi.recipe import load_arch
from tests._fake_worker import ZEN3_CPUINFO_FLAGS

ZEN3 = ZEN3_CPUINFO_FLAGS
#: znver5's flags that Zen 3 lacks: AVX-512 and AVX-VNNI. Same names in both spellings.
ZNVER5_BEYOND_ZEN3 = {
    "avx512_bf16",
    "avx512_bitalg",
    "avx512_vbmi2",
    "avx512_vnni",
    "avx512_vp2intersect",
    "avx512_vpopcntdq",
    "avx512bw",
    "avx512cd",
    "avx512dq",
    "avx512f",
    "avx512ifma",
    "avx512vbmi",
    "avx512vl",
    "avx_vnni",
}
ZEN5_ISH = (*ZEN3, *sorted(ZNVER5_BEYOND_ZEN3))


# --- hostile: a longer or similar name never satisfies a flag (wrong collapse) ------


def test_a_flag_is_not_satisfied_by_a_longer_name_that_contains_it() -> None:
    no_avx = tuple(f for f in ZEN3 if f != "avx")  # still has avx2
    assert "avx" in isaguard.missing("v3", no_avx)
    no_sse = tuple(f for f in ZEN3 if f != "sse")  # still has sse2, sse4_1, sse4a
    assert "sse" in isaguard.missing("v3", no_sse)
    no_vbmi2 = tuple(f for f in ZEN5_ISH if f != "avx512_vbmi2")  # still has avx512vbmi
    assert set(isaguard.missing("znver5", no_vbmi2)) == {"avx512_vbmi2"}


def test_an_unknown_flag_is_refused_even_when_the_worker_reports_that_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _future_arch(tmp_path, monkeypatch, "avx10_2")
    with pytest.raises(isaguard.UnknownFlag, match="avx10_2"):
        isaguard.missing("future", (*ZEN5_ISH, "avx10_2"))


# --- hostile: a different spelling of the same flag still matches (wrong split) ------


def test_cpuinfo_spellings_match_their_cpu_flags_x86_names() -> None:
    """pclmul=pclmulqdq, sha=sha_ni, fma3=fma, sse3=pni: v3 runs on Zen 3."""
    assert isaguard.missing("v3", ZEN3) == ()


# --- R4.1 ----------------------------------------------------------------------------


def test_znver5_on_zen3_names_exactly_the_avx512_and_avx_vnni_flags() -> None:
    assert set(isaguard.missing("znver5", ZEN3)) == ZNVER5_BEYOND_ZEN3


def test_arrowlake_on_zen3_lacks_only_avx_vnni() -> None:
    assert set(isaguard.missing("arrowlake", ZEN3)) == {"avx_vnni"}


def test_every_flag_of_every_real_arch_has_a_cpuinfo_name() -> None:
    for arch in config.available_names("arch"):
        flags = isaguard.required_flags(arch)
        assert set(isaguard.missing(arch, ())) == set(flags)


def test_required_flags_come_from_the_arch_fragment() -> None:
    expected = load_arch(config.recipe_path("arch", "v3")).cpu_flags_x86
    assert set(isaguard.required_flags("v3")) == set(expected)


# --- R4.3 ----------------------------------------------------------------------------


def _future_arch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    root = tmp_path / "variants"
    shutil.copytree(config.variants_dir(), root)
    arch = root / "arch" / "future"
    shutil.copytree(root / "arch" / "v3", arch)
    recipe = arch / "recipe.yaml"
    recipe.write_text(recipe.read_text().replace("arch: v3", "arch: future"))
    make_conf = arch / "portage" / "make.conf"
    lines = [
        f'CPU_FLAGS_X86="aes avx avx2 {flag}"' if line.startswith("CPU_FLAGS_X86=") else line
        for line in make_conf.read_text().splitlines()
    ]
    make_conf.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))


def test_an_untranslatable_flag_is_refused_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _future_arch(tmp_path, monkeypatch, "avx10_2")
    with pytest.raises(isaguard.UnknownFlag) as err:
        isaguard.missing("future", ZEN3)
    assert "avx10_2" in str(err.value)


# --- job_target (R4.2) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "target"),
    [
        (
            ["assemble", "--jobs", "16", "znver5", "minimal", "systemd"],
            ("znver5", "systemd"),
        ),  # hostile
        (["build", "--images", "kde", "v3", "systemd"], ("v3", "systemd")),  # hostile
        (["recipe", "show", "v3", "minimal", "systemd"], None),  # hostile: not a build
        (["assemble", "v3", "minimal", "systemd"], ("v3", "systemd")),
        (["factory", "znver5", "kde", "systemd"], ("znver5", "systemd")),
        (["build", "v3", "systemd", "--images", "kde"], ("v3", "systemd")),
        (["pretend", "arrowlake", "minimal", "openrc"], ("arrowlake", "openrc")),
        (["assemble", "v3", "minimal"], ("v3", "systemd")),  # no init given: systemd
        (
            ["assemble", "--boot-test", "v3", "kde", "systemd"],
            ("v3", "systemd"),
        ),  # hostile: a flag takes no value
        (["build", "--fresh", "--jobs", "4", "znver5", "openrc"], ("znver5", "openrc")),  # hostile
        (["doctor"], None),
        (["world", "kde"], None),
    ],
)
def test_job_target_is_the_arch_and_init_of_a_cpu_running_command(
    args: list[str], target: tuple[str, str] | None
) -> None:
    assert isaguard.job_target(args) == target


# --- what a CPU runs (WorkerStatus.runnable_arches / max_target, contract C4) ---------


def test_a_zen3_runs_exactly_v3() -> None:
    assert isaguard.runnable(ZEN3) == ("v3",)
    assert isaguard.max_target(ZEN3) == "v3"


def test_runnable_follows_the_arch_order_and_max_target_is_its_first() -> None:
    every = set().union(*(isaguard.required_flags(a) for a in isaguard.ARCH_ORDER))
    cpuinfo = tuple(isaguard.CPUINFO_NAME[f] for f in every)
    assert isaguard.runnable(cpuinfo) == isaguard.ARCH_ORDER
    assert isaguard.max_target(cpuinfo) == isaguard.ARCH_ORDER[0]
    assert set(isaguard.ARCH_ORDER) == set(config.available_names("arch"))


def test_a_cpu_missing_one_v3_flag_runs_nothing() -> None:
    lacking = tuple(f for f in ZEN3 if f != "avx2")
    assert isaguard.runnable(lacking) == ()
    assert isaguard.max_target(lacking) is None


def test_local_cpu_flags_reads_this_machines_first_flags_line(tmp_path: Path) -> None:
    """Story 014's host node: the host's own flags, from its /proc/cpuinfo."""
    from shidashi import isaguard

    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(
        "processor\t: 0\nflags\t\t: fpu sse2 avx2 pni\n\nprocessor\t: 1\nflags\t\t: fpu sse2\n"
    )
    assert isaguard.local_cpu_flags(cpuinfo) == ("fpu", "sse2", "avx2", "pni")
    (tmp_path / "empty").write_text("processor\t: 0\n")
    assert isaguard.local_cpu_flags(tmp_path / "empty") == ()
