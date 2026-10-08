"""INTEGRATION tests of the pairing (v3): the worker's real listener
(``kyomei_worker.listen``, its real ``http.server``) and the host's real client
(``kyomei.pair_with`` with the real ``urllib``) talk over a loopback socket, and the
host's real ``complete`` pins the result through the real ``remote.check``.

Only what needs root, a console or a real machine is faked: the worker's files sit under
a temporary root, its privileged commands (hostnamectl, sshd, systemctl, ip, agetty)
go through a recording runner, its console is a recording ``show``/``clear``, its
listener is bound to 127.0.0.1 on a free port (``server_factory``) instead of
0.0.0.0:8765, and the host's ssh/ssh-keygen go through a recording runner. Discovery is
out of scope here: QEMU user networking and loopback carry no multicast (design).

Requirements exercised: R2.1, R2.5, R2.9, R2.10, R7.1, R7.2 (and R1.11, R3.4, R3.5,
R4.1, R4.2, R4.4 across both ends).
"""

import base64
import hashlib
import importlib
import json
import logging
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, kyomei, workers
from shidashi import kyomei_protocol as P


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


WORKER_LIB = _repo_root() / "variants/worker/rootfs/usr/local/lib/shidashi"
SHOWN_CODE = re.compile(r"\b([0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4})\b")


def _ed25519_line(seed: str, comment: str = "") -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _secrets(*codes: str) -> list[str]:
    out: list[str] = []
    for code in codes:
        fmt = P.format_code(code)
        out += [code, fmt, code.lower(), fmt.lower(), P.derive_key(code).hex()]
    return out


def _worker_root(base: Path, seed: str, cmdline: str = "quiet splash\n") -> Path:
    root = base / f"worker-{seed}"
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
        (ssh / f"ssh_host_{kind}_key.pub").write_text(pub + "\n")
    (root / "etc" / "os-release").write_text('NAME="Bentoo"\nID=bentoo\nBUILD_ID="20261006T1200"\n')
    (root / "etc" / "hostname").write_text("shidashi-worker\n")
    (root / "proc" / "sys" / "kernel").mkdir(parents=True)
    (root / "proc" / "sys" / "kernel" / "hostname").write_text("shidashi-worker\n")
    (root / "proc" / "cpuinfo").write_text("processor\t: 0\nflags\t\t: fpu sse4_2 avx2 bmi2\n")
    (root / "proc" / "cmdline").write_text(cmdline)
    (root / "dev").mkdir()
    (root / "dev" / "tty1").write_text("")
    (root / "run").mkdir()
    (root / "mnt" / "work").mkdir(parents=True)
    (root / "root").mkdir()
    return root


def _authorized_keys(root: Path) -> Path:
    return root / "root" / ".ssh" / "authorized_keys"


class _WorkerRunner:
    """The worker's privileged commands, recorded and answered; nothing runs."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, *_a: Any, **kw: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        out = ""
        if argv[0] == "ip":
            out = "lo  UNKNOWN  127.0.0.1/8 \nenp3s0  UP  192.168.15.7/24 \n"
        elif argv[0] == "ssh-keygen" and any(a.startswith("-l") for a in argv):
            src = kw.get("input")
            if src is None:
                src = Path(argv[-1]).read_text()
            src = src.decode() if isinstance(src, bytes) else src
            out = f"256 {_fingerprint(src)} k (ED25519)\n"
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        return subprocess.CompletedProcess(
            argv, 0, out if text else out.encode(), "" if text else b""
        )

    def started_sshd(self) -> bool:
        return any(
            c[0] == "systemctl" and any("sshd" in a for a in c) and ("restart" in c or "start" in c)
            for c in self.calls
        )

    def named(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "hostnamectl" and len(c) > 2]


class _HostSsh:
    """The host's ssh-keygen and ssh: fingerprints answered, the proof succeeds."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, *_a: Any, **kw: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        out = ""
        if argv[0] == "ssh-keygen":
            src = kw.get("input")
            src = src.decode() if isinstance(src, bytes) else src
            assert src is not None
            out = f"256 {_fingerprint(src)} k (ED25519)\n"
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        return subprocess.CompletedProcess(
            argv, 0, out if text else out.encode(), "" if text else b""
        )

    def proofs(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "ssh"]


@pytest.fixture
def kw(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(WORKER_LIB))
    for name in ("kyomei_worker", "kyomei_protocol", "worker_disk"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "shidashi-worker")
    return importlib.import_module("kyomei_worker")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    wdir = config.workers_dir()
    wdir.mkdir(parents=True)
    key = wdir / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nhost\n-----END OPENSSH PRIVATE KEY-----\n")
    key.chmod(0o600)
    pub = _ed25519_line("host-worker-key", "shidashi-worker-key")
    (wdir / "id_ed25519.pub").write_text(pub + "\n")
    impostor = tmp_path / "impostor" / "id_ed25519"
    impostor.parent.mkdir()
    impostor.write_text("private\n")
    (impostor.parent / "id_ed25519.pub").write_text(
        _ed25519_line("an-impostor", "shidashi-worker-key") + "\n"
    )
    return {"wdir": wdir, "key": key, "pub": pub, "impostor": impostor}


