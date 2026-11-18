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

A check's ``detail`` is built in the language shown (:mod:`shidashi.i18n`);
:func:`require_build_host` builds it in English, since it ends up in an error.
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

from shidashi import i18n, isacheck, vmrunner
from shidashi.recipe import TOOLBOX_STAGE

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


#: The binary whose absence sends a boot test to the runner container.
_QEMU = "qemu-system-x86_64"

#: The vm tools the runner container stands in for, and what each is for.
_VM_TOOLS = (
    (_QEMU, i18n.mark("boots the ISO")),
    ("xorriso", i18n.mark("reads the ISO's build.json")),
)

#: The runner container's name, as the ok detail names it.
_RUNNER = f"{vmrunner.RUNNER_ARCH}-{vmrunner.RUNNER_INIT}"

#: What builds the runner tarball on a node, or brings it from the host.
_TARBALL_FIX = (
    f"shidashi factory {vmrunner.RUNNER_ARCH} {TOOLBOX_STAGE} {vmrunner.RUNNER_INIT}"
    f" on this node, or shidashi worker sync push <worker> --arch {vmrunner.RUNNER_ARCH}"
    " from the host"
)


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
    return Check(name, scope, path is not None, path or _missing(why))


def _missing(why: str) -> str:
    """``missing: <why>``, ``why`` marked where it was written."""
    return i18n.t("missing: {why}").format(why=i18n.t(why))


def _runner_lacks(which: Which, access: Access) -> list[str]:
    """What the boot-test runner lacks on this node, each with its fix; empty when
    podman and the runner tarball are both here. Never raises: a toolbox that cannot
    be resolved is a missing tarball, its reason named."""
    lacking: list[str] = []
    if which("podman") is None:
        lacking.append(i18n.t("podman (emerge app-containers/podman)"))
    try:
        tarball = vmrunner.runner_tarball()
    except vmrunner.VmRunnerError as err:
        lacking.append(
            i18n.t("the runner tarball ({error}; then {fix})").format(error=err, fix=_TARBALL_FIX)
        )
    else:
        if not access(str(tarball), os.R_OK):
            lacking.append(
                i18n.t("the runner tarball {path} ({fix})").format(path=tarball, fix=_TARBALL_FIX)
            )
    return lacking


def _vm_tools(which: Which, access: Access) -> list[Check]:
    """``qemu-system-x86_64`` and ``xorriso``: on PATH with QEMU here; otherwise
    served by the runner container when it can run, failed naming its fix when not."""
    if which(_QEMU) is not None:
        return [_tool(name, "vm", why, which) for name, why in _VM_TOOLS]
    lacking = _runner_lacks(which, access)
    if not lacking:
        here = i18n.t("missing here: boot tests run in the {runner} toolbox container")
        return [Check(name, "vm", True, here.format(runner=_RUNNER)) for name, _why in _VM_TOOLS]
    runner = i18n.t("the boot-test runner lacks {what}").format(what="; ".join(lacking))
    # A tool on PATH is unused here: without QEMU the whole boot test is the runner's.
    return [
        Check(
            name,
            "vm",
            False,
            f"{_missing(why)}; {runner}"
            if (path := which(name)) is None
            else i18n.t("{path} unused without {qemu}; {runner}").format(
                path=path, qemu=_QEMU, runner=runner
            ),
        )
        for name, why in _VM_TOOLS
    ]


def _gcc(which: Which, run: Run) -> Check:
    # Present is not enough: the slim worker keeps the gcc driver but prunes cc1,
    # and then `-march=native` names nothing and the ISA check is skipped.
    if which("gcc") is None:
        return Check(
            "gcc", "optional", False, _missing(i18n.mark("names this CPU for the ISA check"))
        )
    arch = isacheck.march_from(run(["gcc", "-march=native", "-Q", "--help=target"]) or "")
    if arch is None:
        return Check(
            "gcc",
            "optional",
            False,
            i18n.t("cannot name this CPU (cc1 missing?): the ISA check of built images is skipped"),
        )
    return Check("gcc", "optional", True, i18n.t("names this CPU: {arch}").format(arch=arch))


