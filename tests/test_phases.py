"""UNIT da camada de planejamento PURO de shidashi.phases (story 003 grupo 3 + 4.1).

Todas as funções testadas aqui são puras ou fazem apenas I/O contra um tmp dir
(sem root, sem nspawn, sem portage):

* 3.1 ``phase_target`` (rebuild→@world, desktop→@<flavor>, ``phase.sets``
  declarados→``@<nome>`` intersectados com ``recipe.sets``, senão
  ``phase.packages``) e ``phase_emerge_argv`` (--emptytree só em rebuild);
* 3.2 ``use_break_lines`` / ``write_use_break`` / ``clear_use_break`` contra um
  rootfs em tmp_path;
* 3.3 ``parse_built_atoms`` sobre saída ``emerge --verbose`` capturada;
* 3.4 ``fork_point`` (presença/ausência do tarball + composição da chave) e
  ``trunk_phase_names`` (exclui ``desktop`` no kde; todas as phases no minimal);
* 4.1 (unit) ``snapshot_fork_point`` → ``restore_fork_point`` round-trip de uma
  árvore tmp simples (conteúdo + layout preservados, escrita atômica do dest).

Contrato derivado de design.md §phases. Os símbolos de ``shidashi.phases`` e
``shidashi.recipe.UseBreak`` são importados de forma tolerante (``try_import``) só
para não abortar a coleção do pytest inteiro enquanto a impl não existe; cada
teste fica Red no uso, nomeando o símbolo pendente (Red esperado da story 003).
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import phases
from shidashi.recipe import Phase, ResolvedRecipe
from tests._pending import try_import

# the binpkg check of a shipped stage extracts the stage3's vdb: stubbed here
pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

UseBreak: Any = try_import("shidashi.recipe", "UseBreak")
phase_target: Any = try_import("shidashi.phases", "phase_target")
phase_emerge_argv: Any = try_import("shidashi.phases", "phase_emerge_argv")
use_break_lines: Any = try_import("shidashi.phases", "use_break_lines")
write_use_break: Any = try_import("shidashi.phases", "write_use_break")
clear_use_break: Any = try_import("shidashi.phases", "clear_use_break")
parse_built_atoms: Any = try_import("shidashi.phases", "parse_built_atoms")
fork_point: Any = try_import("shidashi.phases", "fork_point")
trunk_phase_names: Any = try_import("shidashi.phases", "trunk_phase_names")
stage_fork_point_path: Any = try_import("shidashi.phases", "stage_fork_point_path")
pending_breaks: Any = try_import("shidashi.phases", "pending_breaks")
snapshot_fork_point: Any = try_import("shidashi.phases", "snapshot_fork_point")
restore_fork_point: Any = try_import("shidashi.phases", "restore_fork_point")


def _recipe(
    *,
    flavor: str = "kde",
    sets: tuple[str, ...] = ("base", "graphics", "kde"),
    phases: tuple[Phase, ...] = (),
) -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor=flavor,
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=sets,
        phases=phases,
        portage_layers=("base", "arch/v3", "flavor/kde", "init/systemd"),
    )


def _ffmpeg() -> Any:
    return UseBreak(atom="media-video/ffmpeg", flag="sdl", enable=False)


def _phase(name: str, *, packages: tuple[str, ...] = (), breaks: tuple[Any, ...] = ()) -> Phase:
    # Phase ganha use_break: tuple[UseBreak] na 1.1; sob a forma antiga isto é Red.
    return Phase(name=name, packages=packages, use_break=breaks)


# --- 3.1 phase_target ---------------------------------------------------------


def test_phase_target_of_the_base_is_world_then_its_sets() -> None:
    """The full rebuild is a property of the stage (``emptytree``), not of a
    phase NAME -- the name-based rule is what tied `rebuild` to @world."""
    base = Phase(name="base", stage="base", sets=("base",), emptytree=True)
    assert phase_target(base, _recipe()) == ("@world", "@base")
    assert phase_target(Phase(name="anything", emptytree=True), _recipe()) == ("@world",)


def test_every_stage_after_the_base_updates_world_too() -> None:
    """-uDN @world @<sets>, as OVERVIEW §6.4 says. With the sets alone, a package
    an earlier stage installed is rebuilt for this stage's USE only if it sits in
    the new sets' graph: measured on the real minimal, desktop left vim, kbd and
    fastfetch with the old USE (2026-09-27)."""
    desktop = Phase(name="desktop", stage="desktop", sets=("gpu", "fonts"))
    assert phase_target(desktop, _recipe()) == ("@world", "@gpu", "@fonts")
    assert phase_target(Phase(name="desktop", stage="desktop"), _recipe()) == ("@world",)
    # a phase that is not a stage (openrc's seat) still names only its atoms
    seat = Phase(name="seat", packages=("sys-auth/elogind",))
    assert phase_target(seat, _recipe()) == ("sys-auth/elogind",)


def test_phase_target_seat_is_packages() -> None:
    phase = Phase(name="seat", packages=("sys-apps/dbus", "sys-auth/seatd"))
    assert phase_target(phase, _recipe()) == ("sys-apps/dbus", "sys-auth/seatd")


def test_phase_target_is_the_stages_own_sets_whatever_the_phase_is_called() -> None:
    """No convention by name: `desktop` used to mean @<flavor>. A flavor stage
    lists its own set like any other."""
    flavor = Phase(name="flavor", stage="kde", sets=("kde", "extra-desktop"))
    assert phase_target(flavor, _recipe(flavor="kde")) == ("@world", "@kde", "@extra-desktop")
    assert phase_target(Phase(name="desktop"), _recipe(flavor="kde")) == ()


def test_phase_target_uses_declared_sets() -> None:
    # A fase DECLARA os sets que instala; o nome da fase não importa mais.
    phase = Phase(name="qualquer-nome", sets=("base", "graphics"))
    assert phase_target(phase, _recipe()) == ("@base", "@graphics")


def test_phase_target_takes_every_set_the_stage_declares() -> None:
    # Pre-D24 the base listed each phase's intent for ANY flavor and the target
    # was filtered by the recipe's sets. Now each stage declares only what it
    # installs, so there is nothing to filter.
    phase = Phase(name="desktop", stage="desktop", sets=("gpu", "fonts"))
    assert phase_target(phase, _recipe(sets=("base",))) == ("@world", "@gpu", "@fonts")


def test_phase_target_falls_back_to_packages_when_there_are_no_sets() -> None:
    # the init-prepended `seat` phase names atoms, not sets
    phase = Phase(name="seat", packages=("sys-auth/elogind", "sys-auth/seatd"))
    assert phase_target(phase, _recipe()) == ("sys-auth/elogind", "sys-auth/seatd")


def test_phase_target_unknown_phase_falls_back_to_packages() -> None:
    phase = Phase(name="weird", packages=("cat/pkg",))
    assert phase_target(phase, _recipe(sets=())) == ("cat/pkg",)


# --- 3.1 phase_emerge_argv ----------------------------------------------------


def test_phase_emerge_argv_rebuild_has_emptytree() -> None:
    base = Phase(name="base", stage="base", sets=("base",), emptytree=True)
    argv = phase_emerge_argv(base, _recipe(), emptytree=True)
    assert argv[0] == "emerge"
    assert "--verbose" in argv
    assert "--emptytree" in argv
    assert argv[-2:] == ["@world", "@base"]
    # a phase that is not the base never gets --emptytree, whatever it is called
    other = Phase(name="rebuild", sets=("gpu",))
    assert "--emptytree" not in phase_emerge_argv(other, _recipe(), emptytree=True)


def test_phase_emerge_argv_always_reuses_binpkgs_of_the_generation() -> None:
    """--usepkg on every stage: the PKGDIR is one generation's (D26), checked
    by its fingerprint before the first phase, so a matching binpkg is safe."""
    for phase in (
        Phase(name="base", stage="base", sets=("base",), emptytree=True),
        Phase(name="minimal", stage="minimal", sets=("extra-system",)),
    ):
        assert phase_emerge_argv(phase, _recipe(), emptytree=True)[:3] == [
            "emerge",
            "--verbose",
            "--usepkg",
        ]


def test_phase_emerge_argv_non_rebuild_has_no_emptytree() -> None:
    argv = phase_emerge_argv(Phase(name="graphics", sets=("graphics",)), _recipe(), emptytree=True)
    assert "--emptytree" not in argv
    assert "@graphics" in argv


def test_phase_emerge_argv_emptytree_false_never_emits() -> None:
    argv = phase_emerge_argv(Phase(name="rebuild"), _recipe(), emptytree=False)
    assert "--emptytree" not in argv


# --- 3.2 use_break_lines / write / clear -------------------------------------


def test_use_break_lines_renders_forced_off() -> None:
    phase = _phase("graphics", breaks=(_ffmpeg(),))
    assert use_break_lines(phase) == ("media-video/ffmpeg -sdl",)


def test_use_break_lines_enable_true_has_no_dash() -> None:
    phase = _phase("graphics", breaks=(UseBreak(atom="x/y", flag="z", enable=True),))
    assert use_break_lines(phase) == ("x/y z",)


def test_use_break_lines_empty_phase() -> None:
    assert use_break_lines(Phase(name="rebuild")) == ()


def test_write_use_break_creates_file(tmp_path: Path) -> None:
    phase = _phase("graphics", breaks=(_ffmpeg(),))
    written = write_use_break(tmp_path, phase)
    assert written is not None
    assert written == tmp_path / "etc" / "portage" / "package.use" / "zz-shidashi-use-break"
    assert written.exists()
    assert "media-video/ffmpeg -sdl" in written.read_text(encoding="utf-8")


def test_write_use_break_empty_phase_returns_none(tmp_path: Path) -> None:
    assert write_use_break(tmp_path, Phase(name="rebuild")) is None


def test_clear_use_break_idempotent(tmp_path: Path) -> None:
    phase = _phase("graphics", breaks=(_ffmpeg(),))
    written = write_use_break(tmp_path, phase)
    assert written is not None and written.exists()
    clear_use_break(tmp_path)
    assert not written.exists()
    # idempotente: limpar de novo não levanta
    clear_use_break(tmp_path)
    assert not written.exists()


# --- 3.3 parse_built_atoms ----------------------------------------------------

_EMERGE_KDE_GRAPHICS = """\
These are the packages that would be merged, in order:

