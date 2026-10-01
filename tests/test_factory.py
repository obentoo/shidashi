"""UNIT + INTEGRAÇÃO de shidashi.factory (story 003 grupos 5 e 6).

UNIT (determinista, CI não-Gentoo):
* 6.1 ``_build_binds`` mapeia pkgdir/ccache/sccache/distdir → caminhos fixos do
  container como RW e os repos como RO (``resolve.bind_repos`` monkeypatched);
  ``FactoryResult``/``FactoryError`` são frozen/tipados;
* 6.2 (unit) ``Factory.build`` levanta antes de qualquer trabalho quando
  não-root (``os.geteuid`` monkeypatched);
* 5.2 (unit) ``settle_pass`` com ``breaks`` vazio é no-op (o container é
  monkeypatched p/ garantir que nenhum emerge é chamado).

INTEGRAÇÃO (host-gated, ``@pytest.mark.skipif`` não-root/não-Gentoo): exercitam o
caminho privilegiado real (nspawn + emerge + snapshot). Em CI/sandbox PULAM —
Red DIFERIDO ao host privilegiado real (4.1-int, 5.1, 5.2-int, 5.3, 6.2-int).

Símbolos novos (``Factory``/``FactoryError``/``FactoryResult``/``_build_binds``/
``settle_pass``) são importados de forma tolerante para não abortar a coleção do
pytest enquanto a impl não existe; cada teste unit fica Red no uso, nomeando o
símbolo pendente (Red esperado da story 003).
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from shidashi import factory, phases
from shidashi.recipe import Phase, ResolvedRecipe, SeedSource
from tests._pending import try_import

Factory: Any = try_import("shidashi.factory", "Factory")
FactoryError: Any = try_import("shidashi.factory", "FactoryError")
FactoryResult: Any = try_import("shidashi.factory", "FactoryResult")

_NEEDS_HOST = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST, reason="exige root + systemd-nspawn + stage3 seedado (host Gentoo)"
)


def _recipe(
    *,
    flavor: str = "kde",
    sets: tuple[str, ...] = ("base", "extra-system", "kde"),
    phases_: tuple[Phase, ...] = (),
    seed_source: SeedSource = "download",
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
        seed_source=seed_source,
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
    assert isinstance(err, Exception)  # narrow só ao final: err é Any (try_import)


# --- 6.1 _build_binds --------------------------------------------------------


def test_build_binds_maps_caches_rw_and_repos_ro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    repo_ro = (Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))
    monkeypatch.setattr(factory, "bind_repos", lambda *_a, **_k: [repo_ro], raising=False)

    build_binds: Any = try_import("shidashi.factory", "_build_binds")
    pkgdir = tmp_path / "cache" / "binpkgs" / "v3"
    binds_ro, binds_rw = build_binds(_recipe(), pkgdir=pkgdir)

    # repos vêm de bind_repos (RO)
    assert repo_ro in binds_ro
    # PKGDIR host → /var/cache/binpkgs no container (RW)
    rw_dsts = {dst for _src, dst in binds_rw}
    assert Path("/var/cache/binpkgs") in rw_dsts
    rw_by_dst = {dst: src for src, dst in binds_rw}
    assert rw_by_dst[Path("/var/cache/binpkgs")] == pkgdir
    # ccache/sccache/distdir host (sob cache_dir) também são RW
    rw_srcs = {src for src, _dst in binds_rw}
    assert tmp_path / "cache" / "ccache" in rw_srcs
    assert tmp_path / "cache" / "sccache" in rw_srcs
    assert tmp_path / "cache" / "distfiles" in rw_srcs


def test_ensure_bind_dirs_creates_host_side_sources(tmp_path: Path) -> None:
    # Regressão (pilot Gate 8): systemd-nspawn exige que o source de cada
    # --bind= exista; sem isto o spawn aborta com "Failed to clone …".
    ensure: Any = try_import("shidashi.factory", "_ensure_bind_dirs")
    binds_rw = [
        (tmp_path / "binpkgs" / "v3", Path("/var/cache/binpkgs")),
        (tmp_path / "ccache", Path("/var/cache/ccache")),
    ]
    ensure(binds_rw)
    assert (tmp_path / "binpkgs" / "v3").is_dir()
    assert (tmp_path / "ccache").is_dir()
    # idempotente: rodar de novo não levanta
    ensure(binds_rw)


# --- 6.2 (unit) non-root guard -----------------------------------------------


def test_factory_build_non_root_raises_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("trabalho executado antes da guarda de root")

    monkeypatch.setattr(factory, "fetch_stage3", _boom, raising=False)
    monkeypatch.setattr(factory, "extract_stage3", _boom, raising=False)

    f = Factory(_recipe(), Path("/var/cache/shidashi/binpkgs/v3"))
    with pytest.raises(Exception):  # noqa: B017  (SystemExit/FactoryError/RuntimeError)
        f.build()


# --- 5.2 (unit) settle_pass empty-breaks no-op -------------------------------


class _NoEmergeContainer:
    """Container falso: qualquer ``run`` falha o teste (settle vazio não emerge)."""

    rootfs = Path("/r")

    def run(self, *_a: object, **_k: object) -> object:
        raise AssertionError("settle_pass com breaks vazio NÃO deve chamar emerge")


def test_settle_pass_empty_breaks_is_noop() -> None:
    settle_pass: Any = try_import("shidashi.phases", "settle_pass")
    container = _NoEmergeContainer()
    result = settle_pass(container, _recipe(flavor="minimal"), ())
    # no-op: sem átomos de settle (R4.4)
    assert result.built_atoms == ()


# --- INTEGRAÇÃO host-gated (Red DIFERIDO ao host privilegiado real) ----------


@_skip_privileged
def test_snapshot_restore_real_rootfs_preserves_ownership(tmp_path: Path) -> None:
    # 4.1 (int): snapshot/restore de um rootfs real preservando ownership.
    src = tmp_path / "rootfs"
    (src / "etc").mkdir(parents=True)
    (src / "etc" / "f").write_text("x", encoding="utf-8")
    dest = tmp_path / "fp.tar"
    phases.snapshot_fork_point(src, dest)
    restored = tmp_path / "restored"
    restored.mkdir()
    phases.restore_fork_point(dest, restored)
    st = (restored / "etc" / "f").stat()
    assert st.st_uid == 0  # ownership preservado (root) num host privilegiado


@_skip_privileged
def test_run_phase_executes_and_wraps_failure() -> None:
    # 5.1 (int): run_phase roda um emerge trivial e devolve átomos; não-zero →
    # FactoryError. Exige rootfs seedado real — diferido ao host.
    pytest.skip("integração privilegiada: requer rootfs seedado real (Red diferido)")


@_skip_privileged
def test_settle_pass_reemerges_ffmpeg_with_use_on() -> None:
    # 5.2 (int): settle_pass re-emerge ffmpeg com USE ligado após o break-pass.
    pytest.skip("integração privilegiada: requer rootfs seedado real (Red diferido)")


@_skip_privileged
def test_run_phases_minimal_and_kde() -> None:
    # 5.3 (int): run_phases minimal (trunk+snapshot, sem settle) e kde
    # (resume + settle).
    pytest.skip("integração privilegiada: requer rootfs seedado real (Red diferido)")


@_skip_privileged
def test_full_factory_build_v3_minimal_systemd() -> None:
    # 6.2 (int): shidashi factory v3 minimal systemd produz pkgdir não-vazio +
    # fork-point. Diferido ao host privilegiado real.
    pytest.skip("integração privilegiada: requer host Gentoo seedado (Red diferido)")


# --- seed_source seam: download vs catalyst (R5.1–R5.3, R4.1; story 005) ------


def test_fresh_seed_download_branch_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R5.2 — seed_source=download: fetch+extract como antes, catalyst nunca tocado.
    generic = tmp_path / "generic.tar.xz"
    extracted: dict[str, Any] = {}
    monkeypatch.setattr(factory, "fetch_stage3", lambda p, **k: generic, raising=False)
    monkeypatch.setattr(
        factory, "extract_stage3", lambda tb, rf: extracted.update(tarball=tb), raising=False
    )

    def _no_catalyst(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("build_stage3_catalyst não deve ser chamado no download")

    monkeypatch.setattr(factory, "build_stage3_catalyst", _no_catalyst, raising=False)
    sha = factory._fresh_seed(
        tmp_path / "rootfs", _pointer(), download=True, recipe=_recipe(seed_source="download")
    )
    assert sha == ""
    assert extracted["tarball"] == generic


def test_fresh_seed_catalyst_branch_builds_then_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R5.1 — seed_source=catalyst: fetch (semente) → build → extract do tarball gerado.
    order: list[str] = []
    generic = tmp_path / "generic.tar.xz"
    cat_tarball = tmp_path / "cat-stage3.tar.xz"

    def _fetch(p: Any, **k: Any) -> Path:
        order.append("fetch")
        return generic

    def _build(recipe: Any, seed: Any, **k: Any) -> tuple[Path, str]:
        order.append("build")
        assert seed == generic  # a semente buildada é o stage3 genérico
        return cat_tarball, "ab" * 64

    monkeypatch.setattr(factory, "fetch_stage3", _fetch, raising=False)
    monkeypatch.setattr(factory, "build_stage3_catalyst", _build, raising=False)
    monkeypatch.setattr(
        factory, "extract_stage3", lambda tb, rf: order.append(f"extract:{tb.name}"), raising=False
    )
    sha = factory._fresh_seed(
        tmp_path / "rootfs", _pointer(), download=True, recipe=_recipe(seed_source="catalyst")
    )
    assert sha == "ab" * 64
    assert order == ["fetch", "build", "extract:cat-stage3.tar.xz"]


def test_stepwise_persists_catalyst_seed_sha512(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R5.3/R4.1 — no driver stepwise (caso sem estado), o sha do stage3 buildado é
    # persistido no BuildState.
    from shidashi import state

    monkeypatch.setattr(
        factory,
        "_fresh_seed",
        lambda rootfs, pointer, *, download, recipe: "cafe" * 32,
        raising=False,
    )
    state_path = tmp_path / "state.json"
    recipe = _recipe(seed_source="catalyst")
    fac = Factory(recipe, tmp_path / "pkg")
    stop = fac._seed_or_restore_stepwise(
        recipe,
        tmp_path / "rootfs",
        _pointer(),
        snapshot="SNAP",
        recipe_hash="h",
        fork_points_dir=tmp_path / "fp",
        state_path=state_path,
        completed=(),
        seed_done=False,
        interactive=False,
        download=True,
        on_checkpoint=None,
    )
    assert stop is False
    saved = state.load_state(state_path)
    assert saved is not None
    assert saved.seed_sha512 == "cafe" * 32
    assert saved.seed_done is True


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
    monkeypatch.setattr(
        factory, "_fresh_seed", lambda *a, **k: seeded.append("fresh") or "", raising=False
    )
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
    path = factory.bootstrap_fork_point_path(recipe, snapshot="S", fork_points_dir=tmp_path / "fp")
    assert path.name == "v3-systemd-S-bootstrap.tar"
    (tmp_path / "fp").mkdir()
    _tarball_of(tmp_path, path.name, "bootstrapped")
    rootfs = tmp_path / "rootfs"

    resume, _fp, reused, bootstrapped = factory._seed_or_restore(
        recipe,
        rootfs,
        _pointer(),
        snapshot="S",
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
    _tarball_of(tmp_path, "v3-systemd-S-bootstrap.tar", "bootstrapped")
    _tarball_of(tmp_path, "v3-systemd-S-base.tar", "base built")
    rootfs = tmp_path / "rootfs"

    resume, _fp, reused, bootstrapped = factory._seed_or_restore(
        recipe,
        rootfs,
        _pointer(),
        snapshot="S",
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
    with pytest.raises(FactoryError, match="nothing to update: v3-systemd-20260524T170105Z-kde"):
        Factory(recipe, pkgdir).update(download=False)


def test_update_refuses_a_pkgdir_without_a_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkgdir = _update_env(monkeypatch, tmp_path)
    recipe = _staged_recipe().model_copy(update={"stages": ("base", "kde")})
    fps = factory.config.fork_points_dir()
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
    monkeypatch.setattr(
        factory,
        "_fresh_seed",
        lambda root, *a, **k: seen.append((root / "etc" / "leftover").exists()) or "",
        raising=False,
    )
    (tmp_path / "fp").mkdir()
    factory._seed_or_restore(
        _staged_recipe(),
        rootfs,
        _pointer(),
        snapshot="S",
        fork_points_dir=tmp_path / "fp",
        download=False,
    )
    assert seen == [False]
