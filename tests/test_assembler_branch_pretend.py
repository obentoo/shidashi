"""A branched assemble resolves its plan once (story 019, task 8.1; R2.1, R2.2; D3).

Before a fresh install the assembler runs ``iso_emerge_argv(recipe) + ["--pretend"]``
to judge the plan against the binhost index. Off a trunk, that same pretend is the
image's plan: the install's own output lists only the branch, so the plan must come
from the resolver -- once, not a second time after the install. A resume continues an
install whose plan was judged when it began: it runs no pretend before the resume.

CHARACTERIZATION test of behaviour implemented before it was written (Red was not
establishable). Same harness as ``tests/test_assembler.py``: its autouse fixtures and
the ``_Wired`` / ``_trunked`` fakes are imported; when merged into that module, drop
the import.
"""

import subprocess
from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import checkpoint
from shidashi.assembler import iso_emerge_argv
from tests.test_assembler import (  # noqa: F401 -- autouse fixtures of the module
    _chain,
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _trunked,
    _Wired,
)

KDE = "znver5-kde-systemd"

#: what the resolver chooses for the whole kde image: NOT what the branch install
#: prints (``_Wired.install_output``), so a plan read from the install is told apart
PRETEND = (
    "[binary   N    ] x/a-1-1::gentoo  0 KiB\n"
    "[binary   N    ] x/b-2-1::gentoo  0 KiB\n"
    "[binary   N    ] x/c-3-1::gentoo  0 KiB\n"
)
PRETEND_TOKENS = ["x/a-1-1::gentoo", "x/b-2-1::gentoo", "x/c-3-1::gentoo"]


def _pretends(runs: list[list[str]]) -> list[list[str]]:
    return [argv for argv in runs if argv[:1] == ["emerge"] and "--pretend" in argv]


def _branch_index(runs: list[list[str]]) -> int:
    return next(i for i, argv in enumerate(runs) if argv[:1] == ["emerge"] and "--update" in argv)


def _kde_plan(wired: _Wired) -> list[str]:
    (mark,) = [m for m in wired.store.marks(checkpoint.INSTALL) if KDE in m.images]
    return list(mark.data["plan"])


def test_hostile_the_plan_is_not_read_from_the_branch_install_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch prints only what it added; the image's plan is the whole resolution."""
    wired = _trunked(tmp_path, monkeypatch)
    wired.pretend_output = PRETEND
    assert list(checkpoint.plan_tokens(wired.install_output)) != PRETEND_TOKENS  # the fixture
    wired.assemble(recipe=_chain("kde"))
    assert _kde_plan(wired) == PRETEND_TOKENS


def test_a_branched_install_with_an_index_runs_exactly_one_pretend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _trunked(tmp_path, monkeypatch)
    wired.pretend_output = PRETEND
    assert (wired.binhost / "Packages").is_file()  # an index: the plan is judged
    wired.assemble(recipe=_chain("kde"))

    assert wired.step("trunk")["built"] == "znver5-desktop-systemd"  # a branched install
    # a trunk built here is judged before it installs (validation fixes round 1):
    # its own pretend comes first, then exactly one of the image, before the branch
    trunk_install = next(
        i for i, a in enumerate(wired.runs) if a[:1] == ["emerge"] and "--pretend" not in a
    )
    after_trunk = _pretends(wired.runs[trunk_install:])
    assert after_trunk == [[*iso_emerge_argv(_chain("kde")), "--pretend"]]  # one, the image's
    assert len(_pretends(wired.runs[:trunk_install])) == 1  # the trunk's
    # the image's pretend is the judge's, before the branch -- none resolves it again after
    image_pretend = trunk_install + wired.runs[trunk_install:].index(after_trunk[0])
    assert image_pretend < _branch_index(wired.runs)
    install = wired.step("install")
    assert install["stale_check"] == "judged"
    assert install["plan"] == len(PRETEND_TOKENS)
    assert _kde_plan(wired) == PRETEND_TOKENS  # the saved plan is that pretend's tokens


def test_a_resumed_branched_install_runs_no_pretend_before_the_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _trunked(tmp_path, monkeypatch)
    # the trunk first, so that only kde's branch can fail below
    wired.assemble(recipe=_chain("gnome"))

    wired.fail_install = True
    with pytest.raises(subprocess.CalledProcessError):
        wired.assemble(recipe=_chain("kde"))
    (partial,) = wired.store.marks(checkpoint.PARTIAL)
    assert partial.data["branch"] is True  # the branch failed midway, not the trunk

    wired.fail_install = False
    wired.assemble(recipe=_chain("kde"))
    assert wired.step("seed")["restored"] == checkpoint.PARTIAL
    resume = wired.runs.index(list(asm.ISO_RESUME_ARGV))
    assert _pretends(wired.runs[:resume]) == []  # judged when the install began
    assert "stale_check" not in wired.step("install")
