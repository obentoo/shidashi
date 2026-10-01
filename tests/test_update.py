"""Unit tests of shidashi.update -- the weekly update within a generation (D26)."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from shidashi.container import CommandResult
from shidashi.phases import FactoryError, parse_emerge_plan
from shidashi.recipe import ResolvedRecipe
from shidashi.update import ToolchainChangeError, run_update, toolchain_changes, update_argv


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


_PLAN_OK = """\
[ebuild     U  ] media-libs/mesa-26.3.1::gentoo [26.3.0::gentoo] USE="vulkan" 0 KiB
[ebuild   R    ] sys-devel/gcc-15.3.0::gentoo  USE="-debug" 0 KiB
[ebuild   R    ] sys-devel/gcc-config-2.13::gentoo  0 KiB
"""

_PLAN_GCC = """\
[ebuild     U  ] media-libs/mesa-26.3.1::gentoo [26.3.0::gentoo] 0 KiB
[ebuild  NS    ] sys-devel/gcc-16.2.0:16::gentoo [15.3.0:15::gentoo] 0 KiB
[ebuild     U  ] sys-libs/glibc-2.44::gentoo [2.43-r4::gentoo] 0 KiB
"""


class FakeContainer:
    def __init__(self, plan: str, plan_rc: int = 0) -> None:
        self.rootfs = Path("/nonexistent")
        self.plan, self.plan_rc = plan, plan_rc
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], *, check: bool = True, **_k: Any) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if "--pretend" in argv:
            return CommandResult(self.plan_rc, self.plan, "")
        if "@preserved-rebuild" in argv:
            return CommandResult(0, "", "")
        return CommandResult(
            0, "[ebuild     U  ] media-libs/mesa-26.3.1::gentoo [26.3.0::gentoo] 0 KiB\n", ""
        )


def test_update_argv_updates_world_and_the_images_sets_reusing_binpkgs() -> None:
    assert update_argv(_recipe()) == [
        "emerge",
        "--verbose",
        "--usepkg",
        "--update",
        "--deep",
        "--newuse",
        "--changed-deps",
        "@world",
        "@base",
        "@kde",
    ]
    assert "--pretend" in update_argv(_recipe(), pretend=True)


def test_toolchain_changes_ignore_rebuilds_and_lookalike_names() -> None:
    assert toolchain_changes(parse_emerge_plan(_PLAN_OK)[0]) == ()
    assert toolchain_changes(parse_emerge_plan(_PLAN_GCC)[0]) == (
        "sys-devel/gcc-16.2.0",
        "sys-libs/glibc-2.44",
    )


def test_run_update_plans_then_updates_then_rebuilds_preserved_libs() -> None:
    c = FakeContainer(_PLAN_OK)
    result = run_update(c, _recipe())  # type: ignore[arg-type]
    assert [("--pretend" in a, a[-1]) for a in c.calls] == [
        (True, "@kde"),
        (False, "@kde"),
        (False, "@preserved-rebuild"),
    ]
    assert result.phase.name == "update"
    assert result.phase.stage == "kde"
    assert result.built_atoms == ("media-libs/mesa-26.3.1",)


def test_run_update_refuses_a_toolchain_change_before_building_anything() -> None:
    c = FakeContainer(_PLAN_GCC)
    with pytest.raises(ToolchainChangeError, match="sys-devel/gcc-16.2.0 sys-libs/glibc-2.44"):
        run_update(c, _recipe())  # type: ignore[arg-type]
    assert len(c.calls) == 1  # the pretend only


def test_run_update_reports_a_plan_that_does_not_resolve() -> None:
    c = FakeContainer("!!! Multiple package instances within a single package slot\n", plan_rc=1)
    with pytest.raises(FactoryError, match="does not resolve") as err:
        run_update(c, _recipe())  # type: ignore[arg-type]
    assert "Multiple package instances" in err.value.output
