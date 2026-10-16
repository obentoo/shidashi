"""Unit tests of shidashi.schedule.discover -- the host and the paired workers as nodes.

Contract (design.md v3, story 014):
``discover(*, local, timeout=10.0, workers_dir=None) -> list[Node]`` and
``invoking_user_workers_dir() -> Path``.

- The host: ``os.cpu_count() or 1`` threads, ``os.getloadavg()[0]`` (``0.0`` with a note
  on ``OSError``), ``isaguard.runnable(isaguard.local_cpu_flags())``; its ``trunks`` are the
  union of the ``images`` field (the claimant image keys) of every
  ``config.scratch_dir()/assemble/checkpoints/*/install-*.json`` manifest that is not
  ``install-partial-*`` -- an absent directory or an unreadable manifest contributes
  nothing, never an error (R2.5).
- ``local=True``: the host alone, the registry untouched (R1.6).
- The workers (R6.1, R6.2): the registry is ``workers.load_registry(workers_dir /
  "workers.json")``; ``workers_dir`` defaults to the INVOKING user's
  (``invoking_user_workers_dir()``: under ``sudo`` the ``SUDO_USER``'s data directory,
  never root's; else ``config.workers_dir()``), and the probe's ssh uses that directory's
  key and pinned host keys. An unreadable registry: no worker node is reachable, and the
  reason is said. A worker with no recorded address is found over mDNS (as
  ``cli._on_worker`` does). Each probe is story 010's ``worker.status(remote, *,
  timeout)`` in its own thread, joined by the deadline; the node takes the registry
  name (C3), and from the status ``threads``, ``load1``, ``runnable_arches`` and
  ``trunk_keys`` (as ``trunks``).
- Unreachable with the reason in ``note`` (R3.1, R3.6): ``reachable=False``;
  ``accepts_jobs=False`` (no work disk mounted); a non-empty ``jobs`` (a job already
  running); a probe still running at the deadline; any exception
  (``RemoteUnreachable``, ``HostKeyMismatch``, ``isaguard.UnknownFlag``, ``OSError``); a
  status missing a field.

The real producers are used (``workers.WorkerEntry``/``save_registry``,
``worker.WorkerStatus``, ``remote.Remote``); only ``worker.status``, the host's CPU
probes, ``remote.find`` (mDNS) and ``pwd.getpwnam`` are faked.
"""

import inspect
import json
import os
import pwd
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shidashi import config, isaguard, remote, schedule, worker, workers

ZEN3_ARCHES = ("v1", "v2", "v3")
HOST_ARCHES = ("v1", "v2", "v3", "v4", "znver5")
LAB = "bentoo-lab"


def _status(name: str = LAB, **over: Any) -> worker.WorkerStatus:
    """A real story 010 status of a ready worker (work disk mounted, no job running)."""
    values: dict[str, Any] = {
        "name": name,
        "reachable": True,
        "reason": None,
        "cpu_model": "AMD Ryzen 7 5700X 8-Core Processor",
        "threads": 16,
        "cpu_flags": ("avx2", "fma"),
        "runnable_arches": ZEN3_ARCHES,
        "max_target": "v3",
        "load1": 0.25,
        "work_free": 800 << 30,
        "accepts_jobs": True,
        "image": "20261004",
        "trunks": (),
        "jobs": (),
        "trunk_keys": (),
    }
    values.update(over)
    return worker.WorkerStatus(**values)


def _entry(name: str, address: str | None = "192.0.2.10") -> workers.WorkerEntry:
    return workers.WorkerEntry(
        name=name,
        address=address,
        host_key="ssh-ed25519 AAAAC3NzaFAKE",
        host_key_fingerprint=f"SHA256:{name}-pin",
        paired_at="2026-10-05T10:00:00Z",
    )


