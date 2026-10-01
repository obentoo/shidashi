"""Tests for the ISO boot smoke test (OVERVIEW §7, Phase 1).

The real ISO build (squashfs + dracut ``dmsquash-live`` + grub-mkrescue) and the
QEMU boot are host-gated — they require root + grub-mkrescue + dracut + qemu + cc — and
live in ``scripts/smoke-iso.sh`` (the automated runbook). Here, in the idiom of
tests/test_image.py: off-host checks that the runbook exists, is executable and
its ``--help`` mentions the key tokens (they run in the non-Gentoo CI suite), and a
host-gated test (``@pytest.mark.skipif``) that invokes the minimal self-contained boot and
asserts the sentinel printed by ``/sbin/init`` after the dmsquash-live pivot.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "smoke-iso.sh"

# Host-gating: the real boot needs root + the image toolchain +
# QEMU. On non-Gentoo CI / a non-root sandbox this test is skipped (like the other
# host-gated container/factory tests).
_NEEDS_HOST = (
    os.geteuid() != 0
    or shutil.which("grub-mkrescue") is None
    or shutil.which("dracut") is None
    or shutil.which("qemu-system-x86_64") is None
    or shutil.which("cc") is None
)
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST,
    reason="requires root + grub-mkrescue + dracut + qemu + cc (privileged Gentoo host)",
)


# --- runbook present (off-host) ---------------------------------------------


def test_smoke_runbook_exists_and_executable() -> None:
    assert _SCRIPT.is_file(), "scripts/smoke-iso.sh must exist (smoke test runbook)"
    assert os.access(_SCRIPT, os.X_OK), "scripts/smoke-iso.sh must be executable"


def test_smoke_runbook_help_documents_modes() -> None:
    proc = subprocess.run([str(_SCRIPT), "--help"], capture_output=True, text=True, check=True)
    text = proc.stdout + proc.stderr
    # The two modes (minimal/--iso), the boot module and the sentinel are documented.
    for token in ("--iso", "dmsquash-live", "SHIDASHI_SMOKE_OK", "QEMU"):
        assert token in text, f"--help should mention {token!r}"


def test_smoke_runbook_rejects_unknown_option() -> None:
    proc = subprocess.run([str(_SCRIPT), "--nope"], capture_output=True, text=True)
    assert proc.returncode == 2  # unknown option → exit 2 (usage)


# --- boot smoke (host-gated) -------------------------------------------------


@_skip_privileged
def test_minimal_iso_boots_in_qemu(tmp_path: Path) -> None:
    # Minimal self-contained boot: the script builds the ISO via shidashi.image (the
    # real functions under test) and asserts the sentinel that the static /sbin/init
    # prints on the serial port after dmsquash-live pivots into the squashfs. --work=
    # tmp_path → pytest owns the cleanup of the artifacts.
    proc = subprocess.run(
        [str(_SCRIPT), "--work", str(tmp_path), "--timeout", "240"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"smoke-iso failed:\n{proc.stdout}\n{proc.stderr}"