Calculating dependencies... done!
[ebuild  N    ] media-libs/libsdl2-2.30.5:0/0::gentoo  USE="..."
[ebuild  N    ] media-video/ffmpeg-6.1.1-r1:0/58::gentoo  USE="-sdl"
[ebuild  R    ] media-libs/mesa-24.0.7  USE="..."

>>> Emerging (1 of 3) media-libs/libsdl2-2.30.5
"""


def test_parse_built_atoms_from_kde_graphics_fixture() -> None:
    atoms = parse_built_atoms(_EMERGE_KDE_GRAPHICS)
    assert atoms == (
        "media-libs/libsdl2-2.30.5",
        "media-video/ffmpeg-6.1.1-r1",
        "media-libs/mesa-24.0.7",
    )


def test_parse_built_atoms_empty_on_no_merge() -> None:
    assert parse_built_atoms("Nothing to merge; quitting.\n") == ()


# --- 3.4 fork_point + trunk_phase_names --------------------------------------


def test_fork_point_returns_none_when_absent(tmp_path: Path) -> None:
    assert fork_point(_recipe(), snapshot="20260524", fork_points_dir=tmp_path) is None


def _chain_recipe(flavor: str = "kde") -> ResolvedRecipe:
    """A kde-shaped chain: base (cuts) → minimal (ships) → desktop → flavor (ships)."""
    trunk_cut = UseBreak(atom="dev-lang/python", flag="bluetooth", enable=False)
    return _recipe(
        flavor=flavor,
        phases=(
            Phase(
                name="base", stage="base", sets=("base",), emptytree=True, use_break=(trunk_cut,)
            ),
            Phase(name="minimal", stage="minimal", sets=("extra-system",), ships=True),
            Phase(name="desktop", stage="desktop", sets=("gpu",)),
            Phase(name="flavor", stage=flavor, sets=(flavor,), ships=True),
        ),
    )


def test_stage_fork_point_key_has_no_target_so_images_share_it(tmp_path: Path) -> None:
    """F70: the old key carried the flavor, so the trunk was never shared."""
    kde = stage_fork_point_path(
        _chain_recipe("kde"), "desktop", snapshot="S", fork_points_dir=tmp_path
    )
    gnome = stage_fork_point_path(
        _chain_recipe("gnome"), "desktop", snapshot="S", fork_points_dir=tmp_path
    )
    assert kde == gnome == tmp_path / "v3-systemd-S-desktop.tar"


def test_fork_point_resumes_from_the_deepest_stage_before_the_target(tmp_path: Path) -> None:
    recipe = _chain_recipe()
    for stage in ("base", "desktop"):
        (tmp_path / f"v3-systemd-S-{stage}.tar").write_bytes(b"")
    found = fork_point(recipe, snapshot="S", fork_points_dir=tmp_path)
    assert found is not None
    phase, path = found
    assert (phase.name, path.name) == ("desktop", "v3-systemd-S-desktop.tar")


def test_fork_point_never_restores_the_target_itself(tmp_path: Path) -> None:
    """Asking for an image is asking to build its last stage."""
    (tmp_path / "v3-systemd-S-kde.tar").write_bytes(b"")
    assert fork_point(_chain_recipe(), snapshot="S", fork_points_dir=tmp_path) is None


def test_pending_breaks_resets_at_every_shipped_stage() -> None:
    recipe = _chain_recipe()
    assert [b.atom for b in pending_breaks(recipe, through="base")] == ["dev-lang/python"]
    assert pending_breaks(recipe, through="minimal") == ()  # minimal ships: settled
    assert pending_breaks(recipe, through="desktop") == ()
    assert pending_breaks(recipe, through=None) == ()


class _RecordingContainer:
    """Records every emerge; the rootfs is a real tmp dir so cuts can be written."""

    def __init__(self, rootfs: Path) -> None:
        self.rootfs = rootfs
        self.emerge_calls: list[list[str]] = []

    def run(self, argv: Any, **_k: Any) -> Any:
        from shidashi.container import CommandResult

        self.emerge_calls.append(list(argv))
        return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")


def _run_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    snaps: list[str] = []
    monkeypatch.setattr(
        phases, "snapshot_fork_point", lambda _root, dest: snaps.append(dest.name) or dest
    )
    container = _RecordingContainer(tmp_path / "rootfs")
    # the settle only redoes cuts on INSTALLED packages: python is in the image
    (tmp_path / "rootfs" / "var/db/pkg/dev-lang/python-3.14.7").mkdir(parents=True)
    results = phases.run_phases(
        container,
        _chain_recipe(),
        emptytree=True,
        snapshot="S",
        fork_points_dir=tmp_path,
        **kw,
    )
    return results, container, snaps


def test_run_phases_settles_each_shipped_stage_and_snapshots_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D24: minimal is settled on the way to kde, and desktop starts from it settled."""
    results, container, snaps = _run_chain(tmp_path, monkeypatch)
    assert [(r.phase.name, r.phase.stage) for r in results] == [
        ("base", "base"),
        ("minimal", "minimal"),
        ("settle", "minimal"),
        ("desktop", "desktop"),
        ("flavor", "kde"),
        ("settle", "kde"),
    ]
    assert snaps == [f"v3-systemd-S-{s}.tar" for s in ("base", "minimal", "desktop", "kde")]
    # kde's settle has no pending cut, so it runs no emerge
    (
        base,
        minimal,
        settle_minimal,
        check_minimal,
        check_minimal_settle,
        desktop,
        _flavor,
        check_kde,
        check_kde_settle,
    ) = container.emerge_calls
    assert base[:4] == ["emerge", "--verbose", "--usepkg", "--emptytree"]
    # each shipped image is resolved as the assembler will -- against the
    # stage3's vdb, under the chain's cuts, then the settle of the cut packages
    root = "--root=/var/tmp/shidashi-iso-root"
    pretend = [
        "emerge",
        "--pretend",
        root,
        "--usepkgonly",
        "--binpkg-respect-use=y",
        "--emptytree",
        "@system",
    ]
    settle_check = [
        "emerge",
        "--pretend",
        root,
        "--usepkgonly",
        "--binpkg-respect-use=y",
        "--oneshot",
        "--nodeps",
        "dev-lang/python",
    ]
    assert check_minimal == [*pretend, "@base", "@extra-system"]
    assert check_kde == [*pretend, "@base", "@extra-system", "@gpu", "@kde"]
    # the base's cut was settled at minimal, but a fresh stage3 meets its cycle
    # again: every image replays it
    assert check_minimal_settle == check_kde_settle == settle_check
    assert minimal[3:6] == desktop[3:6] == ["--update", "--deep", "--newuse"]
    # minimal's settle undoes the trunk cut, which rode the base phase
    assert settle_minimal[-1] == "dev-lang/python"
    assert not (
        tmp_path / "rootfs" / "etc" / "portage" / "package.use" / "zz-shidashi-use-break"
    ).exists()


