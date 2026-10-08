"""Tests of ``shidashi kyomei`` (host side, v3) through Typer's CliRunner.

The host discovers, picks and connects: ``mdns.browse`` is replaced by a list of
answers, and the real ``kyomei.pair_with`` runs against a fake worker handed in as its
``opener`` (the protocol is real, only the HTTP hop is not). The SSH proof
(``remote.check``) is faked. Everything the command writes goes under a temporary
``XDG_DATA_HOME`` and ``SHIDASHI_RUNS``. The person's answers come through the runner's
stdin, in the order the command asks: the pick (or the confirmation), then the code.

Requirements exercised: R1.1, R1.2, R1.3, R1.4, R1.5, R1.6, R1.7, R1.8, R1.9, R1.10,
R1.11, R2.4, R2.5, R4.6, R7.5, R7.6.
"""

import base64
import email.message
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import struct
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, config, kyomei, mdns, remote, workers
from shidashi import kyomei_protocol as P
from shidashi.cli import app

runner = CliRunner()

CODE = "K7M4Q2XP"
SERVICE = "_shidashi-kyomei._tcp"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


def _ed25519_line(seed: str, comment: str = "") -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


WORKER_HOST_KEY = _ed25519_line("worker-sshd", "root@shidashi-worker")


def _out(result: Any) -> str:
    out: str = result.stdout + (getattr(result, "stderr", "") or "")
    return out


def _found(name: str, address: str, port: int = 8765) -> Any:
    txt = (("v", "1"), ("image", "20261006T1200"), ("trusted", "no"))
    return mdns.Found(name=name, address=address, port=port, txt=txt)


