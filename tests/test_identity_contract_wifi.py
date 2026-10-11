"""The Wi-Fi half of the identity contract between the host and the worker (story 021,
task 4.3).

The host writes a worker's Wi-Fi connection with ``shidashi.provision.provision(...,
wifi=(ssid, psk))`` as ``identity/wifi/shidashi-wifi.nmconnection`` onto the personalized
ISO at ``/shidashi/identity``; the worker's ``kyomei_worker.py --restore-medium`` reads
the medium dracut mounts at ``/run/initramfs/live`` and installs the connection as
``/etc/NetworkManager/system-connections/shidashi-wifi.nmconnection``, then reloads
NetworkManager. These tests hold the two sides together through a REAL medium: provision
runs xorriso and ssh-keygen for real, the identity is read back from the copy with
``xorriso -osirrox`` as the worker sees its mounted stick, and the worker's own
``restore`` accepts what the host wrote -- no "ignored" message for a connection the host
rendered.

The worker module is imported from the rootfs as the image runs it (as in
tests/test_identity_contract.py); ``shidashi.provision`` is imported inside each test.
Hostile first: two workers whose SSIDs differ only in case must each install their own
connection; the converse, a worker provisioned without Wi-Fi, installs none.

Requirements exercised: R5.1, R5.4, R5.6, R5.8.
"""

import importlib
import inspect
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


ROOT = _repo_root()
WORKER_LIB = ROOT / "variants/worker/rootfs/usr/local/lib/shidashi"

#: Where provision maps the identity on the personalized ISO (story 020's design).
MAP_TARGET = PurePosixPath("/shidashi/identity")
NM_DIR = Path("etc/NetworkManager/system-connections")
NM_FILE = NM_DIR / "shidashi-wifi.nmconnection"
IDENTITY_WIFI = Path("identity/wifi/shidashi-wifi.nmconnection")
PSK = "Zq7 mVx2 r4Lk"

pytestmark = pytest.mark.skipif(
    shutil.which("xorriso") is None or shutil.which("ssh-keygen") is None,
    reason="needs xorriso and ssh-keygen",
)


@pytest.fixture
def kw(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(WORKER_LIB))
    for name in ("kyomei_worker", "kyomei_protocol", "worker_disk"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("kyomei_worker")


@pytest.fixture(autouse=True)
def chowns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Re-owning the installed files to root is recorded, not done: no root here."""
    seen: list[tuple[str, int, int]] = []

    def _chown(path: Any, uid: int, gid: int, *_a: Any, **_k: Any) -> None:
        seen.append((str(path), uid, gid))

    for name in ("chown", "lchown", "fchown"):
        monkeypatch.setattr(os, name, _chown)
    return seen


def _provision() -> Any:
    return importlib.import_module("shidashi.provision")


class _Runner:
    """Records every command the worker runs; each one succeeds."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], *_a: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def reloads(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c
            and Path(c[0]).name == "nmcli"
            and [a for a in c[1:] if not a.startswith("-")] == ["connection", "reload"]
        ]

    def nmcli(self) -> list[list[str]]:
        return [c for c in self.calls if c and Path(c[0]).name == "nmcli"]


@pytest.fixture(scope="module")
def worker_iso(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A generic worker ISO, built with xorriso (El Torito, like GRUB's BIOS boot)."""
    base = tmp_path_factory.mktemp("contract-iso")
    tree = base / "tree"
    (tree / "LiveOS").mkdir(parents=True)
    (tree / "LiveOS" / "squashfs.img").write_bytes(b"squash" * 512)
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "boot" / "grub" / "eltorito.img").write_bytes(bytes(2048))
    iso = base / "worker.iso"
    subprocess.run(
        ["xorriso", "-no_rc", "-as", "mkisofs", "-quiet", "-R", "-V", "BENTOO_WORKER"]
        + ["-b", "boot/grub/eltorito.img", "-no-emul-boot", "-boot-load-size", "4"]
        + ["-boot-info-table", "--protective-msdos-label", "-o", str(iso), str(tree)],
        check=True,
        capture_output=True,
    )
    return iso


@pytest.fixture
def workers_dir(tmp_path: Path, worker_iso: Path) -> Path:
    """The host's workers directory with its real worker key."""
    wd = tmp_path / "xdg" / "shidashi" / "worker"
    wd.mkdir(parents=True, mode=0o700)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "shidashi-worker-key"]
        + ["-f", str(wd / "id_ed25519")],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    return wd


