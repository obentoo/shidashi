"""``ci_guest.prepare``: the guest prepared once per pin set (story 034, task 2.3; R3.1,
R3.4-R3.6, R4.6, R7.1, R7.2, R9.1, Q11).

Contract (design.md, "shidashi/ci_guest.py" -> ``prepare``; Error Handling Strategy):
``prepare(pins, *, root, runner=subprocess.run) -> Path`` returns
``<root>/prepared/<key>.qcow2`` with ``key = prepared_key(pins, prepare_user_data(pins))``.
``vmrunner.preflight`` comes first: a worker without the toolbox gets ``CiGuestError``
carrying its message, which names ``shidashi worker sync push <worker> --arch v3``, and
nothing is done. When the image is missing: ``<root>/prepare.lock`` is held (a second
preparation waits, R3.5), then the base image (``fetch``), then the sweep of orphans --
``jobs/*`` and, only under that lock, ``prepare/*`` (a running preparation's work dir is
never taken, R4.6) --, a NoCloud seed ISO labelled ``cidata`` holding ``user-data`` and
``meta-data`` (xorriso, in the toolbox container), ONE boot of a COPY of the base with that
seed (4 vCPUs, 4 GiB, the seed a read-only cdrom, no credential) in container
``shidashi-vm-ci-prepare-<key>`` started with ``--cgroups=split``, a wait of at most 30
minutes for power-off, the guest's prepared marker on the serial log, then the compressed
image through a ``.part`` renamed over the final name. A failed or timed-out boot writes no
prepared image and raises ``CiGuestError`` with the last 20 lines of the serial log
(R3.4). After a success only the 2 newest prepared images stay (R3.6). One ``ci_prepare``
JSON event (``key``, ``duration_ms``, ``result``).

The toolbox is ``FakeCiToolbox`` (tests/_fake_ci_toolbox.py): no QEMU runs; its "guest"
prints what each test decides.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._fake_ci_toolbox import (
    PREPARED_MARKER,
    Boot,
    FakeCiToolbox,
    missing_toolbox,
    no_download,
    option_values,
    stand_in_toolbox_host,
)
from tests._pending import try_import
from tests.test_ci_guest_seed import write_pins

prepare: Any = try_import("shidashi.ci_guest", "prepare")
CiGuestError: Any = try_import("shidashi.ci_guest", "CiGuestError")
load_pins: Any = try_import("shidashi.ci_guest_seed", "load_pins")
prepare_user_data: Any = try_import("shidashi.ci_guest_seed", "prepare_user_data")
prepared_key: Any = try_import("shidashi.ci_guest_seed", "prepared_key")

BASE = b"QFI\xfb base image stand-in" * 1000
THIRTY_MINUTES = 30 * 60


@dataclass
class PrepareHost:
    root: Path
    pins: Any
    key: str
    toolbox: FakeCiToolbox

    @property
    def prepared(self) -> Path:
        return self.root / "prepared" / f"{self.key}.qcow2"

    @property
    def base(self) -> Path:
        return self.root / "images" / f"{hashlib.sha256(BASE).hexdigest()}.img"

    def prepared_names(self) -> list[str]:
        directory = self.root / "prepared"
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PrepareHost:
    stand_in_toolbox_host(monkeypatch, tmp_path / "host")
    no_download(monkeypatch)
    pins = load_pins(*write_pins(tmp_path / "pins", sha256=hashlib.sha256(BASE).hexdigest()))
    root = tmp_path / "ci"
    (root / "images").mkdir(parents=True)
    key = prepared_key(pins, prepare_user_data(pins))
    h = PrepareHost(root, pins, key, FakeCiToolbox())
    h.base.write_bytes(BASE)  # present and verified: fetch downloads nothing (R2.3)
    return h


def serial_lines(n: int) -> str:
    return "".join(f"serial-line-{i:02d}\n" for i in range(1, n + 1))


def prepared_ok(_argv: list[str]) -> Boot:
    return Boot(rc=0, serial=f"cloud-init: modules done\n{PREPARED_MARKER}\nreboot: Power down\n")


def stopped_after_boot(toolbox: FakeCiToolbox) -> bool:
    """A podman kill/stop/rm, or a QMP-less kill, was issued after the (last) boot."""
    last = max(i for i, c in enumerate(toolbox.calls) if any("qemu-system" in w for w in c))
    return any(
        c[:1] == ["podman"] and any(v in c for v in ("kill", "stop", "rm"))
        for c in toolbox.calls[last + 1 :]
    )


def test_prepare_boots_a_seeded_copy_once_and_keeps_the_image_under_its_key(
    host: PrepareHost,
) -> None:
    """R3.1: no prepared image for the key -> one is made before any job VM boots."""
    host.toolbox.on_boot = prepared_ok
    path = prepare(host.pins, root=host.root, runner=host.toolbox)
    assert path == host.prepared and path.is_file()
    assert host.prepared_names() == [f"{host.key}.qcow2"], "a .part was left behind"
    # the NoCloud seed: label cidata, the pins' user-data, a meta-data with an instance-id
    assert len(host.toolbox.seeds) == 1, host.toolbox.seeds
    seed = host.toolbox.seeds[0]
    assert seed["label"].lower() == "cidata"
    assert seed.get("user-data") == prepare_user_data(host.pins)
    assert yaml.safe_load(seed.get("meta-data", "")).get("instance-id")
    # one boot, of a copy: the cached base image is never the guest's writable disk
    assert len(host.toolbox.boots) == 1
    boot = host.toolbox.boots[0]
    assert not any(f"file={host.base}" in w for w in boot.argv), "the base image was booted"
    assert host.base.read_bytes() == BASE
    timeout = boot.kwargs.get("timeout")
    assert timeout is not None and 0 < timeout <= THIRTY_MINUTES, timeout


def test_prepare_boots_4_cpus_4_gib_from_the_seed_cdrom_in_its_own_split_container(
    host: PrepareHost,
) -> None:
    """The prepare boot's spec, its seed as a read-only cdrom, no credential; QEMU's
    container is the one ``sweep`` probes and stays in the caller's cgroup (R7.2)."""
    host.toolbox.on_boot = prepared_ok
    prepare(host.pins, root=host.root, runner=host.toolbox)
    assert len(host.toolbox.boots) == 1, "the prepare VM did not boot exactly once"
    boot = host.toolbox.boots[0]
    assert option_values(boot.argv, "-smp")[0].split(",")[0] == "4"
    assert option_values(boot.argv, "-m")[0] in ("4G", "4g", "4096", "4096M")
    cdroms = [d.split(",") for d in option_values(boot.argv, "-drive") if "media=cdrom" in d]
    assert len(cdroms) == 1 and "readonly=on" in cdroms[0], cdroms
    seed = next(w.removeprefix("file=") for w in cdroms[0] if w.startswith("file="))
    assert Path(seed).name == "seed.iso"
    assert option_values(boot.argv, "-fw_cfg") == []
    assert not [w for w in boot.argv if "vsock" in w], boot.argv
    assert boot.container == f"shidashi-vm-ci-prepare-{host.key}", boot.outer
    assert [w for w in boot.outer if w.startswith("--cgroups")] == ["--cgroups=split"], boot.outer