def test_run_phases_resumed_from_desktop_builds_only_the_flavor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results, container, snaps = _run_chain(tmp_path, monkeypatch, resume_at="desktop")
    assert [r.phase.name for r in results] == ["flavor", "settle"]
    # the settle has no pending cut to redo; the flavor's image is checked
    flavor, check, check_settle = container.emerge_calls
    assert "--pretend" not in flavor
    assert "--emptytree" in check and "--nodeps" in check_settle
    assert snaps == ["v3-systemd-S-kde.tar"]


def test_trunk_is_everything_up_to_and_including_the_base() -> None:
    """The trunk -- fork point 1 of base → minimal → desktop → flavor -- is what
    every image of one arch × init shares: the init's prepended phases and the
    base, the only full rebuild."""
    phases = (
        Phase(name="seat", packages=("sys-auth/seatd",)),
        Phase(name="base", stage="base", emptytree=True),
        Phase(name="minimal", stage="minimal", ships=True),
        Phase(name="desktop", stage="desktop"),
        Phase(name="flavor", stage="kde", ships=True),
    )
    recipe = _recipe(flavor="kde", sets=("kde",), phases=phases)
    assert trunk_phase_names(recipe) == ("seat", "base")


def test_trunk_phase_names_all_phases_for_minimal() -> None:
    phases = (Phase(name="rebuild"), Phase(name="graphics"), Phase(name="apps"))
    recipe = _recipe(flavor="minimal", sets=(), phases=phases)
    assert trunk_phase_names(recipe) == ("rebuild", "graphics", "apps")