def _nspawn(which: Which, run: Run) -> Check:
    if which("systemd-nspawn") is None:
        return Check(
            "systemd-nspawn",
            "build",
            False,
            _missing(
                i18n.mark(
                    "every build runs in it (a systemd host, package systemd-container on Debian)"
                )
            ),
        )
    out = run(["systemd-nspawn", "--version"]) or ""
    match = re.search(r"systemd (\d+)", out)
    if match is None:
        return Check("systemd-nspawn", "build", False, i18n.t("cannot read its version"))
    version = int(match.group(1))
    if version < MIN_NSPAWN:
        needs = i18n.t("systemd {version}; needs {minimum} or newer")
        return Check(
            "systemd-nspawn", "build", False, needs.format(version=version, minimum=MIN_NSPAWN)
        )
    return Check("systemd-nspawn", "build", True, f"systemd {version}")


def _tar(which: Which, run: Run) -> Check:
    if which("tar") is None:
        return Check(
            "tar", "build", False, _missing(i18n.mark("GNU tar (stage3, fork points, toolbox)"))
        )
    version = run(["tar", "--version"]) or ""
    if "GNU tar" not in version:
        return Check(
            "tar", "build", False, i18n.t("not GNU tar: the rootfs needs --xattrs and --acls")
        )
    usage = run(["tar", "--help"]) or ""
    lacking = [opt for opt in ("--xattrs", "--acls") if opt not in usage]
    if lacking:
        without = i18n.t("GNU tar without {options}").format(options=", ".join(lacking))
        return Check("tar", "build", False, without)
    return Check("tar", "build", True, version.splitlines()[0])


def _device(path: str, scope: Scope, why: str, access: Access) -> Check:
    ok = access(path, os.R_OK | os.W_OK)
    detail = (
        i18n.t("read/write")
        if ok
        else i18n.t("no read/write access: {why}").format(why=i18n.t(why))
    )
    return Check(path, scope, ok, detail)


def _filesystem(work_dir: Path, run: Run) -> Check:
    # the work dir may not exist yet: its nearest existing parent is where it will be
    probe = work_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    out = run(["findmnt", "-no", "FSTYPE", "-T", str(probe)])
    fstype = out.strip() if out else i18n.t("unknown")
    if fstype == "btrfs":
        detail = i18n.t("{fstype} at {path} (copies are reflinks; the assemble keeps checkpoints)")
    else:
        detail = i18n.t(
            "{fstype} at {path} (on btrfs the 8 GB squashfs copy is instant and the assemble"
            " resumes from checkpoints after a failure)"
        )
    return Check("work filesystem", "info", True, detail.format(fstype=fstype, path=work_dir))


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
            if py_ok
            else i18n.t("{found}; needs {minimum} (uv python install)").format(
                found=f"{python[0]}.{python[1]}", minimum=f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
            ),
        ),
        _nspawn(which, run),
        _tar(which, run),
        _tool("gpg", "build", i18n.mark("verifies the stage3 and the ::gentoo snapshot"), which),
        _tool("git", "build", i18n.mark("fetches the pinned overlays"), which),
        _tool("openssl", "build", i18n.mark("hashes the live user's password"), which),
        _tool("objdump", "build", i18n.mark("the ISA check of a built image (binutils)"), which),
        Check(
            "root",
            "info",
            euid == 0,
            i18n.t("running as root")
            if euid == 0
            else i18n.t("not root: factory, assemble and build need it"),
        ),
        _filesystem(work_dir, run),
        *_vm_tools(which, access),
        _device("/dev/kvm", "vm", i18n.mark("add the user to the kvm group"), access),
        _device(
            "/dev/vhost-vsock", "vm", i18n.mark("the test talks to the guest over vsock"), access
        ),
        _gcc(which, run),
        _tool("syft", "optional", i18n.mark("the ISO's SBOM"), which),
    ]


def missing(found: Sequence[Check], scope: Scope) -> list[Check]:
    """The failed checks of ``scope``."""
    return [c for c in found if c.scope == scope and not c.ok]


def require_build_host(work_dir: Path) -> None:
    """Raise :class:`DoctorError` naming what a build needs and this host lacks (in English)."""
    with i18n.english():
        lacking = missing(checks(work_dir), "build")
    if lacking:
        raise DoctorError(
            "this host cannot build: "
            + "; ".join(f"{c.name} ({c.detail})" for c in lacking)
            + " -- run `shidashi doctor`"
        )
