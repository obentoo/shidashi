"""The slim step keeps the kernel the ISO boots (story 021, 1.3 review).

gentoo-kernel-bin keeps its image under ``usr/src/linux-<kver>/arch/x86/boot/bzImage``
and ``usr/lib/modules/<kver>/vmlinuz`` is a relative link to it; with installkernel[uki]
that link is the only vmlinuz ``_locate_kernel`` finds. Pruning ``usr/src/**`` must not
take the bzImage with it.
"""

from pathlib import Path

import pytest

from tests import test_assembler_slim as harness

_SRC = f"usr/src/linux-{harness.KVER}"


def _with_kernel_under_usr_src(rootfs: Path) -> None:
    original(rootfs)
    (rootfs / f"boot/vmlinuz-{harness.KVER}").unlink()
    image = rootfs / _SRC / "arch/x86/boot/bzImage"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"K")
    (rootfs / _SRC / "Makefile").write_bytes(b"source")
    vmlinuz = rootfs / f"usr/lib/modules/{harness.KVER}/vmlinuz"
    vmlinuz.symlink_to(f"../../../src/linux-{harness.KVER}/arch/x86/boot/bzImage")


original = harness._image


def test_pruning_usr_src_keeps_the_kernel_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "_image", _with_kernel_under_usr_src)
    wired = harness._slim_wired(tmp_path, monkeypatch, prune=["usr/share/doc/**", "usr/src/**"])
    wired.assemble()
    pruned = wired.step("slim")["pruned_files"]
    assert f"{_SRC}/Makefile" in pruned  # the sources go ...
    assert f"{_SRC}/arch/x86/boot/bzImage" not in pruned  # ... the image the ISO boots stays
    assert len(wired.isos_built) == 1


def test_only_the_groups_this_kernel_lacks_are_reported_unmatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on the first real slim run: the report named every group, the dropped
    ones too, because it looked after they were gone."""
    wired = harness._slim_wired(tmp_path, monkeypatch)
    wired.assemble()
    assert wired.step("slim")["unmatched_module_groups"] == ["drivers/accel"]
