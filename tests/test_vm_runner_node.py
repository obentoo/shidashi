"""INTEGRATION: ``shidashi vm test`` on a node without QEMU, in the toolbox container
(story 024, task 4.1; R1.1, R1.2, R1.5, R1.7, R2.1, R4.2).

Run on the bentoo-lab as root -- podman, ``/dev/kvm``, ``/dev/vhost-vsock``, the
v3-systemd toolbox tarball, and NO ``qemu-system-x86_64`` on PATH -- with the generic
worker ISO::

    SHIDASHI_RUNNER_NODE=1 SHIDASHI_RUNNER_ISO=/mnt/work/out/iso/<worker iso> \\
        python -m pytest -q tests/test_vm_runner_node.py

Real KVM, real podman, the real runner image: the boot test passes on BIOS and UEFI, the
run's ``boot-test.json`` names the runner image, the image exists afterwards, and no
``shidashi-vm-`` container is left. Opt-in; otherwise SKIPPED -- never green by default.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from shidashi import vmrunner

_ISO = os.environ.get("SHIDASHI_RUNNER_ISO", "")

pytestmark = pytest.mark.skipif(
    os.environ.get("SHIDASHI_RUNNER_NODE") != "1"
    or os.geteuid() != 0
    or shutil.which("qemu-system-x86_64") is not None
    or shutil.which("podman") is None
    or not Path("/dev/kvm").exists()
    or not Path(_ISO).is_file(),
    reason="needs SHIDASHI_RUNNER_NODE=1 and SHIDASHI_RUNNER_ISO on a node without QEMU, "
    "as root, with podman and /dev/kvm",
)


def _containers() -> list[str]:
    done = subprocess.run(
        ["podman", "ps", "-a", "--filter", "name=shidashi-vm-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in done.stdout.splitlines() if line.strip()]


def test_vm_test_passes_in_the_runner_container_and_leaves_nothing_running(
    tmp_path: Path,
) -> None:
    assert _containers() == [], "a shidashi-vm- container was left by an earlier run"
    runs = tmp_path / "runs"
    env = {**os.environ, "SHIDASHI_RUNS": str(runs)}
    env.pop("SHIDASHI_CID_LOCKS", None)  # the host's real CID locks: CIDs are host-wide
    env["COLUMNS"] = "200"
    done = subprocess.run(
        [sys.executable, "-c", "from shidashi.cli import app; app()", "vm", "test", _ISO],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    assert "boot test bios: PASSED" in done.stdout
    assert "boot test uefi: PASSED" in done.stdout

    (attached,) = runs.glob("*/boot-test.json")
    report = json.loads(attached.read_text())
    tag = vmrunner.image_tag(vmrunner.runner_tarball())
    assert report["runner"] == tag
    assert report["passed"] is True
    assert set(report["firmwares"]) == {"bios", "uefi"}
    for firmware in report["firmwares"].values():
        assert firmware["passed"] is True and firmware["ssh_after_s"] is not None

    # the side effects: the image is there for the next boot test, nothing is running
    assert subprocess.run(["podman", "image", "exists", tag], check=False).returncode == 0
    assert _containers() == []
