"""UNIT da camada de planejamento PURO de shidashi.phases (story 003 grupo 3 + 4.1).

Todas as funções testadas aqui são puras ou fazem apenas I/O contra um tmp dir
(sem root, sem nspawn, sem portage):

* 3.1 ``phase_target`` (rebuild→@world, seat→packages, desktop→@<flavor>,
  apps→@bentoo-apps, set-named→@<name>) e ``phase_emerge_argv`` (--emptytree só
  em rebuild);
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

from shidashi.recipe import Phase, ResolvedRecipe, ResolvedUse
from tests._pending import try_import

UseBreak: Any = try_import("shidashi.recipe", "UseBreak")
phase_target: Any = try_import("shidashi.phases", "phase_target")
phase_emerge_argv: Any = try_import("shidashi.phases", "phase_emerge_argv")
use_break_lines: Any = try_import("shidashi.phases", "use_break_lines")
write_use_break: Any = try_import("shidashi.phases", "write_use_break")
clear_use_break: Any = try_import("shidashi.phases", "clear_use_break")
parse_built_atoms: Any = try_import("shidashi.phases", "parse_built_atoms")
fork_point: Any = try_import("shidashi.phases", "fork_point")
trunk_phase_names: Any = try_import("shidashi.phases", "trunk_phase_names")
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
        use=ResolvedUse(enabled=(), disabled=()),
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


def test_phase_target_rebuild_is_world() -> None:
    assert phase_target(Phase(name="rebuild"), _recipe()) == ("@world",)


def test_phase_target_seat_is_packages() -> None:
    phase = Phase(name="seat", packages=("sys-apps/dbus", "sys-auth/seatd"))
    assert phase_target(phase, _recipe()) == ("sys-apps/dbus", "sys-auth/seatd")


def test_phase_target_desktop_is_flavor_set() -> None:
    assert phase_target(Phase(name="desktop"), _recipe(flavor="kde")) == ("@kde",)


def test_phase_target_uses_declared_sets() -> None:
    # A fase DECLARA os sets que instala; o nome da fase não importa mais.
    phase = Phase(name="qualquer-nome", sets=("base", "graphics"))
    assert phase_target(phase, _recipe()) == ("@base", "@graphics")


def test_phase_target_filters_sets_not_in_recipe() -> None:
    # base.yaml enumera a intenção da fase para QUALQUER flavor; um flavor que
    # não declara o set simplesmente não o instala, em vez de pedir um @ausente.
    phase = Phase(name="graphics", sets=("graphics", "gpu"))
    assert phase_target(phase, _recipe(sets=("base", "graphics"))) == ("@graphics",)


def test_phase_target_falls_back_to_packages_when_no_set_matches() -> None:
    phase = Phase(name="graphics", sets=("gpu",), packages=("cat/pkg",))
    assert phase_target(phase, _recipe(sets=("base",))) == ("cat/pkg",)


def test_phase_target_unknown_phase_falls_back_to_packages() -> None:
    phase = Phase(name="weird", packages=("cat/pkg",))
    assert phase_target(phase, _recipe(sets=())) == ("cat/pkg",)


# --- 3.1 phase_emerge_argv ----------------------------------------------------


def test_phase_emerge_argv_rebuild_has_emptytree() -> None:
    argv = phase_emerge_argv(Phase(name="rebuild"), _recipe(), emptytree=True)
    assert argv[0] == "emerge"
    assert "--verbose" in argv
    assert "--emptytree" in argv
    assert "@world" in argv


def test_phase_emerge_argv_non_rebuild_has_no_emptytree() -> None:
    argv = phase_emerge_argv(
        Phase(name="graphics", sets=("graphics",)), _recipe(), emptytree=True
    )
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


def test_fork_point_returns_path_when_present(tmp_path: Path) -> None:
    expected = tmp_path / "v3-kde-systemd-20260524.tar"
    expected.write_bytes(b"")
    got = fork_point(_recipe(), snapshot="20260524", fork_points_dir=tmp_path)
    assert got == expected


def test_fork_point_key_includes_arch_flavor_init_snapshot(tmp_path: Path) -> None:
    (tmp_path / "v3-kde-systemd-SNAP.tar").write_bytes(b"")
    got = fork_point(_recipe(flavor="kde"), snapshot="SNAP", fork_points_dir=tmp_path)
    assert got is not None
    name = got.name
    assert "v3" in name and "kde" in name and "systemd" in name and "SNAP" in name


def test_trunk_phase_names_excludes_desktop_for_kde() -> None:
    phases = (
        Phase(name="rebuild"),
        Phase(name="graphics"),
        Phase(name="desktop"),
        Phase(name="apps"),
    )
    recipe = _recipe(flavor="kde", sets=("kde",), phases=phases)
    names = trunk_phase_names(recipe)
    assert "desktop" not in names
    assert names[0] == "rebuild"
    assert "graphics" in names


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
