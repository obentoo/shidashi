"""The factory's ``check-binpkgs`` judges the settle's binpkgs too (story 019, task 8.2;
R2.3, D3).

A cut package is installed twice by the assembler: under the cut by the install, then
with its final USE by the settle (``--nodeps``). The settle's binpkg is a binpkg the image
ships, so a soname-stale instance planned ONLY by the settle must fail the check -- the
install's plan alone would never show it.

CHARACTERIZATION test of behaviour implemented before it was written (Red was not
establishable). Same harness as ``tests/test_factory_recheck_soname.py``: its fixtures
and ``_Stage`` fake are imported; the fake here answers the settle's pretend with its
own plan. When merged into that module, drop the import.
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import phases
from shidashi.container import CommandResult
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from tests.test_factory_recheck_soname import (
    FULL_INDEX,
    P_CURL,
    P_GLIBC,
    P_SIMDUTF,
    P_VTE_1,
    P_VTE_2,
    VTE_LINE,
    _Stage,
)

pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

#: the cut: vte is installed with ``-vala`` first, then settled with its final USE
VTE_CUT = UseBreak(atom="x11-libs/vte", flag="vala")
#: what the install pretend plans: no vte at all -- the stale build is in no install line
INSTALL = (P_SIMDUTF, P_GLIBC, P_CURL)


class _Settling(_Stage):
    """``_Stage`` whose ``--nodeps`` pretend (the settle) answers its own plan."""

    def __init__(self, tmp: Path, pkgdir: Path, plan: str, settle: str) -> None:
        super().__init__(tmp, pkgdir, plan)
        self.settle = settle
        self.emerges: list[list[str]] = []

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        argv = list(argv)
        if argv[0] == "emerge":
            self.emerges.append(argv)
            return CommandResult(0, self.settle if "--nodeps" in argv else self.plan, "")
        return super().run(argv, env=env, check=check)


def _recipe(*cuts: UseBreak) -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="gnome",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=("gnome",),
        phases=(
            Phase(name="base", stage="base", sets=("base",), emptytree=True),
            Phase(name="gnome", stage="gnome", sets=("gnome",), ships=True, use_break=cuts),
        ),
        portage_layers=("base", "arch/v3", "flavor/gnome", "init/systemd"),
    )


def _stage(tmp: Path, monkeypatch: pytest.MonkeyPatch, *settle: str) -> _Settling:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp / "cache"))
    pkgdir = tmp / "cache" / "binpkgs" / "v3" / "20260823T153057Z"
    pkgdir.mkdir(parents=True)
    (pkgdir / "Packages").write_text(FULL_INDEX)
    install = "".join(f"{line}\n" for line in INSTALL)
    return _Settling(tmp, pkgdir, install, "".join(f"{line}\n" for line in settle))


def _settles(container: _Settling) -> list[list[str]]:
    return [argv for argv in container.emerges if "--nodeps" in argv]


# --- hostile: the settle's plan is judged by what it picks, nothing more -----------------


def test_hostile_a_fresh_settle_build_passes_beside_its_stale_sibling_in_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle picks vte build 2 (needs .so.36, offered by the install's simdutf). The
    index still holds the soname-stale build 1: judging the settle by cpv, or by the
    whole index, would fail a settle that ships no stale binpkg."""
    container = _stage(tmp_path, monkeypatch, P_VTE_2)
    assert phases.check_binpkgs(container, _recipe(VTE_CUT), "gnome") == {"index": "judged"}  # type: ignore[arg-type]
    assert len(_settles(container)) == 1


def test_the_same_plan_without_the_cut_passes_and_runs_no_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cut, no settle: the stale vte build the fake would answer is never asked for, and
    the install's plan (simdutf, glibc, curl) holds nothing stale."""
    container = _stage(tmp_path, monkeypatch, P_VTE_1)
    assert phases.check_binpkgs(container, _recipe(), "gnome") == {"index": "judged"}  # type: ignore[arg-type]
    assert _settles(container) == []
    assert len(container.emerges) == 1


# --- the refusal ------------------------------------------------------------------------


def test_a_stale_binpkg_only_in_the_settle_fails_the_check_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install plans no vte; the settle of the cut plans vte build 1, which needs
    ``libsimdutf.so.34`` while the image's simdutf-9.2.1 offers ``.so.36``."""
    container = _stage(tmp_path, monkeypatch, P_VTE_1)
    with pytest.raises(phases.FactoryError) as err:
        phases.check_binpkgs(container, _recipe(VTE_CUT), "gnome")  # type: ignore[arg-type]
    message = str(err.value)
    assert err.value.phase == "gnome:binpkgs"
    assert VTE_LINE in message
    # the settle is the one that planned it: one settle pretend, of the cut atom, --nodeps
    settles = _settles(container)
    assert len(settles) == 1
    assert settles[0][-1] == "x11-libs/vte"
    # the install's fresh instances are not named
    assert "net-misc/curl" not in message
    assert "dev-cpp/simdutf-9.2.1" not in message
