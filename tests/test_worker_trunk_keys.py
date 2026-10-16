"""The worker reports its trunk claimants: ``WorkerStatus.trunk_keys`` (story 014, 3.3).

Contract (design.md v3, "Worker status"): the status probe also reports, for every
``install-<fp>.json`` checkpoint manifest under the worker's checkpoints (NOT
``install-partial-*``, not ``packages-*``), the image keys of its ``images`` field -- the
claimants (``v3-desktop-systemd``, ``v3-kde-systemd``). ``WorkerStatus`` gains
``trunk_keys: tuple[str, ...] = ()``, sorted and unique. A manifest that cannot be parsed
contributes nothing. An older worker whose probe prints no such section, or a worker
without its work disk mounted, gives ``()``. The existing ``trunks`` field (every
checkpoint file as ``<arch>-<init>/<step>-<fp24>``) is unchanged (R2.5).

Why keys: an install fingerprint embeds the cache path of the pinned repositories
(``/var/cache/shidashi/repos`` on the host, ``/mnt/work/cache/repos`` on a worker), so a
checkpoint id the host computes never equals the worker's; the claimant keys do.

The probe runs for real against tests/_fake_worker.py (its ssh runs the remote script
locally under a temporary root standing for the worker's ``/``); the manifests are
written in the shape ``checkpoint.Store`` writes them.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import remote, worker
from tests._fake_worker import ZEN3_CPUINFO_FLAGS, FakeWorker

CHECKPOINTS = "scratch/assemble/checkpoints"  # under /mnt/work: worker.WORKER_CHECKPOINTS


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    yield w
    w.close()


def _manifest(fw: FakeWorker, store: str, stem: str, images: list[str] | None) -> None:
    """A checkpoint manifest as checkpoint.Store writes it (format 3)."""
    step, _, fp24 = stem.rpartition("-")
    payload: dict[str, Any] = {
        "format": 3,
        "step": step,
        "fingerprint": fp24 + "0" * 40,
        "run_id": "20261014T100000Z-aaaaaa",
        "created": "2026-10-14T10:00:00+00:00",
        "images": images,
        "data": {"since": 0, "reused": []},
    }
    fw.put(f"{CHECKPOINTS}/{store}/{stem}.json", json.dumps(payload, indent=1) + "\n")


def test_status_carries_the_claimant_keys_of_the_install_manifests(fw: FakeWorker) -> None:
    _manifest(fw, "v3-systemd", "install-" + "d" * 24, ["v3-desktop-systemd", "v3-kde-systemd"])
    _manifest(fw, "v3-systemd", "install-" + "a" * 24, ["v3-minimal-systemd"])
    st = worker.status(fw.remote())
    assert st.reachable, st.reason
    assert st.trunk_keys == ("v3-desktop-systemd", "v3-kde-systemd", "v3-minimal-systemd")


def test_partial_installs_and_packages_checkpoints_claim_no_trunk(fw: FakeWorker) -> None:
    # hostile collapse: install-partial-*.json matches install-*.json too
    _manifest(fw, "v3-systemd", "install-partial-" + "b" * 24, ["v3-gnome-systemd"])
    _manifest(fw, "v3-systemd", "packages-" + "e" * 24, ["v3-wm-systemd"])
    _manifest(fw, "v3-systemd", "install-" + "d" * 24, ["v3-desktop-systemd"])
    st = worker.status(fw.remote())
    assert st.trunk_keys == ("v3-desktop-systemd",)


def test_duplicate_claimants_collapse_and_the_keys_are_sorted(fw: FakeWorker) -> None:
    # hostile split: one key claimed by two checkpoints (two stores) is ONE key
    _manifest(fw, "v3-systemd", "install-" + "d" * 24, ["v3-kde-systemd", "v3-desktop-systemd"])
    _manifest(fw, "v3-systemd", "install-" + "c" * 24, ["v3-desktop-systemd"])
    _manifest(fw, "znver5-systemd", "install-" + "f" * 24, ["znver5-desktop-systemd"])
    st = worker.status(fw.remote())
    assert st.trunk_keys == ("v3-desktop-systemd", "v3-kde-systemd", "znver5-desktop-systemd")


def test_keys_of_two_inits_stay_apart(fw: FakeWorker) -> None:
    # hostile collapse: the same trunk stage under systemd and openrc are two trunks
    _manifest(fw, "v3-systemd", "install-" + "d" * 24, ["v3-desktop-systemd"])
    _manifest(fw, "v3-openrc", "install-" + "d" * 24, ["v3-desktop-openrc"])
    st = worker.status(fw.remote())
    assert st.trunk_keys == ("v3-desktop-openrc", "v3-desktop-systemd")


def test_an_unparsable_manifest_contributes_nothing(fw: FakeWorker) -> None:
    fw.put(f"{CHECKPOINTS}/v3-systemd/install-{'9' * 24}.json", '{"format": 3, "images": ["v3-')
    _manifest(fw, "v3-systemd", "install-" + "8" * 24, None)  # no claimants recorded
    _manifest(fw, "v3-systemd", "install-" + "d" * 24, ["v3-desktop-systemd"])
    st = worker.status(fw.remote())
    assert st.reachable, st.reason
    assert st.trunk_keys == ("v3-desktop-systemd",)


def test_a_worker_without_its_work_disk_reports_no_trunk_keys(fw: FakeWorker) -> None:
    _manifest(fw, "v3-systemd", "install-" + "d" * 24, ["v3-desktop-systemd"])
    fw.set(mounted=False)
    st = worker.status(fw.remote())
    assert st.reachable and st.accepts_jobs is False
    assert st.trunk_keys == ()


def test_a_worker_with_no_checkpoints_reports_no_trunk_keys(fw: FakeWorker) -> None:
    st = worker.status(fw.remote())
    assert st.reachable and st.trunk_keys == ()


def test_trunk_keys_come_beside_an_unchanged_trunks_field(fw: FakeWorker) -> None:
    fp = "d" * 24
    _manifest(fw, "v3-systemd", f"install-{fp}", ["v3-desktop-systemd"])
    _manifest(fw, "v3-systemd", "install-partial-" + "b" * 24, ["v3-gnome-systemd"])
    st = worker.status(fw.remote())
    # trunks: every checkpoint file, as before (shidashi worker status reads it)
    assert st.trunks == (f"v3-systemd/install-{fp}", f"v3-systemd/install-partial-{'b' * 24}")
    assert st.trunk_keys == ("v3-desktop-systemd",)


def test_an_older_worker_whose_probe_prints_no_claimants_gives_an_empty_tuple(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # what a worker image from before story 014 answers: the probe's sections up to v2
    old = "\n".join(
        [
            "@threads",
            "16",
            "@model",
            "model name\t: AMD Ryzen 7 5700X 8-Core Processor",
            "@flags",
            "flags\t\t: " + " ".join(ZEN3_CPUINFO_FLAGS),
            "@load",
            "0.25 0.20 0.10 1/300 1234",
            "@mem",
            "MemTotal:       32000000 kB",
            "MemAvailable:   30000000 kB",
            "@image",
            "BUILD_ID=20261004",
            "@work",
            "mounted",
            "@free",
            "Avail",
            "800000000000",
            "@trunks",
            f"v3-systemd/install-{'d' * 24}.json",
            "@jobs",
            "@smart",
            "",
        ]
    )

    def run(target: Any, command: str, **_kw: Any) -> remote.RemoteResult:
        return remote.RemoteResult(command, 0, old, "", 0.1)

    monkeypatch.setattr(worker, "run", run)
    target = remote.Remote(
        name="bentoo-lab",
        address="192.0.2.10",
        key=tmp_path / "id_ed25519",
        known_hosts=tmp_path / "known_hosts",
    )
    st = worker.status(target)
    assert st.reachable and st.threads == 16
    assert st.trunks == (f"v3-systemd/install-{'d' * 24}",)
    assert st.trunk_keys == ()


def test_an_unreachable_worker_has_no_trunk_keys() -> None:
    st = worker.WorkerStatus(name="bentoo-lab", reachable=False, reason="timed out")
    assert st.trunk_keys == ()