# --- 4.1 (unit) snapshot/restore round-trip ----------------------------------


def test_snapshot_then_restore_round_trips_tree(tmp_path: Path) -> None:
    src = tmp_path / "rootfs"
    (src / "etc" / "portage").mkdir(parents=True)
    (src / "etc" / "portage" / "make.conf").write_text("FEATURES=buildpkg\n", encoding="utf-8")
    (src / "var").mkdir()
    (src / "var" / "marker").write_text("hello-trunk", encoding="utf-8")

    dest = tmp_path / "fork-points" / "v3-minimal-systemd-SNAP.tar"
    dest.parent.mkdir(parents=True)
    returned = snapshot_fork_point(src, dest)
    assert returned == dest
    assert dest.exists()

    restored = tmp_path / "restored"
    restored.mkdir()
    restore_fork_point(dest, restored)
    assert (restored / "etc" / "portage" / "make.conf").read_text(
        encoding="utf-8"
    ) == "FEATURES=buildpkg\n"
    assert (restored / "var" / "marker").read_text(encoding="utf-8") == "hello-trunk"


def test_snapshot_writes_atomically_no_partial_temp(tmp_path: Path) -> None:
    src = tmp_path / "rootfs"
    src.mkdir()
    (src / "file").write_text("x", encoding="utf-8")
    dest = tmp_path / "out.tar"
    snapshot_fork_point(src, dest)
    # escrita atômica: o destino existe e nenhum temp pendente fica ao lado
    assert dest.exists()
    leftovers = [p for p in tmp_path.iterdir() if p != dest and p != src]
    assert leftovers == []


