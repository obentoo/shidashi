"""Tests of shidashi.slim -- the pure planning helpers of the ``slim`` step (story 021, task 1.2).

The step makes a live rootfs carry only what its image runs. These helpers decide WHAT
goes, on a tree they only read: the world that depclean is left with, the kernel-module
subtrees to drop, the firmware the remaining modules no longer ask for, the paths to
prune, and the commands that went missing. The assembler does the removing.

Every rule that decides whether two names are the SAME (a firmware file and the name a
module asks for, a command and the file it resolves to) is tested from both hostile
sides first: the near-identical name that must stay apart, the differently spelled name
that must come together, then the plain case.

The module is imported inside each test: until it exists, every test fails on the
missing module, not the whole file on collection.

Requirements exercised: R1.1, R1.4, R1.5, R2.1, R2.3, R3.3.
"""

import dataclasses
import importlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _slim() -> Any:
    return importlib.import_module("shidashi.slim")


def _file(path: Path, data: bytes = b"x", *, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _link(path: Path, target: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return path


def _rel(paths: Iterable[Path | str], root: Path) -> set[str]:
    """The returned paths relative to ``root``, whether given absolute or relative."""
    out: set[str] = set()
    for p in paths:
        path = Path(p)
        out.add(path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix())
    return out


# --- world_text / depclean_argv (R1.1) -----------------------------------------------


def test_world_text_is_the_keep_list_one_atom_per_line() -> None:
    keep = ("sys-apps/systemd", "dev-lang/python:3.14", "net-misc/openssh")
    text = _slim().world_text(keep)
    assert isinstance(text, str)
    assert text.endswith("\n")
    lines = text.splitlines()
    # the atoms verbatim -- a slot stays on its atom -- and nothing else
    assert sorted(lines) == sorted(keep)
    assert all(line.strip() == line and line for line in lines)


def test_world_text_names_no_set() -> None:
    """The world must be the keep list alone: a ``@set`` left in it would keep
    everything that set reaches (the selected-sets gotcha)."""
    text = _slim().world_text(("sys-apps/systemd",))
    assert "@" not in text


def test_depclean_argv_cleans_the_whole_world_without_build_deps() -> None:
    argv = _slim().depclean_argv()
    assert argv[:2] == ["emerge", "--depclean"]
    assert "--with-bdeps=n" in argv
    # no atom named: a named depclean removes only those atoms, not what the world drops
    assert all(arg.startswith("-") for arg in argv[1:])


# --- module_dirs_to_drop (R2.1) ------------------------------------------------------


def _kernel(tmp_path: Path) -> Path:
    """``usr/lib/modules/<kver>`` with network, storage and graphics subtrees."""
    kver_root = tmp_path / "rootfs" / "usr/lib/modules/7.2.6-gentoo-dist"
    kernel = kver_root / "kernel"
    for rel in (
        "drivers/gpu/drm/amd/amdgpu/amdgpu.ko.zst",
        "drivers/media/usb/uvc/uvcvideo.ko.zst",
        "drivers/net/ethernet/mellanox/mlx5/core/mlx5_core.ko.zst",
        "drivers/net/ethernet/intel/e1000e/e1000e.ko.zst",
        "drivers/net/ethernet/realtek/r8169.ko.zst",
        "drivers/net/wireless/intel/iwlwifi/iwlwifi.ko.zst",
        "drivers/nvme/host/nvme.ko.zst",
        "sound/core/snd.ko.zst",
        "fs/btrfs/btrfs.ko.zst",
    ):
        _file(kernel / rel)
    _file(kver_root / "modules.dep", b"kernel/fs/btrfs/btrfs.ko.zst:\n")
    return kver_root


def _dropped(kver_root: Path, result: Iterable[Path | str]) -> set[str]:
    """The returned directories as paths under ``kernel/``, absolute or relative."""
    kernel = kver_root / "kernel"
    out: set[str] = set()
    for p in result:
        path = Path(p)
        if not path.is_absolute():
            path = kernel / path if (kernel / path).exists() else kver_root / path
        out.add(path.relative_to(kernel).as_posix())
    return out


def test_module_dirs_to_drop_names_the_existing_subtrees(tmp_path: Path) -> None:
    kver_root = _kernel(tmp_path)
    drop = ("drivers/gpu", "drivers/media", "sound")
    got = _slim().module_dirs_to_drop(kver_root, drop)
    assert _dropped(kver_root, got) == {"drivers/gpu", "drivers/media", "sound"}


def test_dropping_one_vendor_keeps_its_siblings_and_parents(tmp_path: Path) -> None:
    """Hostile: dropping ``drivers/net/ethernet/mellanox`` must not take the wired
    network of every other vendor (or Wi-Fi) with it."""
    kver_root = _kernel(tmp_path)
    got = _slim().module_dirs_to_drop(kver_root, ("drivers/net/ethernet/mellanox",))
    assert _dropped(kver_root, got) == {"drivers/net/ethernet/mellanox"}


def test_a_subtree_this_kernel_lacks_is_not_an_error(tmp_path: Path) -> None:
    """Kernel layouts move between versions: a group that is not there is skipped."""
    kver_root = _kernel(tmp_path)
    got = _slim().module_dirs_to_drop(kver_root, ("drivers/gpu", "drivers/accel", "drivers/isdn"))
    assert _dropped(kver_root, got) == {"drivers/gpu"}


def test_module_dirs_to_drop_only_reads_the_tree(tmp_path: Path) -> None:
    kver_root = _kernel(tmp_path)
    _slim().module_dirs_to_drop(kver_root, ("drivers/gpu", "sound"))
    assert (kver_root / "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko.zst").is_file()
    assert (kver_root / "kernel/sound/core/snd.ko.zst").is_file()


# --- firmware_wanted / firmware_to_delete (R2.3) --------------------------------------

#: ``modinfo -F firmware`` over the modules that REMAIN: amdgpu's request is absent
#: because its module was dropped.
_MODINFO = """\
iwlwifi-cc-a0-77.ucode
iwlwifi-so-a0-gf-a0.pnvm

rtl_nic/rtl8168h-2.fw
rtl_nic/rtl8168h-2.fw
brcm/brcmfmac43455-sdio.txt
qcom/alias.mbn
ath/aliased-elsewhere.bin
"""


def test_firmware_wanted_parses_one_name_per_line() -> None:
    wanted = _slim().firmware_wanted(_MODINFO.splitlines())
    assert {
        "iwlwifi-cc-a0-77.ucode",
        "iwlwifi-so-a0-gf-a0.pnvm",
        "rtl_nic/rtl8168h-2.fw",
        "brcm/brcmfmac43455-sdio.txt",
        "qcom/alias.mbn",
    } <= set(wanted)
    assert "" not in wanted  # blank lines are not a firmware named ""


def _firmware(tmp_path: Path) -> Path:
    """``usr/lib/firmware`` as linux-firmware[compress-zstd] installs it, with the
    names a careless match would confuse."""
    fw = tmp_path / "rootfs" / "usr/lib/firmware"
    for rel in (
        # wanted, under the compressed names linux-firmware installs
        "iwlwifi-cc-a0-77.ucode.zst",
        "iwlwifi-so-a0-gf-a0.pnvm.xz",
        # wanted, uncompressed
        "rtl_nic/rtl8168h-2.fw",
        # look-alikes of wanted names: other files, to delete
        "rtl_nic/rtl8168h-2.fw.bak",
        "rtl_nic/rtl8168h-2.fw2",
        "rtl8168h-2.fw",
        "other/rtl_nic/rtl8168h-2.fw",
        "iwlwifi-cc-a0-77.ucode.gz",
        # asked only by a dropped module (amdgpu): to delete
        "amdgpu/navi10_gpu_info.bin.zst",
        # asked by nothing
        "mellanox/mlx5_core.bin.zst",
        "intel/ibt-12-16.sfi.zst",
        # kept whatever the modules ask: microcode and the regulatory database
        "intel-ucode/06-9e-0a",
        "amd-ucode/microcode_amd_fam17h.bin",
        "regulatory.db",
        "regulatory.db.p7s",
        # the blobs the symlinks below point at
        "qcom/real.mbn",
        "brcm/brcmfmac43455-sdio.raspberrypi,4-model-b.txt.zst",
    ):
        _file(fw / rel)
    # a wanted alias: the link and the blob it names both stay
    _link(fw / "qcom/alias.mbn", "real.mbn")
    # a wanted alias under its compressed name, pointing into its own directory by ../
    _link(
        fw / "brcm/brcmfmac43455-sdio.txt.zst",
        "../brcm/brcmfmac43455-sdio.raspberrypi,4-model-b.txt.zst",
    )
    # a wanted name whose blob lives in another directory
    _link(fw / "ath/aliased-elsewhere.bin", "../shared/elsewhere.bin")
    _file(fw / "shared/elsewhere.bin")
    # an alias nobody asks for, pointing at a blob nobody asks for by name
    _link(fw / "qcom/unasked-alias.mbn", "unasked-blob.mbn")
    _file(fw / "qcom/unasked-blob.mbn")
    # an alias nobody asks for, pointing at a WANTED file: the link goes, the file stays
    _link(fw / "rtl_nic/old-name.fw", "rtl8168h-2.fw")
    return fw


_KEEP_FIRMWARE = ("intel-ucode/*", "amd-ucode/*", "regulatory.db*")


def _to_delete(fw: Path) -> set[str]:
    slim = _slim()
    wanted = slim.firmware_wanted(_MODINFO.splitlines())
    return _rel(slim.firmware_to_delete(fw, wanted, _KEEP_FIRMWARE), fw)


def test_a_look_alike_of_a_wanted_name_is_deleted(tmp_path: Path) -> None:
    """Hostile collapse: a backup, a longer name, the same basename in another
    directory, a suffix that is not a compression linux-firmware uses -- each is
    another file, and none is what the module asked for."""
    deleted = _to_delete(_firmware(tmp_path))
    assert {
        "rtl_nic/rtl8168h-2.fw.bak",
        "rtl_nic/rtl8168h-2.fw2",
        "rtl8168h-2.fw",
        "other/rtl_nic/rtl8168h-2.fw",
        "iwlwifi-cc-a0-77.ucode.gz",
    } <= deleted


def test_a_compressed_file_matches_the_bare_name_a_module_asks_for(tmp_path: Path) -> None:
    """Hostile split: modinfo prints ``name``, linux-firmware installs ``name.zst`` or
    ``name.xz`` -- the same firmware, which must stay."""
    deleted = _to_delete(_firmware(tmp_path))
    assert "iwlwifi-cc-a0-77.ucode.zst" not in deleted
    assert "iwlwifi-so-a0-gf-a0.pnvm.xz" not in deleted
    assert "rtl_nic/rtl8168h-2.fw" not in deleted


def test_firmware_only_a_dropped_module_asked_for_is_deleted(tmp_path: Path) -> None:
    deleted = _to_delete(_firmware(tmp_path))
    assert "amdgpu/navi10_gpu_info.bin.zst" in deleted
    assert "mellanox/mlx5_core.bin.zst" in deleted
    assert "intel/ibt-12-16.sfi.zst" in deleted  # intel-ucode/* does not reach intel/


def test_microcode_and_the_regulatory_database_are_kept(tmp_path: Path) -> None:
    deleted = _to_delete(_firmware(tmp_path))
    for kept in (
        "intel-ucode/06-9e-0a",
        "amd-ucode/microcode_amd_fam17h.bin",
        "regulatory.db",
        "regulatory.db.p7s",
    ):
        assert kept not in deleted


def test_a_wanted_symlink_keeps_its_target(tmp_path: Path) -> None:
    """Vendors alias many names to one blob: the module asks for the alias, and
    deleting the blob would leave a dangling link."""
    deleted = _to_delete(_firmware(tmp_path))
    assert "qcom/alias.mbn" not in deleted
    assert "qcom/real.mbn" not in deleted
    # the alias under its compressed name, its blob reached through ../
    assert "brcm/brcmfmac43455-sdio.txt.zst" not in deleted
    assert "brcm/brcmfmac43455-sdio.raspberrypi,4-model-b.txt.zst" not in deleted
    # the blob in another directory
    assert "ath/aliased-elsewhere.bin" not in deleted
    assert "shared/elsewhere.bin" not in deleted


def test_an_unwanted_symlink_goes_and_keeps_nothing_alive(tmp_path: Path) -> None:
    deleted = _to_delete(_firmware(tmp_path))
    assert {"qcom/unasked-alias.mbn", "qcom/unasked-blob.mbn"} <= deleted
    # a link nobody asks for goes, even when the file it points at is wanted
    assert "rtl_nic/old-name.fw" in deleted
    assert "rtl_nic/rtl8168h-2.fw" not in deleted


def test_firmware_to_delete_lists_files_only_and_reads_only(tmp_path: Path) -> None:
    fw = _firmware(tmp_path)
    before = sorted(str(p) for p in fw.rglob("*"))
    deleted = _to_delete(fw)
    assert deleted  # it did list something ...
    # ... never a directory, and it removed nothing itself
    assert not any((fw / rel).is_dir() and not (fw / rel).is_symlink() for rel in deleted)
    assert sorted(str(p) for p in fw.rglob("*")) == before


# --- prune_paths (R1.5) --------------------------------------------------------------

_PRUNE = (
    "usr/share/doc/**",
    "usr/share/man/**",
    "usr/include/**",
    "usr/libexec/gcc/**",
    "usr/share/locale/**",
    "**/*.a",
)


def _pruneable(tmp_path: Path) -> Path:
    root = tmp_path / "rootfs"
    for rel in (
        "usr/share/doc/openssh-10.2/README.zst",
        "usr/share/doc/openssh-10.2/html/index.html",
        "usr/share/man/man8/sshd.8.zst",
        "usr/include/stdio.h",
        "usr/include/linux/types.h",
        "usr/libexec/gcc/x86_64-pc-linux-gnu/15/cc1",
        "usr/share/locale/de/LC_MESSAGES/sudo.mo",
        "usr/lib64/libz.a",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libgcc.a",
        # kept: look-alikes of pruned paths, and the runtime the compiler leaves
        "usr/share/doc-base/openssh",
        "usr/share/docs/keep.txt",
        "usr/lib/locale/C.utf8/LC_CTYPE",
        "usr/lib/locale/locale-archive",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libgcc_s.so.1",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libstdc++.so.6",
        "usr/lib64/libbar.am",
        "usr/lib64/libz.a.so",
        "usr/bin/a",
    ):
        _file(root / rel)
    return root


def test_prune_paths_matches_the_globs_at_any_depth(tmp_path: Path) -> None:
    root = _pruneable(tmp_path)
    got = _rel(_slim().prune_paths(root, _PRUNE), root)
    assert {
        "usr/share/doc/openssh-10.2/README.zst",
        "usr/share/doc/openssh-10.2/html/index.html",
        "usr/share/man/man8/sshd.8.zst",
        "usr/include/stdio.h",
        "usr/include/linux/types.h",
        "usr/libexec/gcc/x86_64-pc-linux-gnu/15/cc1",
        "usr/share/locale/de/LC_MESSAGES/sudo.mo",
        "usr/lib64/libz.a",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libgcc.a",
    } <= got


def test_prune_paths_keeps_look_alikes_and_the_compiler_runtime(tmp_path: Path) -> None:
    """Hostile: ``usr/share/doc/**`` is not every path that starts with those
    letters, ``usr/share/locale`` is not the compiled locales in ``usr/lib/locale``,
    ``*.a`` is not every name containing ``.a``, and pruning gcc's internals keeps
    the libraries every C++ program loads."""
    root = _pruneable(tmp_path)
    got = _rel(_slim().prune_paths(root, _PRUNE), root)
    for kept in (
        "usr/share/doc-base/openssh",
        "usr/share/docs/keep.txt",
        "usr/lib/locale/C.utf8/LC_CTYPE",
        "usr/lib/locale/locale-archive",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libgcc_s.so.1",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libstdc++.so.6",
        "usr/lib64/libbar.am",
        "usr/lib64/libz.a.so",
        "usr/bin/a",
    ):
        assert kept not in got
        assert not any(kept.startswith(p + "/") for p in got)  # nor a directory holding it


def test_prune_paths_never_follows_a_symlink_out_of_the_rootfs(tmp_path: Path) -> None:
    """An absolute symlink in the image points at the BUILD HOST when followed from
    outside the container: pruning through it would list the host's own files."""
    root = _pruneable(tmp_path)
    host = tmp_path / "host"
    _file(host / "usr/lib/libhost.a")
    _file(host / "doc/HOST-README")
    _link(root / "usr/lib64/hostlib", str(host / "usr/lib"))
    _link(root / "usr/share/doc/escape", str(host / "doc"))
    got = _slim().prune_paths(root, _PRUNE)
    assert got  # the image's own files are still listed
    for p in got:
        path = Path(p) if Path(p).is_absolute() else root / p
        assert ".." not in Path(p).parts
        assert path.parent.resolve().is_relative_to(root.resolve()), p
    rel = _rel(got, root)
    assert not any(r.startswith("usr/lib64/hostlib/") for r in rel)
    assert not any(r.startswith("usr/share/doc/escape/") for r in rel)
    assert (host / "usr/lib/libhost.a").is_file() and (host / "doc/HOST-README").is_file()


def test_prune_paths_only_reads_and_tolerates_globs_that_match_nothing(tmp_path: Path) -> None:
    root = _pruneable(tmp_path)
    before = sorted(str(p) for p in root.rglob("*"))
    got = _slim().prune_paths(root, ("usr/share/gtk-doc/**", "usr/src/**", *_PRUNE))
    assert got
    assert sorted(str(p) for p in root.rglob("*")) == before


# --- missing_commands (R1.3, R1.4) ----------------------------------------------------


def _commands_root(tmp_path: Path) -> Path:
    root = tmp_path / "rootfs"
    _file(root / "usr/bin/systemd-run", mode=0o755)
    _file(root / "usr/bin/python3.14", mode=0o755)
    _link(root / "usr/bin/python3", "python3.14")  # relative
    # absolute, as the image sees it: re-rooted, it exists
    _link(root / "usr/bin/podman", "/usr/libexec/podman/podman")
    _file(root / "usr/libexec/podman/podman", mode=0o755)
    # merged-usr: /usr/sbin is a relative link to bin
    _file(root / "usr/bin/sshd", mode=0o755)
    _link(root / "usr/sbin", "bin")
    _file(root / "usr/bin/README", mode=0o644)  # not executable
    (root / "usr/bin/a-directory").mkdir(mode=0o755)
    _link(root / "usr/bin/dangling", "/usr/lib/nowhere/dangling")
    _link(root / "usr/bin/loop", "loop")
    return root


def test_present_commands_are_not_missing(tmp_path: Path) -> None:
    root = _commands_root(tmp_path)
    commands = ("/usr/bin/systemd-run", "/usr/bin/python3", "/usr/bin/podman", "/usr/sbin/sshd")
    assert list(_slim().missing_commands(root, commands)) == []


def test_every_missing_command_is_listed(tmp_path: Path) -> None:
    """R1.4: every one, not only the first -- absent, not executable, a directory,
    a dangling link, a link loop."""
    root = _commands_root(tmp_path)
    commands = (
        "/usr/bin/systemd-run",
        "/usr/bin/gone",
        "/usr/bin/README",
        "/usr/bin/a-directory",
        "/usr/bin/dangling",
        "/usr/bin/loop",
        "/usr/sbin/never-there",
    )
    got = _slim().missing_commands(root, commands)
    assert sorted(got) == sorted(commands[1:])


def test_an_absolute_link_is_resolved_in_the_rootfs_not_on_the_host(tmp_path: Path) -> None:
    """Hostile collapse: a link to ``/bin/sh`` exists on any build host; inside an
    image without ``/bin/sh`` it is missing. Resolved on the host, the check would
    pass an image that cannot run the command."""
    root = _commands_root(tmp_path)
    assert os.path.exists("/bin/sh")  # the host has it ...
    _link(root / "usr/bin/needs-sh", "/bin/sh")  # ... the image does not
    # relative, climbing past the image's root: still the image's /bin/sh
    _link(root / "usr/bin/climbs-out", "../../../../../../../../bin/sh")
    got = _slim().missing_commands(root, ("/usr/bin/needs-sh", "/usr/bin/climbs-out"))
    assert sorted(got) == ["/usr/bin/climbs-out", "/usr/bin/needs-sh"]


def test_an_absolute_link_to_a_file_only_the_image_has_is_present(tmp_path: Path) -> None:
    """Hostile split: the target exists in the rootfs only, never on the host."""
    root = _commands_root(tmp_path)
    target = "/opt/shidashi-test-only-021/tool"
    assert not os.path.exists(target)
    _file(root / target.lstrip("/"), mode=0o755)
    _link(root / "usr/bin/tool", target)
    assert list(_slim().missing_commands(root, ("/usr/bin/tool",))) == []


# --- SlimReport (R3.3) ---------------------------------------------------------------


def test_slim_report_carries_what_the_step_removed_and_the_sizes() -> None:
    report = _slim().SlimReport(
        removed_packages=("app-misc/unwanted-1.0",),
        dropped_module_dirs=("drivers/gpu",),
        deleted_firmware_files=("amdgpu/navi10_gpu_info.bin.zst",),
        pruned_files=("usr/share/doc/x/README",),
        bytes_before=4_528_000_000,
        bytes_after=1_200_000_000,
    )
    assert dataclasses.is_dataclass(report)
    fields = dataclasses.asdict(report)  # type: ignore[arg-type]  # is_dataclass narrows Any to instance | class
    assert fields["bytes_before"] == 4_528_000_000 and fields["bytes_after"] == 1_200_000_000
    assert list(fields["removed_packages"]) == ["app-misc/unwanted-1.0"]
    assert list(fields["dropped_module_dirs"]) == ["drivers/gpu"]
