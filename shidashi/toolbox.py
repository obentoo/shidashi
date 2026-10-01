"""The toolbox -- the Bentoo rootfs the ISO tools run in (variants/toolbox).

``mksquashfs``, ``grub-mkrescue`` and ``unsquashfs`` used to run on the build
host, so the ISO carried the HOST's GRUB -- a version no pin recorded, and a
host that had to be Gentoo (or know its distro's package names) to have them.
They now run under ``systemd-nspawn`` in the ``toolbox`` stage's fork point,
built by the factory from the same generation as the image.

The host's paths reach the container through named mounts under :data:`WORK`;
:meth:`Toolbox.path` translates a host path into the container's view, so the
command-line builders of :mod:`shidashi.image` stay pure and unchanged.

:class:`HostTools` runs the same commands on the host, untranslated. Only the
tests and ``scripts/smoke-iso.sh`` (which boots the host's kernel anyway) use
it: the assembler always goes through a :class:`Toolbox`.
"""

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from shidashi.container import Container
from shidashi.phases import restore_fork_point, stage_fork_point_path
from shidashi.recipe import TOOLBOX_STAGE, ResolvedRecipe

__all__ = ["WORK", "HostTools", "Toolbox", "ToolboxError", "Tools", "tarball_path"]

#: Where the host's directories are mounted in the toolbox: ``/work/<name>``.
WORK = Path("/work")

#: What build.json records of the toolbox: tool → the command that prints its version.
_VERSIONS: dict[str, list[str]] = {
    "grub": ["grub-mkrescue", "--version"],
    "squashfs-tools": ["mksquashfs", "-version"],
    "xorriso": ["xorriso", "-version"],
    "mtools": ["mformat", "--version"],
}


class ToolboxError(Exception):
    """The toolbox is missing, or lacks a tool, or a path is not mounted in it."""


class Tools(Protocol):
    """Where :mod:`shidashi.image` and :mod:`shidashi.publish` run their tools."""

    def path(self, host: Path) -> Path:
        """``host`` as the tools see it."""
        ...

    def run(self, argv: Sequence[str]) -> str:
        """Run ``argv``; return its stdout. A failure raises ``CalledProcessError``."""
        ...


class HostTools:
    """The tools of the host, paths untranslated. Tests and scripts/smoke-iso.sh only."""

    def path(self, host: Path) -> Path:
        return host

    def run(self, argv: Sequence[str]) -> str:
        return subprocess.run(list(argv), check=True, capture_output=True, text=True).stdout


def tarball_path(recipe: ResolvedRecipe, *, snapshot: str, fork_points_dir: Path) -> Path:
    """The toolbox's fork point for ``recipe``'s arch × init. Pure.

    The same key as every stage fork point: the toolbox of one arch × init
    serves every image of it.
    """
    return stage_fork_point_path(
        recipe, TOOLBOX_STAGE, snapshot=snapshot, fork_points_dir=fork_points_dir
    )


def _stamp(tarball: Path) -> str:
    stat = tarball.stat()
    return f"{tarball.name} {stat.st_size} {stat.st_mtime_ns}\n"


def ensure_rootfs(tarball: Path, rootfs: Path) -> bool:
    """Extract ``tarball`` into ``rootfs`` unless that tarball is already there.

    Returns whether it extracted. A stamp beside the rootfs (name, size, mtime)
    says which tarball the tree came from; a rebuilt toolbox replaces it.
    PRIVILEGED (ownership and xattrs).
    """
    if not tarball.is_file():
        raise ToolboxError(f"no toolbox at {tarball}")
    stamp = rootfs.with_name(f"{rootfs.name}.stamp")
    if rootfs.is_dir() and stamp.is_file() and stamp.read_text(encoding="utf-8") == _stamp(tarball):
        return False
    stamp.unlink(missing_ok=True)
    shutil.rmtree(rootfs, ignore_errors=True)
    rootfs.mkdir(parents=True)
    restore_fork_point(tarball, rootfs)
    stamp.write_text(_stamp(tarball), encoding="utf-8")
    return True


class Toolbox:
    """The ISO tools in the toolbox rootfs, with the host's directories under :data:`WORK`.

    ``ro`` and ``rw`` name host directories; each is mounted at ``/work/<name>``.
    A host path resolves through the deepest mount that contains it, so the
    image's rootfs can be mounted read-only inside a scratch directory that is
    mounted read-write.
    """

    def __init__(
        self,
        rootfs: Path,
        *,
        ro: Mapping[str, Path] | None = None,
        rw: Mapping[str, Path] | None = None,
        log: Path | None = None,
    ) -> None:
        ro, rw = dict(ro or {}), dict(rw or {})
        if overlap := ro.keys() & rw.keys():
            raise ToolboxError(f"mounted both read-only and read-write: {', '.join(overlap)}")
        self._mounts = {name: host.resolve() for name, host in {**ro, **rw}.items()}
        self.container = Container(
            rootfs,
            ephemeral=False,
            binds=[(host.resolve(), WORK / name) for name, host in ro.items()],
            binds_rw=[(host.resolve(), WORK / name) for name, host in rw.items()],
            log=log,
        )

    def path(self, host: Path) -> Path:
        """``host`` inside the toolbox, through the deepest mount that holds it."""
        resolved = host.resolve()
        best: tuple[int, Path] | None = None
        for name, root in self._mounts.items():
            if resolved == root or root in resolved.parents:
                depth = len(root.parts)
                if best is None or depth > best[0]:
                    best = (depth, WORK / name / resolved.relative_to(root))
        if best is None:
            raise ToolboxError(f"{host} is not under any toolbox mount")
        return best[1]

    def run(self, argv: Sequence[str]) -> str:
        return self.container.run(argv).stdout

    def versions(self) -> dict[str, str]:
        """Each tool's version line, for build.json; also proves the tools are there."""
        found: dict[str, str] = {}
        for tool, argv in _VERSIONS.items():
            try:
                done = self.container.run(argv, check=False)
            except OSError as err:
                raise ToolboxError(f"cannot run the toolbox at {self.container.rootfs}") from err
            output = (done.stdout + done.stderr).strip()
            if done.exit_code != 0 or not output:
                raise ToolboxError(
                    f"the toolbox lacks {argv[0]} ({tool}); rebuild it with "
                    "`shidashi factory <arch> toolbox <init>`"
                )
            found[tool] = output.splitlines()[0]
        return found