class _Worker:
    """``kw.listen`` in a thread, on 127.0.0.1, with a recording console."""

    def __init__(self, kw: Any, root: Path) -> None:
        self.kw, self.root = kw, root
        self.runner = _WorkerRunner()
        self.shown: list[str] = []
        self.servers: list[HTTPServer] = []
        self.outcome: dict[str, Any] = {}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _factory(self, _address: Any, handler: Any, *_a: Any, **_k: Any) -> HTTPServer:
        server = HTTPServer(("127.0.0.1", 0), handler)
        self.servers.append(server)
        return server

    def _run(self) -> None:
        try:
            self.outcome["returned"] = self.kw.listen(
                root=self.root,
                runner=self.runner,
                show=lambda text, *_a, **_k: self.shown.append(str(text)),
                clear=lambda *_a, **_k: None,
                server_factory=self._factory,
            )
        except BaseException as err:  # the test inspects it
            self.outcome["error"] = err

    def start(self) -> _Worker:
        self.thread.start()
        self.wait(lambda: bool(self.servers) and self.code() is not None, "never listened")
        return self

    def wait(self, condition: Any, what: str, seconds: float = 10.0) -> None:
        deadline = time.monotonic() + seconds
        while not condition():
            assert self.thread.is_alive(), f"listen ended: {self.outcome}"
            assert time.monotonic() < deadline, what
            time.sleep(0.02)

    def code(self) -> str | None:
        for text in reversed(self.shown):
            match = SHOWN_CODE.search(text)
            if match:
                return P.normalize_code(match.group(1))
        return None

    def target(self) -> Any:
        port = int(self.servers[0].server_address[1])
        return kyomei.Target(address="127.0.0.1", port=port, name="shidashi-worker")

    def join(self, seconds: float = 10.0) -> dict[str, Any]:
        self.thread.join(seconds)
        self.outcome["alive"] = self.thread.is_alive()
        return self.outcome

    def accepts(self) -> bool:
        port = int(self.servers[0].server_address[1])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False


@pytest.fixture
def worker(kw: Any, tmp_path: Path) -> Any:
    started: list[_Worker] = []

    def _make(seed: str = "w1", cmdline: str = "quiet splash\n") -> _Worker:
        w = _Worker(kw, _worker_root(tmp_path, seed, cmdline))
        started.append(w.start())
        return w

    yield _make
    for w in started:  # a failed test must not leave a listener behind
        if w.thread.is_alive():
            for server in w.servers:
                server.server_close()


def _installed_nothing(w: _Worker) -> bool:
    return (
        not _authorized_keys(w.root).exists()
        and not w.runner.started_sshd()
        and not w.runner.named()
    )


