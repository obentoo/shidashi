"""Tests of the worker's ``slim:`` section, read from the shipped variants tree (v3).

No image is built: like tests/test_worker_rootfs.py these load the repository's real
``variants/`` through ``load_system_config`` and prove what the slim worker keeps --
the build oven's runtime and the console rescue kit (R1.1, R1.3), the module groups it
drops and the ones it must never drop (R2.1), the firmware it always keeps (R2.3), the
paths it prunes and the ones it must never prune (R1.5), its 600 MiB budget (R3.1) --
and that no other image declares ``slim`` at all (R1.6).

The commands the worker's own scripts run are read from those scripts (an AST walk of
kyomei_worker.py and worker_disk.py), so a command added there later and not to
``slim.commands`` fails here.

Requirements exercised: R1.1, R1.3, R1.5, R1.6, R2.1, R2.3, R3.1.
"""

import ast
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from shidashi import config
from shidashi.system import load_system_config


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


ROOT = _repo_root()
VARIANTS = ROOT / "variants"
LIB = VARIANTS / "worker" / "rootfs" / "usr" / "local" / "lib" / "shidashi"
WORKER_SCRIPTS = (LIB / "kyomei_worker.py", LIB / "worker_disk.py")


@pytest.fixture(autouse=True)
def _point_at_real_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(VARIANTS))


def _cfg(target: str, init: str = "systemd") -> Any:
    recipe = config.load_recipe("v3", target, init)
    return load_system_config(recipe, variants_dir=VARIANTS)


def _slim() -> Any:
    slim = _cfg("worker").slim
    assert slim is not None, "the worker's merged system config declares no slim:"
    return slim


# --- helpers (pure) -----------------------------------------------------------------

_OPERATORS = re.compile(r"^[<>=~!]+")
_VERSION = re.compile(r"-\d[^/]*$")


def _package(atom: str) -> str:
    """``category/name`` of an atom, without operator, version, slot or USE."""
    bare = _OPERATORS.sub("", atom).split("[", 1)[0].split(":", 1)[0]
    return _VERSION.sub("", bare)


def _drops(entry: str, subtree: str) -> bool:
    """Whether deleting the ``entry`` subtree deletes ``subtree`` (path segments,
    never string prefixes: ``drivers/net/ethernet/mellanox`` does not drop
    ``drivers/net``, ``drivers/gpu`` does not drop ``drivers/gpio``)."""
    e = entry.strip("/")
    return subtree == e or subtree.startswith(e + "/")


def _matches(globs: tuple[str, ...], path: str) -> list[str]:
    """The globs of ``globs`` that select ``path`` (relative to the rootfs)."""
    target = PurePosixPath(path)
    return [g for g in globs if target.full_match(g.lstrip("/"))]


_COMMAND_WORD = re.compile(r"^(/[\w./+-]+|[a-z][\w.+-]*)$")


