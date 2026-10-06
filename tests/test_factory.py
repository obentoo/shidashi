"""UNIT + INTEGRATION tests of shidashi.factory (story 003 groups 5 and 6).

UNIT (deterministic, non-Gentoo CI):
* 6.1 ``_build_binds`` maps pkgdir/ccache/sccache/distdir → the container's fixed
  paths as RW and the repos as RO (``resolve.bind_repos`` monkeypatched);
  ``FactoryResult``/``FactoryError`` are frozen/typed;
* 6.2 (unit) ``Factory.build`` raises before any work when
  not root (``os.geteuid`` monkeypatched);
* 5.2 (unit) ``settle_pass`` with empty ``breaks`` is a no-op (the container is
  monkeypatched to guarantee no emerge is called).

INTEGRATION (host-gated, ``@pytest.mark.skipif`` non-root/non-Gentoo): they exercise the
real privileged path (nspawn + emerge + snapshot) on the cached ``base`` fork point,
restored into each test's tmp_path (story 015). In CI/sandbox they are SKIPPED; run
them as root in the builder VM (see the comment above ``base_rootfs``).

New symbols (``Factory``/``FactoryError``/``FactoryResult``/``_build_binds``/
``settle_pass``) are imported tolerantly so as not to abort pytest's
collection while the impl does not exist; each unit test goes Red on use, naming the
pending symbol (expected Red of story 003).
"""

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, config, factory, phases
from shidashi.container import Container
from shidashi.flow import StagesFlow
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from shidashi.resolve import bind_repos, install_sets
from shidashi.seed import load_pointer
from shidashi.tree import load_pin_id, pinned_repos
from tests._pending import try_import

Factory: Any = try_import("shidashi.factory", "Factory")
FactoryError: Any = try_import("shidashi.factory", "FactoryError")
FactoryResult: Any = try_import("shidashi.factory", "FactoryResult")

_NEEDS_HOST = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST, reason="requires root + systemd-nspawn (run it in the builder VM)"
)


def _recipe(
    *,
    flavor: str = "kde",
    sets: tuple[str, ...] = ("base", "extra-system", "kde"),
    phases_: tuple[Phase, ...] = (),
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
        phases=phases_,
        portage_layers=("base", "arch/v3", "flavor/kde", "init/systemd"),
    )


def _pointer() -> Any:
    from shidashi.seed import Stage3Pointer

    return Stage3Pointer(
        init="systemd",
        base_url="https://distfiles.gentoo.org/x",
        snapshot="20260524T170105Z",
        filename="stage3-amd64-nomultilib-systemd-20260524T170105Z.tar.xz",
        sha512="0" * 128,
    )


def _make_result() -> Any:
    return FactoryResult(
        pkgdir=Path("/var/cache/shidashi/binpkgs/v3"),
        built_atoms=("media-libs/libsdl2-2.30.5",),
        phases=("rebuild", "graphics"),
        fork_point=Path("/c/fork-points/v3-kde-systemd-SNAP.tar"),
        fork_point_reused=False,
        settle_atoms=("media-video/ffmpeg-6.1.1",),
    )


# --- 6.1 FactoryResult / FactoryError ----------------------------------------


def test_factory_result_is_frozen_and_typed() -> None:
    result = _make_result()
    assert result.pkgdir == Path("/var/cache/shidashi/binpkgs/v3")
    assert result.built_atoms == ("media-libs/libsdl2-2.30.5",)
    assert result.phases == ("rebuild", "graphics")
    assert result.fork_point_reused is False
    assert result.settle_atoms == ("media-video/ffmpeg-6.1.1",)
    with pytest.raises(Exception):  # noqa: B017  (frozen → ValidationError)
        result.fork_point_reused = True


def test_factory_error_carries_phase_and_output() -> None:
    err = FactoryError("emerge failed", phase="graphics", output="!!! error log")
    assert err.phase == "graphics"
    assert err.output == "!!! error log"
    assert isinstance(err, Exception)  # narrow only at the end: err is Any (try_import)


# --- 6.1 _build_binds --------------------------------------------------------


