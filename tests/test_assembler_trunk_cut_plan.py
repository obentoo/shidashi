"""The trunk's INSTALL checkpoint keeps the cut-free plan (story 025, task 1.3).

A trunk with cycle cuts installs under the cuts, then settles the cut atoms from their
final binpkgs. Its checkpoint is checked on the next run by :meth:`_still_valid`, which
resolves the cut-free plan in the restored rootfs; the trunk must therefore record that
same cut-free plan, or every flavor finds the trunk "stale" and installs it again (seen on
the bentoo-lab on 2026-10-14: "the resolver chooses other binpkgs: pipewire-1.6.9-1 …
pipewire-1.6.9-7").
"""

from pathlib import Path

import pytest

from shidashi import checkpoint
from tests.test_assembler import (  # noqa: F401 -- autouse fixtures of the module
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
)
from tests.test_assembler_image_before_trunk_cuts import (
    CUT_FREE,
    TRUNK,
    _cut_chain,
    _wire,
)

GNOME = "znver5-gnome-systemd"


def _trunk_plan(wired: object) -> list[str]:
    (mark,) = [
        m
        for m in wired.store.marks(checkpoint.INSTALL)  # type: ignore[attr-defined]
        if TRUNK in m.images
    ]
    return list(mark.data["plan"])


def test_the_trunks_checkpoint_keeps_the_cut_free_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired, _seen, _judged = _wire(tmp_path, monkeypatch)
    wired.assemble(recipe=_cut_chain("kde"))

    assert _trunk_plan(wired) == CUT_FREE


def test_a_second_flavor_restores_the_trunk_the_first_one_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired, _seen, _judged = _wire(
        tmp_path,
        monkeypatch,
        pulls=frozenset({"app-misc/trunk", "app-misc/kde", "app-misc/gnome"}),
    )
    wired.assemble(recipe=_cut_chain("kde"))
    wired.assemble(recipe=_cut_chain("gnome"))

    trunk = wired.step("trunk")
    assert "restored" in trunk and "built" not in trunk, trunk
    assert "stale" not in wired.step("seed"), wired.step("seed")