@dataclass
class Cluster:
    """The paired workers (in a real registry) and how each answers its status probe."""

    wdir: Path
    scratch: Path
    answers: dict[str, Callable[[], Any]] = field(default_factory=dict)
    addresses: dict[str, str | None] = field(default_factory=dict)
    probed: list[Any] = field(default_factory=list)
    timeouts: list[Any] = field(default_factory=list)
    registry_paths: list[Path] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)

    def pair(
        self, name: str, answer: Callable[[], Any], address: str | None = "192.0.2.10"
    ) -> None:
        self.answers[name] = answer
        self.addresses[name] = address
        write_registry(self.wdir, {n: _entry(n, a) for n, a in self.addresses.items()})


def write_registry(wdir: Path, entries: dict[str, workers.WorkerEntry]) -> None:
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "id_ed25519").write_text("not a real key\n")
    (wdir / "known_hosts").write_text("bentoo-lab ssh-ed25519 AAAAC3NzaFAKE\n")
    workers.save_registry(wdir / "workers.json", entries)


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Cluster]:
    state = Cluster(
        wdir=tmp_path / "user-data" / "shidashi" / "worker", scratch=tmp_path / "scratch"
    )
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(state.scratch))
    monkeypatch.setattr(os, "cpu_count", lambda: 32)
    monkeypatch.setattr(os, "getloadavg", lambda: (3.25, 2.0, 1.0))
    monkeypatch.setattr(isaguard, "local_cpu_flags", lambda *_a: ("avx512f",))
    monkeypatch.setattr(
        isaguard, "runnable", lambda flags: HOST_ARCHES if "avx512f" in flags else ZEN3_ARCHES
    )
    real_load = workers.load_registry

    def load_registry(path: Path) -> dict[str, workers.WorkerEntry]:
        state.registry_paths.append(Path(path))
        return real_load(path)

    def status(remote_: Any, *_a: object, **kw: object) -> Any:
        state.probed.append(remote_)
        state.timeouts.append(kw.get("timeout"))
        return state.answers[remote_.name]()

    monkeypatch.setattr(workers, "load_registry", load_registry)
    monkeypatch.setattr(worker, "status", status)
    yield state
    state.release.set()  # unblock a probe left hanging by a timeout test


def _by_name(nodes: list[schedule.Node]) -> dict[str, schedule.Node]:
    return {n.name: n for n in nodes}


def _host(nodes: list[schedule.Node]) -> schedule.Node:
    (host,) = [n for n in nodes if n.kind == "host"]
    return host


def _manifest(scratch: Path, store: str, stem: str, images: list[str] | None) -> None:
    """A checkpoint manifest as checkpoint.Store writes it (format 3, ``images``)."""
    root = scratch / "assemble" / "checkpoints" / store
    root.mkdir(parents=True, exist_ok=True)
    step = stem.rsplit("-", 1)[0]
    payload = {
        "format": 3,
        "step": step,
        "fingerprint": stem.rsplit("-", 1)[1] + "0" * 40,
        "run_id": "20261004T100000Z-aaaaaa",
        "created": "2026-10-04T10:00:00+00:00",
        "images": images,
        "data": {},
    }
    (root / f"{stem}.json").write_text(json.dumps(payload))


def _sudo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user: str = "alice") -> Path:
    """Run as root under ``sudo`` by ``user``: root's HOME and data dir are not theirs."""
    home = tmp_path / f"home-{user}"
    monkeypatch.setenv("SUDO_USER", user)
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1000")
    monkeypatch.setenv("HOME", str(tmp_path / "root-home"))
    monkeypatch.setenv("USER", "root")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    real = pwd.getpwnam

    def getpwnam(name: str) -> pwd.struct_passwd:
        if name == user:
            return pwd.struct_passwd((user, "x", 1000, 1000, user, str(home), "/bin/bash"))
        if name == "root":
            return pwd.struct_passwd(
                ("root", "x", 0, 0, "root", str(tmp_path / "root-home"), "/bin/bash")
            )
        return real(name)

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    return home / ".local" / "share" / "shidashi" / "worker"


# --- local (R1.6) ------------------------------------------------------------------------


def test_discover_local_is_the_host_alone_and_never_reads_the_registry(cluster: Cluster) -> None:
    cluster.pair(LAB, _status)
    nodes = schedule.discover(local=True)
    assert [n.kind for n in nodes] == ["host"]
    assert cluster.probed == [] and cluster.registry_paths == []


