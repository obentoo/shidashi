"""The identity contract between the host and the worker (story 020, task 4.1).

The host writes a worker's identity (``shidashi.provision.identity_files`` plus an
``ssh-keygen -t ed25519`` host key pair) and maps it onto the personalized ISO at
``/shidashi/identity``; dracut mounts the medium at ``/run/initramfs/live``, and the
worker's ``kyomei_worker.py --restore-medium`` restores it from
``/run/initramfs/live/shidashi/identity``. These tests hold the two sides together:
what the host lays out is what the worker reads and installs, byte for byte.

Layout, as design.md's "Identity layout" names it::

    identity/pairing.json                      {"v": 1, "name", "granting_key_fingerprint",
                                                "paired_at", "source": "provisioned"}
    identity/authorized_keys                   one line: the host's public key
    identity/ssh/ssh_host_ed25519_key{,.pub}   the worker's host key pair

The worker module is imported from the rootfs as the image runs it (the ``kw`` fixture
of tests/test_kyomei_worker.py); ``shidashi.provision`` is imported inside each test,
so until it exists each test fails on the missing module, not the file on collection.
Hostile fixtures come first: a like-shaped identity beside the medium's must not be the
one installed, two workers must stay two, one worker across boots must stay one, and
the generic worker image must carry nothing at the path the worker reads.

Requirements exercised: R1.4, R2.1, R4.1.
"""

import datetime as dt
import importlib
import json
import os
import re
import shutil
import stat
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
WORKER_ROOTFS = ROOT / "variants/worker/rootfs"
WORKER_LIB = WORKER_ROOTFS / "usr/local/lib/shidashi"

#: Where provision maps the identity on the personalized ISO (design.md).
MAP_TARGET = PurePosixPath("/shidashi/identity")
#: Where dracut's dmsquash-live mounts the boot medium.
DRACUT_LIVE = PurePosixPath("/run/initramfs/live")
NOW = dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=dt.UTC)

needs_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="needs OpenSSH's ssh-keygen"
)
needs_xorriso = pytest.mark.skipif(shutil.which("xorriso") is None, reason="needs xorriso")


@pytest.fixture
def kw(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(WORKER_LIB))
    for name in ("kyomei_worker", "kyomei_protocol", "worker_disk"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("kyomei_worker")


@pytest.fixture
def chowns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Re-owning the installed keys to root is recorded, not done: no root here."""
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

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")


def _keygen(path: Path, comment: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True,
        capture_output=True,
    )


def _keygen_fingerprint(pub: Path) -> str:
    """The fingerprint as the host computes it (``ssh-keygen -lf``)."""
    done = subprocess.run(
        ["ssh-keygen", "-lf", str(pub)], check=True, capture_output=True, text=True
    )
    return done.stdout.split()[1]


class _Host:
    """The host's worker key (``id_ed25519``), as ``shidashi worker`` keeps it."""

    def __init__(self, base: Path) -> None:
        self.key = base / "id_ed25519"
        _keygen(self.key, "shidashi-worker-key")
        self.pub = self.key.with_name("id_ed25519.pub").read_text(encoding="utf-8").strip()
        self.fingerprint = _keygen_fingerprint(self.key.with_name("id_ed25519.pub"))


def _lay_out_identity(base: Path, name: str, host: _Host) -> Path:
    """``base/identity`` as provision lays it: identity_files' entries, then the
    worker's ed25519 host key pair under ``ssh/``; directories 0700, files 0600."""
    identity = base / "identity"
    identity.mkdir(parents=True, mode=0o700)
    for rel, data in _provision().identity_files(name, host.pub, host.fingerprint, NOW).items():
        dest = identity / rel
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest.write_bytes(data)
    _keygen(identity / "ssh" / "ssh_host_ed25519_key", name)
    for path in identity.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return identity


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(directory)): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _installed_fingerprint(kw: Any, root: Path) -> str:
    pub = (root / "etc/ssh/ssh_host_ed25519_key.pub").read_text(encoding="utf-8")
    return str(sys.modules["kyomei_protocol"].fingerprint(pub.strip()))


