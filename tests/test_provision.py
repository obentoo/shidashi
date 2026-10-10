"""Tests of shidashi.provision's pure helpers (story 020, task 1.1).

``iso_volume_id`` reads the label an ISO declares in its Primary Volume Descriptor
(the label the worker's kernel command line looks for, ``CDLABEL=BENTOO_WORKER``);
``inside_git_work_tree`` tells whether a directory sits in a git checkout, where an
identity could be committed; ``identity_files`` is the pairing record and the
authorized key the host writes for a worker -- the same shape story 009's worker
persists to its work disk.

The module is imported inside each test: until it exists, every test fails on the
missing module, not the whole file on collection.

Requirements exercised: R1.1, R1.5, R4.3.
"""

import base64
import datetime as dt
import hashlib
import importlib
import json
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

needs_xorriso = pytest.mark.skipif(shutil.which("xorriso") is None, reason="needs xorriso")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _provision() -> Any:
    return importlib.import_module("shidashi.provision")


def _ed25519_line(seed: str, comment: str = "") -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


HOST_PUB = _ed25519_line("host-worker-key", "shidashi-worker-key")
HOST_FP = _fingerprint(HOST_PUB)
NOW = dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=dt.UTC)


def _pvd_image(path: Path, label: bytes, *, magic: bytes = b"CD001") -> Path:
    """An ISO 9660 image reduced to its system area, its Primary Volume Descriptor
    (type 1, ``CD001``, the label space-padded to 32 bytes at 0x8028) and the set
    terminator."""
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = magic
    pvd[6] = 1
    pvd[8:40] = b" " * 32
    pvd[40:72] = label.ljust(32, b" ")
    terminator = bytearray(2048)
    terminator[0] = 255
    terminator[1:6] = magic
    terminator[6] = 1
    path.write_bytes(bytes(16 * 2048) + bytes(pvd) + bytes(terminator))
    return path


# --- iso_volume_id (R1.5) -----------------------------------------------------------


@pytest.mark.parametrize("label", ["BENTOO_WORKERS", "BENTOO_WORKER_OLD", "XBENTOO_WORKER"])
def test_iso_volume_id_keeps_a_lookalike_label_whole(tmp_path: Path, label: str) -> None:
    """Hostile: a label that merely starts or ends like the worker's is another image's
    -- it must come back whole, never trimmed to BENTOO_WORKER."""
    iso = _pvd_image(tmp_path / "x.iso", label.encode())
    assert _provision().iso_volume_id(iso) == label


def test_iso_volume_id_strips_the_space_padding_of_the_descriptor(tmp_path: Path) -> None:
    """Hostile (the converse): the descriptor pads the label with spaces to 32 bytes;
    the padded label IS the worker's label."""
    iso = _pvd_image(tmp_path / "worker.iso", b"BENTOO_WORKER")
    assert _provision().iso_volume_id(iso) == "BENTOO_WORKER"


def test_iso_volume_id_of_a_file_without_the_iso9660_magic_is_a_provision_error(
    tmp_path: Path,
) -> None:
    """Hostile: a disk image that happens to hold the label's bytes at 0x8028 but no
    volume descriptor declares no label at all."""
    fake = _pvd_image(tmp_path / "disk.img", b"BENTOO_WORKER", magic=b"XXXXX")
    prov = _provision()
    with pytest.raises(prov.ProvisionError) as caught:
        prov.iso_volume_id(fake)
    assert "disk.img" in str(caught.value)


def test_iso_volume_id_of_a_file_shorter_than_the_descriptor_names_it(tmp_path: Path) -> None:
    short = tmp_path / "short.iso"
    short.write_bytes(bytes(16 * 2048 + 10))
    prov = _provision()
    with pytest.raises(prov.ProvisionError) as caught:
        prov.iso_volume_id(short)
    assert "short.iso" in str(caught.value)


