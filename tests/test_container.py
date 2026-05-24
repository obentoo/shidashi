"""UNIT + INTEGRAÇÃO de kaji.container.

UNIT (R4.4): ``_nspawn_argv`` é puro e inspecionável — testado sem root.
Contrato (design.md §container): ``_nspawn_argv(rootfs, argv, *, binds,
ephemeral)`` devolve ``["systemd-nspawn", "--directory", str(rootfs),
(opcional "--ephemeral"), ("--bind-ro=src:dst" por bind), "--", *argv]``.
``CommandResult(exit_code, stdout, stderr)`` é um value object.

INTEGRAÇÃO (R4.1-R4.3): exige root + systemd-nspawn + um rootfs seedado — gated
com ``@pytest.mark.skipif``. Em CI não-Gentoo / sandbox não-root estes testes
PULAM (Red diferido ao host privilegiado real).
"""

import os
import shutil
from pathlib import Path

import pytest

from kaji.container import CommandResult, Container, _nspawn_argv

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
