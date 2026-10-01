"""Tests of shidashi.toolbox -- the rootfs the ISO tools run in.

Path translation, the container's binds and the version probe are checked with
the container's ``run`` faked; the extraction with ``restore_fork_point`` faked
(the real one needs root for ownership). No nspawn runs here.
"""

from pathlib import Path

import pytest

from shidashi import config, toolbox
from shidashi.container import CommandResult
from shidashi.recipe import TOOLBOX_STAGE
from shidashi.toolbox import WORK, HostTools, Toolbox, ToolboxError

# --- the stage -------------------------------------------------------------------


def test_the_toolbox_branches_off_the_base_and_is_no_image() -> None:
    recipe = config.load_recipe("v3", TOOLBOX_STAGE, "systemd")
    assert recipe.stages == ("base", "toolbox")
    assert "toolbox" in recipe.sets
    assert not any(p.ships for p in recipe.phases)
    assert TOOLBOX_STAGE not in config.target_names()
    assert TOOLBOX_STAGE in config.factory_names()


def test_the_toolbox_builds_grub_for_bios_and_uefi() -> None:
    make_conf = config.variants_dir() / "toolbox" / "portage" / "make.conf"
    assert 'GRUB_PLATFORMS="pc efi-64"' in make_conf.read_text(encoding="utf-8")


def test_the_tarball_is_the_toolbox_stage_fork_point(tmp_path: Path) -> None:
    recipe = config.load_recipe("v3", "kde", "systemd")
    path = toolbox.tarball_path(recipe, snapshot="SNAP", fork_points_dir=tmp_path)
    assert path == tmp_path / "v3-systemd-SNAP-toolbox.tar"


# --- path translation and binds --------------------------------------------------


def test_a_path_goes_through_the_deepest_mount(tmp_path: Path) -> None:
    scratch, rootfs, out = tmp_path / "scratch", tmp_path / "scratch" / "img", tmp_path / "out"
    tools = Toolbox(tmp_path / "tb", ro={"rootfs": rootfs}, rw={"scratch": scratch, "out": out})
    assert tools.path(rootfs) == WORK / "rootfs"
    assert tools.path(rootfs / "usr" / "bin") == WORK / "rootfs" / "usr" / "bin"
    # beside the image, not inside it: the writable scratch
    assert tools.path(scratch / "img.zstd.squashfs") == WORK / "scratch" / "img.zstd.squashfs"
    assert tools.path(out / "bentoo.iso") == WORK / "out" / "bentoo.iso"


def test_a_path_outside_every_mount_is_refused(tmp_path: Path) -> None:
    tools = Toolbox(tmp_path / "tb", rw={"out": tmp_path / "out"})
    with pytest.raises(ToolboxError, match="not under any toolbox mount"):
        tools.path(tmp_path / "elsewhere")


def test_a_mount_cannot_be_both_read_only_and_writable(tmp_path: Path) -> None:
    with pytest.raises(ToolboxError, match="both"):
        Toolbox(tmp_path / "tb", ro={"x": tmp_path}, rw={"x": tmp_path})


def test_read_only_mounts_bind_read_only(tmp_path: Path) -> None:
    tools = Toolbox(tmp_path / "tb", ro={"rootfs": tmp_path / "r"}, rw={"out": tmp_path / "o"})
    assert tools.container.rootfs == tmp_path / "tb"
    assert tools.container.binds == ((tmp_path / "r", WORK / "rootfs"),)
    assert tools.container.binds_rw == ((tmp_path / "o", WORK / "out"),)
    assert tools.container.ephemeral is False  # an ephemeral Container deletes its rootfs


# --- versions --------------------------------------------------------------------


def _fake_run(
    outputs: dict[str, CommandResult], monkeypatch: pytest.MonkeyPatch, tb: Toolbox
) -> None:
    def run(argv: list[str], **_k: object) -> CommandResult:
        return outputs.get(argv[0], CommandResult(1, "", f"Failed to execute {argv[0]}"))

    monkeypatch.setattr(tb.container, "run", run)


def test_versions_records_the_first_line_of_each_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tb = Toolbox(tmp_path / "tb")
    _fake_run(
        {
            "grub-mkrescue": CommandResult(0, "grub-mkrescue (GRUB) 2.12\n", ""),
            "mksquashfs": CommandResult(0, "mksquashfs version 4.6.1\ncopyright\n", ""),
            "xorriso": CommandResult(0, "xorriso 1.5.6 : RockRidge\nmore\n", ""),
            "mformat": CommandResult(0, "mformat (GNU mtools) 4.0.49\n", ""),
        },
        monkeypatch,
        tb,
    )
    assert tb.versions() == {
        "grub": "grub-mkrescue (GRUB) 2.12",
        "squashfs-tools": "mksquashfs version 4.6.1",
        "xorriso": "xorriso 1.5.6 : RockRidge",
        "mtools": "mformat (GNU mtools) 4.0.49",
    }


def test_a_missing_tool_names_the_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tb = Toolbox(tmp_path / "tb")
    _fake_run(
        {"grub-mkrescue": CommandResult(0, "grub-mkrescue (GRUB) 2.12\n", "")}, monkeypatch, tb
    )
    with pytest.raises(ToolboxError, match="lacks mksquashfs.*shidashi factory"):
        tb.versions()


# --- the rootfs ------------------------------------------------------------------


def test_ensure_rootfs_extracts_once_per_tarball(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extracted: list[Path] = []

    def restore(tarball: Path, rootfs: Path) -> None:
        extracted.append(tarball)
        (rootfs / "usr").mkdir()

    monkeypatch.setattr(toolbox, "restore_fork_point", restore)
    tar = tmp_path / "toolbox.tar"
    tar.write_bytes(b"one")
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"

    assert toolbox.ensure_rootfs(tar, root) is True
    assert toolbox.ensure_rootfs(tar, root) is False  # the same tarball: reused
    assert extracted == [tar]

    tar.write_bytes(b"rebuilt")  # a new toolbox: a fresh tree
    (root / "leftover").write_text("x")
    assert toolbox.ensure_rootfs(tar, root) is True
    assert not (root / "leftover").exists()
    assert len(extracted) == 2


def test_ensure_rootfs_without_a_tarball_raises(tmp_path: Path) -> None:
    with pytest.raises(ToolboxError, match="no toolbox"):
        toolbox.ensure_rootfs(tmp_path / "missing.tar", tmp_path / "root")


def test_host_tools_run_untranslated(tmp_path: Path) -> None:
    tools = HostTools()
    assert tools.path(tmp_path) == tmp_path
    assert tools.run(["echo", "ok"]) == "ok\n"
