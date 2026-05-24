"""UNIT + INTEGRAÇÃO de kaji.factory (story 003 grupos 5 e 6).

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

from kaji import factory, phases
from kaji.recipe import Phase, ResolvedRecipe, ResolvedUse

from tests._pending import try_import

Factory: Any = try_import("kaji.factory", "Factory")
FactoryError: Any = try_import("kaji.factory", "FactoryError")
FactoryResult: Any = try_import("kaji.factory", "FactoryResult")

_NEEDS_HOST = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST, reason="exige root + systemd-nspawn + stage3 seedado (host Gentoo)"
)


def _recipe(
    *,
    flavor: str = "kde",
    sets: tuple[str, ...] = ("graphics", "bentoo-apps", "kde"),
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
        use=ResolvedUse(enabled=(), disabled=()),
        sets=sets,
        phases=phases_,
        portage_layers=("base", "arch/v3", "flavor/kde", "init/systemd"),
    )


def _make_result() -> Any:
    return FactoryResult(
        pkgdir=Path("/var/cache/kaji/binpkgs/v3"),
        built_atoms=("media-libs/libsdl2-2.30.5",),
        phases=("rebuild", "graphics"),
        fork_point=Path("/c/fork-points/v3-kde-systemd-SNAP.tar"),
        fork_point_reused=False,
        settle_atoms=("media-video/ffmpeg-6.1.1",),
    )


# --- 6.1 FactoryResult / FactoryError ----------------------------------------


def test_factory_result_is_frozen_and_typed() -> None:
    result = _make_result()
    assert result.pkgdir == Path("/var/cache/kaji/binpkgs/v3")
    assert result.built_atoms == ("media-libs/libsdl2-2.30.5",)
    assert result.phases == ("rebuild", "graphics")
    assert result.fork_point_reused is False
    assert result.settle_atoms == ("media-video/ffmpeg-6.1.1",)
    with pytest.raises(Exception):  # noqa: B017  (frozen → ValidationError)
        result.fork_point_reused = True


def test_factory_error_carries_phase_and_output() -> None:
    err = FactoryError("emerge failed", phase="graphics", output="!!! error log")
    assert isinstance(err, Exception)
    assert err.phase == "graphics"
    assert err.output == "!!! error log"


# --- 6.1 _build_binds --------------------------------------------------------


def test_build_binds_maps_caches_rw_and_repos_ro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KAJI_CACHE", str(tmp_path / "cache"))
    repo_ro = (Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))
    monkeypatch.setattr(factory, "bind_repos", lambda *_a, **_k: [repo_ro], raising=False)

    build_binds: Any = try_import("kaji.factory", "_build_binds")
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


# --- 6.2 (unit) non-root guard -----------------------------------------------


def test_factory_build_non_root_raises_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("trabalho executado antes da guarda de root")

    monkeypatch.setattr(factory, "fetch_stage3", _boom, raising=False)
    monkeypatch.setattr(factory, "extract_stage3", _boom, raising=False)

    f = Factory(_recipe(), Path("/var/cache/kaji/binpkgs/v3"))
    with pytest.raises(Exception):  # noqa: B017  (SystemExit/FactoryError/RuntimeError)
        f.build()


# --- 5.2 (unit) settle_pass empty-breaks no-op -------------------------------


class _NoEmergeContainer:
    """Container falso: qualquer ``run`` falha o teste (settle vazio não emerge)."""

    rootfs = Path("/r")

    def run(self, *_a: object, **_k: object) -> object:
        raise AssertionError("settle_pass com breaks vazio NÃO deve chamar emerge")


def test_settle_pass_empty_breaks_is_noop() -> None:
    settle_pass: Any = try_import("kaji.phases", "settle_pass")
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
    # 6.2 (int): kaji factory v3 minimal systemd produz pkgdir não-vazio +
    # fork-point. Diferido ao host privilegiado real.
    pytest.skip("integração privilegiada: requer host Gentoo seedado (Red diferido)")