# --- regression: a rootfs keeps its special modes through a fork point ----------


def _special_tree(root: Path) -> None:
    (root / "tmp").mkdir(parents=True)
    (root / "tmp").chmod(0o1777)
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "su").write_text("x")
    (root / "usr" / "bin" / "su").chmod(0o4755)
    (root / "var" / "cache" / "distfiles").mkdir(parents=True)
    (root / "var" / "cache" / "distfiles").chmod(0o2775)


def _modes(root: Path) -> dict[str, str]:
    return {
        rel: oct((root / rel).stat().st_mode & 0o7777)
        for rel in ("tmp", "usr/bin/su", "var/cache/distfiles")
    }


def test_fork_point_round_trip_keeps_sticky_setuid_and_group_write(tmp_path: Path) -> None:
    """Python's tarfile filter="tar" cleared these bits: /tmp came back 0755 and
    su lost its setuid, and locale-gen aborted in the first real run (2026-09-26)."""
    src, restored = tmp_path / "src", tmp_path / "restored"
    _special_tree(src)
    restored.mkdir()
    snapshot_fork_point(src, tmp_path / "fp.tar")
    restore_fork_point(tmp_path / "fp.tar", restored)
    assert _modes(restored) == {
        "tmp": "0o1777",
        "usr/bin/su": "0o4755",
        "var/cache/distfiles": "0o2775",
    }


# --- regression: settle re-emerges only what is installed ------------------------


class _SettleContainer:
    def __init__(self, rootfs: Path) -> None:
        self.rootfs = rootfs
        self.calls: list[list[str]] = []

    def run(self, argv: Any, **_k: Any) -> Any:
        from shidashi.container import CommandResult

        self.calls.append(list(argv))
        return CommandResult(0, "", "")


def test_settle_pass_skips_cut_packages_that_are_not_installed(tmp_path: Path) -> None:
    """The first real run (2026-09-27) failed here: the base's cut
    `media-video/pipewire -ffmpeg` was settled with `emerge --oneshot pipewire`
    in minimal, where pipewire is not installed (the audio server arrives at the
    desktop stage, D24) -- so the settle tried to INSTALL it. A cut whose package
    is absent has nothing to undo."""
    settle_pass: Any = try_import("shidashi.phases", "settle_pass")
    for cpv in (
        "dev-lang/python-3.14.7",
        "dev-lang/python-exec-2.4.10",
        "dev-python/pillow-12.3.0",
        "media-video/pipewire-common-1",
    ):
        (tmp_path / "var/db/pkg" / cpv).mkdir(parents=True)
    c = _SettleContainer(tmp_path)
    breaks = (
        UseBreak(atom="dev-lang/python", flag="bluetooth", enable=False),
        UseBreak(atom="dev-python/pillow", flag="truetype", enable=False),
        UseBreak(atom="media-video/pipewire", flag="ffmpeg", enable=False),
    )
    settle_pass(c, _recipe(flavor="minimal"), breaks, stage="minimal")
    assert c.calls == [
        ["emerge", "--verbose", "--newuse", "--oneshot", "dev-lang/python", "dev-python/pillow"]
    ]


def test_settle_pass_with_no_cut_package_installed_runs_nothing(tmp_path: Path) -> None:
    settle_pass: Any = try_import("shidashi.phases", "settle_pass")
    c = _SettleContainer(tmp_path)
    breaks = (UseBreak(atom="media-video/pipewire", flag="ffmpeg", enable=False),)
    result = settle_pass(c, _recipe(flavor="minimal"), breaks, stage="minimal")
    assert c.calls == []
    assert result.built_atoms == ()


def test_parse_reused_atoms_reads_binary_lines_apart_from_built_ones() -> None:
    """A stage installed from binpkgs reported "—" as built: minimal installed 92
    binaries in the third run (2026-09-27) and the report looked empty."""
    parse_reused_atoms: Any = try_import("shidashi.phases", "parse_reused_atoms")
    out = (
        "[binary     N    ] acct-group/tss-0-r3::gentoo  0 KiB\n"
        '[ebuild   R    ] sys-apps/kbd-2.10.0::gentoo  USE="xkb*" 1.747 KiB\n'
        '[binary   R    ] dev-lang/python-3.14.7-1:3.14::gentoo  USE="-bluetooth" 0 KiB\n'
    )
    assert parse_reused_atoms(out) == ("acct-group/tss-0-r3", "dev-lang/python-3.14.7-1")
    assert parse_built_atoms(out) == ("sys-apps/kbd-2.10.0",)


