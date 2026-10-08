"""Tests of the worker's side of a pairing (variants/worker/rootfs/usr/local/lib/shidashi/
kyomei_worker.py), imported from the rootfs as the image runs it (v3: the worker serves).

Everything privileged is faked: files live under a temporary root (``root=``), every
command goes through a recording runner (``runner=``), the console is a recording
``show``/``clear``, and the listener's server comes from ``server_factory``: a real
``http.server.HTTPServer`` bound to 127.0.0.1 on a free port instead of 0.0.0.0:8765.
The host is played by this file with the HOST's copy of kyomei_protocol.
``/mnt/work`` counts as mounted only where a test says so.

Sections, each selected by its ``-k`` keyword (no other test name contains it):

* ``session``              -- WorkerSession.handle, pure (task 4.1);
* ``persist or restore``   -- the pairing on the work disk (task 4.2, unchanged from v2
                              except that the pairing is set up directly: the v2 ``pair``
                              client is gone, ``listen`` calls ``persist`` now);
* ``listen or console or main`` -- the announcing listener, the console block and the
                              script's dispatch (task 4.4).

Requirements exercised: R2.1, R2.2, R2.3, R2.5, R2.6, R2.9, R2.10, R3.1-R3.7, R5.3-R5.7,
R7.1-R7.4.
"""

import base64
import builtins
import configparser
import datetime as dt
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest

from shidashi import kyomei_protocol as P


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


ROOT = _repo_root()
WORKER_LIB = ROOT / "variants/worker/rootfs/usr/local/lib/shidashi"

CODE = "K7M4Q2XP"
CODE2 = "M4K7XPQ2"
CODE3 = "ZZ00ZZ00"
OTHER_CODE = "K7M4Q2XR"
HOST = "192.168.15.5"
SHOWN_CODE = re.compile(r"\b([0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4})\b")


def _ed25519_line(seed: str, comment: str = "") -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


GRANTED_KEY = _ed25519_line("host-worker-key", "shidashi-worker-key")
VM_SESSION_KEY = _ed25519_line("vm-session-key", "vm-session")
WORKER_HOST_KEY = _ed25519_line("worker-sshd-host-key", "root@shidashi-worker")
RSA_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDexample admin@laptop"


def _secrets(*codes: str) -> list[str]:
    out: list[str] = []
    for code in codes or (CODE,):
        fmt = P.format_code(code)
        out += [code, fmt, code.lower(), fmt.lower(), P.derive_key(code).hex()]
    return out


SECRETS = _secrets(CODE)


def _key_fields(line: str) -> list[str]:
    return line.split()[:2]