def _script_commands(script: Path) -> set[str]:
    """Basenames of every external command ``script`` runs: each list literal whose
    first element is a command word (a string, or a module-level string constant)."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    constants: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    found: set[str] = set()
    for item in ast.walk(tree):
        if not isinstance(item, ast.List) or not item.elts:
            continue
        first = item.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            word = first.value
        elif isinstance(first, ast.Name) and first.id in constants:
            word = constants[first.id]
        else:
            continue
        if _COMMAND_WORD.match(word):
            found.add(PurePosixPath(word).name)
    return found


def test_hostile_the_helpers_compare_path_segments_not_strings() -> None:
    # wrongly collapse: a deeper subtree is not its parent, a sibling is not a prefix
    assert not _drops("drivers/net/ethernet/mellanox", "drivers/net")
    assert not _drops("drivers/gpu", "drivers/gpio")
    assert not _drops("sound", "drivers/soundwire")
    # wrongly split: a parent (with or without a slash) drops every child
    assert _drops("drivers", "drivers/net")
    assert _drops("drivers/net/", "drivers/net/wireless")
    assert _drops("/drivers/usb", "drivers/usb")
    assert _package(">=dev-lang/python-3.14.0:3.14[ssl]") == "dev-lang/python"
    assert _package("sys-apps/nvme-cli") == "sys-apps/nvme-cli"
    assert "sgdisk" in _script_commands(LIB / "worker_disk.py")


# --- only the worker is slimmed (R1.6) ----------------------------------------------


@pytest.mark.parametrize("init", ["systemd", "openrc"])
@pytest.mark.parametrize("target", ["minimal", "kde", "gnome", "wm", "toolbox"])
def test_no_other_image_declares_slim(target: str, init: str) -> None:
    assert _cfg(target, init).slim is None


def test_every_image_but_the_worker_is_unslimmed() -> None:
    """A new image added later is covered too, not only the five named above."""
    others = [t for t in config.factory_names() if t != "worker"]
    assert others, "no image other than the worker found"
    slimmed = [t for t in others if _cfg(t).slim is not None]
    assert slimmed == []


# --- the budget (R3.1) --------------------------------------------------------------


def test_the_worker_budget_is_600_mib() -> None:
    budget = _slim().budget_mb
    assert isinstance(budget, int) and not isinstance(budget, bool)
    assert budget == 600


# --- the keep list (R1.1) -----------------------------------------------------------

BUILD_OVEN = (
    "net-misc/openssh",
    "sys-apps/systemd",
    "app-containers/podman",
    "dev-vcs/git",
    "app-crypt/gnupg",
    "app-arch/tar",
    "sys-devel/binutils",
    "dev-libs/openssl",
    "sys-fs/btrfs-progs",
    "net-misc/rsync",
    "sys-apps/gptfdisk",
    "sys-apps/smartmontools",
    "net-misc/networkmanager",
    "net-wireless/wpa_supplicant",
    "app-admin/sudo-rs",
    "sys-kernel/linux-firmware",
    "sys-firmware/intel-microcode",
)
RESCUE_KIT = (
    "app-editors/nano",
    "sys-apps/less",
    "sys-apps/iproute2",
    "sys-apps/pciutils",
    "sys-apps/usbutils",
    "sys-apps/nvme-cli",
    "sys-apps/lm-sensors",
    "sys-process/htop",
)
#: What made the worker 1850 MB and runs nowhere on a build oven (design, measured).
NOT_AN_OVEN = (
    "dev-lang/rust-bin",
    "dev-lang/rust",
    "app-portage/bentoolkit",
    "net-analyzer/netdata",
    "net-misc/rclone",
    "dev-libs/boost",
    "sys-kernel/gentoo-sources",
    "sys-kernel/bentoo-sources",
    "app-backup/restic",
)


def test_keep_names_the_build_oven_runtime_and_the_rescue_kit() -> None:
    keep = _slim().keep
    packages = {_package(a) for a in keep}
    missing = [p for p in (*BUILD_OVEN, *RESCUE_KIT) if p not in packages]
    assert missing == [], f"keep lacks {missing}"


def test_keep_holds_python_3_14_and_a_binary_kernel() -> None:
    keep = _slim().keep
    assert any(_package(a) == "dev-lang/python" and "3.14" in a for a in keep), keep
    kernels = [a for a in keep if _package(a).startswith("sys-kernel/gentoo-kernel")]
    assert kernels, f"no kernel in keep: {keep}"


def test_hostile_keep_is_not_the_minimal_world() -> None:
    """Wrongly collapse: a keep list that is minimal's world keeps everything."""
    packages = {_package(a) for a in _slim().keep}
    assert sorted(packages & set(NOT_AN_OVEN)) == []


def test_keep_atoms_are_unique() -> None:
    keep = _slim().keep
    assert len({_package(a) for a in keep}) == len(keep), keep


# --- the commands the worker runs (R1.3) --------------------------------------------

