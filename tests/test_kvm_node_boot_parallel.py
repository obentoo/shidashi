"""INTEGRATION: concurrent boot tests on a KVM node (story 006, task 7.3).

A build of two images with ``--boot-test`` runs the two boot tests at the same time,
each on its own CID and session directory, and both pass. On a node with QEMU (the
host) they boot natively; on the worker, which has no QEMU (story 021), each runs in
the v3-systemd toolbox container (story 024, ``shidashi.vmrunner``), the CID lock
held by its ``assemble`` around the container. Opt-in with ``SHIDASHI_KVM_NODE=1`` as
root with ``/dev/kvm``; otherwise SKIPPED -- never green by default. Covers R4.1, R4.2.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SHIDASHI_KVM_NODE") != "1"
    or os.geteuid() != 0
    or not Path("/dev/kvm").exists(),
    reason="needs SHIDASHI_KVM_NODE=1 on the host as root with /dev/kvm",
)


def test_two_concurrent_boot_tests_use_their_own_cids_and_both_pass() -> None:
    started = time.time()
    runs = Path(os.environ.get("SHIDASHI_RUNS", "/var/log/shidashi/runs"))
    with tempfile.TemporaryDirectory(prefix="shidashi-kvm-") as out:
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "from shidashi.cli import app; app()",
                "build",
                "v3",
                "systemd",
                "--images",
                "minimal,worker",
                "--skip-factory",
                "--boot-test",
                "--output-dir",
                out,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    cids = []
    for manifest in runs.glob("*/manifest.json"):
        if manifest.stat().st_mtime < started:
            continue
        data = json.loads(manifest.read_text())
        if data.get("command") != "assemble":
            continue
        cids += [s["cid"] for s in data["steps"] if s.get("step") == "boot-test" and "cid" in s]
    assert len(cids) == 2 and len(set(cids)) == 2, cids
    passed = []
    for manifest in runs.glob("*/manifest.json"):
        if manifest.stat().st_mtime < started:
            continue
        data = json.loads(manifest.read_text())
        if data.get("command") == "assemble":
            passed += [s.get("passed") for s in data["steps"] if s.get("step") == "boot-test"]
    assert passed == [True, True], passed
