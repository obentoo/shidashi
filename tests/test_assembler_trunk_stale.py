"""The trunk's own plan is judged before the trunk is installed (story 019,
validation fixes round 1, task 9.1; R2.1, R2.2).

A flavor that grows from a trunk with no checkpoint yet installs the trunk first
(the ``trunk`` step), then the branch. The audit of 2026-10-09 found the stale check
only in the ``install`` step: the bentoo-lab replay installed the 709 binpkgs of the
``desktop`` trunk in 31m47s before refusing with "nothing was installed". The trunk's
plan -- one ``--pretend`` of the trunk recipe's install -- must be judged before it.

The judge itself (:func:`shidashi.phases.judge_plan`) is replaced by one that flags
vte wherever a plan holds it: what is under test is WHERE the assemble asks it.
Same harness as ``tests/test_assembler_branch_pretend.py``.
"""

from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import binpkgs
from shidashi.assembler import AssemblerError
from shidashi.container import CommandResult
from tests.test_assembler import (  # noqa: F401 -- autouse fixtures of the module
    _chain,
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _trunked,
    _Wired,
)

VTE = binpkgs.Instance(
    cpv="x11-libs/vte-0.84.1", build_id=1, path="x11-libs/vte/vte-0.84.1-1.gpkg.tar"
)
#: the trunk's plan holds the stale vte; the image's (the branch) does not
TRUNK_PLAN = (
    "[binary   N    ] x/a-1-1::gentoo  0 KiB\n"
    "[binary   N    ] x11-libs/vte-0.84.1-1::gentoo  0 KiB\n"
)
IMAGE_PLAN = "[binary   N    ] x/a-1-1::gentoo  0 KiB\n[binary   N    ] x/c-3-1::gentoo  0 KiB\n"


def _judge(
    container: object, instances: object, plan_text: str
) -> tuple[list[binpkgs.Stale], list[binpkgs.SonameStale]]:
    if "x11-libs/vte-0.84.1" not in plan_text:
        return [], []
    needs = binpkgs.Soname(category="x86_64", name="libsimdutf.so.34")
    return [], [binpkgs.SonameStale(instance=VTE, needs=needs, offered=("libsimdutf.so.36",))]


def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trunk_plan: str) -> _Wired:
    wired = _trunked(tmp_path, monkeypatch)
    wired.pretend_output = IMAGE_PLAN
    base: type = vars(asm)["Container"]  # the _Wired fake, patched in by _trunked
    # the trunk's install argv can equal the image's (both ``@world`` here): the
    # trunk's resolution is the first pretend, asked before anything is installed
    asked: list[bool] = []

    class _TrunkPlan(base):  # type: ignore[misc]
        def run(self, argv: list[str], **kw: object) -> object:
            pretend = argv[:1] == ["emerge"] and "--pretend" in argv
            if pretend and not asked and not _installs(wired.runs):
                asked.append(True)
                wired.runs.append(list(argv))
                return CommandResult(0, trunk_plan, "")  # the trunk recipe's resolution
            return super().run(argv, **kw)

    monkeypatch.setattr(asm, "Container", _TrunkPlan)
    monkeypatch.setattr(asm, "judge_plan", _judge)
    return wired


def _installs(runs: list[list[str]]) -> list[list[str]]:
    return [a for a in runs if a[:1] == ["emerge"] and "--pretend" not in a]


def test_a_stale_binpkg_in_the_trunks_plan_stops_the_assemble_before_the_trunk_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _wire(tmp_path, monkeypatch, TRUNK_PLAN)
    assert (wired.binhost / "Packages").is_file()  # an index: plans are judged

    with pytest.raises(AssemblerError) as err:
        wired.assemble(recipe=_chain("kde"))

    message = str(err.value)
    assert "x11-libs/vte-0.84.1" in message, message
    assert "libsimdutf.so.34" in message and "libsimdutf.so.36" in message, message
    assert "nothing was installed" in message, message
    assert _installs(wired.runs) == []  # not the trunk, not the branch: nothing at all


def test_hostile_a_clean_trunk_plan_still_installs_the_trunk_then_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new judgement must not refuse what holds no stale binpkg."""
    wired = _wire(tmp_path, monkeypatch, IMAGE_PLAN)
    wired.assemble(recipe=_chain("kde"))

    assert wired.step("trunk")["built"] == "znver5-desktop-systemd"
    assert wired.step("trunk")["stale_check"] == "judged"  # the trunk's audit field
    assert wired.step("install")["stale_check"] == "judged"
    assert any("--update" in a for a in _installs(wired.runs))  # the branch ran


def test_the_trunks_plan_is_judged_before_the_trunk_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pretend of the trunk's own install precedes it; the image's comes after."""
    wired = _wire(tmp_path, monkeypatch, IMAGE_PLAN)
    wired.assemble(recipe=_chain("kde"))

    pretends = [i for i, a in enumerate(wired.runs) if a[:1] == ["emerge"] and "--pretend" in a]
    first_install = wired.runs.index(_installs(wired.runs)[0])
    # the trunk's before its install; the image's after it, before the branch
    assert pretends and pretends[0] < first_install, wired.runs
    assert len([i for i in pretends if i < first_install]) == 1, wired.runs
    assert len(pretends) == 2, wired.runs
