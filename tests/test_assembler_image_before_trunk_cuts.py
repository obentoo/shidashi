"""The judgement copy and the cycle cuts (story 019, task 9.2; R2.2, R2.4; tech review).

The image's plan is judged on a copy configured as the image -- cuts included, since
the install runs under them. But the image's INSTALL checkpoint is frozen after the
settle step clears the cut file, and the next run checks it against a cut-free
resolution (``_still_valid``). So the plan the copy saves must be the cut-free one, and
that is also the plan of the binpkgs the ISO ships: both resolutions are judged.

The fake resolver models ``--binpkg-respect-use=y`` on the real binhost's split
(pipewire build 1 without ``ffmpeg``, builds 2-7 with it): with the cut file present a
pretend picks ``x/c-3-1``, without it ``x/c-3-7``. Adapted from the reviewer's probe.
"""

import subprocess
from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import binpkgs, checkpoint, config, world
from shidashi.assembler import AssemblerError
from shidashi.container import CommandResult
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from tests.test_assembler import (  # noqa: F401 -- autouse fixtures of the module
    _chain,
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _Wired,
)

KDE = "znver5-kde-systemd"
CUT = UseBreak(atom="media-video/pipewire", flag="ffmpeg", enable=False)
CUT_FILE = ("etc", "portage", "package.use", "zz-shidashi-use-break")
SHIPPED = binpkgs.Instance(cpv="x/c-3", build_id=7, path="x/c/c-3-7.gpkg.tar")


def _cut_chain(flavor: str) -> ResolvedRecipe:
    recipe = _chain(flavor)
    phases = list(recipe.phases)
    phases[2] = Phase(name="desktop", stage="desktop", use_break=(CUT,))
    return recipe.model_copy(update={"phases": tuple(phases)})


def _wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stale_shipped: bool = False,
    copy_pretend_fails: bool = False,
) -> tuple[_Wired, list[tuple[str, bool]]]:
    wired = _Wired(tmp_path, monkeypatch)
    trunk = _cut_chain("kde").model_copy(
        update={
            "flavor": "desktop",
            "stages": ("base", "minimal", "desktop"),
            "phases": _cut_chain("kde").phases[:3],
            "portage_layers": ("base", "arch/znver5", "init/systemd"),
        }
    )
    real = config.load_recipe

    def load_recipe(arch: str, target: str, init: str, **kw: object) -> ResolvedRecipe:
        return trunk if target == "desktop" else real(arch, target, init, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(config, "load_recipe", load_recipe)
    monkeypatch.setattr(asm, "world_atoms", lambda recipe: ("app-misc/trunk",))
    monkeypatch.setattr(
        world, "current_atoms", lambda recipe, variants_dir: (f"app-misc/{recipe.flavor}",)
    )
    base: type = vars(asm)["Container"]  # the _Wired fake
    seen: list[tuple[str, bool]] = []

    class _Resolver(base):  # type: ignore[misc]
        def run(self, argv: list[str], **kw: object) -> object:
            if argv[:1] == ["emerge"] and "--pretend" in argv:
                rootfs = Path(self.rootfs)
                cut = rootfs.joinpath(*CUT_FILE)
                has_cut = cut.is_file() and "pipewire -ffmpeg" in cut.read_text()
                seen.append((rootfs.name, has_cut))
                wired.runs.append(list(argv))
                if copy_pretend_fails and rootfs.name.endswith(".judge"):
                    raise subprocess.CalledProcessError(
                        1, argv, output="emerge: there are no binary packages to satisfy x/z"
                    )
                pipewire = "x/c-3-1" if has_cut else "x/c-3-7"
                return CommandResult(
                    0,
                    f"[binary   N    ] x/a-1-1::gentoo  0 KiB\n"
                    f"[binary   N    ] {pipewire}::gentoo  0 KiB\n",
                    "",
                )
            return super().run(argv, **kw)

    def judge(
        container: object, instances: object, plan_text: str
    ) -> tuple[list[binpkgs.Stale], list[binpkgs.SonameStale]]:
        if stale_shipped and "x/c-3-7" in plan_text:
            needs = binpkgs.Soname(category="x86_64", name="libavcodec.so.61")
            return [], [
                binpkgs.SonameStale(instance=SHIPPED, needs=needs, offered=("libavcodec.so.62",))
            ]
        return [], []

    monkeypatch.setattr(asm, "Container", _Resolver)
    monkeypatch.setattr(asm, "judge_plan", judge)
    return wired, seen


def _installs(runs: list[list[str]]) -> list[list[str]]:
    return [a for a in runs if a[:1] == ["emerge"] and "--pretend" not in a]


def test_the_saved_plan_is_the_cut_free_one_so_the_next_run_restores_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired, _seen = _wire(tmp_path, monkeypatch)
    wired.assemble(recipe=_cut_chain("kde"))
    (mark,) = [m for m in wired.store.marks(checkpoint.INSTALL) if KDE in m.images]
    assert list(mark.data["plan"]) == ["x/a-1-1::gentoo", "x/c-3-7::gentoo"]  # what ships

    wired.assemble(recipe=_cut_chain("kde"))

    seed = wired.step("seed")
    assert seed.get("restored") == checkpoint.PACKAGES, seed  # nothing installed again
    assert _installs(wired.runs) == [], wired.runs


def test_the_shipped_instance_is_judged_before_the_trunk_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale only once the cut is cleared: the binpkg the settle installs and the ISO ships."""
    wired, seen = _wire(tmp_path, monkeypatch, stale_shipped=True)

    with pytest.raises(AssemblerError) as err:
        wired.assemble(recipe=_cut_chain("kde"))

    message = str(err.value)
    assert "x/c-3" in message and "libavcodec.so.61" in message, message
    assert "nothing was installed" in message, message
    assert _installs(wired.runs) == [], wired.runs
    # both resolutions of the image ran on its copy: with the cut, then without it
    assert [s for s in seen if s[0].endswith(".judge")] == [
        (f"{KDE}.judge", True),
        (f"{KDE}.judge", False),
    ], seen


def test_a_failed_pretend_on_the_copy_keeps_emerges_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not reported as a copy failure: the resolver's own error, with its output."""
    wired, _seen = _wire(tmp_path, monkeypatch, copy_pretend_fails=True)

    with pytest.raises(subprocess.CalledProcessError) as err:
        wired.assemble(recipe=_cut_chain("kde"))

    assert "no binary packages to satisfy x/z" in (err.value.output or ""), err.value
    assert not (tmp_path / "scratch" / "assemble" / f"{KDE}.judge").exists()
    assert _installs(wired.runs) == [], wired.runs