def _key_fields(line: str) -> list[str]:
    return line.split()[:2]


# --- hostile: the identity installed is the medium's, and only it -----------------------


@pytest.mark.usefixtures("chowns")
@needs_ssh_keygen
def test_the_medium_identity_is_installed_not_a_like_shaped_work_disk_pairing(
    kw: Any, tmp_path: Path
) -> None:
    """A work-disk pairing of another worker sits where today's restore reads; the
    restore from the medium must install the medium's keys and name, and leave the
    work disk's files untouched (R2.1, R2.4 at the contract's level)."""
    host = _Host(tmp_path / "host")
    medium = _lay_out_identity(tmp_path / "medium", "bentoo-lab", host)
    root = tmp_path / "root"
    decoy = _lay_out_identity(tmp_path / "decoy", "decoy-lab", host)
    disk = root / "mnt/work/.shidashi"
    shutil.copytree(decoy, disk)
    disk_before = _tree(disk)
    runner = _Runner()

    assert kw.restore(root=root, runner=runner, source=medium) == 0

    assert (root / "etc/ssh/ssh_host_ed25519_key").read_bytes() == (
        medium / "ssh/ssh_host_ed25519_key"
    ).read_bytes()
    assert (root / "etc/ssh/ssh_host_ed25519_key").read_bytes() != (
        disk / "ssh/ssh_host_ed25519_key"
    ).read_bytes()
    assert ["hostnamectl", "hostname", "bentoo-lab"] in runner.calls
    assert ["hostnamectl", "hostname", "decoy-lab"] not in runner.calls
    record = json.loads((root / "run/shidashi/pairing.json").read_text(encoding="utf-8"))
    assert record["name"] == "bentoo-lab"
    assert _tree(disk) == disk_before


@pytest.mark.usefixtures("chowns")
@needs_ssh_keygen
def test_two_provisioned_workers_stay_two(kw: Any, tmp_path: Path) -> None:
    """Near-identical identities (same host, same layout, names one letter apart) must
    not collapse: each medium installs its own key and name, and the key each worker
    presents is the one the host pinned for that name, not the other's."""
    host = _Host(tmp_path / "host")
    pinned: dict[str, str] = {}
    presented: dict[str, str] = {}
    for name in ("worker-a", "worker-b"):
        medium = _lay_out_identity(tmp_path / name, name, host)
        pinned[name] = _keygen_fingerprint(medium / "ssh/ssh_host_ed25519_key.pub")
        root = tmp_path / f"root-{name}"
        runner = _Runner()
        assert kw.restore(root=root, runner=runner, source=medium) == 0
        assert ["hostnamectl", "hostname", name] in runner.calls
        presented[name] = _installed_fingerprint(kw, root)

    assert pinned["worker-a"] != pinned["worker-b"]
    assert presented == pinned


@pytest.mark.usefixtures("chowns")
@needs_ssh_keygen
def test_one_provisioned_worker_stays_one_across_boots(kw: Any, tmp_path: Path) -> None:
    """The same medium restored into two fresh RAM roots (two boots) must not split
    into two identities: the same key bytes, the fingerprint the host pinned (R2.6)."""
    host = _Host(tmp_path / "host")
    medium = _lay_out_identity(tmp_path / "medium", "bentoo-lab", host)
    pinned = _keygen_fingerprint(medium / "ssh/ssh_host_ed25519_key.pub")
    installed: list[bytes] = []
    for boot in ("boot-1", "boot-2"):
        root = tmp_path / boot
        assert kw.restore(root=root, runner=_Runner(), source=medium) == 0
        assert _installed_fingerprint(kw, root) == pinned
        installed.append((root / "etc/ssh/ssh_host_ed25519_key").read_bytes())

    assert installed[0] == installed[1]