def test_iso_volume_id_runs_no_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pure stdlib: it reads the descriptor itself, no isoinfo/blkid/xorriso."""

    def _no_tool(*_a: object, **_k: object) -> Any:
        raise AssertionError("iso_volume_id started a process")

    monkeypatch.setattr(subprocess, "run", _no_tool)
    monkeypatch.setattr(subprocess, "Popen", _no_tool)
    iso = _pvd_image(tmp_path / "kde.iso", b"BENTOO_KDE")
    assert _provision().iso_volume_id(iso) == "BENTOO_KDE"


@needs_xorriso
def test_iso_volume_id_reads_the_label_of_a_real_iso(tmp_path: Path) -> None:
    """Fidelity: the label xorriso writes, not only a hand-built descriptor."""
    tree = tmp_path / "tree"
    (tree / "LiveOS").mkdir(parents=True)
    (tree / "LiveOS" / "squashfs.img").write_bytes(b"squash")
    iso = tmp_path / "real.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-quiet", "-V", "BENTOO_WORKER", "-o", str(iso), str(tree)],
        check=True,
        capture_output=True,
    )
    assert _provision().iso_volume_id(iso) == "BENTOO_WORKER"


# --- inside_git_work_tree (R4.3) ----------------------------------------------------


@pytest.mark.parametrize("decoy", [".gitignore", "x.git", ".github", ".git-old"])
def test_a_directory_beside_lookalikes_of_git_is_not_in_a_work_tree(
    tmp_path: Path, decoy: str
) -> None:
    """Hostile: an ancestor holding something NAMED like git is not a checkout."""
    base = tmp_path / "home"
    (base / decoy).mkdir(parents=True)
    assert _provision().inside_git_work_tree(base / "data" / "shidashi" / "worker") is False


def test_a_directory_beside_another_checkout_is_not_in_a_work_tree(tmp_path: Path) -> None:
    """Hostile: a sibling's .git does not make its neighbour a checkout."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    assert _provision().inside_git_work_tree(tmp_path / "data") is False


def test_a_directory_not_created_yet_under_a_checkout_is_in_a_work_tree(tmp_path: Path) -> None:
    """Hostile (the converse): the workers directory may not exist yet on a first
    provision; under a checkout it is in the work tree all the same."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    target = tmp_path / "repo" / "xdg" / "shidashi" / "worker"
    assert not target.exists()
    assert _provision().inside_git_work_tree(target) is True


def test_a_git_file_marks_a_work_tree_like_a_git_directory(tmp_path: Path) -> None:
    """A linked worktree or a submodule has a ``.git`` FILE, not a directory."""
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    assert _provision().inside_git_work_tree(tmp_path / "wt" / "sub") is True


def test_the_root_of_a_checkout_is_in_its_work_tree(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert _provision().inside_git_work_tree(tmp_path) is True


@needs_git
def test_a_directory_in_a_real_git_init_checkout_is_in_a_work_tree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    assert _provision().inside_git_work_tree(tmp_path / "repo" / "deep" / "dir") is True


# --- identity_files (R1.1, the identity layout of design.md) ---------------------------


def test_identity_files_are_the_record_and_the_authorized_key_as_bytes() -> None:
    files = _provision().identity_files("bentoo-lab", HOST_PUB, HOST_FP, NOW)
    assert set(files) == {"pairing.json", "authorized_keys"}
    assert all(isinstance(data, bytes) for data in files.values())


def test_the_record_names_the_worker_the_granting_key_and_its_source() -> None:
    files = _provision().identity_files("bentoo-lab", HOST_PUB, HOST_FP, NOW)
    record = json.loads(files["pairing.json"])
    assert record["v"] == 1
    assert record["name"] == "bentoo-lab"
    assert record["granting_key_fingerprint"] == HOST_FP
    assert record["source"] == "provisioned"
    assert dt.datetime.fromisoformat(record["paired_at"]) == NOW


def test_paired_at_is_the_same_instant_written_in_utc() -> None:
    """Hostile: a clock in another zone is the same instant; the record says it in UTC."""
    local = dt.datetime(2026, 10, 10, 14, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    files = _provision().identity_files("bentoo-lab", HOST_PUB, HOST_FP, local)
    paired_at = dt.datetime.fromisoformat(json.loads(files["pairing.json"])["paired_at"])
    assert paired_at == local
    assert paired_at.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize("pub", [HOST_PUB, HOST_PUB + "\n", "  " + HOST_PUB + "  \n\n"])
def test_authorized_keys_is_exactly_one_line_the_granting_key(pub: str) -> None:
    files = _provision().identity_files("bentoo-lab", pub, HOST_FP, NOW)
    text = files["authorized_keys"].decode()
    assert text.endswith("\n")
    lines = text.splitlines()
    assert len(lines) == 1
    assert lines[0].split()[:2] == HOST_PUB.split()[:2]
    assert _fingerprint(lines[0]) == json.loads(files["pairing.json"])["granting_key_fingerprint"]


def test_identity_files_is_pure_the_same_inputs_give_the_same_bytes() -> None:
    prov = _provision()
    first = prov.identity_files("bentoo-lab", HOST_PUB, HOST_FP, NOW)
    second = prov.identity_files("bentoo-lab", HOST_PUB, HOST_FP, NOW)
    assert first == second


@pytest.mark.parametrize("name", ["Bentoo-Lab", "bentoo_lab", "-lab", "lab-", "a" * 64, ""])
def test_a_name_that_is_not_an_rfc1123_label_is_refused(name: str) -> None:
    """The worker refuses such a name at boot (its restore checks the label): the host
    must not write an identity the worker will reject."""
    prov = _provision()
    with pytest.raises(prov.ProvisionError):
        prov.identity_files(name, HOST_PUB, HOST_FP, NOW)