def test_discover_the_host_is_this_machine_threads_load_and_isa(cluster: Cluster) -> None:
    host = _host(schedule.discover(local=True))
    assert host.threads == 32 and host.load1 == pytest.approx(3.25)
    assert host.reachable
    assert "v3" in host.runnable_arches and "znver5" in host.runnable_arches


# --- the host's own probes failing (Q4) ---------------------------------------------------


def test_discover_a_host_whose_cpu_count_is_unknown_has_one_thread(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert _host(schedule.discover(local=True)).threads == 1


def test_discover_a_host_without_a_load_average_has_load_zero_and_a_note(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_load() -> tuple[float, float, float]:
        raise OSError("getloadavg unavailable")

    monkeypatch.setattr(os, "getloadavg", no_load)
    host = _host(schedule.discover(local=True))
    assert host.reachable and host.load1 == 0.0
    assert "load" in host.note.lower()


# --- the host's trunks: the claimant keys of its install checkpoints (R2.5) ---------------


def test_discover_the_hosts_trunks_are_the_claimant_keys_of_its_install_manifests(
    cluster: Cluster,
) -> None:
    _manifest(
        cluster.scratch,
        "v3-systemd",
        "install-" + "d" * 24,
        ["v3-desktop-systemd", "v3-kde-systemd"],
    )
    _manifest(cluster.scratch, "v3-systemd", "install-" + "a" * 24, ["v3-minimal-systemd"])
    _manifest(cluster.scratch, "znver5-systemd", "install-" + "f" * 24, ["znver5-desktop-systemd"])
    host = _host(schedule.discover(local=True))
    assert host.trunks == frozenset(
        {"v3-desktop-systemd", "v3-kde-systemd", "v3-minimal-systemd", "znver5-desktop-systemd"}
    )
    # keys, never checkpoint ids: an id embeds a fingerprint no other node computes
    assert not any("/" in t or "install-" in t for t in host.trunks)


def test_discover_partial_installs_and_packages_checkpoints_are_no_trunks(
    cluster: Cluster,
) -> None:
    # hostile collapse: install-partial-* also matches install-*.json
    _manifest(cluster.scratch, "v3-systemd", "install-partial-" + "b" * 24, ["v3-gnome-systemd"])
    _manifest(cluster.scratch, "v3-systemd", "packages-" + "e" * 24, ["v3-wm-systemd"])
    _manifest(cluster.scratch, "v3-systemd", "install-" + "d" * 24, ["v3-desktop-systemd"])
    host = _host(schedule.discover(local=True))
    assert host.trunks == frozenset({"v3-desktop-systemd"})


def test_discover_an_unreadable_manifest_contributes_nothing(cluster: Cluster) -> None:
    root = cluster.scratch / "assemble" / "checkpoints" / "v3-systemd"
    root.mkdir(parents=True)
    (root / ("install-" + "c" * 24 + ".json")).write_text('{"format": 3, "images": ["v3-')
    _manifest(cluster.scratch, "v3-systemd", "install-" + "d" * 24, ["v3-desktop-systemd"])
    host = _host(schedule.discover(local=True))
    assert host.reachable and host.trunks == frozenset({"v3-desktop-systemd"})


def test_discover_a_host_without_a_checkpoint_store_has_no_trunks(cluster: Cluster) -> None:
    host = _host(schedule.discover(local=True))  # the scratch directory does not exist
    assert host.trunks == frozenset() and host.reachable


# --- whose registry (R6.1, R6.2) ---------------------------------------------------------


def test_invoking_user_workers_dir_under_sudo_is_the_sudo_users_not_roots(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    alice = _sudo(monkeypatch, tmp_path)
    got = schedule.invoking_user_workers_dir()
    assert got == alice
    assert got != config.workers_dir()  # root's own would be an empty registry


def test_invoking_user_workers_dir_without_sudo_is_this_users(cluster: Cluster) -> None:
    assert schedule.invoking_user_workers_dir() == config.workers_dir() == cluster.wdir


def test_discover_under_sudo_reads_the_sudo_users_registry_key_and_pins(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    alice = _sudo(monkeypatch, tmp_path)
    write_registry(alice, {LAB: _entry(LAB)})
    cluster.answers[LAB] = _status
    nodes = _by_name(schedule.discover(local=False))
    assert nodes[LAB].reachable, nodes[LAB].note
    assert cluster.registry_paths == [alice / "workers.json"]
    (probe,) = cluster.probed
    assert Path(probe.key) == alice / "id_ed25519"
    assert Path(probe.known_hosts) == alice / "known_hosts"


def test_discover_reads_the_registry_of_the_workers_dir_it_is_given(
    cluster: Cluster, tmp_path: Path
) -> None:
    other = tmp_path / "given" / "worker"
    write_registry(other, {LAB: _entry(LAB)})
    cluster.answers[LAB] = _status
    nodes = _by_name(schedule.discover(local=False, workers_dir=other))
    assert nodes[LAB].reachable
    assert cluster.registry_paths == [other / "workers.json"]
    (probe,) = cluster.probed
    assert Path(probe.key) == other / "id_ed25519"


def test_discover_an_unreadable_registry_gives_no_worker_and_says_why(cluster: Cluster) -> None:
    cluster.wdir.mkdir(parents=True)
    (cluster.wdir / "workers.json").write_text("{ not json")
    nodes = schedule.discover(local=False)
    assert _host(nodes).reachable
    assert not [n for n in nodes if n.kind == "worker" and n.reachable]
    assert cluster.probed == []
    notes = " ".join(n.note for n in nodes if not n.reachable).lower()
    assert "registry" in notes or "workers.json" in notes


# --- the paired workers ------------------------------------------------------------------


def test_discover_a_worker_is_what_its_own_status_says_named_by_its_entry(
    cluster: Cluster,
) -> None:
    cluster.pair(LAB, lambda: _status(threads=16, load1=0.25))
    nodes = _by_name(schedule.discover(local=False))
    lab = nodes[LAB]  # C3: the registry name is the node name
    assert lab.kind == "worker" and lab.reachable
    assert lab.threads == 16 and lab.load1 == pytest.approx(0.25)
    (probe,) = cluster.probed
    assert probe.name == LAB and probe.address == "192.0.2.10"


def test_discover_a_worker_with_no_recorded_address_is_found_over_mdns(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up: list[str] = []

    def find(name: str, **_kw: Any) -> str | None:
        looked_up.append(name)
        return "192.0.2.77" if name == LAB else None

    monkeypatch.setattr(remote, "find", find)
    cluster.pair(LAB, _status, address=None)
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert lab.reachable, lab.note
    assert LAB in looked_up
    (probe,) = cluster.probed
    assert probe.address == "192.0.2.77"


def test_discover_a_worker_nobody_finds_over_mdns_is_unreachable_with_the_reason(
    cluster: Cluster,
) -> None:
    cluster.pair(LAB, _status, address=None)  # conftest: no mDNS answer
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert not lab.reachable
    assert "mdns" in lab.note.lower() or "address" in lab.note.lower()


def test_discover_a_workers_arches_are_the_ones_its_status_runs(cluster: Cluster) -> None:
    cluster.pair(LAB, lambda: _status(runnable_arches=ZEN3_ARCHES))
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert "v3" in lab.runnable_arches
    assert "znver5" not in lab.runnable_arches and "arrowlake" not in lab.runnable_arches


def test_discover_a_workers_trunks_are_its_status_trunk_keys_only(cluster: Cluster) -> None:
    # hostile collapse: the host's own claimants and the worker's checkpoint ids
    # (status.trunks) are not the worker's trunk keys
    _manifest(cluster.scratch, "v3-systemd", "install-" + "d" * 24, ["v3-desktop-systemd"])
    cluster.pair(
        LAB,
        lambda: _status(
            trunks=("v3-systemd/install-" + "a" * 24,), trunk_keys=("v3-minimal-systemd",)
        ),
    )
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert lab.trunks == frozenset({"v3-minimal-systemd"})


# --- a worker that is not there, or not ready (R3.1, R3.6, Q4) ---------------------------


def test_discover_probes_with_a_10_second_timeout_by_default(cluster: Cluster) -> None:
    assert inspect.signature(schedule.discover).parameters["timeout"].default == 10.0
    cluster.pair(LAB, _status)
    schedule.discover(local=False)
    assert cluster.timeouts == [10.0]


def test_discover_a_status_saying_unreachable_is_an_unreachable_node_with_its_reason(
    cluster: Cluster,
) -> None:
    # status never raises for a silent worker -- it answers reachable=False
    cluster.pair(
        LAB,
        lambda: worker.WorkerStatus(
            name=LAB, reachable=False, reason="bentoo-lab (192.0.2.10) is unreachable: timed out"
        ),
    )
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert not lab.reachable
    assert "timed out" in lab.note


def test_discover_a_worker_without_its_work_disk_is_not_plannable(cluster: Cluster) -> None:
    # it answers, but cannot take a job (R3.6)
    cluster.pair(LAB, lambda: _status(accepts_jobs=False, work_free=None))
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert not lab.reachable
    assert "work disk" in lab.note.lower()


def test_discover_a_worker_already_running_a_job_is_not_plannable_naming_the_job(
    cluster: Cluster,
) -> None:
    cluster.pair(LAB, lambda: _status(jobs=("shidashi-job-fac-v3",)))
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert not lab.reachable
    assert "fac-v3" in lab.note


def test_discover_a_ready_idle_worker_is_plannable(cluster: Cluster) -> None:
    # the benign half of the two above
    cluster.pair(LAB, lambda: _status(accepts_jobs=True, jobs=()))
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert lab.reachable and lab.note == ""


def test_discover_a_worker_that_does_not_answer_in_time_is_unreachable(cluster: Cluster) -> None:
    def hang() -> worker.WorkerStatus:
        cluster.release.wait(10)
        return _status()

    cluster.pair(LAB, hang)
    started = time.monotonic()
    nodes = _by_name(schedule.discover(local=False, timeout=0.3))
    assert time.monotonic() - started < 5
    lab = nodes[LAB]
    assert not lab.reachable
    assert "time" in lab.note.lower()  # "timed out" / "timeout"
    assert _host(list(nodes.values())).reachable


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (
            ConnectionRefusedError("ssh: connect to host 192.0.2.10 port 22: Connection refused"),
            "Connection refused",
        ),
        (
            remote.RemoteUnreachable(LAB, "192.0.2.10", "Connection timed out"),
            "Connection timed out",
        ),
        (remote.HostKeyMismatch(LAB, "SHA256:pin", "SHA256:impostor"), "does not match the pin"),
        (isaguard.UnknownFlag("znver5", "avx512_bf16"), "avx512_bf16"),
    ],
)
def test_discover_a_probe_that_raises_is_unreachable_with_the_reason(
    cluster: Cluster, error: Exception, needle: str
) -> None:
    def fail() -> worker.WorkerStatus:
        raise error

    cluster.pair(LAB, fail)
    nodes = _by_name(schedule.discover(local=False))
    lab = nodes[LAB]
    assert not lab.reachable
    assert needle in lab.note
    assert _host(list(nodes.values())).reachable


def test_discover_a_worker_with_a_malformed_status_is_unreachable(cluster: Cluster) -> None:
    cluster.pair(LAB, lambda: SimpleNamespace(reachable=True))  # no threads, no load
    lab = _by_name(schedule.discover(local=False))[LAB]
    assert not lab.reachable and lab.note


def test_discover_one_silent_worker_does_not_hide_another(cluster: Cluster) -> None:
    def hang() -> worker.WorkerStatus:
        cluster.release.wait(10)
        return _status("lab-silent")

    cluster.pair("lab-silent", hang, address="192.0.2.11")
    cluster.pair(LAB, lambda: _status(threads=16))
    nodes = _by_name(schedule.discover(local=False, timeout=0.3))
    assert not nodes["lab-silent"].reachable
    assert nodes[LAB].reachable and nodes[LAB].threads == 16