#: The build oven: sshd and its keys, the native job runner (systemd-run, bash, the
#: venv's python3.14), the job's build tools, the work disk, the network, the
#: identity units' python3 and the console entry's sudo.
OVEN_COMMANDS = (
    "sshd",
    "ssh-keygen",
    "systemd-run",
    "systemd-nspawn",
    "systemctl",
    "bash",
    "python3",
    "python3.14",
    "sudo",
    "podman",
    "git",
    "gpg",
    "tar",
    "objdump",
    "openssl",
    "btrfs",
    "mkfs.btrfs",
    "rsync",
    "sgdisk",
    "smartctl",
    "nmcli",
    "wpa_supplicant",
    "depmod",
    "hostnamectl",
    "findmnt",
)
#: The console rescue kit (story constraint).
RESCUE_COMMANDS = (
    "nano",
    "less",
    "ip",
    "ss",
    "lsblk",
    "lspci",
    "lsusb",
    "smartctl",
    "nvme",
    "sensors",
    "htop",
)


def _command_names() -> set[str]:
    return {PurePosixPath(c).name for c in _slim().commands}


def test_commands_are_absolute_paths() -> None:
    relative = [c for c in _slim().commands if not c.startswith("/")]
    assert relative == []


def test_commands_cover_the_build_oven_and_the_rescue_kit() -> None:
    names = _command_names()
    missing = sorted({c for c in (*OVEN_COMMANDS, *RESCUE_COMMANDS) if c not in names})
    assert missing == [], f"slim.commands lacks {missing}"


@pytest.mark.parametrize("script", WORKER_SCRIPTS, ids=lambda p: p.name)
def test_commands_cover_every_command_the_worker_scripts_run(script: Path) -> None:
    run = _script_commands(script)
    assert run, f"no command found in {script.name}: the extraction is broken"
    missing = sorted(run - _command_names())
    assert missing == [], f"{script.name} runs {missing}, absent from slim.commands"


def test_hostile_a_command_is_matched_by_its_own_name_only() -> None:
    """Wrongly collapse: mkfs.btrfs is not btrfs, python3.14 is not python3, ss is
    not sshd -- each must be listed in its own right."""
    names = _command_names()
    for one, other in (("btrfs", "mkfs.btrfs"), ("python3", "python3.14"), ("ss", "sshd")):
        assert one in names and other in names, (one, other)


# --- kernel module groups (R2.1) ----------------------------------------------------

DROPPED = (
    "drivers/gpu",
    "drivers/media",
    "sound",
    "drivers/infiniband",
    "drivers/bluetooth",
    "net/bluetooth",
)
#: Wired and Wi-Fi network, storage (SATA, NVMe, USB), USB, input, filesystems, the
#: VM's virtio, and the sensors the rescue kit reads.
NEVER_DROPPED = (
    "drivers/net",
    "drivers/net/ethernet",
    "drivers/net/ethernet/intel",
    "drivers/net/ethernet/realtek",
    "drivers/net/wireless",
    "drivers/net/wireless/intel/iwlwifi",
    "net/wireless",
    "net/mac80211",
    "drivers/nvme",
    "drivers/ata",
    "drivers/scsi",
    "drivers/usb",
    "drivers/usb/storage",
    "drivers/hid",
    "drivers/input",
    "drivers/block",
    "drivers/md",
    "drivers/virtio",
    "drivers/hwmon",
    "fs",
    "fs/btrfs",
    "crypto",
    "lib",
)


def test_drop_modules_are_subtrees_under_kernel() -> None:
    entries = _slim().drop_modules
    assert entries
    bad = [e for e in entries if e.startswith(("/", "kernel/")) or re.search(r"[*?\[]", e)]
    assert bad == [], "drop_modules are plain subtrees relative to <kver>/kernel/"