def test_build_binds_maps_caches_rw_and_repos_ro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    repo_ro = (Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))
    monkeypatch.setattr(factory, "bind_repos", lambda *_a, **_k: [repo_ro], raising=False)

    build_binds: Any = try_import("shidashi.factory", "_build_binds")
    pkgdir = tmp_path / "cache" / "binpkgs" / "v3"
    binds_ro, binds_rw = build_binds(_recipe(), pkgdir=pkgdir, repos={})

    # repos come from bind_repos (RO)
    assert repo_ro in binds_ro
    # host PKGDIR → /var/cache/binpkgs in the container (RW)
    rw_dsts = {dst for _src, dst in binds_rw}
    assert Path("/var/cache/binpkgs") in rw_dsts
    rw_by_dst = {dst: src for src, dst in binds_rw}
    assert rw_by_dst[Path("/var/cache/binpkgs")] == pkgdir
    # host ccache/sccache/distdir (under cache_dir) are RW too
    rw_srcs = {src for src, _dst in binds_rw}
    assert tmp_path / "cache" / "ccache" in rw_srcs
    assert tmp_path / "cache" / "sccache" in rw_srcs
    assert tmp_path / "cache" / "distfiles" in rw_srcs


def test_ensure_bind_dirs_creates_host_side_sources(tmp_path: Path) -> None:
    # Regression (pilot Gate 8): systemd-nspawn requires the source of each
    # --bind= to exist; without this the spawn aborts with "Failed to clone …".
    ensure: Any = try_import("shidashi.factory", "_ensure_bind_dirs")
    binds_rw = [
        (tmp_path / "binpkgs" / "v3", Path("/var/cache/binpkgs")),
        (tmp_path / "ccache", Path("/var/cache/ccache")),
    ]
    ensure(binds_rw)
    assert (tmp_path / "binpkgs" / "v3").is_dir()
    assert (tmp_path / "ccache").is_dir()
    # idempotent: running it again does not raise
    ensure(binds_rw)


# --- 6.2 (unit) non-root guard -----------------------------------------------


def test_factory_build_non_root_raises_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("work done before the root guard")

    monkeypatch.setattr(factory, "fetch_stage3", _boom, raising=False)
    monkeypatch.setattr(factory, "extract_stage3", _boom, raising=False)

    f = Factory(_recipe(), Path("/var/cache/shidashi/binpkgs/v3"))
    with pytest.raises(Exception):  # noqa: B017  (SystemExit/FactoryError/RuntimeError)
        f.build()


# --- 5.2 (unit) settle_pass empty-breaks no-op -------------------------------


class _NoEmergeContainer:
    """Fake container: any ``run`` fails the test (an empty settle does not emerge)."""

    rootfs = Path("/r")

    def run(self, *_a: object, **_k: object) -> object:
        raise AssertionError("settle_pass with empty breaks must NOT call emerge")


def test_settle_pass_empty_breaks_is_noop() -> None:
    settle_pass: Any = try_import("shidashi.phases", "settle_pass")
    container = _NoEmergeContainer()
    result = settle_pass(container, _recipe(flavor="minimal"), ())
    # no-op: no settle atoms (R4.4)
    assert result.built_atoms == ()


# --- host-gated INTEGRATION (Red DEFERRED to the real privileged host) -------


@_skip_privileged
def test_snapshot_restore_real_rootfs_preserves_ownership(tmp_path: Path) -> None:
    # 4.1 (int): snapshot/restore of a real rootfs preserving ownership.
    src = tmp_path / "rootfs"
    (src / "etc").mkdir(parents=True)
    (src / "etc" / "f").write_text("x", encoding="utf-8")
    dest = tmp_path / "fp.tar"
    phases.snapshot_fork_point(src, dest)
    restored = tmp_path / "restored"
    restored.mkdir()
    phases.restore_fork_point(dest, restored)
    st = (restored / "etc" / "f").stat()
    assert st.st_uid == 0  # ownership preserved (root) on a privileged host