def test_prepare_without_the_toolbox_names_the_sync_command_and_does_nothing(
    host: PrepareHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QEMU, qemu-img and xorriso exist only in the toolbox the host pushes."""
    missing_toolbox(monkeypatch)
    host.toolbox.on_boot = prepared_ok
    with pytest.raises(CiGuestError) as caught:
        prepare(host.pins, root=host.root, runner=host.toolbox)
    assert "shidashi worker sync push" in str(caught.value), caught.value
    assert "--arch v3" in str(caught.value)
    assert host.toolbox.calls == []
    assert host.prepared_names() == []
    assert not (host.root / "prepare").exists()


def test_prepare_an_existing_prepared_image_boots_nothing(host: PrepareHost) -> None:
    host.prepared.parent.mkdir(parents=True)
    host.prepared.write_bytes(b"prepared earlier")
    path = prepare(host.pins, root=host.root, runner=host.toolbox)
    assert path == host.prepared
    assert host.toolbox.tools_run() == []
    assert host.prepared.read_bytes() == b"prepared earlier"


def test_prepare_a_failed_boot_writes_nothing_and_shows_the_last_20_serial_lines(
    host: PrepareHost,
) -> None:
    """R3.4."""
    host.toolbox.on_boot = lambda _argv: Boot(rc=1, serial=serial_lines(30))
    with pytest.raises(CiGuestError) as caught:
        prepare(host.pins, root=host.root, runner=host.toolbox)
    message = str(caught.value)
    assert all(f"serial-line-{i:02d}" in message for i in range(11, 31)), message
    assert "serial-line-10" not in message
    assert host.prepared_names() == []


def test_prepare_a_guest_that_powers_off_without_the_marker_writes_nothing(
    host: PrepareHost,
) -> None:
    """Hostile half of R3.4: QEMU exits 0 when the guest powers off -- also when cloud-init
    gave up half-way (the runner's sha256 refused, apt failed). The marker decides."""
    host.toolbox.on_boot = lambda _argv: Boot(rc=0, serial=serial_lines(5) + "reboot: Power down\n")
    with pytest.raises(CiGuestError):
        prepare(host.pins, root=host.root, runner=host.toolbox)
    assert host.prepared_names() == []
    assert host.toolbox.boots, "nothing was booted"


def test_prepare_the_marker_inside_another_line_is_not_the_marker(host: PrepareHost) -> None:
    """Hostile collapse: a console line QUOTING the marker command (cloud-init reporting a
    failed runcmd) is not the guest printing it."""
    host.toolbox.on_boot = lambda _argv: Boot(
        rc=0,
        serial=f"cloud-init[812]: Failed running: echo {PREPARED_MARKER} > /dev/console\n",
    )
    with pytest.raises(CiGuestError):
        prepare(host.pins, root=host.root, runner=host.toolbox)
    assert host.prepared_names() == []
    assert host.toolbox.boots, "nothing was booted"


def test_prepare_a_boot_past_30_minutes_is_stopped_and_writes_nothing(host: PrepareHost) -> None:
    """R3.4: the 30-minute bound, and the VM stopped -- a ``podman run`` client killed by a
    timeout leaves its container running."""
    host.toolbox.on_boot = lambda _argv: Boot(hang=True, serial=serial_lines(25))
    with pytest.raises(CiGuestError) as caught:
        prepare(host.pins, root=host.root, runner=host.toolbox)
    assert "serial-line-25" in str(caught.value)
    timeout = host.toolbox.boots[0].kwargs.get("timeout")
    assert timeout is not None and 0 < timeout <= THIRTY_MINUTES
    assert stopped_after_boot(host.toolbox)
    assert host.prepared_names() == []


def test_prepare_a_failing_step_is_named(host: PrepareHost) -> None:
    host.toolbox.fail_tool["xorriso"] = 1
    with pytest.raises(CiGuestError) as caught:
        prepare(host.pins, root=host.root, runner=host.toolbox)
    assert "xorriso" in str(caught.value)
    assert host.prepared_names() == []
    assert host.toolbox.boots == [], "booted without a seed"


def _old_prepared(host: PrepareHost, *names: str) -> None:
    directory = host.root / "prepared"
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for age, name in enumerate(reversed(names), start=1):
        path = directory / f"{name}.qcow2"
        path.write_bytes(name.encode())
        os.utime(path, (now - 3600 * age, now - 3600 * age))


def test_prepare_keeps_the_two_newest_prepared_images(host: PrepareHost) -> None:
    """R3.6: oldest first in ``_old_prepared``; the new one and the newest old one stay."""
    _old_prepared(host, "0000000000000001", "0000000000000002", "0000000000000003")
    host.toolbox.on_boot = prepared_ok
    prepare(host.pins, root=host.root, runner=host.toolbox)
    assert host.prepared_names() == sorted([f"{host.key}.qcow2", "0000000000000003.qcow2"])


def test_prepare_a_failure_deletes_no_older_prepared_image(host: PrepareHost) -> None:
    """Hostile half of R3.6: retention follows a success only -- a failed prepare must not
    leave the worker with nothing to boot."""
    _old_prepared(host, "0000000000000001", "0000000000000002", "0000000000000003")
    host.toolbox.on_boot = lambda _argv: Boot(rc=1, serial=serial_lines(3))
    with pytest.raises(CiGuestError):
        prepare(host.pins, root=host.root, runner=host.toolbox)
    assert host.prepared_names() == [
        "0000000000000001.qcow2",
        "0000000000000002.qcow2",
        "0000000000000003.qcow2",
    ]
    assert host.toolbox.boots, "nothing was booted"


def test_prepare_sweeps_orphan_job_and_prepare_directories_first(host: PrepareHost) -> None:
    """R4.6: a job directory, or an older preparation's work dir, whose VM is gone (a
    crash, a reboot) is removed before this preparation boots."""
    orphan = host.root / "jobs" / "8000"
    orphan.mkdir(parents=True)
    (orphan / "overlay.qcow2").write_bytes(b"left by a crash")
    stale = host.root / "prepare" / "00000000000000aa"
    stale.mkdir(parents=True)
    (stale / "disk.qcow2").write_bytes(b"left by a crashed prepare")
    present: list[bool] = []

    def boot(argv: list[str]) -> Boot:
        present.append(orphan.exists() or stale.exists())
        return prepared_ok(argv)

    host.toolbox.on_boot = boot
    prepare(host.pins, root=host.root, runner=host.toolbox)
    assert present == [False], "the orphans were still there when the prepare VM booted"
    assert not orphan.exists() and not stale.exists()
    assert host.prepared.is_file()


_HOLDER = """
import fcntl, pathlib, sys, time
lock = open(sys.argv[1], "a")
fcntl.flock(lock, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).write_text("held")
deadline = time.monotonic() + 60
while not pathlib.Path(sys.argv[3]).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""


@pytest.fixture
def lock_holder(
    host: PrepareHost, tmp_path: Path
) -> Iterator[tuple[Path, subprocess.Popen[bytes]]]:
    """Another process -- another ``prepare`` -- holding ``<root>/prepare.lock``."""
    held, release = tmp_path / "held", tmp_path / "release"
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(host.root / "prepare.lock"), str(held), str(release)]
    )
    deadline = time.monotonic() + 10
    while not held.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert held.exists(), "the holder never took the lock"
    try:
        yield release, child
    finally:
        release.write_text("go")
        child.wait(timeout=30)


def test_prepare_waits_for_another_preparation_and_does_not_prepare_twice(
    host: PrepareHost, lock_holder: tuple[Path, subprocess.Popen[bytes]]
) -> None:
    """R3.5: the second one waits on the lock -- touching nothing of the first one's,
    R4.6 -- then finds the first one's image."""
    release, _child = lock_holder
    host.toolbox.on_boot = prepared_ok
    # the first preparation's work dir: its QEMU runs under the lock this one waits for
    running = host.root / "prepare" / host.key
    running.mkdir(parents=True)
    (running / "disk.qcow2").write_bytes(b"being prepared")
    outcome: dict[str, Any] = {}

    def second() -> None:
        try:
            outcome["path"] = prepare(host.pins, root=host.root, runner=host.toolbox)
        except BaseException as err:  # reported below, on the test's thread
            outcome["error"] = err

    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive(), f"prepare did not wait for the lock: {outcome}"
    assert host.toolbox.tools_run() == [], "prepare ran a step while another one held the lock"
    assert (running / "disk.qcow2").exists(), "a running preparation's work dir was swept"
    # the first preparation finishes: its image appears, then it lets go
    host.prepared.parent.mkdir(parents=True, exist_ok=True)
    host.prepared.write_bytes(b"made by the first one")
    release.write_text("go")
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["path"] == host.prepared
    assert host.prepared.read_bytes() == b"made by the first one"
    assert host.toolbox.boots == []


def test_prepare_emits_one_json_event(host: PrepareHost, capfd: pytest.CaptureFixture[str]) -> None:
    """R9.1, Q9."""
    host.toolbox.on_boot = prepared_ok
    prepare(host.pins, root=host.root, runner=host.toolbox)
    found = []
    for line in capfd.readouterr().err.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") == "ci_prepare":
            found.append(obj)
    assert len(found) == 1, found
    assert found[0].get("key") == host.key
    assert found[0].get("result") and isinstance(found[0].get("duration_ms"), int)