def test_run_phases_stop_after_a_stage_ends_with_its_fork_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--stop-after desktop: resume from minimal, build ONLY the desktop stage and
    snapshot it -- the next run without it resumes from that fork point."""
    results, container, snaps = _run_chain(
        tmp_path, monkeypatch, resume_at="minimal", stop_after="desktop"
    )
    assert [(r.phase.name, r.phase.stage) for r in results] == [("desktop", "desktop")]
    assert snaps == ["v3-systemd-S-desktop.tar"]
    assert len(container.emerge_calls) == 1


def test_run_phases_stop_after_a_shipped_stage_includes_its_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results, _container, snaps = _run_chain(tmp_path, monkeypatch, stop_after="minimal")
    assert [(r.phase.name, r.phase.stage) for r in results] == [
        ("base", "base"),
        ("minimal", "minimal"),
        ("settle", "minimal"),
    ]
    assert snaps == ["v3-systemd-S-base.tar", "v3-systemd-S-minimal.tar"]


# --- the stage steps come from variants/flow.yaml ---------------------------------


def _flow_with(**changes: Any) -> Any:
    from shidashi.flow import StagesFlow

    base = phases.stages_flow().model_dump()
    base.update(changes)
    return StagesFlow.model_validate(base)


def test_an_option_added_in_the_flow_reaches_the_stage_emerge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding e.g. --keep-going is an edit of flow.yaml, not of Python."""
    flow = phases.stages_flow()
    edited = _flow_with(
        emerge={**flow.emerge.model_dump(), "options": (*flow.emerge.options, "--keep-going")}
    )
    monkeypatch.setattr(phases, "stages_flow", lambda: edited)
    base = Phase(name="base", stage="base", sets=("base",), emptytree=True)
    argv = phase_emerge_argv(base, _recipe(), emptytree=True)
    assert argv[:4] == ["emerge", "--verbose", "--usepkg", "--keep-going"]


