"""A freshly extracted toolbox can take its /work binds read-only (story 026, task 1.2).

The toolbox tarball has no ``/work``, and a read-only nspawn cannot create a bind's
mount point: "Failed to make parents of …/work/out: Read-only file system" -- measured on
the bentoo-lab on 2026-10-14, the first time a rebuilt toolbox was extracted (the old
tree carried a ``/work`` left by a writable run). The extraction creates ``/work`` and
the toolbox mounts a tmpfs on it, so nspawn can create every ``/work/<name>`` under it;
with an empty ``/work`` alone nspawn still fails ("Failed to create mount point
…/work/out").
"""

import subprocess
from pathlib import Path

import pytest

from shidashi import container, toolbox
from shidashi.toolbox import WORK, Toolbox


def test_the_extraction_creates_the_work_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(toolbox, "restore_fork_point", lambda tarball, rootfs: None)
    tar = tmp_path / "toolbox.tar"
    tar.write_bytes(b"one")
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"

    assert toolbox.ensure_rootfs(tar, root) is True
    assert (root / WORK.relative_to("/")).is_dir()


def test_the_toolbox_mounts_a_tmpfs_on_work_before_its_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmds: list[list[str]] = []

    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(container, "inside_container", lambda: False)
    Toolbox(tmp_path / "v3-systemd", rw={"out": tmp_path / "out"}).run(["true"])
    (cmd,) = cmds
    options = cmd[: cmd.index("--")]
    tmpfs = [i for i, a in enumerate(options) if a.startswith(f"--tmpfs={WORK}")]
    binds = [i for i, a in enumerate(options) if a.startswith(("--bind=", "--bind-ro="))]
    assert len(tmpfs) == 1, options
    assert binds and tmpfs[0] < min(binds), options


def test_an_image_container_gets_no_work_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmds: list[list[str]] = []

    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(container, "inside_container", lambda: False)
    container.Container(tmp_path / "image", ephemeral=False).run(["true"])
    (cmd,) = cmds
    assert not any(a.startswith(f"--tmpfs={WORK}") for a in cmd)


def test_a_current_tree_without_work_gets_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree extracted before this fix is current but has no /work: it gets one too."""
    monkeypatch.setattr(toolbox, "restore_fork_point", lambda tarball, rootfs: None)
    tar = tmp_path / "toolbox.tar"
    tar.write_bytes(b"one")
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    assert toolbox.ensure_rootfs(tar, root) is True
    (root / WORK.relative_to("/")).rmdir()

    assert toolbox.ensure_rootfs(tar, root) is False  # current: not extracted again
    assert (root / WORK.relative_to("/")).is_dir()
