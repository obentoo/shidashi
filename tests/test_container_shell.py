"""UNIT + INTEGRAÇÃO do shell interativo de kaji.container (story 004 grupo 4).

UNIT (R7.2/R8.4): ``_nspawn_shell_argv(rootfs, *, binds, binds_rw)`` é puro e
inspecionável — ``systemd-nspawn --directory <rootfs>`` + o MESMO bloco de
``--bind-ro=``/``--bind=`` de ``_nspawn_argv``, porém SEM comando final (sem o
separador ``--`` nem argv). A paridade do bloco de binds com o run argv é o
contrato chave (R7.2); a ausência de comando final faz o nspawn cair no shell de
login do container.

INTEGRAÇÃO (R7.1/R7.3): ``Container.shell()`` ABRE um shell real no rootfs vivo
(stdio herdado) — exige root + systemd-nspawn + um rootfs seedado e um pty;
host-gated, Red DIFERIDO ao host privilegiado real (PULA em CI/sandbox).

``_nspawn_shell_argv``/``Container.shell`` importados de forma tolerante; até a
impl existir os testes unit ficam Red por símbolo pendente (Red esperado 004).
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from kaji.container import Container, _nspawn_argv
from tests._pending import try_import

_nspawn_shell_argv: Any = try_import("kaji.container", "_nspawn_shell_argv")

_NEEDS_ROOT = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_ROOT, reason="exige root + systemd-nspawn + pty (host Gentoo privilegiado)"
)


# --- 4.1 _nspawn_shell_argv puro (R7.2/R8.4) ---------------------------------


def test_shell_argv_basic_shape_no_trailing_command() -> None:
    argv = _nspawn_shell_argv(Path("/scratch/rootfs"), binds=[], binds_rw=[])
    assert argv[0] == "systemd-nspawn"
    assert "--directory" in argv
    assert argv[argv.index("--directory") + 1] == "/scratch/rootfs"
    # SEM comando final: nenhum separador "--" (cai no shell de login do container)
    assert "--" not in argv


def test_shell_argv_emits_ro_then_rw_binds() -> None:
    ro = [(Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))]
    rw = [(Path("/h/pkgdir"), Path("/var/cache/binpkgs"))]
    argv = _nspawn_shell_argv(Path("/r"), binds=ro, binds_rw=rw)
    ro_flag = "--bind-ro=/var/db/repos/gentoo:/var/db/repos/gentoo"
    rw_flag = "--bind=/h/pkgdir:/var/cache/binpkgs"
    assert ro_flag in argv
    assert rw_flag in argv
    assert argv.index(ro_flag) < argv.index(rw_flag)


def test_shell_argv_bind_block_parity_with_run_argv() -> None:
    # R7.2: o bloco de binds do shell argv == o bloco de binds do run argv.
    ro = [(Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))]
    rw = [
        (Path("/h/pkgdir"), Path("/var/cache/binpkgs")),
        (Path("/h/ccache"), Path("/var/cache/ccache")),
    ]
    run = _nspawn_argv(Path("/r"), ["emerge", "@world"], binds=ro, binds_rw=rw, ephemeral=False)
    shell = _nspawn_shell_argv(Path("/r"), binds=ro, binds_rw=rw)

    def bind_block(argv: list[str]) -> list[str]:
        return [a for a in argv if a.startswith("--bind-ro=") or a.startswith("--bind=")]

    assert bind_block(shell) == bind_block(run)


def test_shell_argv_no_rw_binds_default_empty() -> None:
    argv = _nspawn_shell_argv(Path("/r"), binds=[], binds_rw=())
    assert not any(a.startswith("--bind=") for a in argv)


# --- INTEGRAÇÃO host-gated (R7.1/R7.3 — Red DIFERIDO ao host real) -----------


@_skip_privileged
def test_container_shell_opens_in_live_rootfs(tmp_path: Path) -> None:
    # R7.1/R7.3: shell() ABRE um nspawn no rootfs vivo (stdio herdado) e RETORNA
    # quando o shell sai, SEM teardown do rootfs. Exige root+nspawn+pty: diferido.
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    container = Container(rootfs, ephemeral=False)
    assert hasattr(container, "shell")
    pytest.skip("integração privilegiada: requer rootfs seedado + pty (Red diferido)")