def test_the_steps_after_the_emerge_run_in_the_declared_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declared snapshot-before-settle, the fork point is taken first."""
    steps = [s.model_dump() for s in phases.stages_flow().steps]
    settle = next(s for s in steps if s["do"] == "settle")
    snap = next(s for s in steps if s["do"] == "snapshot")
    rest = [s for s in steps if s["do"] not in ("settle", "snapshot", "check-binpkgs")]
    order = [*rest, snap, settle]
    edited = _flow_with(steps=order)
    monkeypatch.setattr(phases, "stages_flow", lambda: edited)
    events: list[str] = []
    monkeypatch.setattr(
        phases, "snapshot_fork_point", lambda _root, dest: events.append(f"snap:{dest.name}")
    )
    real_settle = phases.settle_pass

    def _settle(*a: Any, **k: Any) -> Any:
        events.append("settle")
        return real_settle(*a, **k)

    monkeypatch.setattr(phases, "settle_pass", _settle)
    container = _RecordingContainer(tmp_path / "rootfs")
    phases.run_phases(
        container,
        _chain_recipe(),
        emptytree=True,
        snapshot="S",
        fork_points_dir=tmp_path,
        stop_after="minimal",
    )
    assert events == ["snap:v3-systemd-S-base.tar", "snap:v3-systemd-S-minimal.tar", "settle"]


def test_the_flow_refuses_stage_steps_that_make_no_sense() -> None:
    from pydantic import ValidationError

    flow = phases.stages_flow()
    steps = [s.model_dump() for s in flow.steps]
    no_settle = [s for s in steps if s["do"] != "settle"]
    with pytest.raises(ValidationError, match="`settle` exactly once"):
        _flow_with(steps=no_settle)
    emerge_first = sorted(steps, key=lambda s: s["do"] != "emerge-stage")
    with pytest.raises(ValidationError, match="must come before emerge-stage"):
        _flow_with(steps=emerge_first)


def test_shipped_sets_are_those_of_the_stages_up_to_the_image() -> None:
    recipe = _chain_recipe()
    assert phases.shipped_sets(recipe, "minimal") == ("base", "extra-system")
    assert phases.shipped_sets(recipe, "kde") == ("base", "extra-system", "gpu", "kde")
    with pytest.raises(ValueError, match="gnome"):
        phases.shipped_sets(recipe, "gnome")


def test_the_binpkg_check_resolves_with_the_assemblers_own_options() -> None:
    """The check and the ISO cannot drift apart: same options, same targets."""
    from shidashi.assembler import iso_emerge_argv

    recipe = _chain_recipe()
    recipe = recipe.model_copy(update={"sets": phases.shipped_sets(recipe, "kde")})
    check = phases.binpkg_check_argv(recipe, "kde", root="/r")
    iso = iso_emerge_argv(recipe)
    assert check[:3] == ["emerge", "--pretend", "--root=/r"]
    assert [a for a in check if a not in ("--pretend", "--root=/r")] == [
        a for a in iso if a != "--verbose"
    ]


def test_a_missing_binpkg_fails_the_stage_naming_the_package(tmp_path: Path) -> None:
    """The first kde ISO stopped on ccache, installed by the bootstrap without a
    binpkg (2026-09-29); the factory must stop on it instead."""
    import subprocess

    class _NoBinpkg(_RecordingContainer):
        def run(self, argv: Any, **_k: Any) -> Any:
            if "--pretend" in argv:
                raise subprocess.CalledProcessError(
                    1,
                    argv,
                    output="",
                    stderr='emerge: there are no binary packages to satisfy "dev-util/ccache".\n',
                )
            return super().run(argv)

    with pytest.raises(phases.FactoryError, match="no binpkg for dev-util/ccache") as err:
        phases.check_binpkgs(_NoBinpkg(tmp_path), _chain_recipe(), "kde")  # type: ignore[arg-type]
    assert err.value.phase == "kde:binpkgs"
    assert "dev-util/ccache" in err.value.output


def test_the_flow_refuses_a_binpkg_check_before_the_settle() -> None:
    steps = [s.model_dump() for s in phases.stages_flow().steps]
    check = next(s for s in steps if s["do"] == "check-binpkgs")
    order = [check, *(s for s in steps if s is not check)]
    with pytest.raises(ValueError, match="after settle"):
        _flow_with(steps=order)


def test_the_binpkg_check_replays_the_assemblers_two_passes(
    tmp_path: Path, no_stage3_vdb: list[Path]
) -> None:
    """F76: against the stage3's vdb, under the cuts, then the settle -- and it
    leaves nothing behind in the build rootfs (the fork point is already taken,
    but a later stage builds on this rootfs)."""
    cut_file = tmp_path / "etc/portage/package.use/zz-shidashi-use-break"
    seen: list[tuple[list[str], str]] = []

    class _Witness(_RecordingContainer):
        def run(self, argv: Any, **_k: Any) -> Any:
            seen.append((list(argv), cut_file.read_text() if cut_file.is_file() else ""))
            return super().run(argv)

    phases.check_binpkgs(_Witness(tmp_path), _chain_recipe(), "kde")  # type: ignore[arg-type]

    assert no_stage3_vdb == [tmp_path / "var/tmp/shidashi-iso-root"]
    (install, cuts_then), (settle, cuts_after) = seen
    assert "--emptytree" in install and cuts_then == "dev-lang/python -bluetooth\n"
    assert settle[-2:] == ["--nodeps", "dev-lang/python"] and cuts_after == ""
    assert not cut_file.exists()
    assert not (tmp_path / "var/tmp/shidashi-iso-root").exists()


def test_a_cycle_with_no_cut_fails_the_stage_and_cleans_up(
    tmp_path: Path, no_stage3_vdb: list[Path]
) -> None:
    import subprocess

    class _Cycle(_RecordingContainer):
        def run(self, argv: Any, **_k: Any) -> Any:
            raise subprocess.CalledProcessError(
                1, argv, output=" * Error: circular dependencies:\n", stderr=""
            )

    with pytest.raises(phases.FactoryError, match="cycle with no cut") as err:
        phases.check_binpkgs(_Cycle(tmp_path), _chain_recipe(), "kde")  # type: ignore[arg-type]
    assert err.value.phase == "kde:binpkgs"
    assert not (tmp_path / "etc/portage/package.use/zz-shidashi-use-break").exists()
    assert not (tmp_path / "var/tmp/shidashi-iso-root").exists()


def test_image_cuts_cover_the_stages_up_to_the_image_or_the_whole_chain() -> None:
    recipe = _chain_recipe()
    trunk = UseBreak(atom="dev-lang/python", flag="bluetooth", enable=False)
    assert phases.image_cuts(recipe, "base") == (trunk,)
    assert phases.image_cuts(recipe, "kde") == (trunk,)
    assert phases.image_cuts(recipe) == (trunk,)


def test_every_stage_and_step_is_in_the_audit_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mandatory audit trail: one step per stage, one per step of the stage in
    flow.yaml's order, and each stage's packages attached."""
    from shidashi import audit

    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        _run_chain(tmp_path, monkeypatch)
    manifest = audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))
    steps = [s["step"] for s in manifest["steps"]]
    assert steps[:5] == [
        "stage:base/apply-config",
        "stage:base/write-cuts",
        "stage:base/emerge-stage",
        "stage:base/module-rebuild",
        "stage:base/fork-point",
    ]
    assert "stage:minimal/settle" in steps and "stage:minimal/check-binpkgs" in steps
    assert steps[-1] == "stage:kde"
    emerge = next(s for s in manifest["steps"] if s["step"] == "stage:base/emerge-stage")
    assert emerge["argv"][:4] == ["emerge", "--verbose", "--usepkg", "--emptytree"]
    assert {"packages-base.json", "packages-kde.json"} <= set(manifest["attachments"])


# --- module-rebuild (F83): modules for a kernel that is not installed ---------------


