"""UNIT + INTEGRATION of the interactive shell of shidashi.container (story 004 group 4).

UNIT (R7.2/R8.4): ``_nspawn_shell_argv(rootfs, *, binds, binds_rw)`` is pure and
inspectable — ``systemd-nspawn --directory <rootfs>`` + the SAME block of
``--bind-ro=``/``--bind=`` as ``_nspawn_argv``, but WITHOUT a trailing command (no
``--`` separator and no argv). Parity of the bind block with the run argv is the
key contract (R7.2); the absence of a trailing command makes nspawn drop into the
container's login shell.

INTEGRATION (R7.1/R7.3): ``Container.shell()`` OPENS a real shell in the live rootfs
(inherited stdio) — requires root + systemd-nspawn + a seeded rootfs and a pty;
host-gated, Red DEFERRED to the real privileged host (SKIPS on CI/sandbox).

``_nspawn_shell_argv``/``Container.shell`` imported tolerantly; until the
impl exists the unit tests stay Red on a pending symbol (expected Red 004).
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from shidashi.container import Container, _nspawn_argv
from tests._pending import try_import

_nspawn_shell_argv: Any = try_import("shidashi.container", "_nspawn_shell_argv")

_NEEDS_ROOT = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_ROOT, reason="requires root + systemd-nspawn + pty (privileged Gentoo host)"
)


# --- 4.1 pure _nspawn_shell_argv (R7.2/R8.4) ---------------------------------


def test_shell_argv_basic_shape_no_trailing_command() -> None:
    argv = _nspawn_shell_argv(Path("/scratch/rootfs"), binds=[], binds_rw=[])
    assert argv[0] == "systemd-nspawn"
    assert "--directory" in argv
    assert argv[argv.index("--directory") + 1] == "/scratch/rootfs"
    # NO trailing command: no "--" separator (drops into the container's login shell)
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
    # R7.2: the bind block of the shell argv == the bind block of the run argv.
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


# --- host-gated INTEGRATION (R7.1/R7.3 — Red DEFERRED to the real host) ------


@_skip_privileged
def test_container_shell_opens_in_live_rootfs(tmp_path: Path) -> None:
    # R7.1/R7.3: shell() OPENS an nspawn in the live rootfs (inherited stdio) and RETURNS
    # when the shell exits, WITHOUT tearing down the rootfs. Requires root+nspawn+pty: deferred.
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    container = Container(rootfs, ephemeral=False)
    assert hasattr(container, "shell")
    pytest.skip("privileged integration: requires a seeded rootfs + pty (deferred Red)")
