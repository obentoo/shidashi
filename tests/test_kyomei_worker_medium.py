"""Tests of the worker restoring a provisioned identity from its boot medium (story 020,
task 2.1): ``restore(root, runner, *, source=..., announce=...)`` and ``--restore-medium``.

The identity is the pairing record story 009 persists to the work disk, written by the
HOST onto the boot medium at ``/shidashi/identity/`` (``pairing.json``,
``authorized_keys``, ``ssh/ssh_host_ed25519_key{,.pub}``); dracut mounts the medium at
``/run/initramfs/live``. Here the medium is a directory under the temporary root, its
files carrying the modes ISO 9660 Rock Ridge keeps from the provisioning user (a
group- and world-readable private key), so nothing relies on the medium's modes.

Everything privileged is faked as in tests/test_kyomei_worker.py (its ``kw`` fixture,
its temporary root and its recording runner are reused, not copied). ``os.chown`` is
recorded, never run: the tests do not run as root.

Requirements exercised: R2.1, R2.2 (no pairing service announced), R2.3, R2.4, R2.6,
R3.1.
"""

import base64
import configparser
import hashlib
import json
import os
import stat
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_kyomei_worker import (
    GRANTED_KEY,
    VM_SESSION_KEY,
    _authorized_keys,
    _ed25519_line,
    _exit_code,
    _fingerprint,
    _host_keys,
    _key_fields,
    _mount,
    _no_console,
    _paired_on_disk,
    _ram_record,
    _reboot,
    _Runner,
    _worker_root,
    kw,
)

#: The ``kw`` fixture (the worker module, imported from the rootfs) comes from
#: tests/test_kyomei_worker.py; naming it here marks the import as used.
_FIXTURES = (kw,)

MEDIUM_DIR = Path("run/initramfs/live/shidashi/identity")
MEDIUM_PATH = Path("/run/initramfs/live/shidashi/identity")
WORKER_DNSSD = Path("run/systemd/dnssd/shidashi-worker.dnssd")
KYOMEI_DNSSD = Path("run/systemd/dnssd/shidashi-kyomei.dnssd")
ISSUE = Path("run/issue.d/50-shidashi-kyomei.issue")
PRIVATE = "ssh_host_ed25519_key"
PUBLIC = "ssh_host_ed25519_key.pub"


# --- helpers ----------------------------------------------------------------------------