# --- privileged INTEGRATION on the cached base fork point (story 015) ----------
#
# Real nspawn + emerge, in minutes: each test restores the cached ``base`` fork
# point into its own tmp_path and emerges small binpkgs only. Run as root, with
# the pins the fork point and the binhost were built from (SHIDASHI_SEEDS_DIR)
# and a --basetemp on a disk with room for a rootfs (~5 GB) and its snapshot:
#
#   python3 -m pytest -q -p no:cacheprovider --basetemp=/mnt/work/scratch/pytest \
#       tests/test_factory.py
#
# A missing fork point skips the test naming the file; nothing here builds one.

#: Not installed by the base, ~170 KB, every dependency already in the base.
_SMALL_ATOM = "dev-libs/wayland"
#: The binhost holds vim with and without ``wayland``; the base installs it without.
_CUT = UseBreak(atom="app-editors/vim", flag="wayland", enable=True)
#: The one-atom kit (``variants/kits/system/laptop``) a trimmed stage installs.
_TRIM_SET = "laptop"
_TRIM_ATOM = "app-laptop/laptop-mode-tools"
#: What makes every emerge here binary-only: one that would compile fails instead.
_BINPKG_ONLY = ("--usepkgonly", "--binpkg-respect-use=y")


def _base_fork_point(fork_points_dir: Path) -> Path:
    """The cached ``base`` fork point of the pinned stage3 and pins; skips naming it.

    The key with the pins (story 016) first, then the older key without them;
    both carry the pinned stage3's snapshot, so a fork point of another seed is
    never restored under this one.
    """
    recipe = config.load_recipe("v3", "minimal", "systemd")
    snapshot = load_pointer(recipe.init, seeds_dir=config.seeds_dir()).snapshot
    keyed = phases.stage_fork_point_path(
        recipe,
        "base",
        snapshot=snapshot,
        pins=load_pin_id(config.seeds_dir()),
        fork_points_dir=fork_points_dir,
    )
    unkeyed = fork_points_dir / f"{recipe.arch}-{recipe.init}-{snapshot}-base.tar"
    for candidate in (keyed, unkeyed):
        if candidate.is_file():
            return candidate
    pytest.skip(f"no base fork point in the cache: expected {keyed} or {unkeyed}")


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


@pytest.fixture
def base_rootfs(tmp_path: Path) -> Iterator[Path]:
    """The cached base fork point restored into ``tmp_path/rootfs``; the cache is
    only read."""
    fork_points = config.fork_points_dir()
    tarball = _base_fork_point(fork_points)
    before = (_mtime_ns(fork_points), _mtime_ns(tarball))
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    phases.restore_fork_point(tarball, rootfs)
    yield rootfs
    assert (_mtime_ns(fork_points), _mtime_ns(tarball)) == before, "the cache was written"


def _pkgdir() -> Path:
    """The generation's PKGDIR: the binhost the emerges install from."""
    snapshot = load_pointer("systemd", seeds_dir=config.seeds_dir()).snapshot
    return config.pkgdir("v3", snapshot)


def _container(rootfs: Path) -> Container:
    """An nspawn on ``rootfs`` with the pinned repos and the binhost, all READ-ONLY.

    The repos are bound as :meth:`Factory.build` binds them; the PKGDIR is bound
    read-only too, which binary-only emerges never need to write.
    """
    repos = pinned_repos(seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=False)
    binds = bind_repos(rootfs / "etc" / "portage" / "repos.conf", pinned=repos)
    binds.append((_pkgdir(), Path("/var/cache/binpkgs")))
    return Container(rootfs, binds=binds)


def _binpkg_only_flow() -> StagesFlow:
    """The flow in force, with binary-only emerges and no rebuild from source.

    ``perl-rebuild`` and ``module-rebuild`` rebuild with ``--usepkg=n`` by design;
    the cached base still holds a perl 5.42 directory, so they would compile
    ~24 perl modules in every test. Their own tests cover them.
    """
    flow = phases.stages_flow()
    return flow.model_copy(
        update={
            "emerge": flow.emerge.model_copy(
                update={"options": (*flow.emerge.options, *_BINPKG_ONLY)}
            ),
            "settle": flow.settle.model_copy(
                update={"options": (*flow.settle.options, *_BINPKG_ONLY)}
            ),
            "steps": tuple(s for s in flow.steps if s.do not in ("perl-rebuild", "module-rebuild")),
        }
    )