@pytest.fixture
def kw(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(WORKER_LIB))
    for name in ("kyomei_worker", "kyomei_protocol", "worker_disk"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("kyomei_worker")


def _trust(address: str = HOST, fingerprint: str | None = None) -> Any:
    """A Trust built by the worker's own copy of the protocol."""
    worker_protocol = sys.modules["kyomei_protocol"]
    return worker_protocol.Trust(
        address=address, fingerprint=fingerprint or _fingerprint(GRANTED_KEY)
    )


def _hello(
    code: str = CODE,
    *,
    mode: str = "code",
    nonce: str | None = None,
    key: str = GRANTED_KEY,
    name: str | None = "bentoo-lab",
    kind: str = "hello",
    **over: Any,
) -> bytes:
    """A hello as the host sends it (host's protocol copy)."""
    payload: dict[str, Any] = {
        "v": 1,
        "mode": mode,
        "nonce": nonce or P.new_nonce(),
        "authorized_key": key,
        "name": name,
    }
    payload.update(over)
    tag = None if mode == "trusted" else P.mac(P.derive_key(code), kind, payload)
    return json.dumps({"payload": payload, "mac": tag}).encode()


def _headers(headers: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}


# ===================================================================================
# session -- task 4.1: WorkerSession.handle
# ===================================================================================


class _Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


def _session(kw: Any, *, trust: Any = None, clock: _Clock | None = None, **kwargs: Any) -> Any:
    codes = iter((CODE, CODE2, CODE3))
    return kw.WorkerSession(
        host_key=WORKER_HOST_KEY,
        hostname="shidashi-worker",
        addresses=("192.168.15.7", "10.0.0.4"),
        cpu_flags=("avx2", "bmi2", "sse4_2"),
        image="20261006T1200",
        trust=trust,
        code_factory=lambda: next(codes),
        clock=clock or _Clock(),
        **kwargs,
    )


def test_session_answers_a_good_code_hello_with_a_welcome_bound_to_the_hosts_nonce(
    kw: Any,
) -> None:
    session = _session(kw)
    nonce = P.new_nonce()
    status, body, _headers_ = session.handle(_hello(nonce=nonce), HOST)
    assert status == 200
    welcome, tag = P.parse_welcome(body)
    payload = json.loads(body)["payload"]
    assert tag is not None and P.verify(P.derive_key(CODE), "welcome", payload, tag)
    assert welcome.nonce == nonce
    assert welcome.worker_nonce != nonce
    assert len(base64.urlsafe_b64decode(welcome.worker_nonce + "==")) == 16
    assert _key_fields(welcome.host_key) == _key_fields(WORKER_HOST_KEY)
    assert welcome.hostname == "bentoo-lab"  # the name the host gave
    assert tuple(welcome.addresses) == ("192.168.15.7", "10.0.0.4")
    assert tuple(welcome.cpu_flags) == ("avx2", "bmi2", "sse4_2")
    assert welcome.image == "20261006T1200"


def test_session_an_unnamed_hello_gets_the_workers_own_hostname(kw: Any) -> None:
    status, body, _h = _session(kw).handle(_hello(name=None), HOST)
    assert status == 200
    assert P.parse_welcome(body)[0].hostname == "shidashi-worker"


def test_session_records_the_grant_and_closes_after_one_pairing(kw: Any) -> None:
    session = _session(kw)
    assert not session.closed and session.result is None
    status, _body, _h = session.handle(_hello(), "192.168.15.5")
    assert status == 200
    assert session.closed
    granted = session.result
    assert granted is not None
    assert _key_fields(granted.key) == _key_fields(GRANTED_KEY)
    assert granted.name == "bentoo-lab"
    assert granted.trusted is False
    assert granted.peer == "192.168.15.5"


def test_session_draws_a_fresh_worker_nonce_per_session(kw: Any) -> None:
    nonces = set()
    for _ in range(5):
        _s, body, _h = _session(kw).handle(_hello(), HOST)
        nonces.add(P.parse_welcome(body)[0].worker_nonce)
    assert len(nonces) == 5


# Trusted mode (R7.1-R7.3) -- hostile halves first: an address or a fingerprint that
# only LOOKS like the trusted one is another host; then the trusted key under another
# comment IS the trusted key; then the plain case.


@pytest.mark.parametrize("peer", ["192.168.15.50", "192.168.15.4", "92.168.15.5", "10.0.0.5"])
def test_session_refuses_a_trusted_hello_from_an_address_that_only_looks_trusted(
    kw: Any, peer: str
) -> None:
    session = _session(kw, trust=_trust("192.168.15.5"))
    status, body, _h = session.handle(_hello(mode="trusted"), peer)
    assert status == 403
    assert body == b""
    assert session.failures == 1
    assert session.result is None


@pytest.mark.parametrize("case", ["another key, same comment", "fingerprint in another case"])
def test_session_refuses_a_trusted_hello_whose_key_is_not_the_trusted_one(
    kw: Any, case: str
) -> None:
    if case == "another key, same comment":
        trust = _trust()
        key = _ed25519_line("an-impostor", "shidashi-worker-key")
    else:  # base64 is case-sensitive: the swapped-case fingerprint names another key
        trust = _trust(fingerprint=_fingerprint(GRANTED_KEY).swapcase().replace("sha256", "SHA256"))
        key = GRANTED_KEY
    session = _session(kw, trust=trust)
    status, body, _h = session.handle(_hello(mode="trusted", key=key), HOST)
    assert status == 403
    assert body == b""
    assert session.failures == 1


@pytest.mark.parametrize("comment", ["", "another-comment", "host@laptop"])
def test_session_accepts_the_trusted_key_under_any_comment(kw: Any, comment: str) -> None:
    key = " ".join([*_key_fields(GRANTED_KEY), comment]).strip()
    session = _session(kw, trust=_trust())
    status, _body, _h = session.handle(_hello(mode="trusted", key=key), HOST)
    assert status == 200


def test_session_refuses_every_trusted_hello_without_a_trust_parameter(kw: Any) -> None:
    session = _session(kw, trust=None)
    status, body, _h = session.handle(_hello(mode="trusted"), HOST)
    assert status == 403
    assert body == b""
    assert session.failures == 1


def test_session_pairs_a_trusted_hello_from_the_trusted_host_without_a_mac(kw: Any) -> None:
    session = _session(kw, trust=_trust())
    nonce = P.new_nonce()
    status, body, _h = session.handle(_hello(mode="trusted", nonce=nonce, name=None), HOST)
    assert status == 200
    welcome, tag = P.parse_welcome(body)
    assert tag is None
    assert welcome.nonce == nonce
    assert session.result.trusted is True
    assert session.closed


# Refusals (R2.1, R2.6) -- nothing about the worker leaks, and each one counts.


@pytest.mark.parametrize(
    "case", ["another code", "a welcome-kind mac", "a payload tampered after the mac", "garbage"]
)
def test_session_answers_403_with_an_empty_body_to_a_hello_that_does_not_verify(
    kw: Any, case: str
) -> None:
    session = _session(kw)
    if case == "another code":
        body = _hello(OTHER_CODE)
    elif case == "a welcome-kind mac":
        body = _hello(kind="welcome")
    elif case == "a payload tampered after the mac":
        doc = json.loads(_hello())
        doc["payload"]["authorized_key"] = _ed25519_line("an-impostor")
        body = json.dumps(doc).encode()
    else:
        doc = json.loads(_hello())
        doc["mac"] = "00" * 32
        body = json.dumps(doc).encode()
    status, answer, headers = session.handle(body, HOST)
    assert status == 403
    assert answer == b""
    leaks = " ".join(_headers(headers).values()).encode() + answer
    for leak in (*_key_fields(WORKER_HOST_KEY), "shidashi-worker", "avx2", *SECRETS):
        assert leak.encode() not in leaks
    assert session.failures == 1
    assert not session.closed
    assert session.result is None


def test_session_accepts_a_hello_of_exactly_sixteen_kib(kw: Any) -> None:
    body = _hello()
    padded = body + b" " * (16384 - len(body))
    assert _session(kw).handle(padded, HOST)[0] == 200


@pytest.mark.parametrize(
    "case",
    ["one byte over 16 KiB", "not json", "an extra field", "a missing field", "v 2", "null mac"],
)
def test_session_answers_400_and_counts_a_failure_for_a_malformed_hello(kw: Any, case: str) -> None:
    session = _session(kw)
    if case == "one byte over 16 KiB":
        body = _hello()
        body += b" " * (16385 - len(body))
    elif case == "not json":
        body = b"hello=1"
    elif case == "an extra field":
        body = _hello(admin=True)
    elif case == "a missing field":
        doc = json.loads(_hello())
        del doc["payload"]["nonce"]
        body = json.dumps(doc).encode()
    elif case == "v 2":
        body = _hello(v=2)
    else:  # a code-mode hello without its MAC
        doc = json.loads(_hello())
        doc["mac"] = None
        body = json.dumps(doc).encode()
    status, answer, _h = session.handle(body, HOST)
    assert status == 400
    assert _key_fields(WORKER_HOST_KEY)[1].encode() not in answer
    assert session.failures == 1
    assert session.result is None


# Replay (R2.3) -- hostile halves first: a nonce that only LOOKS like a seen one is not
# a replay; a seen nonce under a fresh, valid MAC IS one.


def test_session_a_nonce_differing_from_a_seen_one_only_in_case_is_not_a_replay(
    kw: Any,
) -> None:
    session = _session(kw)
    session.seen_nonces.add("q83vEjRWeJASNFZ4kBI0Vg")
    status, _b, _h = session.handle(_hello(nonce="Q83vEjRWeJASNFZ4kBI0Vg"), HOST)
    assert status == 200


def test_session_a_seen_nonce_under_a_fresh_valid_mac_is_a_replay(kw: Any) -> None:
    session = _session(kw)
    seen = "q83vEjRWeJASNFZ4kBI0Vg"
    session.seen_nonces.add(seen)
    status, answer, _h = session.handle(_hello(nonce=seen, name="other-machine"), HOST)
    assert status == 403
    assert answer == b""
    assert session.failures == 1
    assert session.result is None


def test_session_a_second_hello_after_a_pairing_is_refused(kw: Any) -> None:
    session = _session(kw)
    assert session.handle(_hello(), HOST)[0] == 200
    first = session.result
    status, answer, _h = session.handle(_hello(name="second-host"), "192.168.15.99")
    assert status == 403
    assert _key_fields(WORKER_HOST_KEY)[1].encode() not in answer
    assert session.result == first


# The failure bound and the lockout (R2.9)


def test_session_three_failures_lock_for_thirty_seconds_then_rotate_to_a_new_code(
    kw: Any,
) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock)
    assert session.code == CODE
    assert session.handle(_hello(OTHER_CODE), HOST)[0] == 403
    assert session.handle(b"{", HOST)[0] == 400
    assert session.handle(_hello(kind="welcome"), HOST)[0] == 403
    assert session.failures == 3
    # locked: even the right code is answered 423, with how long to wait
    status, answer, headers = session.handle(_hello(), HOST)
    assert status == 423
    assert answer == b""
    assert int(_headers(headers)["retry-after"]) == 30
    assert session.result is None
    assert session.code != CODE  # the code is discarded
    clock.now += 10
    status, _a, headers = session.handle(_hello(), HOST)
    assert status == 423
    assert 20 <= int(_headers(headers)["retry-after"]) <= 21
    clock.now += 20
    session.rotate()
    assert session.code == CODE2
    assert session.failures == 0
    assert session.handle(_hello(CODE), HOST)[0] == 403  # the old code is gone for good
    assert session.handle(_hello(CODE2), HOST)[0] == 200


