"""The image's plan is judged on a throwaway copy before its trunk installs (story 019,
validation fixes round 1, task 9.2; R2.2, R2.4; design D3, v4).

A flavor whose trunk has no checkpoint installs the trunk first. The image's own
configuration is written only after that install (the trunk checkpoint is shared by
every flavor), so its plan cannot be resolved in the real rootfs before it. Judging the
trunk's plan (task 9.1) is not enough: an instance only the IMAGE selects still lets
the whole trunk install before the refusal. The image's plan is therefore resolved on
a copy of the seeded rootfs, ``<rootfs>.judge``, configured as the image, and removed
afterwards; the ``install`` step reuses that plan instead of resolving it again.

The fake resolver answers each ``--pretend`` by the configuration of the rootfs it runs
in (its world file): a rootfs configured as the trunk resolves the trunk's plan, one
configured as the image resolves the image's. The trunk's and the image's install argv
are identical here (both ``@world``), so the argv cannot tell them apart. The judge
(:func:`shidashi.phases.judge_plan`) is replaced by one that flags vte wherever a plan
holds it. Same harness as ``tests/test_assembler_trunk_stale.py``.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import binpkgs, checkpoint
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

KDE = "znver5-kde-systemd"
VTE = binpkgs.Instance(
    cpv="x11-libs/vte-0.84.1", build_id=1, path="x11-libs/vte/vte-0.84.1-1.gpkg.tar"
)
#: the trunk's world, as ``_trunked`` composes it (``world_atoms``)
TRUNK_WORLD = "app-misc/trunk\n"
#: the trunk's plan is clean. Its tokens are those of what the trunk install prints
#: (``_Wired.install_output``), so a restored trunk stays valid on the next flavor.
TRUNK_PLAN = "[binary   N    ] x/a-1-1::gentoo  0 KiB\n[binary   N    ] x/b-2:0::gentoo  0 KiB\n"
#: the image's plan holds the stale vte: only the image selects it
STALE_IMAGE_PLAN = (
    "[binary   N    ] x/a-1-1::gentoo  0 KiB\n"
    "[binary   N    ] x/c-3-1::gentoo  0 KiB\n"
    "[binary   N    ] x11-libs/vte-0.84.1-1::gentoo  0 KiB\n"
)
#: a clean image plan, different from both the trunk's plan and the install's output
CLEAN_IMAGE_PLAN = (
    "[binary   N    ] x/a-1-1::gentoo  0 KiB\n"
    "[binary   N    ] x/b-2-1::gentoo  0 KiB\n"
    "[binary   N    ] x/c-3-1::gentoo  0 KiB\n"
)
CLEAN_IMAGE_TOKENS = ["x/a-1-1::gentoo", "x/b-2-1::gentoo", "x/c-3-1::gentoo"]
#: what a killed run left inside its copy
LEFTOVER = "left-by-a-killed-run"


@dataclass(frozen=True)
class _Pretend:
    """One ``--pretend``: where it ran and what that rootfs was configured as."""

    index: int  # its position in ``_Wired.runs``
    rootfs: Path
    world: str
    leftover: bool


def _judge(
    container: object, instances: object, plan_text: str
) -> tuple[list[binpkgs.Stale], list[binpkgs.SonameStale]]:
    if "x11-libs/vte-0.84.1" not in plan_text:
        return [], []
    needs = binpkgs.Soname(category="x86_64", name="libsimdutf.so.34")
    return [], [binpkgs.SonameStale(instance=VTE, needs=needs, offered=("libsimdutf.so.36",))]


def _rootfs(tmp_path: Path, flavor: str) -> Path:
    return tmp_path / "scratch" / "assemble" / f"znver5-{flavor}-systemd"


def _judge_copy(tmp_path: Path, flavor: str) -> Path:
    real = _rootfs(tmp_path, flavor)
    return real.with_name(f"{real.name}.judge")


def _wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_plan: str
) -> tuple[_Wired, list[_Pretend]]:
    wired = _trunked(tmp_path, monkeypatch)
    base: type = vars(asm)["Container"]  # the _Wired fake, patched in by _trunked
    seen: list[_Pretend] = []

    class _Resolver(base):  # type: ignore[misc]
        """A pretend resolves what the rootfs it runs in is configured to install."""

        def run(self, argv: list[str], **kw: object) -> object:
            if argv[:1] == ["emerge"] and "--pretend" in argv:
                rootfs = Path(self.rootfs)
                world = rootfs / "var" / "lib" / "portage" / "world"
                configured = world.read_text() if world.is_file() else ""
                seen.append(
                    _Pretend(len(wired.runs), rootfs, configured, (rootfs / LEFTOVER).exists())
                )
                wired.runs.append(list(argv))
                if configured == TRUNK_WORLD:
                    return CommandResult(0, TRUNK_PLAN, "")
                # configured as the image, or not configured at all (nothing to resolve)
                return CommandResult(0, image_plan if configured else "", "")
            return super().run(argv, **kw)

    monkeypatch.setattr(asm, "Container", _Resolver)
    monkeypatch.setattr(asm, "judge_plan", _judge)
    return wired, seen


def _installs(runs: list[list[str]]) -> list[int]:
    return [i for i, a in enumerate(runs) if a[:1] == ["emerge"] and "--pretend" not in a]


def _copies(seen: list[_Pretend], tmp_path: Path, flavor: str) -> set[Path]:
    """Every rootfs a pretend ran in that is not the image's own."""
    return {p.rootfs for p in seen if p.rootfs != _rootfs(tmp_path, flavor)}


