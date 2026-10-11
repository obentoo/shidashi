"""Firmware the slim step must keep, beyond the plain names (story 021, 1.2 review).

``modinfo -F firmware`` prints patterns for some modules (ath11k, brcmfmac), and
linux-firmware aliases blobs through file AND directory symlinks; on a merged-usr
image ``lib`` itself is a link to ``usr/lib``. Every case here was a file a kept
module loads that ``firmware_to_delete`` listed for deletion.
"""

from pathlib import Path

import pytest

from shidashi import slim


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def _link(path: Path, target: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return path


def _image(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "rootfs"
    fw = root / "usr/lib/firmware"
    fw.mkdir(parents=True)
    return root, fw


def _deleted(fw: Path, wanted: set[str], keep: tuple[str, ...] = ()) -> set[str]:
    return {p.relative_to(fw).as_posix() for p in slim.firmware_to_delete(fw, wanted, keep)}


def test_a_pattern_a_module_asks_for_keeps_every_file_it_matches(tmp_path: Path) -> None:
    _, fw = _image(tmp_path)
    _file(fw / "ath11k/WCN6855/hw2.0/amss.bin.zst")
    _file(fw / "ath11k/WCN6855/hw2.0/board-2.bin.zst")
    _file(fw / "brcm/brcmfmac43455-sdio.raspberrypi,4-model-b.txt.zst")
    _file(fw / "ath11k/QCA6390/hw2.0/amss.bin.zst")  # another chip: not asked for
    deleted = _deleted(fw, {"ath11k/WCN6855/hw2.0/*", "brcm/brcmfmac*-sdio.*.txt"})
    assert deleted == {"ath11k/QCA6390/hw2.0/amss.bin.zst"}


def test_a_link_through_merged_usr_keeps_its_target(tmp_path: Path) -> None:
    root, fw = _image(tmp_path)
    _link(root / "lib", "usr/lib")
    _file(fw / "b1.bin.zst")
    _link(fw / "a1.bin.zst", "../../../lib/firmware/b1.bin.zst")
    _file(fw / "b2.bin.zst")
    _link(fw / "a2.bin.zst", "/lib/firmware/b2.bin.zst")
    assert _deleted(fw, {"a1.bin", "a2.bin"}) == set()


def test_an_absolute_lib_link_is_followed_in_the_image_never_on_the_host(tmp_path: Path) -> None:
    root, fw = _image(tmp_path)
    _link(root / "lib", "/usr/lib")
    _file(fw / "blob.bin.zst")
    _link(fw / "alias.bin.zst", "/lib/firmware/blob.bin.zst")
    assert _deleted(fw, {"alias.bin"}) == set()


def test_a_name_through_a_directory_link_keeps_the_file(tmp_path: Path) -> None:
    _, fw = _image(tmp_path)
    _file(fw / "nv/ad102/gsp/x.bin.zst")
    _link(fw / "nv/ad103", "ad102")
    assert _deleted(fw, {"nv/ad103/gsp/x.bin"}) == set()


def test_a_wanted_directory_link_keeps_what_is_under_its_target(tmp_path: Path) -> None:
    _, fw = _image(tmp_path)
    _file(fw / "dir/real/y.bin.zst")
    _file(fw / "dir/other/z.bin.zst")
    _link(fw / "dir/alias", "real")
    assert _deleted(fw, {"dir/alias"}) == {"dir/other/z.bin.zst"}


def test_a_firmware_root_that_is_not_usr_lib_firmware_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    (root / "lib/firmware").mkdir(parents=True)
    with pytest.raises(ValueError, match="usr/lib/firmware"):
        slim.firmware_to_delete(root / "lib/firmware", set(), ())