@pytest.fixture
def binpkg_only(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _binpkg_only_flow()
    monkeypatch.setattr(phases, "stages_flow", lambda: flow)


def _trimmed_minimal(*, ships: bool) -> ResolvedRecipe:
    """The real minimal recipe with its last stage cut down to one small kit.

    The stage keeps the base's layers, so its configuration does not change and
    ``-uDN @world`` has nothing to rebuild: the run takes seconds, not the hour
    of the real ``extra-system``.
    """
    recipe = config.load_recipe("v3", "minimal", "systemd")
    base = recipe.phases[0]
    stage = Phase(
        name="minimal", stage="minimal", sets=(_TRIM_SET,), layers=base.layers, ships=ships
    )
    # only the base's excludes: the minimal stage's and the init's name atoms of
    # the minimal kits cut out here (an unmatched exclude is a ResolveError)
    kept = {atom: origin for atom, origin in recipe.exclude_origin.items() if origin == "base"}
    return recipe.model_copy(
        update={
            "phases": (base, stage),
            "sets": (*base.sets, _TRIM_SET),
            "exclude": tuple(atom for atom in recipe.exclude if atom in kept),
            "exclude_origin": kept,
        }
    )


def _installed_use(rootfs: Path, cp: str) -> set[str]:
    category, name = cp.split("/")
    (entry,) = (rootfs / "var" / "db" / "pkg" / category).glob(f"{name}-[0-9]*")
    return set((entry / "USE").read_text(encoding="utf-8").split())


def test_base_rootfs_skips_without_a_fork_point(tmp_path: Path) -> None:
    # 1.1: an empty fork-points directory skips, naming the files it looked for
    with pytest.raises(pytest.skip.Exception, match=r"v3-systemd-.*-base\.tar"):
        _base_fork_point(tmp_path)


@_skip_privileged
def test_base_rootfs_restores_the_cached_fork_point(base_rootfs: Path) -> None:
    # 1.1: a real Gentoo rootfs, root-owned (the cache's mtime: the fixture's teardown)
    os_release = base_rootfs / "usr" / "lib" / "os-release"
    assert os_release.is_file()
    assert os_release.stat().st_uid == 0
    assert (base_rootfs / "etc" / "passwd").stat().st_uid == 0
    assert phases.is_installed(base_rootfs, "sys-apps/portage")


@_skip_privileged
@pytest.mark.usefixtures("binpkg_only")
def test_run_phase_executes_and_wraps_failure(base_rootfs: Path) -> None:
    # 5.1 (int): run_phase emerges one binpkg and returns it; a target that cannot
    # resolve raises FactoryError with the phase and emerge's own text
    recipe = config.load_recipe("v3", "minimal", "systemd")
    with _container(base_rootfs) as container:
        ok = phases.run_phase(
            container, recipe, Phase(name="small", packages=(_SMALL_ATOM,)), emptytree=False
        )
        assert any(a.startswith(f"{_SMALL_ATOM}-") for a in ok.reused_atoms), ok.output
        assert phases.is_installed(base_rootfs, _SMALL_ATOM)

        missing = Phase(name="missing", packages=("dev-libs/shidashi-no-such-package",))
        with pytest.raises(phases.FactoryError) as caught:
            phases.run_phase(container, recipe, missing, emptytree=False)
    assert caught.value.phase == "missing"
    assert "there are no binary packages to satisfy" in caught.value.output


@_skip_privileged
@pytest.mark.usefixtures("binpkg_only")
def test_settle_pass_reemerges_a_cut_atom_with_its_final_use(base_rootfs: Path) -> None:
    # 5.2 (int): the break-pass installs vim with the cut's USE; settle_pass removes
    # the cut and re-emerges it with the final USE; no breaks, no emerge
    recipe = config.load_recipe("v3", "minimal", "systemd")
    assert _CUT.flag not in _installed_use(base_rootfs, _CUT.atom)
    cut_phase = Phase(name="break", packages=(_CUT.atom,), use_break=(_CUT,))
    with _container(base_rootfs) as container:
        phases.run_phase(container, recipe, cut_phase, emptytree=False)
        assert _CUT.flag in _installed_use(base_rootfs, _CUT.atom)

        settled = phases.settle_pass(container, recipe, (_CUT,))
        assert _CUT.flag not in _installed_use(base_rootfs, _CUT.atom), settled.output
        assert not base_rootfs.joinpath(*phases._USE_BREAK_FILE).exists()

        idle = phases.settle_pass(container, recipe, ())
    assert idle.built_atoms == ()
    assert idle.output == ""


@_skip_privileged
@pytest.mark.usefixtures("binpkg_only")
def test_run_phases_resumes_after_the_base_fork_point(base_rootfs: Path, tmp_path: Path) -> None:
    # 5.3 (int): resuming at the base runs only the stage after it, and writes that
    # stage's fork point where it is told to -- never into the cache
    recipe = _trimmed_minimal(ships=False)
    install_sets(base_rootfs, recipe)
    fork_points = tmp_path / "fork-points"
    fork_points.mkdir()
    snapshot = load_pointer(recipe.init, seeds_dir=config.seeds_dir()).snapshot
    pins = load_pin_id(config.seeds_dir())
    with _container(base_rootfs) as container:
        results = phases.run_phases(
            container,
            recipe,
            emptytree=True,
            resume_at="base",
            snapshot=snapshot,
            pins=pins,
            fork_points_dir=fork_points,
        )
    assert [r.phase.name for r in results] == ["minimal"]
    assert phases.is_installed(base_rootfs, _TRIM_ATOM)
    written = phases.stage_fork_point_path(
        recipe, "minimal", snapshot=snapshot, pins=pins, fork_points_dir=fork_points
    )
    assert written.is_file()
    assert [p.name for p in fork_points.iterdir()] == [written.name]


@pytest.fixture
def overlay_cache(tmp_path: Path) -> Iterator[Path]:
    """A writable view of the real cache: an overlay whose upper layer is in tmp_path.

    The factory writes into its cache (fork points, binpkgs, its generation
    file); here every write lands in tmp_path, and the real cache is the
    overlay's read-only lower layer.
    """
    lower = config.cache_dir()
    upper, work, merged = tmp_path / "upper", tmp_path / "work", tmp_path / "cache"
    for d in (upper, work, merged):
        d.mkdir()
    options = f"lowerdir={lower},upperdir={upper},workdir={work}"
    subprocess.run(["mount", "-t", "overlay", "overlay", "-o", options, str(merged)], check=True)
    try:
        yield merged
    finally:
        subprocess.run(["umount", str(merged)], check=True)


@_skip_privileged
@pytest.mark.usefixtures("binpkg_only")
def test_full_factory_build_v3_minimal_systemd(
    overlay_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 6.2 (int): `shidashi factory v3 minimal systemd` resumes from the base fork
    # point, builds the (trimmed) minimal stage, settles it, checks its binpkgs and
    # writes its fork point -- every write in the overlay's upper layer
    real_fork_points = config.fork_points_dir()
    base = _base_fork_point(real_fork_points)
    recipe = _trimmed_minimal(ships=True)
    monkeypatch.setattr(config, "load_recipe", lambda *_a, **_k: recipe)
    monkeypatch.setenv("SHIDASHI_CACHE", str(overlay_cache))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    snapshot = load_pointer(recipe.init, seeds_dir=config.seeds_dir()).snapshot
    pins = load_pin_id(config.seeds_dir())
    fork_points = config.fork_points_dir()
    keyed_base = phases.stage_fork_point_path(
        recipe, "base", snapshot=snapshot, pins=pins, fork_points_dir=fork_points
    )
    if not keyed_base.exists():
        # an older, unkeyed base fork point: named as the pinned build expects it
        keyed_base.symlink_to(base)
    before = _mtime_ns(real_fork_points)
    pkgdir = config.pkgdir(recipe.arch, snapshot)

    result = CliRunner().invoke(
        cli.app,
        ["factory", "v3", "minimal", "systemd", "--no-download", "--pkgdir", str(pkgdir)],
    )

    assert result.exit_code == 0, result.output
    assert any(pkgdir.iterdir())
    written = phases.stage_fork_point_path(
        recipe, "minimal", snapshot=snapshot, pins=pins, fork_points_dir=fork_points
    )
    assert written.is_file()
    assert (tmp_path / "upper" / "fork-points" / written.name).is_file()
    assert _mtime_ns(real_fork_points) == before


# --- fresh seed: fetch + extract the stage3 ------------------------------------


def test_fresh_seed_fetches_then_extracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generic = tmp_path / "generic.tar.xz"
    extracted: dict[str, Any] = {}
    monkeypatch.setattr(factory, "fetch_stage3", lambda p, **k: generic, raising=False)
    monkeypatch.setattr(
        factory, "extract_stage3", lambda tb, rf: extracted.update(tarball=tb), raising=False
    )
    factory._fresh_seed(tmp_path / "rootfs", _pointer(), download=True)
    assert extracted["tarball"] == generic


# --- toolchain bootstrap: where a build starts from (BOOTSTRAP-PROCESS §5) ------


def _staged_recipe() -> ResolvedRecipe:
    return _recipe(
        phases_=(
            Phase(name="base", stage="base", emptytree=True),
            Phase(name="flavor", stage="kde", ships=True),
        )
    )


def _tarball_of(tmp_path: Path, name: str, marker: str) -> Path:
    tree = tmp_path / f"tree-{name}"
    (tree / "etc").mkdir(parents=True)
    (tree / "etc" / "marker").write_text(marker, encoding="utf-8")
    return phases.snapshot_fork_point(tree, tmp_path / "fp" / name)


def _no_fresh_seed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seeded: list[str] = []

    def fresh_seed(*_a: object, **_k: object) -> None:
        seeded.append("fresh")

    monkeypatch.setattr(factory, "_fresh_seed", fresh_seed, raising=False)
    return seeded


def test_seed_or_restore_without_checkpoints_seeds_fresh_and_asks_for_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeded = _no_fresh_seed(monkeypatch)
    (tmp_path / "fp").mkdir()
    resume, _fp, reused, bootstrapped = factory._seed_or_restore(
        _staged_recipe(),
        tmp_path / "rootfs",
        _pointer(),
        snapshot="S",
        pins="P",
        fork_points_dir=tmp_path / "fp",
        download=False,
    )
    assert (resume, reused, bootstrapped) == (None, False, False)
    assert seeded == ["fresh"]


def test_seed_or_restore_restores_the_bootstrap_checkpoint_instead_of_the_stage3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeded = _no_fresh_seed(monkeypatch)
    recipe = _staged_recipe()
    path = factory.bootstrap_fork_point_path(
        recipe, snapshot="S", pins="P", fork_points_dir=tmp_path / "fp"
    )
    assert path.name == "v3-systemd-S-P-bootstrap.tar"
    (tmp_path / "fp").mkdir()
    _tarball_of(tmp_path, path.name, "bootstrapped")
    rootfs = tmp_path / "rootfs"

    resume, _fp, reused, bootstrapped = factory._seed_or_restore(
        recipe,
        rootfs,
        _pointer(),
        snapshot="S",
        pins="P",
        fork_points_dir=tmp_path / "fp",
        download=False,
    )
    assert (resume, reused, bootstrapped) == (None, False, True)
    assert seeded == []
    assert (rootfs / "etc" / "marker").read_text(encoding="utf-8") == "bootstrapped"


def test_seed_or_restore_prefers_a_stage_fork_point_over_the_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_fresh_seed(monkeypatch)
    recipe = _staged_recipe()
    (tmp_path / "fp").mkdir()
    _tarball_of(tmp_path, "v3-systemd-S-P-bootstrap.tar", "bootstrapped")
    _tarball_of(tmp_path, "v3-systemd-S-P-base.tar", "base built")
    rootfs = tmp_path / "rootfs"

    resume, _fp, reused, bootstrapped = factory._seed_or_restore(
        recipe,
        rootfs,
        _pointer(),
        snapshot="S",
        pins="P",
        fork_points_dir=tmp_path / "fp",
        download=False,
    )
    assert (resume, reused, bootstrapped) == ("base", True, True)
    assert (rootfs / "etc" / "marker").read_text(encoding="utf-8") == "base built"


def test_portage_ids_come_from_the_rootfs(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/passwd").write_text(
        "root:x:0:0::/root:/bin/bash\nportage:x:250:250:portage:/var/lib/portage/home:/sbin/nologin\n",
        encoding="utf-8",
    )
    assert factory.portage_ids(tmp_path) is None  # no group file yet
    (tmp_path / "etc/group").write_text("portage:x:250:\n", encoding="utf-8")
    assert factory.portage_ids(tmp_path) == (250, 250)


def test_ensure_bind_dirs_hands_only_ccache_to_the_images_portage_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "etc/passwd").write_text("portage:x:250:250::/:/sbin/nologin\n", encoding="utf-8")
    (rootfs / "etc/group").write_text("portage:x:250:\n", encoding="utf-8")
    chowned: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(os, "chown", lambda p, u, g: chowned.append((Path(p), u, g)))
    binds = [
        (tmp_path / "ccache", Path("/var/cache/ccache")),
        (tmp_path / "distfiles", Path("/var/cache/distfiles")),
    ]
    factory._ensure_bind_dirs(binds, rootfs=rootfs)
    assert chowned == [(tmp_path / "ccache", 250, 250)]
    assert (tmp_path / "distfiles").is_dir()


# --- Factory.update preconditions (D26) -------------------------------------------


def _update_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(factory, "load_pointer", lambda init, *, seeds_dir: _pointer())
    monkeypatch.setattr(factory, "pinned_repos", lambda **_k: {"gentoo": tmp_path / "tree"})
    return tmp_path / "cache" / "binpkgs" / "v3" / "SNAP"


def test_update_refuses_when_the_image_was_never_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkgdir = _update_env(monkeypatch, tmp_path)
    recipe = _staged_recipe().model_copy(update={"stages": ("base", "kde")})
    with pytest.raises(FactoryError, match="nothing to update: v3-systemd-20260524T170105Z-p"):
        Factory(recipe, pkgdir).update(download=False)


def test_update_refuses_a_pkgdir_without_a_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkgdir = _update_env(monkeypatch, tmp_path)
    recipe = _staged_recipe().model_copy(update={"stages": ("base", "kde")})
    fps = config.fork_points_dir()
    fps.mkdir(parents=True)
    (tmp_path / "fp").mkdir()
    _tarball_of(tmp_path, "img", "kde image")
    (tmp_path / "fp" / "img").replace(fps / "v3-systemd-20260524T170105Z-kde.tar")
    with pytest.raises(FactoryError, match="never starts one"):
        Factory(recipe, pkgdir).update(download=False)


def test_seed_or_restore_wipes_a_leftover_rootfs_before_a_fresh_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed --keep run leaves its rootfs behind (2026-09-26: a stage3 with
    broken modes); a clean start must not extract a new stage3 on top of it."""
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "etc" / "leftover").write_text("from the failed run", encoding="utf-8")
    seen: list[bool] = []

    def fresh_seed(root: Path, *_a: object, **_k: object) -> None:
        seen.append((root / "etc" / "leftover").exists())

    monkeypatch.setattr(factory, "_fresh_seed", fresh_seed, raising=False)
    (tmp_path / "fp").mkdir()
    factory._seed_or_restore(
        _staged_recipe(),
        rootfs,
        _pointer(),
        snapshot="S",
        pins="P",
        fork_points_dir=tmp_path / "fp",
        download=False,
    )
    assert seen == [False]