def _attempt(wired: _Wired, flavor: str) -> AssemblerError | None:
    try:
        wired.assemble(recipe=_chain(flavor))
    except AssemblerError as err:
        return err
    return None


def test_hostile_a_stale_binpkg_only_in_the_images_plan_refuses_before_the_trunk_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trunk's plan is clean: only the image's plan, judged before the trunk, refuses."""
    wired, _seen = _wire(tmp_path, monkeypatch, STALE_IMAGE_PLAN)
    assert (wired.binhost / "Packages").is_file()  # an index: plans are judged
    assert "x11-libs/vte" not in TRUNK_PLAN  # the fixture: the trunk alone would pass

    with pytest.raises(AssemblerError) as err:
        wired.assemble(recipe=_chain("kde"))

    message = str(err.value)
    assert "x11-libs/vte-0.84.1" in message, message
    assert "libsimdutf.so.34" in message and "libsimdutf.so.36" in message, message
    assert "nothing was installed" in message, message
    assert "shidashi factory znver5 kde systemd" in message, message  # the image's hint
    assert [wired.runs[i] for i in _installs(wired.runs)] == []  # not the trunk, nothing
    built = [s for s in wired.steps if s["step"] == "trunk" and "built" in s]
    assert built == [], built  # no trunk checkpoint either
    assert not [m for m in wired.store.marks(checkpoint.INSTALL)], "a checkpoint was saved"


@pytest.mark.parametrize(
    ("image_plan", "leftover"),
    [(STALE_IMAGE_PLAN, False), (CLEAN_IMAGE_PLAN, False), (CLEAN_IMAGE_PLAN, True)],
    ids=["after-a-refusal", "after-a-pass", "replacing-a-killed-runs-leftover"],
)
def test_the_judge_copy_is_gone_after_the_assemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_plan: str, leftover: bool
) -> None:
    wired, seen = _wire(tmp_path, monkeypatch, image_plan)
    judge = _judge_copy(tmp_path, "kde")
    if leftover:
        judge.mkdir(parents=True)
        (judge / LEFTOVER).write_text("x")

    refused = _attempt(wired, "kde")

    assert (refused is not None) == (image_plan == STALE_IMAGE_PLAN), refused
    # the image's plan was resolved in its copy, and only there
    assert _copies(seen, tmp_path, "kde") == {judge}, seen
    assert not judge.exists() and not judge.is_symlink(), "the copy is still there"
    # a killed run's copy is not reused: the judged copy is the seeded rootfs
    assert not any(p.leftover for p in seen if p.rootfs == judge), seen


def test_a_clean_plan_resolves_the_image_once_before_the_trunk_and_saves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired, seen = _wire(tmp_path, monkeypatch, CLEAN_IMAGE_PLAN)
    # the fixture: the saved plan tells which answer it came from
    install_tokens = list(checkpoint.plan_tokens(wired.install_output))
    assert list(checkpoint.plan_tokens(TRUNK_PLAN)) == install_tokens != CLEAN_IMAGE_TOKENS

    wired.assemble(recipe=_chain("kde"))

    assert wired.step("trunk")["built"] == "znver5-desktop-systemd"  # the trunk installed
    first_install = _installs(wired.runs)[0]
    image = [p for p in seen if p.world == "app-misc/kde\n"]
    assert len(image) == 1, seen  # the image is resolved once in the whole run
    assert image[0].rootfs == _judge_copy(tmp_path, "kde"), image  # on its copy
    assert image[0].index < first_install, (image, wired.runs)  # before the trunk installs
    # the install step resolves nothing again: no pretend after the trunk's install
    assert [p for p in seen if p.index > first_install] == [], (seen, wired.runs)
    (mark,) = [m for m in wired.store.marks(checkpoint.INSTALL) if KDE in m.images]
    assert list(mark.data["plan"]) == CLEAN_IMAGE_TOKENS  # the copy's pretend, saved
    assert "judged before the trunk" in wired.step("trunk").values(), wired.step("trunk")


def test_a_restored_trunk_makes_no_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: the second flavor grows from the trunk checkpoint; its plan is judged in place."""
    wired, seen = _wire(tmp_path, monkeypatch, CLEAN_IMAGE_PLAN)
    wired.assemble(recipe=_chain("kde"))  # builds the trunk
    seen.clear()
    calls = len(wired.store.backend.calls)  # type: ignore[attr-defined]

    wired.assemble(recipe=_chain("gnome"))

    assert wired.step("trunk")["restored"] == "znver5-desktop-systemd"
    assert _copies(seen, tmp_path, "gnome") == set(), seen
    later = wired.store.backend.calls[calls:]  # type: ignore[attr-defined]
    assert not [c for c in later if c[1].name.endswith(".judge")], later
    assert not _judge_copy(tmp_path, "gnome").exists()
    # the image's plan is resolved once, in the install step, on the real rootfs
    assert [p.rootfs for p in seen if p.world == "app-misc/gnome\n"] == [
        _rootfs(tmp_path, "gnome")
    ], seen