def _options(argv: list[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    for i, arg in enumerate(argv):
        if arg == "-o":
            key, _, value = argv[i + 1].partition("=")
            opts[key] = value
    return opts


def test_one_pairing_over_loopback_installs_names_pins_and_closes(
    worker: Any,
    host: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    w = worker()
    code = w.code()
    assert code is not None
    typed = P.format_code(code).lower()  # as a person types it off the worker's screen
    paired = kyomei.pair_with(w.target(), host["key"], code=typed, name="bentoo-lab")
    outcome = w.join()

    # R2.10: one pairing closes the worker's listener
    assert not outcome["alive"], "the listener outlived the pairing"
    assert "error" not in outcome, outcome.get("error")
    assert not w.accepts()

    # the worker installed the host's key, took the name, started sshd, kept a RAM record
    keys = _authorized_keys(w.root).read_text().splitlines()
    assert [k.split()[:2] for k in keys if k.strip()] == [host["pub"].split()[:2]]
    assert w.runner.named() and w.runner.named()[0][-1] == "bentoo-lab"
    assert w.runner.started_sshd()
    record = json.loads((w.root / "run" / "shidashi" / "pairing.json").read_text())
    assert record["name"] == "bentoo-lab"
    assert record["granting_key_fingerprint"] == _fingerprint(host["pub"])

    # the host pins the worker's own sshd key under its name, and proves it once
    ssh = _HostSsh()
    entry = kyomei.complete(paired, host["wdir"] / "workers.json", runner=ssh)
    pub = (w.root / "etc/ssh/ssh_host_ed25519_key.pub").read_text()
    assert entry.name == "bentoo-lab"
    assert entry.address == "127.0.0.1"  # where the host connected (R2.8)
    assert entry.host_key.split()[:2] == pub.split()[:2]
    assert entry.host_key_fingerprint == _fingerprint(pub)
    assert workers.load_registry(host["wdir"] / "workers.json")["bentoo-lab"] == entry
    (proof,) = ssh.proofs()
    opts = _options(proof)
    assert opts["HostKeyAlias"] == "bentoo-lab"
    assert opts["StrictHostKeyChecking"] == "yes"
    assert opts["UserKnownHostsFile"] == str(host["wdir"] / "known_hosts")
    assert "root@127.0.0.1" in proof

    # R2.10: the code cannot pair a second host
    before = _authorized_keys(w.root).read_bytes()
    with pytest.raises(kyomei.PairingError):
        kyomei.pair_with(w.target(), host["impostor"], code=code, name="evil")
    assert _authorized_keys(w.root).read_bytes() == before

    # R2.5: the code reached no log, no stream, no file on either end
    out, err = capfd.readouterr()
    logged = " ".join(r.getMessage() for r in caplog.records)
    files = [p for d in (w.root, host["wdir"]) for p in d.rglob("*") if p.is_file()]
    for secret in _secrets(code):
        assert secret not in logged, secret
        assert secret not in err and secret not in out, secret
        for path in files:
            assert secret.encode() not in path.read_bytes(), path


def test_three_wrong_codes_lock_the_worker_and_a_new_code_appears(
    worker: Any, host: dict[str, Any], kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = kw.WorkerSession

    def _short(*args: Any, **kwargs: Any) -> Any:
        kwargs["lockout"] = 1.0  # 30 s on the worker; one second here
        return real(*args, **kwargs)

    monkeypatch.setattr(kw, "WorkerSession", _short)
    w = worker()
    first = w.code()
    assert first is not None
    wrong = [c for c in ("00000000", "11111111", "22222222", "33333333") if c != first][:3]
    for code in wrong:  # R2.1: refused, nothing granted
        with pytest.raises(kyomei.PairingRefused) as err:
            kyomei.pair_with(w.target(), host["key"], code=code, name="bentoo-lab")
        assert err.value.status == 403
    assert _installed_nothing(w)

    # R2.9: locked -- even the right code is answered 423 with how long to wait
    with pytest.raises(kyomei.PairingRefused) as err:
        kyomei.pair_with(w.target(), host["key"], code=first, name="bentoo-lab")
    assert err.value.status == 423
    assert err.value.retry_after is not None and err.value.retry_after >= 1
    assert _installed_nothing(w)

    # then a new code on the worker's console, and only it pairs
    w.wait(lambda: w.code() not in (None, first), "no new code after the lockout")
    second = w.code()
    assert second is not None and second != first
    with pytest.raises(kyomei.PairingRefused):
        kyomei.pair_with(w.target(), host["key"], code=first, name="bentoo-lab")
    paired = kyomei.pair_with(w.target(), host["key"], code=second, name="bentoo-lab")
    assert paired.trusted is False
    assert not w.join()["alive"]
    entry = kyomei.complete(paired, host["wdir"] / "workers.json", runner=_HostSsh())
    assert entry.name == "bentoo-lab"


def test_trusted_mode_pairs_only_the_configured_key_from_the_configured_address(
    worker: Any, host: dict[str, Any]
) -> None:
    w = worker("trusted", f"quiet shidashi.trust=127.0.0.1,{_fingerprint(host['pub'])}\n")

    # R7.2, hostile first: the same comment on another key, from the trusted address
    with pytest.raises(kyomei.PairingRefused) as err:
        kyomei.pair_with(w.target(), host["impostor"], code=None, name=None)
    assert err.value.status == 403
    assert _installed_nothing(w)

    # R7.1: the trusted key from the trusted address pairs without a code
    paired = kyomei.pair_with(w.target(), host["key"], code=None, name=None)
    assert paired.trusted is True
    assert paired.name == "shidashi-worker"
    assert not w.join()["alive"]
    keys = _authorized_keys(w.root).read_text().splitlines()
    assert [k.split()[:2] for k in keys if k.strip()] == [host["pub"].split()[:2]]
    entry = kyomei.complete(paired, host["wdir"] / "workers.json", runner=_HostSsh())
    pub = (w.root / "etc/ssh/ssh_host_ed25519_key.pub").read_text()
    assert entry.host_key_fingerprint == _fingerprint(pub)  # trust on first use


def test_trusted_mode_refuses_the_right_key_from_another_address(
    worker: Any, host: dict[str, Any]
) -> None:
    """The parameter trusts 192.168.15.5; the hello comes from 127.0.0.1."""
    w = worker("elsewhere", f"quiet shidashi.trust=192.168.15.5,{_fingerprint(host['pub'])}\n")
    with pytest.raises(kyomei.PairingRefused) as err:
        kyomei.pair_with(w.target(), host["key"], code=None, name=None)
    assert err.value.status == 403
    assert _installed_nothing(w)
    # the window is still open for the code on the worker's screen
    code = w.code()
    assert code is not None
    paired = kyomei.pair_with(w.target(), host["key"], code=code, name=None)
    assert paired.trusted is False
    assert not w.join()["alive"]
