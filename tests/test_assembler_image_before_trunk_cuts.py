"""The judgement copy and the cycle cuts (story 019, task 9.2; story 025).

The image's plan is judged on a copy of the seeded stage3 configured as the image,
before its trunk installs (story 019, R2.2, R2.4). Under the cuts that is the whole
``--emptytree`` plan. Without them it cannot be: the cut-free desktop USE holds the
``ffmpeg -> libsdl2 -> pipewire -> ffmpeg`` cycle by construction, and Portage breaks a
cycle of binaries only with an installed instance -- the bare seed has none, so the
cut-free ``--emptytree`` pretend on the copy always ends in "circular dependencies"
(story 025, C1). The cut-free side is therefore judged piece by piece: the final
binpkgs of the cut atoms alone, ``--nodeps``. The INSTALL checkpoint keeps the cut-free
plan, resolved where it is resolvable: in the installed rootfs (E3), so that the next
run's ``_still_valid`` restores it.

The fake resolver models ``--binpkg-respect-use=y`` on the real binhost's split
(pipewire build 1 without ``ffmpeg``, build 7 with it) and Portage's cycle rule:

- a rootfs whose world pulls the cycle resolves, under the cut, ``x/a`` and ``x/c-3-1``;
  without it, ``x/a``, ``x/c-3-7`` and ``x/d-4-1`` (a dependency only the cut-free USE
  pulls) -- but only where pipewire is installed; elsewhere it raises Portage's
  "circular dependencies", as ``container.run`` does on exit 1;
- ``--nodeps`` prints only the named atoms' binaries (``x/c-3-1`` or ``x/c-3-7``);
- an install of a rootfs whose world pulls the cycle installs pipewire (its vdb entry).
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import binpkgs, checkpoint, config, world
from shidashi.assembler import AssemblerError
from shidashi.container import CommandResult
from shidashi.phases import ISO_SETTLE_OPTIONS
from shidashi.recipe import ResolvedRecipe, UseBreak
from tests.test_assembler import (  # noqa: F401 -- autouse fixtures of the module
    _chain,
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _Wired,
)

KDE = "znver5-kde-systemd"
TRUNK = "znver5-desktop-systemd"
PIPEWIRE = "media-video/pipewire"
CUT = UseBreak(atom=PIPEWIRE, flag="ffmpeg", enable=False)
CUT_FILE = ("etc", "portage", "package.use", "zz-shidashi-use-break")
WORLD_FILE = ("var", "lib", "portage", "world")
SHIPPED = binpkgs.Instance(cpv="x/c-3", build_id=7, path="x/c/c-3-7.gpkg.tar")
#: the image's plan under the cut, and without it (what the ISO ships)
UNDER_CUT = ["x/a-1-1::gentoo", "x/c-3-1::gentoo"]
CUT_FREE = ["x/a-1-1::gentoo", "x/c-3-7::gentoo", "x/d-4-1::gentoo"]
#: what Portage prints when no installed instance can break the cycle
CYCLE = (
    "!!! Error: circular dependencies:\n\n"
    "(media-video/ffmpeg-7.1.1-r2:0/59.61.61::gentoo, binary scheduled for merge) depends on\n"
    " (media-libs/libsdl2-2.32.8:0/0::gentoo, binary scheduled for merge) (runtime)\n"
    "  (media-video/pipewire-1.4.7:0/0.4::gentoo, binary scheduled for merge) (runtime)\n"
    "   (media-video/ffmpeg-7.1.1-r2:0/59.61.61::gentoo, binary scheduled for merge) (runtime)\n"
    "\nIt might be possible to break this cycle\n"
    "by applying the following change:\n"
    "- media-video/pipewire-1.4.7 (Change USE: -ffmpeg)\n"
)
#: emerge's own errors, one per pretend of the copy that can fail
FAILURES = {
    "emptytree": "emerge: there are no binary packages to satisfy x/z",
    "nodeps": f"emerge: there are no binary packages to satisfy {PIPEWIRE}",
}


@dataclass(frozen=True)
class _Pretend:
    """One ``--pretend``: its position in ``_Wired.runs``, where it ran, how."""

    index: int
    rootfs: str  # the rootfs directory's name
    cut: bool  # the cut file was present
    kind: str  # "nodeps", "emptytree" or "other"

    @property
    def shape(self) -> tuple[str, bool, str]:
        return (self.rootfs, self.cut, self.kind)


def _cut_chain(flavor: str, *cuts: UseBreak, in_image: bool = False) -> ResolvedRecipe:
    """The chain with ``cuts`` (default: the pipewire cut) in the trunk's or the image's phase."""
    recipe = _chain(flavor)
    phases = list(recipe.phases)
    at = 3 if in_image else 2
    phases[at] = phases[at].model_copy(update={"use_break": cuts or (CUT,)})
    return recipe.model_copy(update={"phases": tuple(phases)})


def _wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    recipe: ResolvedRecipe | None = None,
    pulls: frozenset[str] = frozenset({"app-misc/trunk", "app-misc/kde"}),
    stale_shipped: bool = False,
    copy_pretend_fails: str | None = None,
) -> tuple[_Wired, list[_Pretend], list[tuple[str, str]]]:
    """The assemble, its pretends and the judge's calls ``(rootfs name, plan text)``.

    ``pulls``: the world atoms whose closure holds the cycle (the trunk's and the
    image's by default). ``copy_pretend_fails``: the kind of pretend that fails on the
    judge copy with emerge's own error.
    """
    wired = _Wired(tmp_path, monkeypatch)
    # the tree holds the dependency only the cut-free USE pulls in
    (tmp_path / "pinned-gentoo" / "x" / "d").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pinned-gentoo" / "x" / "d" / "d-4.ebuild").touch()
    chain = recipe or _cut_chain("kde")
    trunk = chain.model_copy(
        update={
            "flavor": "desktop",
            "stages": ("base", "minimal", "desktop"),
            "phases": chain.phases[:3],
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
    seen: list[_Pretend] = []
    judged: list[tuple[str, str]] = []

    def pulls_cycle(rootfs: Path) -> bool:
        path = rootfs.joinpath(*WORLD_FILE)
        atoms = set(path.read_text().split()) if path.is_file() else set()
        return bool(atoms & pulls)

    def pipewire_installed(rootfs: Path) -> bool:
        vdb = rootfs / "var" / "db" / "pkg" / "media-video"
        return vdb.is_dir() and any(d.name.startswith("pipewire-") for d in vdb.iterdir())

    class _Resolver(base):  # type: ignore[misc]
        def run(self, argv: list[str], **kw: object) -> object:
            rootfs = Path(self.rootfs)
            if argv[:1] == ["emerge"] and "--pretend" in argv:
                cut = rootfs.joinpath(*CUT_FILE)
                has_cut = cut.is_file() and "pipewire -ffmpeg" in cut.read_text()
                kind = (
                    "nodeps"
                    if "--nodeps" in argv
                    else "emptytree"
                    if "--emptytree" in argv
                    else "other"
                )
                seen.append(_Pretend(len(wired.runs), rootfs.name, has_cut, kind))
                wired.runs.append(list(argv))
                if copy_pretend_fails == kind and rootfs.name.endswith(".judge"):
                    raise subprocess.CalledProcessError(1, argv, output=FAILURES[kind])
                pipewire = "x/c-3-1" if has_cut else "x/c-3-7"
                if kind == "nodeps":  # only the named atoms' binaries
                    return CommandResult(0, f"[binary   N    ] {pipewire}::gentoo  0 KiB\n", "")
                if not pulls_cycle(rootfs):
                    return CommandResult(0, "[binary   N    ] x/a-1-1::gentoo  0 KiB\n", "")
                if not has_cut and not pipewire_installed(rootfs):
                    # Portage: no installed instance to break the cycle with
                    raise subprocess.CalledProcessError(1, argv, output=CYCLE)
                chosen = UNDER_CUT if has_cut else CUT_FREE
                return CommandResult(
                    0,
                    "".join(f"[binary   N    ] {t}  0 KiB\n" for t in chosen),
                    "",
                )
            if (
                argv[:1] == ["emerge"]
                and ("--emptytree" in argv or "--update" in argv)
                and pulls_cycle(rootfs)
            ):
                # the install merges the cycle: pipewire is installed from now on
                (rootfs / "var" / "db" / "pkg" / "media-video" / "pipewire-1.4.7").mkdir(
                    parents=True, exist_ok=True
                )
            return super().run(argv, **kw)

    def judge(
        container: object, instances: object, plan_text: str
    ) -> tuple[list[binpkgs.Stale], list[binpkgs.SonameStale]]:
        judged.append((Path(container.rootfs).name, plan_text))  # type: ignore[attr-defined]
        if stale_shipped and "x/c-3-7" in plan_text:
            needs = binpkgs.Soname(category="x86_64", name="libavcodec.so.61")
            return [], [
                binpkgs.SonameStale(instance=SHIPPED, needs=needs, offered=("libavcodec.so.62",))
            ]
        return [], []

    monkeypatch.setattr(asm, "Container", _Resolver)
    monkeypatch.setattr(asm, "judge_plan", judge)
    return wired, seen, judged


def _installs(runs: list[list[str]]) -> list[list[str]]:
    return [a for a in runs if a[:1] == ["emerge"] and "--pretend" not in a]


def _first_install(runs: list[list[str]]) -> int:
    return next(i for i, a in enumerate(runs) if a[:1] == ["emerge"] and "--pretend" not in a)


def _on_copy(seen: list[_Pretend], flavor: str = KDE) -> list[tuple[str, bool, str]]:
    return [p.shape for p in seen if p.rootfs == f"{flavor}.judge"]


def _atoms(argv: list[str]) -> list[str]:
    """The positional arguments of an emerge command line: what it names."""
    return [a for a in argv[1:] if not a.startswith("-")]


def _kde_plan(wired: _Wired) -> list[str]:
    # the flavor's own checkpoint: the trunk's lists the flavor too (story 006, R3.11)
    (mark,) = [
        m
        for m in wired.store.marks(checkpoint.INSTALL)
        if KDE in m.images and TRUNK not in m.images
    ]
    return list(mark.data["plan"])


# --- the judgement before the trunk (task 1.1) ------------------------------------------


def test_an_image_with_cuts_grows_from_a_fresh_trunk_although_its_cut_free_plan_has_a_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1, E1: the bug -- the cut-free ``--emptytree`` pretend on the bare copy is a cycle."""
    wired, seen, _judged = _wire(tmp_path, monkeypatch)

    wired.assemble(recipe=_cut_chain("kde"))

    assert wired.step("trunk")["built"] == TRUNK  # the trunk installed in this run
    assert [a for a in _installs(wired.runs) if "--update" in a], wired.runs  # the image too
    # on its copy: the whole plan under the cuts, then the cut atoms alone without them
    assert _on_copy(seen) == [
        (f"{KDE}.judge", True, "emptytree"),
        (f"{KDE}.judge", False, "nodeps"),
    ], seen
    (nodeps,) = [p for p in seen if p.kind == "nodeps"]
    assert nodeps.index < _first_install(wired.runs), wired.runs  # before the trunk installs
    argv = wired.runs[nodeps.index]
    assert argv[0] == "emerge", argv
    for option in (*ISO_SETTLE_OPTIONS, "--nodeps", "--verbose", "--pretend"):
        assert option in argv, argv
    assert "--emptytree" not in argv, argv
    assert _atoms(argv) == [PIPEWIRE], argv


def test_the_shipped_instance_is_judged_before_the_trunk_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2, Q4: stale only once the cut is cleared -- the binpkg the settle installs and the
    ISO ships, refused before anything installs."""
    wired, seen, _judged = _wire(tmp_path, monkeypatch, stale_shipped=True)

    with pytest.raises(AssemblerError) as err:
        wired.assemble(recipe=_cut_chain("kde"))

    message = str(err.value)
    assert (
        "the kde image would install binpkgs built against a library its tree no longer "
        "ships; nothing was installed." in message
    ), message
    assert "x/c-3" in message and "libavcodec.so.61" in message, message
    assert _installs(wired.runs) == [], wired.runs
    # replaces story 019's [(judge, True), (judge, False)]: the cut-free side is the
    # cut atoms' --nodeps pretend, not a second --emptytree resolution
    assert _on_copy(seen) == [
        (f"{KDE}.judge", True, "emptytree"),
        (f"{KDE}.judge", False, "nodeps"),
    ], seen
    assert not (tmp_path / "scratch" / "assemble" / f"{KDE}.judge").exists()


def test_hostile_the_cut_atoms_are_judged_on_their_own_output_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GOTCHA: the --nodeps output never concatenated with the under-cut one -- a package in
    both would be judged, and reported, twice."""
    wired, _seen, judged = _wire(tmp_path, monkeypatch)

    wired.assemble(recipe=_cut_chain("kde"))

    on_copy = [text for rootfs, text in judged if rootfs == f"{KDE}.judge"]
    assert len(on_copy) == 2, judged  # one judgement per resolution
    assert list(checkpoint.plan_tokens(on_copy[0])) == UNDER_CUT, on_copy[0]
    assert list(checkpoint.plan_tokens(on_copy[1])) == ["x/c-3-7::gentoo"], on_copy[1]


def test_hostile_each_cut_atom_is_named_once_and_none_is_merged_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two cuts on one atom name it once; two atoms stay two (sorted)."""
    cuts = (
        CUT,
        UseBreak(atom=PIPEWIRE, flag="sdl", enable=False),
        UseBreak(atom="media-libs/libsdl2", flag="pipewire", enable=False),
    )
    recipe = _cut_chain("kde", *cuts)
    wired, seen, _judged = _wire(tmp_path, monkeypatch, recipe=recipe)

    wired.assemble(recipe=recipe)

    (nodeps,) = [p for p in seen if p.kind == "nodeps"]
    assert _atoms(wired.runs[nodeps.index]) == ["media-libs/libsdl2", PIPEWIRE], wired.runs


def test_a_failed_pretend_on_the_copy_keeps_emerges_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U4: not reported as a copy failure: the resolver's own error, with its output."""
    wired, _seen, _judged = _wire(tmp_path, monkeypatch, copy_pretend_fails="emptytree")

    with pytest.raises(subprocess.CalledProcessError) as err:
        wired.assemble(recipe=_cut_chain("kde"))

    assert "no binary packages to satisfy x/z" in (err.value.output or ""), err.value
    assert not (tmp_path / "scratch" / "assemble" / f"{KDE}.judge").exists()
    assert _installs(wired.runs) == [], wired.runs


def test_a_failed_cut_atom_pretend_keeps_emerges_output_and_removes_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U4 on the new resolution: the --nodeps pretend's own error, nothing installed."""
    wired, seen, _judged = _wire(tmp_path, monkeypatch, copy_pretend_fails="nodeps")

    with pytest.raises(subprocess.CalledProcessError) as err:
        wired.assemble(recipe=_cut_chain("kde"))

    assert f"no binary packages to satisfy {PIPEWIRE}" in (err.value.output or ""), err.value
    assert [p for p in seen if p.kind == "nodeps"], seen  # it was that pretend that failed
    assert not (tmp_path / "scratch" / "assemble" / f"{KDE}.judge").exists()
    assert _installs(wired.runs) == [], wired.runs


def test_an_image_without_cuts_is_judged_once_and_names_no_cut_atom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U1 guard: no cuts, no cycle -- one --emptytree judgement on the copy, no --nodeps."""
    wired, seen, _judged = _wire(tmp_path, monkeypatch, recipe=_chain("kde"), pulls=frozenset())

    wired.assemble(recipe=_chain("kde"))

    assert _on_copy(seen) == [(f"{KDE}.judge", False, "emptytree")], seen
    assert not [p for p in seen if p.kind == "nodeps"], seen


# --- the plan the INSTALL checkpoint keeps (task 1.2) -----------------------------------


def test_the_saved_plan_is_the_cut_free_one_so_the_next_run_restores_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3, U3: the cut-free plan resolved in the installed rootfs -- with ``x/d``, which only
    the cut-free USE pulls in and no --nodeps resolution can name."""
    wired, seen, _judged = _wire(tmp_path, monkeypatch)
    wired.assemble(recipe=_cut_chain("kde"))

    assert _kde_plan(wired) == CUT_FREE, _kde_plan(wired)  # what ships
    branch = next(i for i, a in enumerate(wired.runs) if "--update" in a and "--pretend" not in a)
    # resolved in the installed rootfs, after the image's install
    assert any(p.index > branch and p.shape == (KDE, False, "emptytree") for p in seen), seen

    wired.assemble(recipe=_cut_chain("kde"))

    seed = wired.step("seed")
    assert seed.get("restored") == checkpoint.PACKAGES, seed  # nothing installed again
    assert _installs(wired.runs) == [], wired.runs


def test_an_image_cut_of_its_own_saves_the_plan_resolved_after_its_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3: the image installs under its own cut and settles it; the plan resolved before the
    settle is the under-cut one -- the INSTALL checkpoint keeps the one after it."""
    recipe = _cut_chain("kde", in_image=True)
    wired, seen, _judged = _wire(
        tmp_path, monkeypatch, recipe=recipe, pulls=frozenset({"app-misc/kde"})
    )

    wired.assemble(recipe=recipe)

    assert wired.step("settle")["atoms"] == [PIPEWIRE], wired.step("settle")
    settle = next(
        i
        for i, a in enumerate(wired.runs)
        if a[:1] == ["emerge"] and "--pretend" not in a and a[-1:] == [PIPEWIRE]
    )
    after = [p for p in seen if p.index > settle and p.rootfs == KDE]
    assert [p.shape for p in after] == [(KDE, False, "emptytree")], seen  # resolved after it
    assert _kde_plan(wired) == CUT_FREE, _kde_plan(wired)

    wired.assemble(recipe=recipe)

    assert wired.step("seed").get("restored") == checkpoint.PACKAGES, wired.step("seed")
    assert _installs(wired.runs) == [], wired.runs
