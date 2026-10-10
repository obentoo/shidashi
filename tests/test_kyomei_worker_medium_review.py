"""Regression tests for the Tech Review of the medium restore (story 020, sub-task 2.1,
review round 1).

Each test pins one finding: a medium restore that fails partway must not start sshd
(the work-disk restore would then copy its own host keys over /etc/ssh while sshd
serves the medium's, and its ``start`` is a no-op on an active sshd); reloading
systemd-resolved must be bounded by the module's command timeout, inside a oneshot
unit that has no start timeout; and a medium whose public host key is not ed25519 is
refused before anything is written, as is one whose private host key is not an
openssh-key-v1 file declaring exactly the public key beside it.

The worker module, its temporary root and its recording runner come from
tests/test_kyomei_worker.py; the provisioned medium from tests/test_kyomei_worker_medium.py.
Requirements: R2.1, R2.3, R3.1.
"""

import base64
import json
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_kyomei_worker import (
    GRANTED_KEY,
    _authorized_keys,
    _ed25519_line,
    _exit_code,
    _host_keys,
    _ram_record,
    kw,
)
from tests.test_kyomei_worker_medium import (
    PRIVATE,
    PUBLIC,
    WORKER_DNSSD,
    _AnnounceRunner,
    _Chowns,
    _fresh_root,
    _medium,
    _openssh_private,
    _starts_sshd,
)

#: The ``kw`` fixture comes from tests/test_kyomei_worker.py; naming it marks it used.
_FIXTURES = (kw,)


def _rsa_blob() -> str:
    """A well-formed ``ssh-rsa`` public key blob (base64), so only its type is wrong."""
    blob = b"".join(
        struct.pack(">I", len(part)) + part for part in (b"ssh-rsa", b"\x01\x00\x01", bytes(257))
    )
    return base64.b64encode(blob).decode()


class _FailingRunner(_AnnounceRunner):
    """Fails the commands named in ``fail`` (non-zero exit) and makes those named in
    ``hang`` raise ``TimeoutExpired``, as ``subprocess.run`` does when its timeout runs
    out; records every call's keyword arguments."""

    def __init__(
        self, root: Path, *, fail: tuple[str, ...] = (), hang: tuple[str, ...] = ()
    ) -> None:
        super().__init__(root)
        self.failing, self.hanging = fail, hang
        self.kwargs: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(x) for x in argv]
        self.kwargs.append((argv, dict(kwargs)))
        joined = " ".join(argv)
        if any(h in joined for h in self.hanging):
            self.calls.append(argv)
            self.events.append(("run", tuple(argv)))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)
        if any(f in joined for f in self.failing):
            self.calls.append(argv)
            self.events.append(("run", tuple(argv)))
            return subprocess.CompletedProcess(argv, 1, "", "Failed to set hostname\n")
        return super().__call__(argv, *args, **kwargs)


# ===================================================================================
# finding 1: a medium restore that fails partway does not start sshd
# ===================================================================================


