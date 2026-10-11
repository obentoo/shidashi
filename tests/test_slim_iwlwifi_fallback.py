"""iwlwifi's firmware fallback in the slim step (story 021, task 2 decision of 2026-10-11).

iwlwifi's MODULE_FIRMWARE names only the newest ucode API it speaks
(``iwlwifi-cc-a0-77.ucode``); at load time the driver walks down to older APIs of
the same chip when that file is absent. linux-firmware ships some chips only at an
older API, so keeping just the literal name could leave a card with no firmware.
``firmware_to_delete`` keeps, for a wanted ``iwlwifi-<stem>-<N>.ucode``, every
``iwlwifi-<stem>-<M>.ucode`` with an integer ``M < N`` in the same directory -- and
nothing else (``keep_firmware`` stays free of an ``iwlwifi-*`` glob).
"""

from pathlib import Path

from shidashi import slim


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def _firmware(tmp_path: Path) -> Path:
    fw = tmp_path / "rootfs" / "usr" / "lib" / "firmware"
    fw.mkdir(parents=True)
    return fw


def _deleted(fw: Path, wanted: set[str]) -> set[str]:
    return {p.relative_to(fw).as_posix() for p in slim.firmware_to_delete(fw, wanted, ())}


def test_an_older_api_of_the_wanted_stem_is_kept(tmp_path: Path) -> None:
    fw = _firmware(tmp_path)
    _file(fw / "iwlwifi-cc-a0-72.ucode.zst")
    _file(fw / "iwlwifi-cc-a0-46.ucode")
    _file(fw / "iwlwifi-cc-a0-77.ucode.xz")
    assert _deleted(fw, {"iwlwifi-cc-a0-77.ucode"}) == set()


def test_the_fallback_is_found_when_the_wanted_api_is_not_shipped(tmp_path: Path) -> None:
    """The case the fallback exists for: the module asks for -6, the tree has -4."""
    fw = _firmware(tmp_path)
    _file(fw / "iwlwifi-6000-4.ucode.zst")
    assert _deleted(fw, {"iwlwifi-6000-6.ucode"}) == set()


def test_hostile_another_stem_is_deleted(tmp_path: Path) -> None:
    """Wrongly collapse: a stem that is a prefix or a sibling is another chip."""
    fw = _firmware(tmp_path)
    _file(fw / "iwlwifi-cc-a0-72.ucode")
    _file(fw / "iwlwifi-cc-72.ucode")
    _file(fw / "iwlwifi-so-a0-gf-a0-72.ucode")
    _file(fw / "iwlwifi-ty-a0-gf-a0-72.ucode.zst")
    assert _deleted(fw, {"iwlwifi-cc-a0-77.ucode"}) == {
        "iwlwifi-cc-72.ucode",
        "iwlwifi-so-a0-gf-a0-72.ucode",
        "iwlwifi-ty-a0-gf-a0-72.ucode.zst",
    }


def test_hostile_a_higher_api_is_deleted(tmp_path: Path) -> None:
    fw = _firmware(tmp_path)
    _file(fw / "iwlwifi-cc-a0-77.ucode")
    _file(fw / "iwlwifi-cc-a0-89.ucode.zst")
    _file(fw / "iwlwifi-cc-a0-100.ucode")  # numeric, never a string compare
    assert _deleted(fw, {"iwlwifi-cc-a0-77.ucode"}) == {
        "iwlwifi-cc-a0-89.ucode.zst",
        "iwlwifi-cc-a0-100.ucode",
    }


def test_hostile_a_non_numeric_api_is_neither_a_fallback_nor_a_trigger(tmp_path: Path) -> None:
    fw = _firmware(tmp_path)
    _file(fw / "iwlwifi-x-6a.ucode")
    _file(fw / "iwlwifi-x-5.ucode")
    _file(fw / "iwlwifi-y-4.ucode")
    # a wanted non-numeric name keeps itself only; a numeric one never keeps "6a"
    assert _deleted(fw, {"iwlwifi-x-6.ucode", "iwlwifi-y-5a.ucode"}) == {
        "iwlwifi-x-6a.ucode",
        "iwlwifi-y-4.ucode",
    }


def test_hostile_the_fallback_stays_in_the_wanted_names_directory(tmp_path: Path) -> None:
    fw = _firmware(tmp_path)
    _file(fw / "intel/iwlwifi/iwlwifi-cc-a0-72.ucode.zst")
    _file(fw / "iwlwifi-cc-a0-72.ucode")
    assert _deleted(fw, {"intel/iwlwifi/iwlwifi-cc-a0-77.ucode"}) == {"iwlwifi-cc-a0-72.ucode"}


def test_hostile_other_firmware_is_not_kept_by_an_iwlwifi_name(tmp_path: Path) -> None:
    fw = _firmware(tmp_path)
    _file(fw / "iwlwifi-cc-a0-72.pnvm")
    _file(fw / "rtl_nic/rtl8168h-2.fw")
    assert _deleted(fw, {"iwlwifi-cc-a0-77.ucode"}) == {
        "iwlwifi-cc-a0-72.pnvm",
        "rtl_nic/rtl8168h-2.fw",
    }