def _kernel_rootfs(root: Path) -> Path:
    """gentoo-kernel-bin installed; nvidia-drivers' modules from a binpkg built
    for gentoo-kernel (the 2026-10-01 factory)."""
    modules = root / "usr/lib/modules"
    (modules / "7.2.6-gentoo-dist-bin").mkdir(parents=True)
    (modules / "7.2.6-gentoo-dist-bin/vmlinuz").write_bytes(b"kernel")
    (modules / "7.2.6-gentoo-dist/video").mkdir(parents=True)
    (modules / "7.2.6-gentoo-dist/video/nvidia.ko").write_bytes(b"ko")
    (modules / "7.2.6-gentoo-dist/modules.dep").write_text("")
    vdb = root / "var/db/pkg"
    (vdb / "x11-drivers/nvidia-drivers-615.71.09").mkdir(parents=True)
    (vdb / "x11-drivers/nvidia-drivers-615.71.09/CONTENTS").write_text(
        "dir /lib/modules/7.2.6-gentoo-dist\n"
        "obj /lib/modules/7.2.6-gentoo-dist/video/nvidia.ko abc 1\n"
    )
    (vdb / "sys-kernel/gentoo-kernel-bin-7.2.6").mkdir(parents=True)
    (vdb / "sys-kernel/gentoo-kernel-bin-7.2.6/CONTENTS").write_text(
        "obj /usr/lib/modules/7.2.6-gentoo-dist-bin/vmlinuz abc 1\n"
    )
    return root


class _RebuildContainer:
    """Runs nothing: an emerge "rebuilds" nvidia-drivers against the -bin kernel
    (its CONTENTS moves); rm removes the directory on the host path."""

    def __init__(self, rootfs: Path, *, rebuild_moves: bool = True) -> None:
        self.rootfs = rootfs
        self.calls: list[list[str]] = []
        self.rebuild_moves = rebuild_moves

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> Any:
        import shutil

        from shidashi.container import CommandResult

        self.calls.append(list(argv))
        if argv[0] == "emerge" and self.rebuild_moves:
            contents = self.rootfs / "var/db/pkg/x11-drivers/nvidia-drivers-615.71.09/CONTENTS"
            contents.write_text("obj /lib/modules/7.2.6-gentoo-dist-bin/video/nvidia.ko d 2\n")
        if argv[0] == "rm":
            for path in argv[2:]:
                shutil.rmtree(self.rootfs / path.lstrip("/"))
        return CommandResult(0, "", "")


def test_stale_module_dirs_are_the_ones_without_a_kernel(tmp_path: Path) -> None:
    root = _kernel_rootfs(tmp_path)
    assert phases.stale_module_dirs(root) == ("7.2.6-gentoo-dist",)
    assert phases.module_owners(root, ("7.2.6-gentoo-dist",)) == (
        "x11-drivers/nvidia-drivers-615.71.09",
    )
    assert phases.stale_module_dirs(tmp_path / "empty") == ()


def test_no_kernel_installed_means_nothing_to_compare(tmp_path: Path) -> None:
    (tmp_path / "usr/lib/modules/7.2.6-gentoo-dist").mkdir(parents=True)
    assert phases.stale_module_dirs(tmp_path) == ()


def test_module_rebuild_rebuilds_the_owners_from_source_and_drops_the_dir(
    tmp_path: Path,
) -> None:
    root = _kernel_rootfs(tmp_path)
    container = _RebuildContainer(root)
    done = phases.module_rebuild(container, phase="desktop")  # type: ignore[arg-type]
    assert done == {
        "stale": ["7.2.6-gentoo-dist"],
        "rebuilt": ["x11-drivers/nvidia-drivers-615.71.09"],
    }
    assert container.calls[0] == [
        "emerge",
        "--oneshot",
        "--verbose",
        "--usepkg=n",
        "=x11-drivers/nvidia-drivers-615.71.09",
    ]
    assert not (root / "usr/lib/modules/7.2.6-gentoo-dist").exists()
    assert (root / "usr/lib/modules/7.2.6-gentoo-dist-bin/vmlinuz").exists()


def test_module_rebuild_is_a_no_op_when_every_module_matches(tmp_path: Path) -> None:
    root = _kernel_rootfs(tmp_path)
    container = _RebuildContainer(root)
    phases.module_rebuild(container, phase="desktop")  # type: ignore[arg-type]
    container.calls.clear()
    assert phases.module_rebuild(container, phase="kde") == {"stale": []}  # type: ignore[arg-type]
    assert container.calls == []


def test_module_rebuild_fails_when_the_rebuild_did_not_move_the_modules(tmp_path: Path) -> None:
    root = _kernel_rootfs(tmp_path)
    container = _RebuildContainer(root, rebuild_moves=False)
    with pytest.raises(phases.FactoryError, match="still owned by x11-drivers/nvidia-drivers"):
        phases.module_rebuild(container, phase="desktop")  # type: ignore[arg-type]
    assert (root / "usr/lib/modules/7.2.6-gentoo-dist").exists()  # kept for diagnosis


def test_the_flow_runs_module_rebuild_before_the_fork_point() -> None:
    from shidashi.flow import StagesFlow

    kinds = [s.do for s in phases.stages_flow().steps]
    assert kinds.index("emerge-stage") < kinds.index("module-rebuild") < kinds.index("snapshot")
    bad = phases.stages_flow().model_dump()
    steps = [s for s in bad["steps"] if s["do"] != "module-rebuild"]
    steps.append({"name": "modules", "do": "module-rebuild"})  # after snapshot
    bad["steps"] = steps
    with pytest.raises(ValueError, match="module-rebuild must come after emerge-stage"):
        StagesFlow.model_validate(bad)
