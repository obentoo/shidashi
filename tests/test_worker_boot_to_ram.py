"""The worker boots copied to RAM by default (headless bentoo-lab, 2026-11-06).

The machine runs with no GPU, so nobody picks a menu entry: the entry GRUB boots
after its timeout is the one the worker runs. ``boot_to_ram`` in a variant's
system.yaml puts the ``rd.live.ram=1`` entry first; every other image keeps the
plain live session first (a desktop squashfs is ~8 GB, too much to copy to RAM).
"""

from pathlib import Path

from shidashi import config, system
from shidashi.image import _grub_cfg, _stage_iso_tree


def _entries(cfg: str) -> list[str]:
    return [line for line in cfg.splitlines() if line.startswith("menuentry ")]


def test_boot_to_ram_makes_the_copy_to_ram_entry_the_default() -> None:
    cfg = _grub_cfg(volume="BENTOO_WORKER", title="W", text_target=None, boot_to_ram=True)
    assert "set default=0" in cfg
    first = cfg.split("menuentry ", 2)[1]
    assert first.startswith('"W (copy to RAM)"') and "rd.live.ram=1" in first
    # the plain live session stays offered, and the RAM entry is not listed twice
    names = _entries(cfg)
    assert 'menuentry "W" {' in names
    assert sum("(copy to RAM)" in n for n in names) == 1


def test_without_boot_to_ram_the_live_session_stays_first() -> None:
    cfg = _grub_cfg(volume="B", title="T", text_target=None)
    assert _entries(cfg)[0] == 'menuentry "T" {'


def test_the_staged_medium_carries_the_ram_default(tmp_path: Path) -> None:
    parts = [tmp_path / n for n in ("r.sq", "vmlinuz", "initrd")]
    for part in parts:
        part.write_bytes(b"x")
    root = tmp_path / "iso"
    _stage_iso_tree(
        root,
        squashfs=parts[0],
        kernel=parts[1],
        initramfs=parts[2],
        volume="BENTOO_WORKER",
        title="W",
        build_id="b",
        text_target=None,
        extra={},
        boot_to_ram=True,
    )
    menu = (root / "boot/grub/grub.cfg").read_text()
    assert _entries(menu)[0] == 'menuentry "W (copy to RAM)" {'


def test_only_the_worker_declares_boot_to_ram() -> None:
    variants = config.variants_dir()
    worker = system.load_system_config(
        config.load_recipe("v3", "worker", "systemd"), variants_dir=variants
    )
    kde = system.load_system_config(
        config.load_recipe("v3", "kde", "systemd"), variants_dir=variants
    )
    assert worker.boot_to_ram is True
    assert kde.boot_to_ram is False
