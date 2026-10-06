"""Unit tests of the update's toolchain rule on the ABI comparison (story 016, task 2.1, D8).

``toolchain_changes(plan, current)`` returns, like ``abi_differences``, the
fields an update plan would end the generation on; ``run_update(..., current=)``
refuses such a plan before building anything (R7.x).
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from shidashi import update
from shidashi.container import CommandResult
from shidashi.generation import GenerationFingerprint
from shidashi.recipe import ResolvedRecipe
from shidashi.state import EmergePlanEntry
from shidashi.update import ToolchainChangeError, run_update, toolchain_changes


def _fp(**over: Any) -> GenerationFingerprint:
    fields: dict[str, Any] = {
        "arch": "v3",
        "profile": "default/linux/amd64/23.0/no-multilib/systemd",
        "common_flags": "-O2",
        "chost": "x86_64-pc-linux-gnu",
        "llvm_slot": "22",
        "gcc": "16.2.0",
        "binutils": "2.46.1",
        "glibc": "2.43",
    }
    fields.update(over)
    return GenerationFingerprint(**fields)


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="kde",
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=("base", "kde"),
        phases=(),
        portage_layers=(),
        stages=("base", "minimal", "desktop", "kde"),
    )


_MESA = "[ebuild     U  ] media-libs/mesa-26.3.1::gentoo [26.3.0::gentoo] 0 KiB\n"
GCC_PATCH = _MESA + (
    "[ebuild     U  ] sys-devel/gcc-16.2.1_p20260926:16::gentoo [16.2.0:16::gentoo] 0 KiB\n"
)
GCC_MAJOR = _MESA + "[ebuild  NS    ] sys-devel/gcc-17.1.0:17::gentoo [16.2.0:16::gentoo] 0 KiB\n"
GLIBC_UP = _MESA + "[ebuild     U  ] sys-libs/glibc-2.44::gentoo [2.43::gentoo] 0 KiB\n"
GLIBC_DOWN = _MESA + "[ebuild     UD ] sys-libs/glibc-2.42::gentoo [2.43::gentoo] 0 KiB\n"
BINUTILS = _MESA + (
    "[ebuild     U  ] sys-devel/binutils-2.47:2.47::gentoo [2.46.1:2.46::gentoo] 0 KiB\n"
)
REBUILDS = _MESA + (
    "[ebuild   R    ] sys-devel/gcc-16.2.0:16::gentoo  0 KiB\n"
    "[ebuild     U  ] sys-devel/gcc-config-3.0::gentoo [2.12::gentoo] 0 KiB\n"
    "[ebuild  N     ] cross-x86_64-pc-linux-gnu/gcc-17.1.0::crossdev 0 KiB\n"
)
_GCC17 = "[ebuild  NS    ] sys-devel/gcc-17.1.0:17::gentoo [16.2.0:16::gentoo] 0 KiB\n"
_GCC16 = "[ebuild     U  ] sys-devel/gcc-16.2.1_p20260926:16::gentoo [16.2.0:16::gentoo] 0 KiB\n"
BINARY_GCC_MAJOR = _MESA + (
    "[binary   NS   ] sys-devel/gcc-17.1.0:17::gentoo [16.2.0:16::gentoo] 0 KiB\n"
)
BINARY_GCC_PATCH = _MESA + (
    "[binary     U  ] sys-devel/gcc-16.2.1_p20260926-1:16::gentoo [16.2.0:16::gentoo] 0 KiB\n"
)


class FakeContainer:
    def __init__(self, plan: str) -> None:
        self.rootfs = Path("/nonexistent")
        self.plan = plan
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], *, check: bool = True, **_k: Any) -> CommandResult:
        self.calls.append(list(argv))
        if "--pretend" in argv:
            return CommandResult(0, self.plan, "")
        return CommandResult(0, "", "")


def _changes(plan: str, **installed: str) -> dict[str, tuple[str, str]]:
    return toolchain_changes(update.toolchain_plan(plan), _fp(**installed))


# --- hostile: what must be refused -------------------------------------------------------


def test_a_gcc_major_change_refuses_before_building_anything() -> None:
    """R7.4."""
    c = FakeContainer(GCC_MAJOR)
    with pytest.raises(ToolchainChangeError) as err:
        run_update(c, _recipe(), current=_fp(gcc="16.2.0"))  # type: ignore[arg-type]
    assert "gcc" in str(err.value) and "16.2.0" in str(err.value) and "17.1.0" in str(err.value)
    assert len(c.calls) == 1  # the pretend only


def test_a_glibc_downgrade_refuses_before_building_anything() -> None:
    """R7.5."""
    c = FakeContainer(GLIBC_DOWN)
    with pytest.raises(ToolchainChangeError) as err:
        run_update(c, _recipe(), current=_fp(glibc="2.43"))  # type: ignore[arg-type]
    assert "glibc" in str(err.value) and "2.43" in str(err.value) and "2.42" in str(err.value)
    assert len(c.calls) == 1


@pytest.mark.parametrize("plan", [_MESA + _GCC17 + _GCC16, _MESA + _GCC16 + _GCC17])
def test_a_new_gcc_slot_beside_a_patch_in_the_old_one_refuses_in_either_order(plan: str) -> None:
    """Hostile third element: two gcc entries in one plan. The newest installed
    gcc after the update is 17, whichever line the plan lists last."""
    assert _changes(plan, gcc="16.2.0") == {"gcc": ("16.2.0", "17.1.0")}


def test_a_lookalike_or_cross_gcc_is_not_read_as_gcc() -> None:
    """gcc-config-3.0 must not become gcc version 'config-3.0' (R7.6)."""
    assert _changes(REBUILDS, gcc="16.2.0") == {}


# --- benign: what must run -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "installed"),
    [
        (GCC_PATCH, {"gcc": "16.2.0"}),  # R7.1
        (GLIBC_UP, {"glibc": "2.43"}),  # R7.2
        (BINUTILS, {"binutils": "2.46.1"}),  # R7.3
        (REBUILDS, {"gcc": "16.2.0"}),  # R7.6
    ],
)
def test_an_abi_neutral_plan_runs_the_update(plan: str, installed: dict[str, str]) -> None:
    c = FakeContainer(plan)
    run_update(c, _recipe(), current=_fp(**installed))  # type: ignore[arg-type]
    assert [("--pretend" in a, a[-1]) for a in c.calls] == [
        (True, "@kde"),
        (False, "@kde"),
        (False, "@preserved-rebuild"),
    ]


def test_toolchain_changes_names_installed_and_planned_versions() -> None:
    assert _changes(GCC_MAJOR, gcc="16.2.0") == {"gcc": ("16.2.0", "17.1.0")}
    assert _changes(GLIBC_DOWN, glibc="2.43") == {"glibc": ("2.43", "2.42")}
    assert _changes(GCC_PATCH, gcc="16.2.0") == {}


# --- binary lines (R7.7) and toolchain_plan ------------------------------------------------


def test_a_binary_gcc_major_change_refuses_before_building_anything() -> None:
    """R7.7: `--usepkg` may plan the toolchain as a binpkg; it is judged all the same."""
    c = FakeContainer(BINARY_GCC_MAJOR)
    with pytest.raises(ToolchainChangeError) as err:
        run_update(c, _recipe(), current=_fp(gcc="16.2.0"))  # type: ignore[arg-type]
    assert "gcc" in str(err.value) and "17.1.0" in str(err.value)
    assert len(c.calls) == 1


def test_a_binary_abi_neutral_plan_runs_the_update() -> None:
    c = FakeContainer(BINARY_GCC_PATCH)
    run_update(c, _recipe(), current=_fp(gcc="16.2.0"))  # type: ignore[arg-type]
    assert len(c.calls) == 3


def test_toolchain_plan_reads_ebuild_and_binary_lines_with_their_ops() -> None:
    output = (
        _MESA
        + "[binary   NS   ] sys-devel/gcc-17.1.0:17::gentoo [16.2.0:16::gentoo] 0 KiB\n"
        + "[ebuild     U  ] sys-libs/glibc-2.44::gentoo [2.43::gentoo] 0 KiB\n"
        + "[binary   R    ] sys-devel/binutils-2.46.1:2.46::gentoo  0 KiB\n"
    )
    assert [(e.atom, e.op) for e in update.toolchain_plan(output)] == [
        ("sys-devel/gcc-17.1.0", "NS"),
        ("sys-libs/glibc-2.44", "U"),
        ("sys-devel/binutils-2.46.1", "R"),
    ]
    assert all(isinstance(e, EmergePlanEntry) for e in update.toolchain_plan(output))


def test_toolchain_plan_keeps_only_the_toolchain() -> None:
    """Lookalikes (gcc-config, a cross gcc) and every other package are dropped."""
    output = REBUILDS + "[binary     U  ] sys-devel/gcc-config-3.0::gentoo [2.12::gentoo] 0 KiB\n"
    assert [(e.atom, e.op) for e in update.toolchain_plan(output)] == [
        ("sys-devel/gcc-16.2.0", "R")
    ]
    assert update.toolchain_plan(_MESA) == ()
    assert update.toolchain_plan("Nothing to merge; quitting.\n") == ()
