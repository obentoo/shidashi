"""UNIT + INTEGRAÇÃO de shidashi.container.

UNIT (R4.4): ``_nspawn_argv`` é puro e inspecionável — testado sem root.
Contrato (design.md §container): ``_nspawn_argv(rootfs, argv, *, binds, binds_rw,
ephemeral)`` devolve ``["systemd-nspawn", "--directory", str(rootfs),
(opcional "--ephemeral"), ("--bind-ro=src:dst" por bind RO), ("--bind=src:dst"
por bind RW, após os RO), "--", *argv]``. ``CommandResult(exit_code, stdout,
stderr)`` é um value object.

Story 003 (2.2): ``_nspawn_argv`` ganha ``binds_rw`` (emitindo ``--bind=`` após
``--bind-ro=``); ``Container`` aceita e propaga ``binds_rw``. Back-compat
(R7.3): sem ``binds_rw`` o argv é idêntico ao da story 002.

INTEGRAÇÃO (R4.1-R4.3): exige root + systemd-nspawn + um rootfs seedado — gated
com ``@pytest.mark.skipif``. Em CI não-Gentoo / sandbox não-root estes testes
PULAM (Red diferido ao host privilegiado real).
"""

import os
import shutil
from pathlib import Path

import pytest

from shidashi.container import CommandResult, Container, _nspawn_argv

_NEEDS_ROOT = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_ROOT, reason="exige root + systemd-nspawn (host Gentoo privilegiado)"
)


# --- CommandResult value object ----------------------------------------------


def test_command_result_carries_fields() -> None:
    r = CommandResult(exit_code=0, stdout="ok", stderr="")
    assert r.exit_code == 0
    assert r.stdout == "ok"
    assert r.stderr == ""


# --- _nspawn_argv puro (R4.4) ------------------------------------------------


def test_nspawn_argv_basic_shape() -> None:
    argv = _nspawn_argv(
        Path("/scratch/rootfs"),
        ["emerge", "--pretend"],
        binds=[],
        ephemeral=False,
    )
    assert argv[0] == "systemd-nspawn"
    assert "--directory" in argv
    assert argv[argv.index("--directory") + 1] == "/scratch/rootfs"
    # o argv do comando vem após o separador "--"
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["emerge", "--pretend"]


def test_nspawn_argv_ephemeral_flag() -> None:
    with_eph = _nspawn_argv(Path("/r"), ["sh"], binds=[], ephemeral=True)
    without = _nspawn_argv(Path("/r"), ["sh"], binds=[], ephemeral=False)
    assert "--ephemeral" in with_eph
    assert "--ephemeral" not in without


def test_nspawn_argv_emits_ro_binds() -> None:
    binds = [(Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))]
    argv = _nspawn_argv(Path("/r"), ["sh"], binds=binds, ephemeral=False)
    assert "--bind-ro=/var/db/repos/gentoo:/var/db/repos/gentoo" in argv
    # binds vêm antes do separador de comando
    assert argv.index("--bind-ro=/var/db/repos/gentoo:/var/db/repos/gentoo") < argv.index("--")


# --- read-write binds (story 003 2.2 — R7.1/R7.2/R7.3) -----------------------


def test_nspawn_argv_no_rw_binds_is_story002_backcompat() -> None:
    # sem binds_rw o argv é EXATAMENTE o da story 002 (default vazio)
    ro = [(Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))]
    legacy = _nspawn_argv(Path("/r"), ["emerge", "@world"], binds=ro, ephemeral=False)
    explicit_empty = _nspawn_argv(
        Path("/r"), ["emerge", "@world"], binds=ro, binds_rw=(), ephemeral=False
    )
    assert legacy == explicit_empty
    assert not any(a.startswith("--bind=") for a in legacy)


def test_nspawn_argv_emits_rw_binds_after_ro() -> None:
    ro = [(Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))]
    rw = [(Path("/var/cache/shidashi/binpkgs/v3"), Path("/var/cache/binpkgs"))]
    argv = _nspawn_argv(Path("/r"), ["sh"], binds=ro, binds_rw=rw, ephemeral=False)
    ro_flag = "--bind-ro=/var/db/repos/gentoo:/var/db/repos/gentoo"
    rw_flag = "--bind=/var/cache/shidashi/binpkgs/v3:/var/cache/binpkgs"
    assert ro_flag in argv
    assert rw_flag in argv
    # RW vem DEPOIS do RO e ANTES do separador de comando
    assert argv.index(ro_flag) < argv.index(rw_flag) < argv.index("--")


def test_nspawn_argv_rw_binds_in_declared_order() -> None:
    rw = [
        (Path("/h/pkgdir"), Path("/var/cache/binpkgs")),
        (Path("/h/ccache"), Path("/var/cache/ccache")),
        (Path("/h/distfiles"), Path("/var/cache/distfiles")),
    ]
    argv = _nspawn_argv(Path("/r"), ["sh"], binds=[], binds_rw=rw, ephemeral=False)
    rw_flags = [a for a in argv if a.startswith("--bind=")]
    assert rw_flags == [
        "--bind=/h/pkgdir:/var/cache/binpkgs",
        "--bind=/h/ccache:/var/cache/ccache",
        "--bind=/h/distfiles:/var/cache/distfiles",
    ]


def test_container_threads_binds_rw_into_command() -> None:
    # Container guarda binds_rw e o repassa ao _nspawn_argv ao montar o comando.
    rw = [(Path("/h/pkgdir"), Path("/var/cache/binpkgs"))]
    container = Container(Path("/r"), ephemeral=False, binds_rw=rw)
    assert tuple(container.binds_rw) == tuple(rw)
    cmd = _nspawn_argv(
        container.rootfs,
        ["sh"],
        binds=container.binds,
        binds_rw=container.binds_rw,
        ephemeral=container.ephemeral,
    )
    assert "--bind=/h/pkgdir:/var/cache/binpkgs" in cmd


# --- INTEGRAÇÃO host-gated (R4.1-R4.3) ---------------------------------------


@_skip_privileged
def test_container_runs_command_and_tears_down(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    with Container(rootfs, ephemeral=True) as c:
        result = c.run(["true"], check=True)
        assert result.exit_code == 0


@_skip_privileged
def test_container_check_true_raises_on_nonzero(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    with Container(rootfs, ephemeral=True) as c, pytest.raises(Exception):  # noqa: B017
        c.run(["false"], check=True)