def _openssh_private(seed: str, comment: str = "", *, publics: list[bytes] | None = None) -> str:
    """An unencrypted openssh-key-v1 private key whose public half is
    ``_ed25519_line(seed)`` -- the format ``ssh-keygen -t ed25519`` writes, built here so
    no tool runs. ``publics`` overrides the key blobs the header declares."""

    def string(data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + data

    blob = base64.b64decode(_ed25519_line(seed).split()[1])
    raw = blob[-32:]
    secret = hashlib.sha256(f"secret-{seed}".encode()).digest() + raw
    check = struct.pack(">I", 0x5EED5EED) * 2
    private = check + string(b"ssh-ed25519") + string(raw) + string(secret)
    private += string(comment.encode())
    private += bytes(range(1, 1 + (-len(private) % 8)))
    declared = [blob] if publics is None else publics
    body = b"openssh-key-v1\0" + string(b"none") + string(b"none") + string(b"")
    body += struct.pack(">I", len(declared)) + b"".join(string(p) for p in declared)
    body += string(private)
    text = base64.b64encode(body).decode()
    lines = [text[i : i + 70] for i in range(0, len(text), 70)]
    return (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + "\n".join(lines)
        + "\n-----END OPENSSH PRIVATE KEY-----\n"
    )


def _medium(
    root: Path,
    *,
    name: str = "bentoo-lab",
    seed: str = "provisioned-bentoo-lab",
    record: dict[str, Any] | None = None,
    authorized: str | None = None,
    keys: tuple[str, ...] = (PRIVATE, PUBLIC),
) -> Path:
    """A provisioned identity as the host writes it, at the medium's mount under ``root``.

    The modes are the provisioning user's, kept by Rock Ridge: private key 0644, public
    0664 -- sshd refuses the private key as is, so the worker must not copy the modes.
    """
    source = root / MEDIUM_DIR
    (source / "ssh").mkdir(parents=True)
    if record is None:
        record = {
            "v": 1,
            "name": name,
            "granting_key_fingerprint": _fingerprint(GRANTED_KEY),
            "paired_at": "2026-10-10T12:00:00+00:00",
            "source": "provisioned",
        }
    (source / "pairing.json").write_text(json.dumps(record) + "\n")
    (source / "authorized_keys").write_text((authorized or GRANTED_KEY) + "\n")
    if PRIVATE in keys:
        private = source / "ssh" / PRIVATE
        private.write_text(_openssh_private(seed, name))
        private.chmod(0o644)
    if PUBLIC in keys:
        public = source / "ssh" / PUBLIC
        public.write_text(_ed25519_line(seed, name) + "\n")
        public.chmod(0o664)
    return source


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under ``directory``: its bytes and its mode."""
    return {
        str(p.relative_to(directory)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _fresh_root(base: Path, seed: str) -> Path:
    """A booted live root (its own fresh host keys) with a credential key for root."""
    root = _worker_root(base, seed)
    keys = _authorized_keys(root)
    keys.parent.mkdir(mode=0o700)
    keys.write_text(VM_SESSION_KEY)  # a credential key, no final newline
    return root


class _Chowns:
    """Records os.chown/os.lchown/os.fchown (never runs them) into the runner's events,
    so a test can tell what was re-owned, to whom, and before which command."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, runner: _Runner) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self._runner = runner
        monkeypatch.setattr(os, "chown", self._by_path)
        monkeypatch.setattr(os, "lchown", self._by_path)
        monkeypatch.setattr(os, "fchown", self._by_fd)

    def _record(self, target: str, uid: int, gid: int) -> None:
        self.calls.append((target, uid, gid))
        self._runner.events.append(("chown", target, uid, gid))

    def _by_path(self, path: Any, uid: int, gid: int, *_a: Any, **_k: Any) -> None:
        self._record(os.fspath(path), uid, gid)

    def _by_fd(self, fd: int, uid: int, gid: int) -> None:
        self._record(os.readlink(f"/proc/self/fd/{fd}"), uid, gid)

    def owners_of(self, path: Path) -> list[tuple[int, int]]:
        """The (uid, gid) given to ``path``."""
        return [(uid, gid) for target, uid, gid in self.calls if _names(target, path)]


def _names(target: str, path: Path) -> bool:
    """Whether a chown ``target`` is ``path`` -- directly, or the temp sibling it was
    written through (``.<name>.<random>.tmp``) before the rename."""
    t = Path(target)
    if t.parent != path.parent:
        return False
    temp_of = t.name[1:].rsplit(".", 2)[0] if t.name.startswith(".") else None
    return t.name == path.name or temp_of == path.name


class _AnnounceRunner(_Runner):
    """Also records, at each resolved reload, whether the worker's .dnssd was there."""

    def __init__(self, root: Path, **kwargs: Any) -> None:
        super().__init__(root, **kwargs)
        self.worker_dnssd_at_reload: list[bool] = []

    def __call__(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(x) for x in argv]
        if argv[0] == "systemctl" and any("resolved" in x for x in argv):
            self.worker_dnssd_at_reload.append((self.root / WORKER_DNSSD).exists())
        return super().__call__(argv, *args, **kwargs)


def _index(events: list[tuple[Any, ...]], match: Any) -> int:
    for i, event in enumerate(events):
        if match(event):
            return i
    raise AssertionError(f"no such event in {events}")


def _starts_sshd(event: tuple[Any, ...]) -> bool:
    return (
        event[0] == "run"
        and event[1][0] == "systemctl"
        and any("sshd" in a for a in event[1])
        and ("start" in event[1] or "restart" in event[1])
    )


def _reloads_resolved(event: tuple[Any, ...]) -> bool:
    return event[0] == "run" and event[1][0] == "systemctl" and "reload" in event[1]


def _ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(path.read_text())
    return parser


# --- R2.1: the identity installed, nobody at the console ---------------------------------


def test_medium_restore_installs_the_identity_re_owned_and_re_moded_before_sshd(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fresh_root(tmp_path, "boot-1")
    boot_keys = _host_keys(root)
    source = _medium(root)
    _no_console(monkeypatch)
    runner = _Runner(root)
    chowns = _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    # hostile first: the boot's own fresh ed25519 key must not survive the restore
    ssh = root / "etc" / "ssh"
    assert (ssh / PRIVATE).read_bytes() != boot_keys[PRIVATE]
    assert (ssh / PRIVATE).read_bytes() == (source / "ssh" / PRIVATE).read_bytes()
    assert (ssh / PUBLIC).read_bytes() == (source / "ssh" / PUBLIC).read_bytes()
    assert not any(c[0] == "ssh-keygen" for c in runner.calls)

    # the medium's modes are not kept: sshd refuses a group- or world-readable key
    assert stat.S_IMODE((ssh / PRIVATE).stat().st_mode) == 0o600
    assert stat.S_IMODE((ssh / PUBLIC).stat().st_mode) == 0o644
    # nor its owner (Rock Ridge keeps the provisioning user's uid)
    assert (0, 0) in chowns.owners_of(ssh / PRIVATE)
    assert (0, 0) in chowns.owners_of(ssh / PUBLIC)
    assert all(owner == (0, 0) for owner in chowns.owners_of(ssh / PRIVATE))

    # the host's key for root, beside the credential key, once
    lines = _authorized_keys(root).read_text().splitlines()
    assert VM_SESSION_KEY in lines
    assert [_key_fields(ln) for ln in lines].count(_key_fields(GRANTED_KEY)) == 1

    # the RAM record the pairing window's unit skips on
    record = json.loads(_ram_record(root).read_text())
    assert record["name"] == "bentoo-lab"
    assert record["granting_key_fingerprint"] == _fingerprint(GRANTED_KEY)
    assert stat.S_IMODE(_ram_record(root).stat().st_mode) == 0o600

    # the name, then sshd -- with the identity's keys in place and re-owned before it starts
    assert runner.hostnames_set() == [["hostnamectl", "hostname", "bentoo-lab"]]
    assert runner.at_sshd_start is not None
    assert runner.at_sshd_start["host_keys"][PRIVATE] == (source / "ssh" / PRIVATE).read_bytes()
    sshd = _index(runner.events, _starts_sshd)
    key_chowns = [
        i
        for i, e in enumerate(runner.events)
        if e[0] == "chown" and (_names(e[1], ssh / PRIVATE) or _names(e[1], ssh / PUBLIC))
    ]
    assert key_chowns and max(key_chowns) < sshd


def test_medium_restore_never_modifies_the_medium(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: re-moding the medium's files in place instead of the installed copies
    would leave /etc/ssh as the medium had it (and the medium is read-only anyway)."""
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    before = _snapshot(source)
    runner = _Runner(root)
    chowns = _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    assert _snapshot(source) == before
    assert not [c for c in chowns.calls if Path(c[0]).is_relative_to(source)]


# --- R2.6: the same host key across reboots ------------------------------------------------


def test_medium_restore_presents_the_same_host_key_on_every_boot(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile (split): each boot makes its own fresh key; the medium's must win on both."""
    fingerprints = []
    for seed in ("boot-1", "boot-2"):
        root = _fresh_root(tmp_path, seed)
        source = _medium(root)  # the same identity: the same stick, rebooted
        runner = _Runner(root)
        _Chowns(monkeypatch, runner)
        assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0
        fingerprints.append(_fingerprint((root / "etc" / "ssh" / PUBLIC).read_text()))
    assert fingerprints[0] == fingerprints[1]
    assert fingerprints[0] == _fingerprint(_ed25519_line("provisioned-bentoo-lab"))


# --- R2.4 / R2.5: which source wins is the caller's choice -------------------------------


def test_medium_restore_wins_over_the_work_disk_and_leaves_its_pairing_unchanged(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sources present. Hostile (collapse) first: the default call -- today's
    --restore -- must keep taking the work disk and announce nothing, even with a medium
    identity mounted; only the medium call takes the medium, and it leaves the disk's
    pairing files byte-for-byte as they were."""
    first = _paired_on_disk(tmp_path, kw, monkeypatch)  # the disk pairs "bentoo-lab"
    disk = first / "mnt" / "work" / ".shidashi"
    disk_pub = (disk / "ssh" / PUBLIC).read_bytes()

    # today's --restore: the disk, never the medium
    today = _reboot(tmp_path, first)
    _medium(today, name="vmworker", seed="provisioned-vmworker")
    _mount(monkeypatch, first, today)
    runner = _Runner(today, mounted=True)
    _Chowns(monkeypatch, runner)
    assert _exit_code(kw, kw.restore, root=today, runner=runner) == 0
    assert (today / "etc" / "ssh" / PUBLIC).read_bytes() == disk_pub
    assert runner.hostnames_set() == [["hostnamectl", "hostname", "bentoo-lab"]]
    assert not (today / WORKER_DNSSD).exists()

    # --restore-medium: the medium, and the disk untouched
    second = _worker_root(tmp_path, "boot-3")
    (second / "mnt" / "work").rmdir()
    (second / "mnt" / "work").symlink_to(first / "mnt" / "work")
    source = _medium(second, name="vmworker", seed="provisioned-vmworker")
    _mount(monkeypatch, first, today, second)
    before = _snapshot(disk)
    runner = _Runner(second, mounted=True)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=second, runner=runner, source=source) == 0

    assert (second / "etc" / "ssh" / PUBLIC).read_bytes() == (source / "ssh" / PUBLIC).read_bytes()
    assert (second / "etc" / "ssh" / PUBLIC).read_bytes() != disk_pub
    assert runner.hostnames_set() == [["hostnamectl", "hostname", "vmworker"]]
    assert json.loads(_ram_record(second).read_text())["name"] == "vmworker"
    assert _snapshot(disk) == before


# --- R3.1 / R2.2: the name announced, the pairing window not ------------------------------


def test_medium_restore_announces_the_identitys_name_and_host_key_over_mdns(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    runner = _AnnounceRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source, announce=True) == 0

    dnssd = root / WORKER_DNSSD
    service = _ini(dnssd)["Service"]
    # hostile first: %H expands to whatever the hostname is when resolved reads the file
    # (still shidashi-worker at boot) -- the name must be the identity's, written out
    assert service["Name"] != "%H"
    assert service["Name"] == "bentoo-lab"
    assert service["Type"] == "_shidashi-worker._tcp"
    assert service["Port"] == "22"
    # hostile: the record holds two SHA256 fingerprints; the announced one is the
    # worker's host key, never the host's granting key
    host_key_fp = _fingerprint((source / "ssh" / PUBLIC).read_text())
    txt = service["TxtText"].split()
    assert f"fp={_fingerprint(GRANTED_KEY)}" not in txt
    assert "v=1" in txt
    assert f"fp={host_key_fp}" in txt

    # resolved reloaded with the file in place, after sshd was started
    assert True in runner.worker_dnssd_at_reload
    reload_at = max(i for i, e in enumerate(runner.events) if _reloads_resolved(e))
    assert reload_at > _index(runner.events, _starts_sshd)

    # R2.2: the pairing window stays closed -- no pairing service, no code on the console
    assert not (root / KYOMEI_DNSSD).exists()
    assert not (root / ISSUE).exists()
    for other in (root / WORKER_DNSSD).parent.iterdir():
        assert "_shidashi-kyomei._tcp" not in other.read_text(), other


def test_medium_restore_without_announce_writes_no_announcement(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    runner = _Runner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    assert not (root / WORKER_DNSSD).exists()
    assert not any(_reloads_resolved(e) for e in runner.events)


# --- R2.3: a bad identity is ignored, the pairing window opens as today -------------------


def _bad_record(over: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "v": 1,
        "name": "bentoo-lab",
        "granting_key_fingerprint": _fingerprint(GRANTED_KEY),
        "paired_at": "2026-10-10T12:00:00+00:00",
    }
    record.update(over)
    return {k: v for k, v in record.items() if v is not None}


BAD_IDENTITIES: dict[str, dict[str, Any]] = {
    "record-not-json": {"raw_record": "{not json"},
    "record-unreadable": {"record_dir": True},
    "record-unknown-version": {"record": _bad_record({"v": 2})},
    "record-without-fingerprint": {"record": _bad_record({"granting_key_fingerprint": None})},
    "record-bad-name": {"record": _bad_record({"name": "Bad_Name!"})},
    "authorized-keys-missing": {"drop": "authorized_keys"},
    "authorized-keys-another-key": {"authorized": _ed25519_line("intruder")},
    "private-key-missing": {"keys": (PUBLIC,)},
    "public-key-missing": {"keys": (PRIVATE,)},
    "no-host-keys": {"keys": ()},
}


@pytest.mark.parametrize("case", sorted(BAD_IDENTITIES))
def test_medium_restore_refuses_a_bad_identity_and_writes_nothing(
    tmp_path: Path,
    kw: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """Incomplete counts as bad: a record without host keys would skip the pairing window
    and leave the boot's own key, which the host's pin refuses -- a worker nobody reaches."""
    spec = BAD_IDENTITIES[case]
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(
        root,
        record=spec.get("record"),
        authorized=spec.get("authorized"),
        keys=spec.get("keys", (PRIVATE, PUBLIC)),
    )
    if "raw_record" in spec:
        (source / "pairing.json").write_text(spec["raw_record"])
    if spec.get("record_dir"):
        (source / "pairing.json").unlink()
        (source / "pairing.json").mkdir()
    if "drop" in spec:
        (source / spec["drop"]).unlink()
    host_keys = _host_keys(root)
    authorized = _authorized_keys(root).read_text()
    runner = _AnnounceRunner(root)
    chowns = _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source, announce=True) == 1

    assert not _ram_record(root).exists()  # so the pairing window opens as today
    assert _host_keys(root) == host_keys
    assert _authorized_keys(root).read_text() == authorized
    assert not (root / WORKER_DNSSD).exists()
    # no hostname, no sshd, no resolved reload (reading a fingerprint is harmless)
    assert [c for c in runner.calls if c[0] != "ssh-keygen"] == []
    assert chowns.calls == []
    out, err = capsys.readouterr()
    assert "kyomei:" in err
    assert str(source) in err or "identity" in err.lower() or "pairing" in err.lower()


# --- main: --restore-medium ------------------------------------------------------------------


def test_main_dispatches_restore_medium_from_the_live_medium_and_keeps_restore_as_today(
    kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    answer = {"code": 0}

    def _restore(*args: Any, **kwargs: Any) -> int:
        calls.append(kwargs)
        return answer["code"]

    monkeypatch.setattr(kw, "restore", _restore)

    # hostile first: today's --restore must not pick up the medium or announce
    assert _exit_code(kw, kw.main, ["--restore"]) == 0
    assert calls[-1].get("source") is None
    assert not calls[-1].get("announce")

    assert _exit_code(kw, kw.main, ["--restore-medium"]) == 0
    assert len(calls) == 2
    assert Path("/") / Path(calls[-1]["source"]) == MEDIUM_PATH
    assert calls[-1]["announce"] is True

    # the unit fails visibly when the medium restore fails
    answer["code"] = 1
    assert _exit_code(kw, kw.main, ["--restore-medium"]) == 1