def test_the_generic_worker_image_carries_nothing_at_the_path_the_worker_reads(
    kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4.1: the rootfs every worker shares holds no identity -- nothing at the medium
    path ``--restore-medium`` reads, no ``shidashi/identity`` anywhere, no sshd host
    key and no pairing record. Otherwise every worker booted from the generic ISO
    would come up as the same worker."""
    seen: list[dict[str, Any]] = []

    def _restore(*args: Any, **kwargs: Any) -> int:
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(kw, "restore", _restore)
    assert kw.main(["--restore-medium"]) == 0
    assert len(seen) == 1, "--restore-medium does not restore from the medium"
    medium = PurePosixPath(Path(seen[0]["source"]))
    assert medium.is_absolute()

    assert not (WORKER_ROOTFS / medium.relative_to("/")).exists()
    leaked = [
        str(p.relative_to(WORKER_ROOTFS))
        for p in WORKER_ROOTFS.rglob("*")
        if p.match("ssh_host_*_key")
        or p.match("ssh_host_*_key.pub")
        or p.name == "pairing.json"
        or p.parts[-2:] == ("shidashi", "identity")
    ]
    assert leaked == []


# --- the path: the map target under dracut's mount --------------------------------------


def test_restore_medium_reads_the_map_target_under_dracuts_live_mount(
    kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--restore-medium`` restores from ``/run/initramfs/live/shidashi/identity``:
    provision's map target ``/shidashi/identity`` on the medium dracut mounts at
    ``/run/initramfs/live``, and announces the name (design.md, kyomei_worker.py)."""
    seen: list[dict[str, Any]] = []

    def _restore(*args: Any, **kwargs: Any) -> int:
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(kw, "restore", _restore)

    assert kw.main(["--restore-medium"]) == 0
    assert len(seen) == 1
    assert Path(seen[0]["source"]) == Path("/run/initramfs/live/shidashi/identity")
    assert Path(seen[0]["source"]) == Path(DRACUT_LIVE / MAP_TARGET.relative_to("/"))
    assert seen[0].get("announce") is True


# --- the fields: everything the worker reads, the host writes ---------------------------


@needs_ssh_keygen
def test_every_field_the_worker_reads_is_in_what_the_host_writes(kw: Any, tmp_path: Path) -> None:
    """``restore`` reads ``v``, ``name`` and ``granting_key_fingerprint`` from the record
    and finds the key it names in ``authorized_keys`` by the WORKER's fingerprint; the
    host's ``ssh-keygen -lf`` spelling must be the one the worker computes."""
    host = _Host(tmp_path / "host")
    identity = _lay_out_identity(tmp_path / "medium", "bentoo-lab", host)
    worker_protocol = sys.modules["kyomei_protocol"]

    record = json.loads((identity / "pairing.json").read_text(encoding="utf-8"))
    assert type(record["v"]) is int and record["v"] == 1
    assert record["name"] == "bentoo-lab"
    assert re.fullmatch(worker_protocol.HOSTNAME_RE, record["name"])
    assert isinstance(record["granting_key_fingerprint"], str)
    assert record["source"] == "provisioned"
    dt.datetime.fromisoformat(record["paired_at"])

    lines = (identity / "authorized_keys").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert _key_fields(lines[0]) == _key_fields(host.pub)
    assert worker_protocol.fingerprint(lines[0]) == record["granting_key_fingerprint"]
    assert record["granting_key_fingerprint"] == host.fingerprint

    keys = sorted(p.name for p in (identity / "ssh").iterdir())
    assert keys == ["ssh_host_ed25519_key", "ssh_host_ed25519_key.pub"]


# --- the round trip -----------------------------------------------------------------


@pytest.mark.usefixtures("chowns")
@needs_ssh_keygen
def test_a_provisioned_identity_is_restored_and_installs_exactly_its_files(
    kw: Any, tmp_path: Path
) -> None:
    """The directory the host lays out, ``source: provisioned`` included, is accepted
    by the worker's restore, which installs exactly its key pair, the host's key for
    root, the record and the name, and starts sshd -- generating no key of its own."""
    host = _Host(tmp_path / "host")
    medium = _lay_out_identity(tmp_path / "medium", "bentoo-lab", host)
    medium_before = _tree(medium)
    root = tmp_path / "root"
    runner = _Runner()

    assert kw.restore(root=root, runner=runner, source=medium) == 0

    assert sorted(p.name for p in (root / "etc/ssh").iterdir()) == [
        "ssh_host_ed25519_key",
        "ssh_host_ed25519_key.pub",
    ]
    for name in ("ssh_host_ed25519_key", "ssh_host_ed25519_key.pub"):
        assert (root / "etc/ssh" / name).read_bytes() == (medium / "ssh" / name).read_bytes()
    authorized = (root / "root/.ssh/authorized_keys").read_text(encoding="utf-8").splitlines()
    assert [_key_fields(line) for line in authorized] == [_key_fields(host.pub)]
    written = json.loads((medium / "pairing.json").read_text(encoding="utf-8"))
    record = json.loads((root / "run/shidashi/pairing.json").read_text(encoding="utf-8"))
    assert record == written
    assert record["source"] == "provisioned"
    assert ["hostnamectl", "hostname", "bentoo-lab"] in runner.calls
    assert any(c[-1:] == ["sshd.service"] and "start" in c for c in runner.calls)
    assert not any(c and c[0] == "ssh-keygen" for c in runner.calls)
    assert _tree(medium) == medium_before


@pytest.mark.usefixtures("chowns")
@needs_ssh_keygen
@needs_xorriso
def test_the_identity_read_back_from_a_real_iso_medium_is_restored(kw: Any, tmp_path: Path) -> None:
    """Fidelity: through a real ISO 9660 medium, not only a directory. The identity is
    mapped to ``/shidashi/identity`` with xorriso (as provision does), read back as the
    worker sees its mounted medium, and restored; Rock Ridge keeps the provisioning
    user's modes (the public key is 0600 there), so the installed modes are the
    worker's own: 0600 private, 0644 public."""
    host = _Host(tmp_path / "host")
    identity = _lay_out_identity(tmp_path / "provision", "bentoo-lab", host)
    base_tree = tmp_path / "base"
    (base_tree / "LiveOS").mkdir(parents=True)
    (base_tree / "LiveOS" / "squashfs.img").write_bytes(b"squash")
    base_iso = tmp_path / "worker.iso"
    personalized = tmp_path / "bentoo-lab.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-quiet", "-V", "BENTOO_WORKER", "-o", str(base_iso),
         str(base_tree)],
        check=True,
        capture_output=True,
    )  # fmt: skip
    subprocess.run(
        ["xorriso", "-indev", str(base_iso), "-outdev", str(personalized),
         "-boot_image", "any", "replay", "-map", str(identity), str(MAP_TARGET)],
        check=True,
        capture_output=True,
    )  # fmt: skip
    live = tmp_path / "live"
    live.mkdir()
    medium = live / MAP_TARGET.relative_to("/")
    subprocess.run(
        ["xorriso", "-osirrox", "on", "-indev", str(personalized),
         "-extract", str(MAP_TARGET), str(medium)],
        check=True,
        capture_output=True,
    )  # fmt: skip
    root = tmp_path / "root"
    runner = _Runner()

    assert kw.restore(root=root, runner=runner, source=medium) == 0

    private = root / "etc/ssh/ssh_host_ed25519_key"
    public = root / "etc/ssh/ssh_host_ed25519_key.pub"
    assert private.read_bytes() == (identity / "ssh/ssh_host_ed25519_key").read_bytes()
    assert public.read_bytes() == (identity / "ssh/ssh_host_ed25519_key.pub").read_bytes()
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(public.stat().st_mode) == 0o644
    assert _installed_fingerprint(kw, root) == _keygen_fingerprint(
        identity / "ssh/ssh_host_ed25519_key.pub"
    )
    assert ["hostnamectl", "hostname", "bentoo-lab"] in runner.calls