def test_drop_modules_remove_graphics_media_sound_infiniband_and_bluetooth() -> None:
    entries = _slim().drop_modules
    not_dropped = [d for d in DROPPED if not any(_drops(e, d) for e in entries)]
    assert not_dropped == []


@pytest.mark.parametrize("subtree", NEVER_DROPPED)
def test_hostile_drop_modules_never_reach_network_storage_usb_or_input(subtree: str) -> None:
    culprits = [e for e in _slim().drop_modules if _drops(e, subtree)]
    assert culprits == [], f"{subtree} would be deleted by {culprits}"


# --- firmware (R2.3) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "intel-ucode/06-55-04",
        "amd-ucode/microcode_amd_fam17h.bin",
        "amd-ucode/microcode_amd_fam19h.bin.zst",
        "regulatory.db",
        "regulatory.db.p7s",
    ],
)
def test_keep_firmware_keeps_microcode_and_the_regulatory_database(path: str) -> None:
    assert _matches(_slim().keep_firmware, path), path


@pytest.mark.parametrize(
    "path",
    [
        "nvidia/ga102/gsp/gsp-535.113.01.bin",
        "amdgpu/navi10_sos.bin.zst",
        "qcom/a660_gmu.bin",
        "mellanox/mlxsw_spectrum-13.2000.1122.mfa2",
        "iwlwifi-cc-a0-77.ucode",
        "rtl_nic/rtl8168h-2.fw",
    ],
)
def test_hostile_keep_firmware_does_not_keep_what_no_module_asks(path: str) -> None:
    """Wrongly collapse: a glob that keeps everything leaves the 975 MB in place;
    firmware a remaining module asks for (iwlwifi, rtl_nic) is kept by modinfo, not here."""
    assert _matches(_slim().keep_firmware, path) == []


# --- pruned paths (R1.5) ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "usr/share/doc/openssh-9.9_p1/README.bz2",
        "usr/share/man/man8/sshd.8.bz2",
        "usr/share/info/tar.info.bz2",
        "usr/include/stdio.h",
        "usr/src/linux-6.12.1-gentoo-dist/Makefile",
        "usr/libexec/gcc/x86_64-pc-linux-gnu/15/cc1",
        "usr/share/locale/de/LC_MESSAGES/tar.mo",
        "usr/lib64/libc_nonshared.a",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libgcc.a",
    ],
)
def test_prune_removes_docs_headers_sources_catalogs_statics_and_compiler_internals(
    path: str,
) -> None:
    assert _matches(_slim().prune, path), path


@pytest.mark.parametrize(
    "path",
    [
        # the near twins of what is pruned
        "usr/lib/locale/locale-archive",
        "usr/lib/locale/C.utf8/LC_CTYPE",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libstdc++.so.6",
        "usr/lib/gcc/x86_64-pc-linux-gnu/15/libgcc_s.so.1",
        "usr/libexec/podman/netavark",
        # what the worker runs from
        "usr/local/lib/shidashi/kyomei_worker.py",
        "usr/local/bin/shidashi",
        "usr/lib/python3.14/encodings/utf_8.py",
        "usr/lib/systemd/system/sshd.service",
        "usr/share/dbus-1/system.d/org.freedesktop.NetworkManager.conf",
        "usr/share/terminfo/l/linux",
        "usr/lib/modules/6.12.1-gentoo-dist/kernel/drivers/net/ethernet/intel/e1000e/e1000e.ko",
        "usr/lib/firmware/regulatory.db",
        "etc/ssh/sshd_config.d/10-shidashi-worker.conf",
    ],
)
def test_hostile_prune_never_reaches_runtime_files(path: str) -> None:
    assert _matches(_slim().prune, path) == [], path


# --- no stale bytecode ships (design: Security) -------------------------------------


def test_no_bytecode_is_tracked_under_variants() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "variants"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert listed, "git ls-files listed nothing: not a checkout?"
    stray = [p for p in listed if "__pycache__" in p or p.endswith((".pyc", ".pyo"))]
    assert stray == []