def test_session_two_failures_leave_the_code_usable(kw: Any) -> None:
    session = _session(kw)
    session.handle(_hello(OTHER_CODE), HOST)
    session.handle(_hello(OTHER_CODE), HOST)
    assert session.handle(_hello(), HOST)[0] == 200


def test_session_honours_a_custom_failure_limit_and_lockout(kw: Any) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock, max_failures=1, lockout=5.0)
    session.handle(_hello(OTHER_CODE), HOST)
    status, _a, headers = session.handle(_hello(), HOST)
    assert status == 423
    assert int(_headers(headers)["retry-after"]) == 5


# Secrets (R2.5)


def test_session_never_puts_the_code_in_an_answer_a_header_a_log_or_its_repr(
    kw: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = _Clock()
    session = _session(kw, clock=clock)
    texts: list[bytes] = []
    for body in (_hello(OTHER_CODE), b"not json", _hello(kind="welcome"), _hello()):
        _s, answer, headers = session.handle(body, HOST)
        texts += [answer, json.dumps(_headers(headers)).encode()]
    clock.now += 31
    session.rotate()
    _s, answer, headers = session.handle(_hello(CODE2), HOST)
    texts += [answer, json.dumps(_headers(headers)).encode()]
    texts += [r.getMessage().encode() for r in caplog.records]
    texts += [repr(session).encode(), str(session).encode()]
    for text in texts:
        for secret in _secrets(CODE, CODE2):
            assert secret.encode() not in text, f"{secret!r} leaked"


# ===================================================================================
# persist / restore -- task 4.2 (contract unchanged from v2)
# ===================================================================================


def _worker_root(base: Path, seed: str = "boot-1") -> Path:
    """A live root as the worker boots it: fresh host keys, os-release, cpuinfo,
    the kernel command line, an empty /mnt/work directory (a directory is not a mount)."""
    root = base / f"root-{seed}"
    ssh = root / "etc" / "ssh"
    ssh.mkdir(parents=True)
    pubs = {
        "ed25519": _ed25519_line(f"{seed}-ed25519", "root@shidashi-worker"),
        "ecdsa": f"ecdsa-sha2-nistp256 AAAAE2VjZHNh{seed} root@shidashi-worker",
        "rsa": f"ssh-rsa AAAAB3NzaC1yc2E{seed} root@shidashi-worker",
    }
    for kind, pub in pubs.items():
        private = ssh / f"ssh_host_{kind}_key"
        private.write_text(f"-----BEGIN OPENSSH PRIVATE KEY-----\n{seed}-{kind}\n-----END\n")
        private.chmod(0o600)
        public = ssh / f"ssh_host_{kind}_key.pub"
        public.write_text(pub + "\n")
        public.chmod(0o644)
    (root / "etc" / "os-release").write_text('NAME="Bentoo"\nID=bentoo\nBUILD_ID="20261005T1200"\n')
    (root / "etc" / "hostname").write_text("shidashi-worker\n")
    (root / "proc" / "sys" / "kernel").mkdir(parents=True)
    (root / "proc" / "sys" / "kernel" / "hostname").write_text("shidashi-worker\n")
    (root / "proc" / "cpuinfo").write_text(
        "processor\t: 0\nflags\t\t: fpu sse4_2 avx2 bmi2\n\n"
        "processor\t: 1\nflags\t\t: fpu sse4_2 avx2 bmi2\n"
    )
    (root / "proc" / "cmdline").write_text(
        "BOOT_IMAGE=/vmlinuz root=live:CDLABEL=BENTOO rd.live.image quiet splash\n"
    )
    (root / "dev").mkdir()
    (root / "dev" / "tty1").write_text("")
    (root / "run").mkdir()
    (root / "mnt" / "work").mkdir(parents=True)
    (root / "root").mkdir()
    return root


def _authorized_keys(root: Path) -> Path:
    return root / "root" / ".ssh" / "authorized_keys"


def _ram_record(root: Path) -> Path:
    return root / "run" / "shidashi" / "pairing.json"


def _dnssd(root: Path) -> Path:
    return root / "run" / "systemd" / "dnssd" / "shidashi-kyomei.dnssd"


def _host_keys(root: Path) -> dict[str, bytes]:
    ssh = root / "etc" / "ssh"
    return {p.name: p.read_bytes() for p in sorted(ssh.glob("ssh_host_*_key*"))}


class _Runner:
    """Records every command; answers the few whose output matters; runs nothing."""

    def __init__(self, root: Path, *, mounted: bool = False, fail: dict[str, int] | None = None):
        self.root, self.mounted = root, mounted
        self.fail = dict(fail or {})  # "sshd -t" -> how many times it fails
        self.calls: list[list[str]] = []
        self.events: list[tuple[Any, ...]] = []
        self.at_sshd_start: dict[str, Any] | None = None
        self.resolved_reloads: list[bool] = []  # was the .dnssd file there at each reload

    def __call__(self, argv: Any, *_a: Any, **kw: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.events.append(("run", tuple(argv)))
        out, err, rc = "", "", 0
        if argv[0] == "ip":
            out = (
                "lo               UNKNOWN        127.0.0.1/8 \n"
                "enp3s0           UP             192.168.15.7/24 \n"
                "wlp2s0           UP             10.0.0.4/8 \n"
            )
        elif argv[0] == "ssh-keygen" and "-A" in argv:
            ssh = self.root / "etc" / "ssh"
            ssh.mkdir(parents=True, exist_ok=True)
            for kind in ("ed25519", "ecdsa", "rsa"):
                private = ssh / f"ssh_host_{kind}_key"
                if not private.exists():
                    private.write_text(f"-----BEGIN OPENSSH PRIVATE KEY-----\nnew-{kind}\n")
                    private.chmod(0o600)
                    line = _ed25519_line(f"generated-{kind}", "root@shidashi-worker")
                    (ssh / f"ssh_host_{kind}_key.pub").write_text(line + "\n")
        elif argv[0] == "ssh-keygen" and any(a.startswith("-l") for a in argv):
            src = kw.get("input")
            if src is None:
                src = Path(argv[-1]).read_text()
            src = src.decode() if isinstance(src, bytes) else src
            out = f"256 {_fingerprint(src)} k (ED25519)\n"
        elif argv[0] in ("mountpoint", "findmnt"):
            hit = self.mounted and any(a.rstrip("/").endswith("mnt/work") for a in argv)
            rc = 0 if hit else 1
            out = "/mnt/work\n" if hit else ""
        elif argv[0].endswith("sshd") and "-t" in argv and self.fail.get("sshd -t", 0) > 0:
            self.fail["sshd -t"] -= 1
            rc, err = 255, "/etc/ssh/sshd_config line 3: Bad configuration option\n"
        elif argv[0] == "systemctl" and any("resolved" in a for a in argv):
            self.resolved_reloads.append(_dnssd(self.root).exists())
        elif (
            argv[0] == "systemctl"
            and any("sshd" in a for a in argv)
            and ("start" in argv or "restart" in argv)
        ):
            keys = _authorized_keys(self.root)
            self.at_sshd_start = {
                "authorized_keys": keys.read_text() if keys.exists() else "",
                "host_keys": _host_keys(self.root),
            }
        if kw.get("check") and rc:
            raise subprocess.CalledProcessError(rc, argv, out, err)
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        if text:
            return subprocess.CompletedProcess(argv, rc, out, err)
        return subprocess.CompletedProcess(argv, rc, out.encode(), err.encode())

    def started_sshd(self) -> bool:
        return self.at_sshd_start is not None

    def hostnames_set(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "hostnamectl" and len(c) > 2]


def _mount(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> None:
    """Make each root's mnt/work a mount point for os.path.ismount/Path.is_mount."""
    real = os.path.ismount
    targets = {str(r / "mnt" / "work") for r in roots}
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p).rstrip("/") in targets or real(p))


def _exit_code(kw: Any, fn: Any, *args: Any, **kwargs: Any) -> int:
    """The process exit status the worker command would end with."""
    protocol = sys.modules.get("kyomei_protocol")
    protocol_error = getattr(protocol, "ProtocolError", ())
    try:
        value = fn(*args, **kwargs)
    except SystemExit as exit_:
        if exit_.code is None:
            return 0
        if isinstance(exit_.code, int):
            return exit_.code
        print(exit_.code, file=sys.stderr)
        return 1
    except protocol_error as err:
        print(err, file=sys.stderr)
        return 1
    if isinstance(value, bool):
        return 0 if value else 1
    return value if isinstance(value, int) else 0


def _no_console(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_a: Any) -> str:
        raise AssertionError("asked the console for input")

    monkeypatch.setattr(builtins, "input", _refuse)


def _granted(root: Path, *, name: str = "bentoo-lab", before: tuple[str, ...] = ()) -> None:
    """The state a verified hello leaves in RAM: root's authorized_keys holding the
    granted key (after whatever was there) and the RAM record naming it."""
    keys = _authorized_keys(root)
    keys.parent.mkdir(mode=0o700, exist_ok=True)
    keys.write_text("".join(line + "\n" for line in (*before, GRANTED_KEY)))
    keys.chmod(0o600)
    record = _ram_record(root)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps(
            {
                "v": 1,
                "name": name,
                "granting_key_fingerprint": _fingerprint(GRANTED_KEY),
                "paired_at": "2026-10-06T12:00:00+00:00",
            }
        )
    )
    record.chmod(0o600)


