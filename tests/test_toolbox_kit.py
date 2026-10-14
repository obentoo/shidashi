"""The toolbox carries the boot-test tools (story 024, task 1.1; R5.1, R5.2).

On a node without QEMU (the slim worker) the whole boot test runs in a container made
from the v3-systemd toolbox, so the toolbox stage must install what a boot test runs:
``qemu-system-x86_64`` with its OVMF (both from app-emulation/qemu), ``ssh`` and
``ssh-keygen`` (net-misc/openssh), ``systemd-ssh-proxy`` (systemd) and ``xorriso``
(dev-libs/libisoburn). The toolbox keeps assembling ISOs exactly as before: the same
ISO tools, the same version probe.

The hostile half comes first: the new tools enter the TOOLBOX only -- the worker image
the toolbox serves must still not carry QEMU (story 021), nor any other image.
"""

from pathlib import Path

import pytest

from shidashi import config, toolbox
from shidashi.recipe import TOOLBOX_STAGE
from shidashi.resolve import set_closure, world_atoms

_INITS = ("systemd", "openrc")

#: What R5.1 asks the toolbox to install for a boot test.
_BOOT_TEST_ATOMS = {"app-emulation/qemu", "net-misc/openssh"}

#: The ISO tools the toolbox has always carried (R5.2).
_ISO_ATOMS = {"sys-boot/grub", "dev-libs/libisoburn", "sys-fs/mtools"}


def _kit() -> Path:
    return config.variants_dir() / "kits" / "tools" / "toolbox"


def _atoms(lines: list[str]) -> set[str]:
    found: set[str] = set()
    for line in lines:
        token = line.split("#", 1)[0].split()
        if token and not token[0].startswith("@"):
            found.add(token[0])
    return found


def _toolbox_atoms(init: str) -> set[str]:
    closure = set_closure(config.load_recipe("v3", TOOLBOX_STAGE, init))
    return _atoms(closure["toolbox"])


# --- hostile half: the boot-test tools must not leak out of the toolbox ------------------


@pytest.mark.parametrize("image", ["worker", "minimal"])
@pytest.mark.parametrize("init", _INITS)
def test_the_boot_test_tools_never_reach_an_image_through_the_toolbox(
    image: str, init: str
) -> None:
    """The worker hosts the boot test, it does not carry it: adding QEMU to the toolbox
    kit must not put QEMU in the worker (or minimal) image's world."""
    atoms = set(world_atoms(config.load_recipe("v3", image, init)))
    assert "app-emulation/qemu" not in atoms
    assert "app-emulation/virtiofsd" not in atoms


def test_virtiofsd_is_not_added_to_the_toolbox() -> None:
    """A boot test passes no shares: the runner needs no virtiofsd."""
    assert "app-emulation/virtiofsd" not in _toolbox_atoms("systemd")


# --- R5.1: the toolbox installs what a boot test runs -----------------------------------


@pytest.mark.parametrize("init", _INITS)
def test_the_toolbox_installs_qemu_and_openssh(init: str) -> None:
    """qemu-system-x86_64 and its OVMF come with app-emulation/qemu; ssh and ssh-keygen
    with net-misc/openssh -- in the toolbox set itself, under every init."""
    assert _toolbox_atoms(init) >= _BOOT_TEST_ATOMS


def test_the_toolbox_keeps_xorriso_and_is_built_on_the_systemd_stage3() -> None:
    """xorriso comes with dev-libs/libisoburn, in the toolbox set; systemd-ssh-proxy with
    the systemd stage3 the v3-systemd toolbox is built on (not a set: task 1.2's tarball
    listing proves the file itself)."""
    recipe = config.load_recipe("v3", TOOLBOX_STAGE, "systemd")
    assert recipe.init == "systemd"
    assert "dev-libs/libisoburn" in _toolbox_atoms("systemd")


def test_the_kit_header_says_why_qemu_is_there() -> None:
    """The kit's header names the boot test in the toolbox container (story 024)."""
    header = "\n".join(
        line for line in _kit().read_text(encoding="utf-8").splitlines() if line.startswith("#")
    ).lower()
    assert "boot test" in header and "024" in header


# --- R5.2: the toolbox still assembles ISOs as before ----------------------------------


@pytest.mark.parametrize("init", _INITS)
def test_the_toolbox_keeps_its_iso_tools(init: str) -> None:
    assert _toolbox_atoms(init) >= _ISO_ATOMS


def test_the_toolbox_stage_is_unchanged() -> None:
    recipe = config.load_recipe("v3", TOOLBOX_STAGE, "systemd")
    assert recipe.stages == ("base", "toolbox")
    assert not any(p.ships for p in recipe.phases)
    assert TOOLBOX_STAGE not in config.target_names()


def test_the_toolbox_version_probe_is_unchanged() -> None:
    """build.json records the same ISO tools of the toolbox as before (R5.2)."""
    assert set(toolbox._VERSIONS) == {"grub", "squashfs-tools", "xorriso", "mtools"}
