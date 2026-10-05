"""What the build host must provide -- ``shidashi doctor``.

Everything Gentoo-specific runs inside a container: ``emerge`` in the image's
stage3, the ISO tools in the toolbox (:mod:`shidashi.toolbox`), the repositories
from pins (:mod:`shidashi.tree`). What is left on the host is what this module
checks, so that a missing piece fails in a second with its name, not an hour
into a build.

Each check has a scope: ``build`` (factory, assemble, build, pretend refuse to
start without it), ``vm`` (only ``shidashi vm``) or ``optional``. The probes of
the host (``which``, running a command, device access) are parameters, so the
tests describe a host instead of depending on this one.
"""

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = ["MIN_NSPAWN", "Check", "DoctorError", "checks", "missing", "require_build_host"]

#: systemd-nspawn's oldest version with every option Shidashi passes:
#: --console=pipe (242); --as-pid2, --resolv-conf=, --timezone= are older.
MIN_NSPAWN = 242

#: The Python the code is written for (it uses 3.14 syntax).
MIN_PYTHON = (3, 14)

Scope = Literal["build", "vm", "optional", "info"]

Which = Callable[[str], str | None]
Run = Callable[[Sequence[str]], str | None]
Access = Callable[[str, int], bool]


class DoctorError(Exception):
    """The host lacks something a build needs."""


@dataclass(frozen=True)
class Check:
    """One requirement of the host and what was found."""

    name: str
    scope: Scope
    ok: bool
    detail: str


def _run(argv: Sequence[str]) -> str | None:
    """stdout+stderr of ``argv``, or ``None`` when it cannot run or fails."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=30)
    except OSError, subprocess.TimeoutExpired:
        return None
    if done.returncode != 0:
        return None
    return done.stdout + done.stderr


def _tool(name: str, scope: Scope, why: str, which: Which) -> Check:
    path = which(name)
    return Check(name, scope, path is not None, path or f"missing: {why}")


def _nspawn(which: Which, run: Run) -> Check:
    if which("systemd-nspawn") is None:
        return Check(
            "systemd-nspawn",
            "build",
            False,
            "missing: every build runs in it (a systemd host, package systemd-container on Debian)",
        )
    out = run(["systemd-nspawn", "--version"]) or ""
    match = re.search(r"systemd (\d+)", out)
    if match is None:
        return Check("systemd-nspawn", "build", False, "cannot read its version")
    version = int(match.group(1))
    if version < MIN_NSPAWN:
        return Check(
            "systemd-nspawn", "build", False, f"systemd {version}; needs {MIN_NSPAWN} or newer"
        )
    return Check("systemd-nspawn", "build", True, f"systemd {version}")


def _tar(which: Which, run: Run) -> Check:
    if which("tar") is None:
        return Check("tar", "build", False, "missing: GNU tar (stage3, fork points, toolbox)")
    version = run(["tar", "--version"]) or ""
    if "GNU tar" not in version:
        return Check("tar", "build", False, "not GNU tar: the rootfs needs --xattrs and --acls")
    usage = run(["tar", "--help"]) or ""
    lacking = [opt for opt in ("--xattrs", "--acls") if opt not in usage]
    if lacking:
        return Check("tar", "build", False, f"GNU tar without {', '.join(lacking)}")
    return Check("tar", "build", True, version.splitlines()[0])


def _device(path: str, scope: Scope, why: str, access: Access) -> Check:
    ok = access(path, os.R_OK | os.W_OK)
    return Check(path, scope, ok, "read/write" if ok else f"no read/write access: {why}")


def _filesystem(work_dir: Path, run: Run) -> Check:
    # the work dir may not exist yet: its nearest existing parent is where it will be
    probe = work_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    out = run(["findmnt", "-no", "FSTYPE", "-T", str(probe)])
    fstype = out.strip() if out else "unknown"
    detail = f"{fstype} at {work_dir}"
    if fstype == "btrfs":
        detail += " (copies are reflinks; the assemble keeps checkpoints)"
    else:
        detail += (
            " (on btrfs the 8 GB squashfs copy is instant and the assemble"
            " resumes from checkpoints after a failure)"
        )
    return Check("work filesystem", "info", True, detail)


def checks(
    work_dir: Path,
    *,
    which: Which = shutil.which,
    run: Run = _run,
    access: Access = os.access,
    euid: int | None = None,
    python: tuple[int, int] = (sys.version_info.major, sys.version_info.minor),
) -> list[Check]:
    """Every requirement of this host, in the order a build meets them."""
    euid = os.geteuid() if euid is None else euid
    py_ok = python >= MIN_PYTHON
    return [
        Check(
            "python",
            "build",
            py_ok,
            f"{python[0]}.{python[1]}"
            + ("" if py_ok else f"; needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} (uv python install)"),
        ),
        _nspawn(which, run),
        _tar(which, run),
        _tool("gpg", "build", "verifies the stage3 and the ::gentoo snapshot", which),
        _tool("git", "build", "fetches the pinned overlays", which),
        _tool("openssl", "build", "hashes the live user's password", which),
        _tool("objdump", "build", "the ISA check of a built image (binutils)", which),
        Check(
            "root",
            "info",
            euid == 0,
            "running as root" if euid == 0 else "not root: factory, assemble and build need it",
        ),
        _filesystem(work_dir, run),
        _tool("qemu-system-x86_64", "vm", "boots the ISO", which),
        _tool("xorriso", "vm", "reads the ISO's build.json", which),
        _device("/dev/kvm", "vm", "add the user to the kvm group", access),
        _device("/dev/vhost-vsock", "vm", "the test talks to the guest over vsock", access),
        _tool("gcc", "optional", "names this CPU for the ISA check", which),
        _tool("syft", "optional", "the ISO's SBOM", which),
    ]


def missing(found: Sequence[Check], scope: Scope) -> list[Check]:
    """The failed checks of ``scope``."""
    return [c for c in found if c.scope == scope and not c.ok]


def require_build_host(work_dir: Path) -> None:
    """Raise :class:`DoctorError` naming what a build needs and this host lacks."""
    lacking = missing(checks(work_dir), "build")
    if lacking:
        raise DoctorError(
            "this host cannot build: "
            + "; ".join(f"{c.name} ({c.detail})" for c in lacking)
            + " -- run `shidashi doctor`"
        )
