"""A VM disk carries a serial (story 021, 5.2).

``worker disk-init`` refuses a disk whose serial the operator has not confirmed, and a
virtio disk without ``serial=`` has none: a VM worker could never get a work disk. The
serial is stable for one file and different for two files of the same name.
"""

import re
from pathlib import Path

from shidashi import vm


def _drive(tmp_path: Path, disk: Path) -> str:
    argv = vm.qemu_argv(
        vm.VmSpec(iso=tmp_path / "x.iso", disks=(disk,)),
        pubkey="ssh-ed25519 AAAA test",
        qmp=tmp_path / "qmp.sock",
        serial=tmp_path / "serial.log",
        pidfile=tmp_path / "qemu.pid",
    )
    # the drive names the file; the device that carries the serial follows it
    at = next(i for i, arg in enumerate(argv) if arg.startswith(f"file={disk},"))
    assert argv[at + 1] == "-device", argv[at : at + 3]
    return argv[at + 2]


def _serial(drive: str) -> str:
    found = re.search(r"(?:^|,)serial=([^,]*)", drive)
    assert found, drive
    return found.group(1)


def test_a_disk_has_a_serial_lsblk_can_show(tmp_path: Path) -> None:
    serial = _serial(_drive(tmp_path, tmp_path / "work.qcow2"))
    assert re.fullmatch(r"[a-z0-9-]{1,20}", serial), serial  # virtio-blk keeps 20 bytes
    assert serial.startswith("work-")


def test_the_serial_is_stable_for_one_file(tmp_path: Path) -> None:
    disk = tmp_path / "work.qcow2"
    assert _serial(_drive(tmp_path, disk)) == _serial(_drive(tmp_path, disk))


def test_two_files_of_the_same_name_get_two_serials(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    one = _serial(_drive(tmp_path, tmp_path / "a/work.qcow2"))
    two = _serial(_drive(tmp_path, tmp_path / "b/work.qcow2"))
    assert one != two


def test_a_name_with_odd_characters_still_gives_a_plain_serial(tmp_path: Path) -> None:
    serial = _serial(_drive(tmp_path, tmp_path / "Disk, Ünïcode_ & spaces.qcow2"))
    assert re.fullmatch(r"[a-z0-9-]{1,20}", serial), serial