def _provisioned_medium(
    tmp_path: Path, worker_iso: Path, workers_dir: Path, name: str, wifi: Any
) -> Path:
    """Provision ``name`` for real and read its identity back from the copy, at the path
    the worker finds it under its live mount."""
    iso = tmp_path / "dist" / name / "bentoo-2026.10.11-worker-systemd-v3.iso"
    iso.parent.mkdir(parents=True)
    shutil.copyfile(worker_iso, iso)
    kwargs: dict[str, Any] = {} if wifi is None else {"wifi": wifi}
    result = _provision().provision(
        name, iso, workers_dir=workers_dir, replace=False, runner=subprocess.run, **kwargs
    )
    medium = tmp_path / "live" / name / MAP_TARGET.relative_to("/")
    medium.parent.mkdir(parents=True)
    subprocess.run(
        ["xorriso", "-no_rc", "-osirrox", "on", "-indev", str(result.iso_path),
         "-extract", str(MAP_TARGET), str(medium)],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return medium


def _live_root(base: Path) -> Path:
    """A RAM root whose NetworkManager connections directory exists, as the image ships it."""
    root = base
    (root / NM_DIR).mkdir(parents=True, mode=0o700)
    return root


# --- hostile: SSIDs one case apart stay two connections ---------------------------------


def test_two_workers_whose_ssids_differ_only_in_case_each_install_their_own_connection(
    kw: Any,
    tmp_path: Path,
    worker_iso: Path,
    workers_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SSIDs are case-sensitive bytes: ``Bentoo-Lab`` and ``bentoo-lab`` are two networks.
    Each worker must install exactly what the host rendered for ITS network."""
    installed: dict[str, bytes] = {}
    for name, ssid in (("worker-a", "Bentoo-Lab"), ("worker-b", "bentoo-lab")):
        medium = _provisioned_medium(tmp_path, worker_iso, workers_dir, name, (ssid, PSK))
        root = _live_root(tmp_path / f"root-{name}")
        runner = _Runner()

        assert kw.restore(root=root, runner=runner, source=medium) == 0

        expected = _provision().wifi_connection(ssid, PSK)
        installed[name] = (root / NM_FILE).read_bytes()
        assert installed[name] == expected
        assert installed[name] == (workers_dir / name / IDENTITY_WIFI).read_bytes()
        assert len(runner.reloads()) == 1
        assert ["hostnamectl", "hostname", name] in runner.calls

    assert installed["worker-a"] != installed["worker-b"]
    _out, err = capsys.readouterr()
    assert "ignored" not in err


# --- the round trip ---------------------------------------------------------------------


def test_a_provisioned_wifi_connection_is_installed_from_a_real_medium_and_reloaded(
    kw: Any,
    tmp_path: Path,
    worker_iso: Path,
    workers_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    medium = _provisioned_medium(
        tmp_path, worker_iso, workers_dir, "bentoo-lab", ("bentoo-lab", PSK)
    )
    root = _live_root(tmp_path / "root")
    runner = _Runner()

    assert kw.restore(root=root, runner=runner, source=medium) == 0

    expected = _provision().wifi_connection("bentoo-lab", PSK)
    assert (root / NM_FILE).read_bytes() == expected
    assert (root / NM_FILE).read_bytes() == (
        workers_dir / "bentoo-lab" / IDENTITY_WIFI
    ).read_bytes()
    assert len(runner.reloads()) == 1
    assert (root / "run/shidashi/pairing.json").is_file()
    out, err = capsys.readouterr()
    assert "ignored" not in err
    assert PSK not in out and PSK not in err


# --- the converse: no --wifi, no connection ----------------------------------------------


def test_a_worker_provisioned_without_wifi_restores_with_no_connection_and_no_nmcli(
    kw: Any,
    tmp_path: Path,
    worker_iso: Path,
    workers_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # the provision API must already accept wifi: a no-wifi call through a wifi-aware
    # provision is the converse being exercised
    assert "wifi" in inspect.signature(_provision().provision).parameters
    medium = _provisioned_medium(tmp_path, worker_iso, workers_dir, "bentoo-lab", None)
    assert not (medium / "wifi").exists()
    root = _live_root(tmp_path / "root")
    runner = _Runner()

    assert kw.restore(root=root, runner=runner, source=medium) == 0

    assert list((root / NM_DIR).iterdir()) == []
    assert runner.nmcli() == []
    assert ["hostnamectl", "hostname", "bentoo-lab"] in runner.calls
    _out, err = capsys.readouterr()
    assert "wifi" not in err.lower()