def test_a_failed_hostname_on_the_medium_path_does_not_start_sshd(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hostile: sshd started with the medium's key while no RAM record exists lets the
    work-disk restore copy ITS host keys over /etc/ssh -- the served key and the
    restored pairing then disagree."""
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    runner = _FailingRunner(root, fail=("hostnamectl",))
    _Chowns(monkeypatch, runner)

    code = _exit_code(kw, kw.restore, root=root, runner=runner, source=source, announce=True)

    assert code == 1
    assert not any(_starts_sshd(e) for e in runner.events)
    assert not _ram_record(root).exists()  # R2.3: the fallback units still run
    assert not (root / WORKER_DNSSD).exists()
    assert "kyomei:" in capsys.readouterr().err


def test_a_failed_authorized_keys_step_on_the_medium_path_starts_nothing(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An earlier step failed: no hostname, no sshd, no record -- the boot goes on as
    on a generic medium."""
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    keys = _authorized_keys(root)
    keys.unlink()
    keys.mkdir()  # reading it fails with an OSError
    runner = _FailingRunner(root)
    _Chowns(monkeypatch, runner)

    code = _exit_code(kw, kw.restore, root=root, runner=runner, source=source, announce=True)

    assert code == 1
    assert runner.calls == []
    assert not _ram_record(root).exists()
    assert "kyomei: restoring the authorized_keys failed" in capsys.readouterr().err


def test_the_work_disk_restore_still_starts_sshd_after_a_failed_hostname(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work-disk path keeps today's behaviour: sshd is always started."""
    root = _fresh_root(tmp_path, "boot-1")
    disk = _medium(root)  # the same files, at the work disk's directory
    target = root / "mnt" / "work" / ".shidashi"
    target.parent.mkdir(parents=True, exist_ok=True)
    disk.rename(target)
    runner = _FailingRunner(root, fail=("hostnamectl",))

    assert _exit_code(kw, kw.restore, root=root, runner=runner) == 1

    assert any(_starts_sshd(e) for e in runner.events)
    assert _ram_record(root).exists()


# ===================================================================================
# finding 2: reloading systemd-resolved is bounded
# ===================================================================================


def test_reloading_resolved_passes_the_command_timeout(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    runner = _FailingRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source, announce=True) == 0

    reloads = [kwargs for argv, kwargs in runner.kwargs if "reload" in argv]
    assert reloads
    assert all(kwargs.get("timeout") == kw._COMMAND_TIMEOUT for kwargs in reloads)


def test_a_reload_that_times_out_fails_the_announcement_without_undoing_sshd(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    runner = _FailingRunner(root, hang=("reload",))
    _Chowns(monkeypatch, runner)

    code = _exit_code(kw, kw.restore, root=root, runner=runner, source=source, announce=True)

    assert code == 1
    err = capsys.readouterr().err
    assert "kyomei: announcing failed:" in err
    assert "timed out" in err
    # the worker stays reachable at its address: sshd up, the pairing recorded
    assert any(_starts_sshd(e) for e in runner.events)
    assert not any("stop" in c for c in runner.calls)
    assert json.loads(_ram_record(root).read_text())["name"] == "bentoo-lab"


def test_a_reload_that_times_out_while_withdrawing_only_warns(
    tmp_path: Path, kw: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pairing window's own announcement: a hung resolved must not break the
    listener's cleanup -- a warning, as for a failed reload."""
    root = _fresh_root(tmp_path, "boot-1")
    runner = _FailingRunner(root, hang=("reload",))

    kw._withdraw(root, runner)

    assert "kyomei: reloading systemd-resolved failed" in capsys.readouterr().err


# ===================================================================================
# finding 4: the medium's host key must be ed25519
# ===================================================================================


def test_a_medium_whose_public_host_key_is_not_ed25519_is_refused(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Base64 that decodes is not enough: an RSA line in the ed25519 slot would be
    installed as the worker's ed25519 key."""
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    (source / "ssh" / PUBLIC).write_text(f"ssh-rsa {_rsa_blob()} root@bentoo-lab\n")
    host_keys = _host_keys(root)
    authorized = _authorized_keys(root).read_text()
    runner = _FailingRunner(root)
    chowns = _Chowns(monkeypatch, runner)

    code = _exit_code(kw, kw.restore, root=root, runner=runner, source=source, announce=True)

    assert code == 1
    assert _host_keys(root) == host_keys
    assert _authorized_keys(root).read_text() == authorized
    assert not _ram_record(root).exists()
    assert runner.calls == [] and chowns.calls == []
    assert "kyomei: cannot restore:" in capsys.readouterr().err


def test_a_public_host_key_line_whose_blob_is_not_ed25519_is_refused(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: the line says ssh-ed25519, the key inside is another type."""
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    (source / "ssh" / PUBLIC).write_text(f"ssh-ed25519 {_rsa_blob()} root@bentoo-lab\n")
    runner = _FailingRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 1
    assert not _ram_record(root).exists()
    assert GRANTED_KEY not in _authorized_keys(root).read_text()


# ===================================================================================
# finding 4: the medium's private and public host keys belong together
# ===================================================================================


def _ed25519_blob(seed: str) -> bytes:
    return base64.b64decode(_ed25519_line(seed).split()[1])


#: Private key files the medium path must refuse, each beside the fixture's .pub.
BAD_PRIVATE_KEYS: dict[str, str] = {
    # hostile: a coherent key, but another worker's -- sshd would serve a key whose
    # public half is not the one announced and pinned
    "another-workers-key": _openssh_private("provisioned-other-worker", "other-worker"),
    "not-openssh-key-v1": (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n"
    ),
    "placeholder-body": (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nprovisioned-bentoo-lab\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    ),
    "truncated-header": "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    + base64.b64encode(b"openssh-key-v1\0" + struct.pack(">I", 4) + b"no").decode()
    + "\n-----END OPENSSH PRIVATE KEY-----\n",
    "two-keys": _openssh_private(
        "provisioned-bentoo-lab",
        publics=[_ed25519_blob("provisioned-bentoo-lab"), _ed25519_blob("provisioned-x")],
    ),
    "no-key": _openssh_private("provisioned-bentoo-lab", publics=[]),
}


@pytest.mark.parametrize("case", sorted(BAD_PRIVATE_KEYS))
def test_a_medium_whose_host_key_halves_do_not_belong_together_is_refused(
    tmp_path: Path,
    kw: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root)
    (source / "ssh" / PRIVATE).write_text(BAD_PRIVATE_KEYS[case])
    host_keys = _host_keys(root)
    authorized = _authorized_keys(root).read_text()
    runner = _FailingRunner(root)
    chowns = _Chowns(monkeypatch, runner)

    code = _exit_code(kw, kw.restore, root=root, runner=runner, source=source, announce=True)

    assert code == 1
    assert _host_keys(root) == host_keys
    assert _authorized_keys(root).read_text() == authorized
    assert not _ram_record(root).exists()
    assert not (root / WORKER_DNSSD).exists()
    assert runner.calls == [] and chowns.calls == []  # no ssh-keygen either: stdlib only
    err = capsys.readouterr().err
    assert "kyomei: cannot restore:" in err
    assert PRIVATE in err


def test_the_medium_fixture_key_pair_belongs_together(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coherent pair is accepted: the check refuses mismatches, not the format."""
    root = _fresh_root(tmp_path, "boot-1")
    source = _medium(root, name="vmworker", seed="provisioned-vmworker")
    runner = _FailingRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0
    assert not any(c[0] == "ssh-keygen" for c in runner.calls)