class _Response(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status
        self.headers = email.message.Message()

    def getcode(self) -> int:
        return self.status


class _Worker:
    """The worker behind the HTTP hop, speaking the real protocol."""

    def __init__(self) -> None:
        self.mode = "good"
        self.status = 200
        self.headers: dict[str, str] = {}
        self.error: BaseException | None = None
        self.hellos: list[dict[str, Any]] = []

    def __call__(self, req: Any, data: Any = None, timeout: Any = None, **_k: Any) -> Any:
        url = getattr(req, "full_url", req)
        if self.error is not None:
            raise self.error
        if self.status != 200:
            hdrs = email.message.Message()
            for key, value in self.headers.items():
                hdrs[key] = value
            raise urllib.error.HTTPError(url, self.status, "refused", hdrs, io.BytesIO(b""))
        doc = json.loads(getattr(req, "data", None) or data)
        hello = doc["payload"]
        self.hellos.append(hello)
        payload = {
            "v": 1,
            "nonce": hello["nonce"],
            "worker_nonce": P.new_nonce(),
            "host_key": WORKER_HOST_KEY,
            "hostname": hello["name"] or "shidashi-worker",
            "addresses": ["192.168.15.6"],
            "cpu_flags": ["avx2", "bmi2"],
            "image": "20261006T1200",
        }
        tag = None
        if hello["mode"] == "code":
            code = "K7M4Q2XR" if self.mode == "forged" else CODE
            tag = P.mac(P.derive_key(code), "welcome", payload)
        return _Response(200, json.dumps({"payload": payload, "mac": tag}).encode())


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("COLUMNS", "200")
    return {"wdir": tmp_path / "xdg" / "shidashi" / "worker", "runs": tmp_path / "runs"}


@pytest.fixture
def existing_key(env: dict[str, Path]) -> Path:
    """A worker key already on disk: the command must use it, not replace it."""
    wdir = env["wdir"]
    wdir.mkdir(parents=True)
    key = wdir / "id_ed25519"
    key.write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nexisting\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    key.chmod(0o600)
    (wdir / "id_ed25519.pub").write_text(_ed25519_line("existing-worker-key", "shidashi") + "\n")
    return key


class _Harness:
    """Discovery, the HTTP hop and the proof, faked; records what the command asked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.found: list[Any] = [_found("shidashi-worker", "192.168.15.6")]
        self.browse_calls: list[tuple[str, float]] = []
        self.pair_calls: list[dict[str, Any]] = []
        self.proofs: list[Any] = []
        self.proof_error: BaseException | None = None
        self.worker = _Worker()
        real_pair_with = kyomei.pair_with

        def _browse(service: str, wait: float, **_kw: Any) -> list[Any]:
            self.browse_calls.append((service, wait))
            return list(self.found)

        def _pair_with(target: Any, worker_key: Path, *, code: Any, name: Any, **kw: Any) -> Any:
            self.pair_calls.append({"target": target, "code": code, "name": name})
            kw["opener"] = self.worker
            return real_pair_with(target, worker_key, code=code, name=name, **kw)

        def _check(entry: Any, *_a: Any, **_k: Any) -> None:
            self.proofs.append(entry)
            if self.proof_error is not None:
                raise self.proof_error

        monkeypatch.setattr(mdns, "browse", _browse)
        for module in (kyomei, cli):
            monkeypatch.setattr(module, "browse", _browse, raising=False)
        monkeypatch.setattr(kyomei, "pair_with", _pair_with)
        monkeypatch.setattr(cli, "pair_with", _pair_with, raising=False)
        monkeypatch.setattr(remote, "check", _check)
        monkeypatch.setattr(kyomei, "check", _check, raising=False)
        monkeypatch.setattr(kyomei, "default_address", lambda: "192.168.15.5")
        monkeypatch.setattr(cli, "default_address", lambda: "192.168.15.5", raising=False)
        # one proof attempt: the retry window itself is complete()'s test (test_kyomei)
        monkeypatch.setattr(kyomei, "PROOF_WINDOW", 0.0)

    def discovered(self) -> bool:
        return bool(self.browse_calls)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, env: dict[str, Path]) -> _Harness:
    return _Harness(monkeypatch)


def _kyomei(*args: str, input: str = "") -> Any:
    return runner.invoke(app, ["kyomei", *args], input=input)


# --- arguments: refused before anything else (R1.7) -------------------------------------


@pytest.mark.parametrize(
    "name", ["Bad_Name", "bentoo.lab", "BentooLab", "-lab", "lab-", "a" * 64, "lab name", ""]
)
def test_kyomei_a_name_that_is_not_an_rfc1123_label_exits_2_before_discovery(
    harness: _Harness, existing_key: Path, name: str
) -> None:
    result = _kyomei("--name", name, input="y\nk7m4-q2xp\n")
    assert result.exit_code == 2, _out(result)
    assert not harness.discovered()
    assert harness.pair_calls == []


@pytest.mark.parametrize("wait", ["0", "31", "abc"])
def test_kyomei_a_wait_outside_1_to_30_exits_2_before_discovery(
    harness: _Harness, existing_key: Path, wait: str
) -> None:
    result = _kyomei("--wait", wait, input="y\nk7m4-q2xp\n")
    assert result.exit_code == 2, _out(result)
    assert not harness.discovered()


@pytest.mark.parametrize(
    "address", ["bentoo-lab", "::1", "192.168.15.6:0", "192.168.15.6:70000", "192.168.015.6"]
)
def test_kyomei_an_address_that_is_not_ipv4_and_port_exits_2(
    harness: _Harness, existing_key: Path, address: str
) -> None:
    result = _kyomei("--address", address, input="k7m4-q2xp\n")
    assert result.exit_code == 2, _out(result)
    assert harness.pair_calls == []


def test_kyomei_never_takes_the_code_as_an_option(harness: _Harness, existing_key: Path) -> None:
    """An option would leave the code in the shell's history."""
    result = _kyomei("--address", "192.168.15.6", "--code", CODE)
    assert result.exit_code == 2, _out(result)
    assert harness.pair_calls == []


# --- discovery and the pick (R1.1-R1.5) --------------------------------------------------


def test_kyomei_discovers_for_three_seconds_and_confirms_a_single_worker(
    harness: _Harness, existing_key: Path
) -> None:
    result = _kyomei(input="y\nk7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    assert harness.browse_calls == [(SERVICE, 3)]
    assert len(harness.pair_calls) == 1
    target = harness.pair_calls[0]["target"]
    assert (target.address, target.port) == ("192.168.15.6", 8765)
    assert "shidashi-worker" in _out(result) and "192.168.15.6" in _out(result)


@pytest.mark.parametrize("wait", ["1", "30"])
def test_kyomei_passes_the_wait_to_discovery(
    harness: _Harness, existing_key: Path, wait: str
) -> None:
    result = _kyomei("--wait", wait, input="y\nk7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    assert harness.browse_calls == [(SERVICE, int(wait))]


@pytest.mark.parametrize(
    ("address", "expected"),
    [("10.8.0.2:9000", ("10.8.0.2", 9000)), ("10.8.0.2", ("10.8.0.2", 8765))],
)
def test_kyomei_an_address_skips_discovery(
    harness: _Harness, existing_key: Path, address: str, expected: tuple[str, int]
) -> None:
    result = _kyomei("--address", address, input="k7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    assert not harness.discovered()
    target = harness.pair_calls[0]["target"]
    assert (target.address, target.port) == expected


def test_kyomei_lists_several_workers_and_pairs_the_one_picked(
    harness: _Harness, existing_key: Path
) -> None:
    harness.found = [
        _found("shidashi-worker", "192.168.15.7"),
        _found("shidashi-worker", "192.168.15.8"),
    ]
    result = _kyomei(input="2\nk7m4-q2xp\n")
    out = _out(result)
    assert result.exit_code == 0, out
    assert "192.168.15.7" in out and "192.168.15.8" in out
    assert harness.pair_calls[0]["target"].address == "192.168.15.8"


def test_kyomei_a_no_to_the_single_worker_cancels_with_exit_1(
    harness: _Harness, existing_key: Path
) -> None:
    result = _kyomei(input="n\n")
    assert result.exit_code == 1, _out(result)
    assert "cancel" in _out(result).lower()
    assert "Traceback" not in _out(result)
    assert harness.pair_calls == []


def test_kyomei_nothing_found_exits_1_naming_the_address_option(
    harness: _Harness, existing_key: Path
) -> None:
    harness.found = []
    result = _kyomei()
    assert result.exit_code == 1, _out(result)
    assert "--address" in _out(result)
    assert "Traceback" not in _out(result)
    assert harness.pair_calls == []


def test_kyomei_passes_the_name_it_was_given(harness: _Harness, existing_key: Path) -> None:
    result = _kyomei("--name", "bentoo-lab", "--address", "192.168.15.6", input="k7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    assert harness.pair_calls[0]["name"] == "bentoo-lab"
    assert set(workers.load_registry(config.workers_dir() / "workers.json")) == {"bentoo-lab"}


# --- the code (R1.6, R2.5) -------------------------------------------------------------


@pytest.mark.parametrize("typed", ["k7m4-q2xp", "K7M4Q2XP", "k7M4 q2xP"])
def test_kyomei_accepts_the_code_with_or_without_dash_in_either_case(
    harness: _Harness, existing_key: Path, typed: str
) -> None:
    result = _kyomei("--address", "192.168.15.6", input=f"{typed}\n")
    assert result.exit_code == 0, _out(result)
    assert P.normalize_code(harness.pair_calls[0]["code"]) == CODE


def test_kyomei_asks_again_for_a_code_that_is_not_eight_crockford_characters(
    harness: _Harness, existing_key: Path
) -> None:
    result = _kyomei("--address", "192.168.15.6", input="k7m4-q2x\nK7M4-Q2XU\nk7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    assert len(harness.pair_calls) == 1
    assert P.normalize_code(harness.pair_calls[0]["code"]) == CODE


def test_kyomei_summarizes_a_code_pairing_without_a_trust_on_first_use_note(
    harness: _Harness, existing_key: Path
) -> None:
    result = _kyomei("--name", "bentoo-lab", "--address", "192.168.15.6", input="k7m4-q2xp\n")
    out = _out(result)
    assert result.exit_code == 0, out
    assert "bentoo-lab" in out and "192.168.15.6" in out
    assert _fingerprint(WORKER_HOST_KEY) in out
    assert "first use" not in out.lower()
    assert len(harness.proofs) == 1


# --- trusted mode (R7.5, R7.6) -----------------------------------------------------------


def test_kyomei_trusted_asks_no_code_and_notes_trust_on_first_use(
    harness: _Harness, existing_key: Path
) -> None:
    result = _kyomei("--trusted", "--address", "192.168.15.6")  # stdin empty: no prompt
    out = _out(result)
    assert result.exit_code == 0, out
    assert harness.pair_calls[0]["code"] is None
    assert harness.worker.hellos[0]["mode"] == "trusted"
    assert _fingerprint(WORKER_HOST_KEY) in out
    assert "first use" in out.lower() or "not authenticated" in out.lower()


def test_kyomei_trust_param_prints_the_kernel_parameter_and_touches_no_network(
    harness: _Harness, existing_key: Path, env: dict[str, Path]
) -> None:
    result = _kyomei("--trust-param")
    out = _out(result)
    assert result.exit_code == 0, out
    pub = (existing_key.parent / "id_ed25519.pub").read_text()
    assert f"shidashi.trust=192.168.15.5,{_fingerprint(pub)}" in out.split()
    assert not harness.discovered()
    assert harness.pair_calls == []
    assert not (env["wdir"] / "workers.json").exists()


# --- the worker key (R1.9) -------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH's ssh-keygen")
def test_kyomei_creates_an_owner_only_ed25519_key_without_passphrase_when_absent(
    harness: _Harness, env: dict[str, Path]
) -> None:
    result = _kyomei("--address", "192.168.15.6", input="k7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    key = env["wdir"] / "id_ed25519"
    pub = (env["wdir"] / "id_ed25519.pub").read_text()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert pub.startswith("ssh-ed25519 ")
    assert "ENCRYPTED" not in key.read_text()  # an encrypted OpenSSH key says so
    # and the hello grants exactly that key
    assert harness.worker.hellos[0]["authorized_key"].split()[:2] == pub.split()[:2]


def test_kyomei_keeps_an_existing_worker_key(harness: _Harness, existing_key: Path) -> None:
    before = existing_key.read_bytes(), (existing_key.parent / "id_ed25519.pub").read_bytes()
    result = _kyomei("--address", "192.168.15.6", input="k7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    after = existing_key.read_bytes(), (existing_key.parent / "id_ed25519.pub").read_bytes()
    assert after == before


# --- outcomes: exit 1 without a traceback (R1.10, R1.11, R2.4, R4.6) -------------------


def test_kyomei_exits_0_once_paired_and_proven_and_records_the_worker(
    harness: _Harness, existing_key: Path
) -> None:
    result = _kyomei("--name", "bentoo-lab", input="y\nk7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    assert len(harness.proofs) == 1
    entry = workers.load_registry(config.workers_dir() / "workers.json")["bentoo-lab"]
    assert entry.address == "192.168.15.6"
    assert entry.host_key_fingerprint == _fingerprint(WORKER_HOST_KEY)


def _fail(harness: _Harness, case: str) -> list[str]:
    """Arrange ``case``; return the words its message must carry."""
    worker = harness.worker
    if case == "unreachable":
        worker.error = urllib.error.URLError(TimeoutError("timed out"))
        return ["192.168.15.6", "8765"]
    if case == "refused":
        worker.status = 403
        return ["refused"]
    if case == "locked":
        worker.status, worker.headers = 423, {"Retry-After": "27"}
        return ["lock", "27"]
    if case == "unauthenticated":
        worker.mode = "forged"
        return ["authenticat"]
    if case == "install failed":
        worker.status = 500
        return ["install"]
    harness.proof_error = remote.RemoteUnreachable("shidashi-worker", "192.168.15.6")
    return ["not proven"]


@pytest.mark.parametrize(
    "case", ["unreachable", "refused", "locked", "unauthenticated", "install failed", "unproven"]
)
def test_kyomei_exits_1_naming_why_the_pairing_failed(
    harness: _Harness, existing_key: Path, env: dict[str, Path], case: str
) -> None:
    words = _fail(harness, case)
    result = _kyomei("--address", "192.168.15.6", input="k7m4-q2xp\n")
    out = _out(result)
    assert result.exit_code == 1, out
    assert "Traceback" not in out
    for word in words:
        assert word in out.lower(), (word, out)
    if case != "unproven":  # nothing pinned without a welcome that authenticates
        assert not (env["wdir"] / "known_hosts").exists()
        assert harness.proofs == []
    else:
        assert "shidashi-worker" in workers.load_registry(env["wdir"] / "workers.json")


def test_kyomei_a_refusal_never_reads_as_an_unreachable_worker(
    harness: _Harness, existing_key: Path
) -> None:
    harness.worker.status = 403
    out = _out(_kyomei("--address", "192.168.15.6", input="k7m4-q2xp\n")).lower()
    assert "unreachable" not in out and "did not answer" not in out and "timed out" not in out


# --- the registry is checked before any worker can pair (R1.8) --------------------------


def test_kyomei_a_malformed_registry_exits_1_naming_it_before_discovery(
    harness: _Harness, existing_key: Path, env: dict[str, Path]
) -> None:
    (env["wdir"] / "workers.json").write_text("{not json")
    result = _kyomei(input="y\nk7m4-q2xp\n")
    assert result.exit_code == 1, _out(result)
    assert "workers.json" in _out(result)
    assert "Traceback" not in _out(result)
    assert not harness.discovered()  # no worker installed a key the host cannot record
    assert harness.pair_calls == []


def test_kyomei_a_registry_directory_it_cannot_write_exits_1_before_discovery(
    harness: _Harness, existing_key: Path, env: dict[str, Path]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    env["wdir"].chmod(0o500)
    try:
        result = _kyomei(input="y\nk7m4-q2xp\n")
    finally:
        env["wdir"].chmod(0o700)
    assert result.exit_code == 1, _out(result)
    assert str(env["wdir"]) in _out(result) or "workers.json" in _out(result)
    assert "Traceback" not in _out(result)
    assert not harness.discovered()


def test_kyomei_a_failing_ssh_keygen_exits_1_before_discovery(
    harness: _Harness, env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = subprocess.run

    def _run(argv: Any, *a: Any, **kw: Any) -> Any:
        if list(argv)[:1] == ["ssh-keygen"]:
            return subprocess.CompletedProcess(argv, 1, "", "ssh-keygen: cannot write key\n")
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _run)
    result = _kyomei(input="y\nk7m4-q2xp\n")
    assert result.exit_code == 1, _out(result)
    assert "Traceback" not in _out(result)
    assert not harness.discovered()


# --- the code stays off disk, out of the logs, never echoed back (R2.5) ----------------


def _all_files(*dirs: Path) -> list[Path]:
    return [p for d in dirs if d.exists() for p in d.rglob("*") if p.is_file()]


@pytest.mark.parametrize("outcome", ["paired", "refused", "unauthenticated"])
def test_kyomei_writes_the_code_nowhere(
    harness: _Harness,
    existing_key: Path,
    env: dict[str, Path],
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    if outcome == "refused":
        harness.worker.status = 403
    elif outcome == "unauthenticated":
        harness.worker.mode = "forged"
    result = _kyomei("--name", "bentoo-lab", input="y\nk7m4-q2xp\n")
    out = _out(result)
    # the person typed "k7m4-q2xp" (the runner may echo it); the command never prints
    # the code back in any form of its own
    for secret in (CODE, P.format_code(CODE), P.derive_key(CODE).hex()):
        assert secret not in out, out
    secrets = [CODE, P.format_code(CODE), CODE.lower(), "k7m4-q2xp", P.derive_key(CODE).hex()]
    for record in caplog.records:
        assert not any(s in record.getMessage() for s in secrets), record.getMessage()
    for path in _all_files(env["wdir"], env["runs"]):
        data = path.read_bytes()
        for secret in secrets:
            assert secret.encode() not in data, f"{path} carries the code"


def test_kyomei_lab_script_is_gone() -> None:
    """The plain-HTTP lab script is removed once the command replaces it."""
    assert not (_repo_root() / "lab" / "worker" / "kyomei.sh").exists()