def test_persist_keeps_everything_in_ram_when_mnt_work_is_not_mounted_and_says_so(
    tmp_path: Path, kw: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hostile: /mnt/work exists as a plain directory -- writing there is writing to RAM."""
    root = _worker_root(tmp_path)
    _granted(root)
    kw.persist(root)
    assert list((root / "mnt" / "work").iterdir()) == []
    out, err = capsys.readouterr()
    assert "ram" in (out + err).lower()


def _paired_on_disk(tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _worker_root(tmp_path, "boot-1")
    _mount(monkeypatch, root)
    _granted(root, name="bentoo-lab")
    kw.persist(root)
    return root


def test_persist_writes_its_record_under_mnt_work_owner_only(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _paired_on_disk(tmp_path, kw, monkeypatch)
    dest = root / "mnt" / "work" / ".shidashi"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700

    pairing = dest / "pairing.json"
    assert stat.S_IMODE(pairing.stat().st_mode) == 0o600
    record = json.loads(pairing.read_text())
    assert record["v"] == 1
    assert record["name"] == "bentoo-lab"
    assert record["granting_key_fingerprint"] == _fingerprint(GRANTED_KEY)
    assert record["paired_at"]

    granted = dest / "authorized_keys"
    assert stat.S_IMODE(granted.stat().st_mode) == 0o600
    assert _key_fields(GRANTED_KEY) in [_key_fields(ln) for ln in granted.read_text().splitlines()]

    for kind in ("ed25519", "ecdsa", "rsa"):
        private = dest / "ssh" / f"ssh_host_{kind}_key"
        public = dest / "ssh" / f"ssh_host_{kind}_key.pub"
        assert private.read_bytes() == (root / "etc/ssh" / private.name).read_bytes()
        assert public.read_bytes() == (root / "etc/ssh" / public.name).read_bytes()
        assert stat.S_IMODE(private.stat().st_mode) == 0o600
        assert stat.S_IMODE(public.stat().st_mode) == 0o644


def test_persist_writes_neither_the_code_nor_its_key_to_the_disk(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _paired_on_disk(tmp_path, kw, monkeypatch)
    files = [p for p in (root / "mnt" / "work").rglob("*") if p.is_file()]
    assert files
    for path in files:
        data = path.read_bytes()
        for secret in SECRETS:
            assert secret.encode() not in data, f"{path} carries the code"


def _reboot(tmp_path: Path, first: Path) -> Path:
    """A new RAM root (its own fresh host keys) with the same disk at /mnt/work."""
    second = _worker_root(tmp_path, "boot-2")
    shutil.rmtree(second / "mnt" / "work")
    shutil.copytree(first / "mnt" / "work", second / "mnt" / "work", symlinks=True)
    keys = _authorized_keys(second)
    keys.parent.mkdir(mode=0o700)
    keys.write_text(VM_SESSION_KEY)  # a credential key, no final newline
    return second


def test_restore_brings_back_the_pinned_host_keys_the_key_and_the_name_without_input(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _paired_on_disk(tmp_path, kw, monkeypatch)
    pinned = _host_keys(first)
    second = _reboot(tmp_path, first)
    assert _host_keys(second) != pinned  # the boot made its own keys
    _mount(monkeypatch, first, second)
    _no_console(monkeypatch)
    runner = _Runner(second, mounted=True)

    assert _exit_code(kw, kw.restore, root=second, runner=runner) == 0

    # R5.5: the host key the host pinned at pairing is the one sshd will present
    assert _host_keys(second) == pinned
    for kind in ("ed25519", "ecdsa", "rsa"):
        assert stat.S_IMODE((second / "etc/ssh" / f"ssh_host_{kind}_key").stat().st_mode) == 0o600
        assert (
            stat.S_IMODE((second / "etc/ssh" / f"ssh_host_{kind}_key.pub").stat().st_mode) == 0o644
        )
    restored_pub = (second / "etc/ssh/ssh_host_ed25519_key.pub").read_text()
    pinned_pub = (first / "etc/ssh/ssh_host_ed25519_key.pub").read_text()
    assert _fingerprint(restored_pub) == _fingerprint(pinned_pub)
    assert not any(c[0] == "ssh-keygen" and "-A" in c for c in runner.calls)

    # R5.4: the granted key (beside the credential key), the name, sshd
    lines = _authorized_keys(second).read_text().splitlines()
    assert VM_SESSION_KEY in lines
    assert [_key_fields(ln) for ln in lines].count(_key_fields(GRANTED_KEY)) == 1
    assert runner.hostnames_set() == [["hostnamectl", "hostname", "bentoo-lab"]]
    assert runner.at_sshd_start is not None
    assert runner.at_sshd_start["host_keys"] == pinned  # keys in place BEFORE sshd starts


def test_restore_twice_keeps_the_granted_key_once(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _paired_on_disk(tmp_path, kw, monkeypatch)
    second = _reboot(tmp_path, first)
    _mount(monkeypatch, first, second)
    _no_console(monkeypatch)
    for _ in range(2):
        assert _exit_code(kw, kw.restore, root=second, runner=_Runner(second, mounted=True)) == 0
    lines = _authorized_keys(second).read_text().splitlines()
    assert [_key_fields(ln) for ln in lines].count(_key_fields(GRANTED_KEY)) == 1


def test_persist_keeps_only_the_granted_key_never_a_vm_session_key(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _worker_root(tmp_path, "boot-1")
    _mount(monkeypatch, root)
    _granted(root, before=(VM_SESSION_KEY,))  # a credential that must not outlive this boot
    kw.persist(root)
    persisted = (root / "mnt/work/.shidashi/authorized_keys").read_text().splitlines()
    assert [_key_fields(ln) for ln in persisted if ln.strip()] == [_key_fields(GRANTED_KEY)]


class _HostnameFails(_Runner):
    def __call__(self, argv: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(x) for x in argv]
        if argv[0] == "hostnamectl":
            self.calls.append(argv)
            if kw.get("check"):
                raise subprocess.CalledProcessError(1, argv, "", "Failed to connect to bus")
            return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
        return super().__call__(argv, *a, **kw)


def test_restore_still_starts_sshd_when_setting_the_hostname_fails(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """At local-fs.target hostnamed may not be up yet; that must not leave sshd stopped."""
    first = _paired_on_disk(tmp_path, kw, monkeypatch)
    second = _reboot(tmp_path, first)
    _mount(monkeypatch, first, second)
    _no_console(monkeypatch)
    runner = _HostnameFails(second, mounted=True)
    assert _exit_code(kw, kw.restore, root=second, runner=runner) != 0
    assert runner.started_sshd()
    out, err = capsys.readouterr()
    assert "hostname" in (out + err).lower()


def test_restore_refuses_a_persisted_key_that_is_not_the_granted_one(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: someone edited the disk's authorized_keys; the record names the key."""
    first = _paired_on_disk(tmp_path, kw, monkeypatch)
    (first / "mnt/work/.shidashi/authorized_keys").write_text(_ed25519_line("intruder") + "\n")
    second = _reboot(tmp_path, first)
    _mount(monkeypatch, first, second)
    _no_console(monkeypatch)
    runner = _Runner(second, mounted=True)
    assert _exit_code(kw, kw.restore, root=second, runner=runner) == 1
    assert _authorized_keys(second).read_text() == VM_SESSION_KEY
    assert not runner.started_sshd()


def test_persist_requiring_a_mount_fails_with_persist_error_when_there_is_none(
    tmp_path: Path, kw: Any
) -> None:
    """disk-init persists with require_mount=True; no mount is an error there."""
    root = _worker_root(tmp_path)
    _granted(root)
    with pytest.raises(kw.PersistError):
        kw.persist(root, require_mount=True)
    assert list((root / "mnt" / "work").iterdir()) == []


# ===================================================================================
# listen, console, main -- task 4.4
# ===================================================================================


class _Listening:
    """``kw.listen`` in a thread, with a recording console and a loopback server."""

    def __init__(
        self,
        kw: Any,
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        port: int = 8765,
        lockout: float | None = None,
        runner: _Runner | None = None,
    ) -> None:
        self.kw, self.root, self.port_asked = kw, root, port
        self.runner = runner or _Runner(root)
        self.events = self.runner.events
        self.shown: list[str] = []
        self.cleared = 0
        self.requested: list[tuple[str, int]] = []
        self.servers: list[HTTPServer] = []
        self.outcome: dict[str, Any] = {}
        monkeypatch.setattr(socket, "gethostname", lambda: "shidashi-worker")
        if lockout is not None:
            real = kw.WorkerSession

            def _short(*args: Any, **kwargs: Any) -> Any:
                kwargs["lockout"] = lockout
                return real(*args, **kwargs)

            monkeypatch.setattr(kw, "WorkerSession", _short)
        self.thread = threading.Thread(target=self._run, daemon=True)

    # -- the fakes handed to listen --------------------------------------------------
    def _show(self, text: Any, *_a: Any, **_k: Any) -> None:
        self.shown.append(str(text))
        self.events.append(("show",))

    def _clear(self, *_a: Any, **_k: Any) -> None:
        self.cleared += 1
        self.events.append(("clear",))

    def _factory(self, address: Any, handler: Any, *_a: Any, **_k: Any) -> HTTPServer:
        self.requested.append(tuple(address))
        events, root = self.events, self.root
        if not isinstance(handler, type):  # not a class: serve it as it is, unobserved
            server = HTTPServer(("127.0.0.1", 0), handler)
            self.servers.append(server)
            return server

        class _Spy(handler):  # type: ignore[misc]
            def send_response_only(self, code: int, message: str | None = None) -> None:
                keys = _authorized_keys(root)
                events.append(
                    (
                        "send",
                        code,
                        {
                            "granted": keys.exists() and GRANTED_KEY in keys.read_text(),
                            "record": _ram_record(root).exists(),
                        },
                    )
                )
                super().send_response_only(code, message)

        server = HTTPServer(("127.0.0.1", 0), _Spy)
        self.servers.append(server)
        return server

    def _run(self) -> None:
        try:
            self.outcome["returned"] = self.kw.listen(
                root=self.root,
                runner=self.runner,
                port=self.port_asked,
                show=self._show,
                clear=self._clear,
                server_factory=self._factory,
            )
        except BaseException as err:  # the test inspects it
            self.outcome["error"] = err

    # -- driving it -------------------------------------------------------------------
    def start(self) -> _Listening:
        self.thread.start()
        self.wait(lambda: self.servers and self.code(), "listen never served nor showed a code")
        return self

    def wait(self, condition: Any, what: str, seconds: float = 10.0) -> None:
        deadline = time.monotonic() + seconds
        while not condition():
            assert self.thread.is_alive() or condition(), f"listen ended: {self.outcome}"
            assert time.monotonic() < deadline, what
            time.sleep(0.02)

    def code(self) -> str | None:
        for text in reversed(self.shown):
            match = SHOWN_CODE.search(text)
            if match:
                return P.normalize_code(match.group(1))
        return None

    @property
    def port(self) -> int:
        return int(self.servers[0].server_address[1])

    def post(
        self, body: bytes, path: str = "/kyomei/v1", method: str = "POST"
    ) -> tuple[int, bytes, dict[str, str]]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body if method == "POST" else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), _headers(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, err.read(), _headers(err.headers)

    def join(self, seconds: float = 10.0) -> dict[str, Any]:
        self.thread.join(seconds)
        self.outcome["alive"] = self.thread.is_alive()
        return self.outcome

    def accepts(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                return True
        except OSError:
            return False


@pytest.fixture
def listening(tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Factory: ``listening(root=None, **options)`` -> a started _Listening."""
    started: list[_Listening] = []

    def _make(root: Path | None = None, **options: Any) -> _Listening:
        lst = _Listening(kw, root or _worker_root(tmp_path), monkeypatch, **options)
        started.append(lst.start())
        return lst

    yield _make
    for lst in started:  # a failed test must not leave a listener behind
        if lst.thread.is_alive():
            for server in lst.servers:
                server.server_close()


def _good(lst: _Listening, **over: Any) -> tuple[int, bytes, dict[str, str]]:
    code = lst.code()
    assert code is not None
    return lst.post(_hello(code, **over))


def test_listen_serves_port_8765_on_every_ipv4_address_and_pairs_once(listening: Any) -> None:
    lst = listening()
    assert lst.requested == [("0.0.0.0", 8765)]
    code = lst.code()
    assert code is not None
    status, body, _h = _good(lst)
    assert status == 200
    welcome, tag = P.parse_welcome(body)
    payload = json.loads(body)["payload"]
    assert tag is not None and P.verify(P.derive_key(code), "welcome", payload, tag)
    pub = (lst.root / "etc/ssh/ssh_host_ed25519_key.pub").read_text()
    assert _key_fields(welcome.host_key) == _key_fields(pub)
    assert tuple(welcome.addresses) == ("192.168.15.7", "10.0.0.4")  # no lo, no prefix
    assert {"avx2", "bmi2", "sse4_2"} <= set(welcome.cpu_flags)
    assert welcome.image == "20261005T1200"
    outcome = lst.join()
    assert not outcome["alive"], "the listener outlived the pairing"
    assert "error" not in outcome, outcome.get("error")
    assert outcome["returned"] in (0, None)
    assert not lst.accepts()


def test_listen_makes_host_keys_only_when_the_worker_has_none(
    tmp_path: Path, listening: Any
) -> None:
    with_keys = listening(_worker_root(tmp_path, "with-keys"))
    assert _good(with_keys)[0] == 200
    with_keys.join()
    assert not any(c[0] == "ssh-keygen" and "-A" in c for c in with_keys.runner.calls)

    bare = _worker_root(tmp_path, "no-keys")
    for path in (bare / "etc" / "ssh").glob("ssh_host_*"):
        path.unlink()
    without = listening(bare)
    keygen = [i for i, c in enumerate(without.runner.calls) if c[0] == "ssh-keygen" and "-A" in c]
    assert len(keygen) == 1
    status, body, _h = _good(without)
    assert status == 200
    pub = (bare / "etc/ssh/ssh_host_ed25519_key.pub").read_text()
    assert _key_fields(P.parse_welcome(body)[0].host_key) == _key_fields(pub)
    without.join()


@pytest.mark.parametrize("port", [8765, 9000])
def test_listen_announces_the_service_over_mdns_while_the_window_is_open(
    listening: Any, port: int
) -> None:
    lst = listening(port=port)
    assert lst.requested == [("0.0.0.0", port)]
    dnssd = _dnssd(lst.root)
    assert dnssd.is_file()
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(dnssd.read_text())
    service = parser["Service"]
    assert service["Name"] in ("%H", "shidashi-worker")
    assert service["Type"] == "_shidashi-kyomei._tcp"
    assert service["Port"] == str(port)
    txt = service["TxtText"].split()
    assert set(txt) == {"v=1", "image=20261005T1200", "trusted=no"}
    # resolved re-read the file after it was written
    assert True in lst.runner.resolved_reloads
    assert _good(lst)[0] == 200
    lst.join()


def test_listen_shows_the_code_the_name_the_addresses_and_the_host_key_fingerprint(
    listening: Any,
) -> None:
    lst = listening()
    text = next(t for t in lst.shown if SHOWN_CODE.search(t))
    assert P.format_code(lst.code() or "") in text
    assert "shidashi-worker" in text
    assert "192.168.15.7" in text and "10.0.0.4" in text
    assert "127.0.0.1" not in text
    pub = (lst.root / "etc/ssh/ssh_host_ed25519_key.pub").read_text()
    assert _fingerprint(pub) in text
    assert _good(lst)[0] == 200
    lst.join()


def test_listen_shows_a_new_code_once_the_lockout_passes(listening: Any) -> None:
    lst = listening(lockout=0.5)
    first = lst.code()
    assert first is not None
    wrong = "00000000" if first != "00000000" else "11111111"
    statuses = [lst.post(_hello(wrong))[0] for _ in range(3)]
    assert statuses == [403, 403, 403]
    status, _body, headers = lst.post(_hello(first))
    assert status == 423
    assert "retry-after" in headers
    lst.wait(lambda: lst.code() not in (None, first), "no new code after the lockout")
    second = lst.code()
    assert second is not None and second != first
    assert lst.post(_hello(first))[0] == 403  # the old code is gone
    status, body, _h = lst.post(_hello(second))
    assert status == 200
    lst.join()


def test_listen_notes_a_malformed_trust_parameter_on_the_console_and_ignores_it(
    tmp_path: Path, listening: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _worker_root(tmp_path)
    (root / "proc" / "cmdline").write_text("quiet shidashi.trust=bentoo-host,SHA256:nope\n")
    lst = listening(root)
    said = "\n".join(lst.shown) + "".join(capsys.readouterr())
    assert "shidashi.trust" in said
    assert "ignor" in said.lower()
    assert "trusted=no" in _dnssd(root).read_text()
    status, _b, _h = lst.post(_hello(mode="trusted"))  # from 127.0.0.1, any key: refused
    assert status == 403
    assert _good(lst)[0] == 200  # code mode still works
    lst.join()


def test_listen_announces_trusted_and_pairs_the_trusted_key_from_the_trusted_host(
    tmp_path: Path, listening: Any
) -> None:
    root = _worker_root(tmp_path)
    (root / "proc" / "cmdline").write_text(
        f"quiet shidashi.trust=127.0.0.1,{_fingerprint(GRANTED_KEY)} splash\n"
    )
    lst = listening(root)
    announced = _dnssd(root).read_text()
    assert "trusted=yes" in announced and "trusted=no" not in announced
    impostor = _ed25519_line("an-impostor", "shidashi-worker-key")
    assert lst.post(_hello(mode="trusted", key=impostor))[0] == 403
    status, body, _h = lst.post(_hello(mode="trusted", name=None))
    assert status == 200
    assert P.parse_welcome(body)[1] is None  # no MAC in trusted mode
    lst.join()
    keys = _authorized_keys(root).read_text().splitlines()
    assert [_key_fields(k) for k in keys if k.strip()] == [_key_fields(GRANTED_KEY)]


def test_listen_installs_the_key_names_the_worker_and_restarts_sshd_before_answering(
    tmp_path: Path, kw: Any, listening: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _worker_root(tmp_path)
    keys = _authorized_keys(root)
    keys.parent.mkdir(mode=0o700)
    keys.write_text(f"{RSA_KEY}\n{VM_SESSION_KEY}")  # a credential file: no final newline
    saved: list[dict[str, Any]] = []

    def _persist(*args: Any, **kwargs: Any) -> None:
        where = kwargs.get("root", args[0] if args else None)
        saved.append({"root": Path(str(where)), "record": _ram_record(root).exists()})

    monkeypatch.setattr(kw, "persist", _persist)
    lst = listening(root)
    lst.events.clear()
    status, _body, _h = _good(lst, name="bentoo-lab")
    assert status == 200
    # at the moment the client holds its 200, everything is in place
    lines = keys.read_text().splitlines()
    assert RSA_KEY in lines and VM_SESSION_KEY in lines
    assert [_key_fields(ln) for ln in lines].count(_key_fields(GRANTED_KEY)) == 1
    assert keys.read_text().endswith("\n")
    assert stat.S_IMODE(keys.stat().st_mode) & 0o022 == 0
    assert stat.S_IMODE(keys.parent.stat().st_mode) & 0o022 == 0
    named = lst.runner.hostnames_set()
    assert named and named[0][-1] == "bentoo-lab" and "--transient" not in named[0]
    assert any(c[0].endswith("sshd") and "-t" in c for c in lst.runner.calls)
    assert lst.runner.at_sshd_start is not None
    assert GRANTED_KEY in lst.runner.at_sshd_start["authorized_keys"]
    assert _ram_record(root).exists()
    assert saved and saved[0]["root"] == root and saved[0]["record"]
    # and in that order: everything before the 200 went out
    sends = [e for e in lst.events if e[0] == "send"]
    if sends:
        assert sends[0][1] == 200
        assert sends[0][2] == {"granted": True, "record": True}
    lst.join()


def test_listen_writes_the_ram_record_without_the_code(listening: Any) -> None:
    lst = listening()
    code = lst.code() or ""
    assert _good(lst, name="bentoo-lab")[0] == 200
    lst.join()
    record_path = _ram_record(lst.root)
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    record = json.loads(record_path.read_text())
    assert record["v"] == 1
    assert record["name"] == "bentoo-lab"
    assert record["granting_key_fingerprint"] == P.fingerprint(GRANTED_KEY)
    assert dt.datetime.fromisoformat(record["paired_at"]).utcoffset() == dt.timedelta(0)
    data = record_path.read_bytes()
    for secret in _secrets(code):
        assert secret.encode() not in data


def test_listen_an_unnamed_hello_leaves_the_hostname_alone(listening: Any) -> None:
    lst = listening()
    assert _good(lst, name=None)[0] == 200
    lst.join()
    assert lst.runner.hostnames_set() == []


# Installing exactly once (R3.5) -- hostile halves first: a key that only shares the
# granted key's comment is ANOTHER key (the granted one is still added); the granted key
# under another comment is the SAME key (not added again).


def test_listen_adds_the_granted_key_beside_another_key_with_the_same_comment(
    tmp_path: Path, listening: Any
) -> None:
    root = _worker_root(tmp_path)
    lookalike = _ed25519_line("an-older-host", "shidashi-worker-key")
    keys = _authorized_keys(root)
    keys.parent.mkdir(mode=0o700)
    keys.write_text(lookalike + "\n")
    lst = listening(root)
    assert _good(lst)[0] == 200
    lst.join()
    blobs = [_key_fields(line) for line in keys.read_text().splitlines() if line.strip()]
    assert blobs.count(_key_fields(lookalike)) == 1
    assert blobs.count(_key_fields(GRANTED_KEY)) == 1


def test_listen_does_not_add_the_granted_key_again_under_another_comment(
    tmp_path: Path, listening: Any
) -> None:
    root = _worker_root(tmp_path)
    keys = _authorized_keys(root)
    keys.parent.mkdir(mode=0o700)
    keys.write_text(" ".join(_key_fields(GRANTED_KEY)) + " added-by-hand\n")
    lst = listening(root)
    assert _good(lst)[0] == 200
    lst.join()
    blobs = [_key_fields(line) for line in keys.read_text().splitlines() if line.strip()]
    assert blobs == [_key_fields(GRANTED_KEY)]


@pytest.mark.parametrize("failure", ["sshd -t", "disk"])
def test_listen_answers_500_and_keeps_the_window_open_when_installing_fails(
    tmp_path: Path, kw: Any, listening: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = _worker_root(tmp_path)
    runner = _Runner(root, fail={"sshd -t": 1} if failure == "sshd -t" else {})
    if failure == "disk":
        calls = {"n": 0}
        real_persist = kw.persist

        def _persist_once_broken(*a: Any, **k: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise kw.PersistError("cannot write /mnt/work/.shidashi: No space left on device")
            return real_persist(*a, **k)

        monkeypatch.setattr(kw, "persist", _persist_once_broken)
    lst = listening(root, runner=runner)
    code = lst.code()
    status, body, _h = _good(lst)
    assert status == 500
    assert _key_fields(WORKER_HOST_KEY)[1].encode() not in body  # no welcome
    assert lst.thread.is_alive() and lst.accepts()  # the window stays open...
    assert _dnssd(root).exists()
    # ...with a NEW code (R2.13): the hello that failed crossed the LAN under the old one
    lst.wait(lambda: lst.code() not in (None, code), "no new code after the 500", seconds=5)
    new = lst.code()
    assert new is not None and new != code
    status, _body, _h = lst.post(_hello(new))
    assert status == 200
    lst.join()
    blobs = [_key_fields(ln) for ln in _authorized_keys(root).read_text().splitlines()]
    assert blobs.count(_key_fields(GRANTED_KEY)) == 1


def test_listen_withdraws_the_announcement_and_clears_the_console_after_pairing(
    listening: Any,
) -> None:
    lst = listening()
    assert _dnssd(lst.root).exists()
    assert _good(lst)[0] == 200
    outcome = lst.join()
    assert not outcome["alive"]
    assert not _dnssd(lst.root).exists()
    assert lst.runner.resolved_reloads and lst.runner.resolved_reloads[-1] is False
    assert lst.cleared >= 1
    assert not lst.accepts()


def test_listen_answers_only_a_post_to_the_kyomei_path(listening: Any) -> None:
    lst = listening()
    assert lst.post(b"", method="GET")[0] != 200
    assert lst.post(_hello(lst.code() or ""), path="/id_ed25519.pub")[0] != 200
    assert _good(lst)[0] == 200
    lst.join()


def test_listen_refuses_a_body_over_sixteen_kib_and_keeps_serving(listening: Any) -> None:
    lst = listening()
    body = _hello(lst.code() or "")
    try:
        status = lst.post(body + b" " * (20 * 1024 - len(body)))[0]
    except urllib.error.URLError, ConnectionError:
        status = 400  # the worker stopped reading at the cap and closed
    assert status == 400
    assert _good(lst)[0] == 200
    lst.join()


def test_listen_writes_the_code_to_no_file_no_log_and_no_stream(
    listening: Any, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    lst = listening()
    code = lst.code() or ""
    bad = _hello("00000000" if code != "00000000" else "11111111")
    good = _hello(code)
    lst.post(bad)
    lst.post(good)
    lst.join()
    out, err = capfd.readouterr()
    logged = " ".join(r.getMessage() for r in caplog.records)
    fragments = [
        json.loads(bad)["mac"],
        json.loads(good)["mac"],
        json.loads(good)["payload"]["nonce"],
    ]
    for secret in (*_secrets(code), *fragments):
        assert secret not in out and secret not in err, f"a stream carries {secret!r}"
        assert secret not in logged, f"a log line carries {secret!r}"
    for path in [p for p in lst.root.rglob("*") if p.is_file()]:
        data = path.read_bytes()
        for secret in _secrets(code):
            assert secret.encode() not in data, f"{path} carries the code"


def _console_call(fn: Any, runner: Any, root: Path, *args: Any) -> Any:
    """Call a console helper with ``root=`` and, if it takes one, our ``runner=``."""
    if "runner" in inspect.signature(fn).parameters:
        return fn(*args, root=root, runner=runner)
    return fn(*args, root=root)


def test_console_show_writes_the_issue_block_and_tty1_and_reloads_agetty(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _worker_root(tmp_path)
    runner = _Runner(root)
    monkeypatch.setattr(subprocess, "run", runner)
    block = "K7M4-Q2XP  bentoo-lab  192.168.15.7  SHA256:abc"
    _console_call(kw.console_show, runner, root, block)
    issue = root / "run" / "issue.d" / "50-shidashi-kyomei.issue"
    assert block in issue.read_text()
    assert stat.S_IMODE(issue.stat().st_mode) == 0o600  # v4 (R2.5): agetty reads it as root
    assert block in (root / "dev" / "tty1").read_text()
    assert ["agetty", "--reload"] in [c[:2] for c in runner.calls] or any(
        c[0].endswith("agetty") and "--reload" in c for c in runner.calls
    )


def test_console_clear_removes_the_issue_block(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _worker_root(tmp_path)
    runner = _Runner(root)
    monkeypatch.setattr(subprocess, "run", runner)
    _console_call(kw.console_show, runner, root, "K7M4-Q2XP  bentoo-lab")
    issue = root / "run" / "issue.d" / "50-shidashi-kyomei.issue"
    assert issue.exists()
    _console_call(kw.console_clear, runner, root)
    assert not issue.exists()


def test_main_dispatches_listen_and_restore(kw: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _listen(*_a: Any, **_k: Any) -> int:
        seen.append("listen")
        return 0

    def _restore(*_a: Any, **_k: Any) -> int:
        seen.append("restore")
        return 0

    monkeypatch.setattr(kw, "listen", _listen)
    monkeypatch.setattr(kw, "restore", _restore)
    assert _exit_code(kw, kw.main, ["--listen"]) == 0
    assert _exit_code(kw, kw.main, ["--restore"]) == 0
    assert seen == ["listen", "restore"]
    assert _exit_code(kw, kw.main, ["--bogus"]) != 0
    assert seen == ["listen", "restore"]
