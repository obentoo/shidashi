"""Shared fixtures."""

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

# Typer decides at IMPORT time to force colored help when it sees one of these
# (a CI runner sets GITHUB_ACTIONS): ANSI codes then split "--format" and the
# help tests fail on the runner only (found by `act -j quality`). Cleared here,
# before any test module imports typer.
for _var in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_var, None)


@pytest.fixture(autouse=True)
def _runs_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's audit trails go to a temporary directory, never /var/log."""
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path_factory.mktemp("runs")))


@pytest.fixture(autouse=True)
def _registry_in_tmp(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No test reads the invoking user's worker registry. Story 028's guard refuses a
    host factory of an arch a paired worker owns, so a v3 owned on this machine would
    otherwise fail every v3 test. A test that pairs workers sets its own."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))


@pytest.fixture(autouse=True)
def _checkpoints_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assemble's checkpoints stay off unless a test hands in a backend: on a
    btrfs /tmp the real ``btrfs`` command would otherwise run, without root."""
    from shidashi import checkpoint

    monkeypatch.setattr(checkpoint, "filesystem_type", lambda _path: "tmpfs")


@pytest.fixture
def no_stage3_vdb(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The binpkg check without a cached stage3: its vdb extraction is recorded,
    not run. Returns the destinations it was asked for."""
    from shidashi import phases

    extracted: list[Path] = []

    def _extract(_tarball: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        extracted.append(dest)

    monkeypatch.setattr(phases, "_seed_tarball", lambda _recipe: Path("/stage3.tar.xz"))
    monkeypatch.setattr(phases, "_extract_vdb", _extract)
    return extracted


@pytest.fixture(autouse=True)
def _build_host_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's host check passes: tests describe hosts through doctor.checks'
    probes (tests/test_doctor.py), never through the machine they run on --
    a CI runner has no systemd-nspawn."""
    from shidashi import doctor

    monkeypatch.setattr(doctor, "require_build_host", lambda _work_dir: None)


@pytest.fixture
def qemu_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node has QEMU, whatever the machine running the suite has (a CI runner has
    none): ``vmrunner`` takes its native path -- straight to ``vm.boot_test`` and
    ``vm.read_build_info``, which such a test stubs -- and its preflight needs nothing
    (story 024). Opt-in per test, never autouse: tests/test_vmrunner*.py test
    ``vmrunner.native`` itself."""
    from shidashi import vmrunner

    monkeypatch.setattr(vmrunner, "native", lambda *_a, **_k: True)


#: podman's default seccomp profile, cut to the shape the runner reads: the socket rules
#: of /usr/share/containers/seccomp.json (containers-common, podman 6), AF_VSOCK deny
#: included. tests/test_vmrunner_review.py reads the real file where there is one.
_SECCOMP_DEFAULT = {
    "defaultAction": "SCMP_ACT_ERRNO",
    "defaultErrno": "ENOSYS",
    "defaultErrnoRet": 38,
    "syscalls": [
        {"names": ["read", "write"], "action": "SCMP_ACT_ALLOW", "args": []},
        {
            "names": ["socket"],
            "action": "SCMP_ACT_ERRNO",
            "args": [{"index": 0, "value": 40, "valueTwo": 0, "op": "SCMP_CMP_EQ"}],
            "errnoRet": 1,
            "errno": "EPERM",
        },
        {
            "names": ["socket"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 16, "valueTwo": 0, "op": "SCMP_CMP_NE"}],
            "excludes": {"caps": ["CAP_AUDIT_WRITE"]},
        },
    ],
}


@pytest.fixture(autouse=True)
def _seccomp_default(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's default seccomp profile is a fixture copy of podman's, never the
    machine's own (story 024, task 2.4): a CI runner without containers-common has no
    /usr/share/containers/seccomp.json. A test may point it elsewhere itself."""
    import json

    from shidashi import vmrunner

    profile = tmp_path_factory.mktemp("seccomp") / "seccomp.json"
    profile.write_text(json.dumps(_SECCOMP_DEFAULT), encoding="utf-8")
    monkeypatch.setattr(vmrunner, "_DEFAULT_SECCOMP", profile)


@pytest.fixture(autouse=True)
def _locks_in_tmp(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that leaves SHIDASHI_CACHE unset takes its owner locks in a temporary
    directory, never in the real /var/cache/shidashi/locks. A test that sets it (at any
    point) gets the real ``<cache>/locks`` under its own cache."""
    from shidashi import ownership

    real = ownership.locks_dir
    spare = tmp_path_factory.mktemp("locks")

    def _locks_dir() -> Path:
        return real() if os.environ.get("SHIDASHI_CACHE") else spare

    monkeypatch.setattr(ownership, "locks_dir", _locks_dir)


@pytest.fixture(autouse=True)
def _cid_locks_in_tmp(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test's vsock CID locks go to a temporary directory, never the host's real
    /tmp/shidashi-vm-cids (story 006, task 8.3); a test may point it elsewhere itself."""
    monkeypatch.setenv(
        "SHIDASHI_CID_LOCKS", str(tmp_path_factory.mktemp("cids") / "shidashi-vm-cids")
    )


@pytest.fixture(autouse=True)
def _no_mdns_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No worker answers the mDNS lookup of a worker command (story 020) unless a test
    says otherwise: no test sends a multicast packet or waits the real 5 s. Patched as
    ``remote.find`` -- ``mdns.find`` itself stays real for its own tests."""
    from shidashi import remote

    monkeypatch.setattr(remote, "find", lambda _name, **_kw: None)


@pytest.fixture
def host_only_planner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``shidashi build`` plans every image on this host, from nothing on this machine.

    Every build is planned (story 014): without this, a test of the build CLI would read
    the real recipes' worlds (``schedule.image_specs``) and the invoking user's worker
    registry, then ssh each paired worker (``schedule.discover``). HOME and
    ``XDG_DATA_HOME`` point into ``tmp_path``, ``SUDO_USER`` is dropped, the specs come
    from the image names alone and the only node is the host, able to run every arch it
    is asked for. Opt-in per module (``pytestmark``), never autouse: tests of the planner
    patch these seams themselves.
    """
    import socket

    from shidashi import schedule

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home" / ".local" / "share"))
    monkeypatch.delenv("SUDO_USER", raising=False)
    arches: set[str] = set()

    def image_specs(
        arch: str, init: str, images: Sequence[str], **_kw: object
    ) -> dict[str, schedule.ImageSpec]:
        arches.add(arch)
        return {
            f"{arch}-{image}-{init}": schedule.ImageSpec(
                key=f"{arch}-{image}-{init}", arch=arch, image=image, packages=1000
            )
            for image in images
        }

    def discover(**_kw: object) -> list[schedule.Node]:
        return [
            schedule.Node(
                name=socket.gethostname(),
                kind="host",
                threads=os.cpu_count() or 1,
                load1=0.0,
                runnable_arches=frozenset(arches),
                trunks=frozenset(),
                sec_per_pkg=None,
                reachable=True,
            )
        ]

    monkeypatch.setattr(schedule, "image_specs", image_specs)
    monkeypatch.setattr(schedule, "discover", discover)
