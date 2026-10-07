"""Tests of shidashi.kyomei -- the host's side of a pairing (v3: the host is the client).

Sections, each selected by its ``-k`` keyword (the test names carry it; no other test
name or parameter id contains it):

* ``pair_with``   -- the hello, the welcome's check, the refusals (task 3.1, unit; the
                     worker is a fake ``opener``);
* ``choose or address or trust_param`` -- the pick among the mDNS answers,
                     ``parse_address``, ``default_address``, ``trust_param`` (task 3.2);
* ``complete``    -- pin, register and prove (task 3.3; the SSH proof is faked);
* ``contract``    -- the host's real ``pair_with`` against the worker's real
                     ``WorkerSession.handle`` and ``complete`` (task 6.1).

Never a socket, never root. The code must never reach a log line, an error message or
a repr (R2.5): every secret check looks for the raw code, its displayed and lower-case
forms, and the derived key.

Requirements exercised: R1.2, R1.3, R1.4, R1.5, R1.10, R1.11, R1.12, R2.2, R2.4, R2.5,
R2.6, R2.7, R2.8, R4.1, R4.2, R4.3, R4.4, R4.6, R7.1, R7.5, R7.6.
"""

import base64
import datetime as dt
import email.message
import hashlib
import importlib
import io
import json
import logging
import shutil
import struct
import subprocess
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, kyomei, mdns, remote, workers
from shidashi import kyomei_protocol as P


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


ROOT = _repo_root()
WORKER_LIB = ROOT / "variants/worker/rootfs/usr/local/lib/shidashi"

CODE = "K7M4Q2XP"
OTHER_CODE = "K7M4Q2XR"
PRIVATE_MARKER = "PRIVATE-KEY-MATERIAL-NEVER-SENT"
WORKER_IP = "192.168.15.6"


def _ed25519_line(seed: str, comment: str = "") -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


WORKER_HOST_KEY = _ed25519_line("worker-sshd-host-key", "root@shidashi-worker")
GRANTED_KEY = _ed25519_line("host-worker-key", "shidashi-worker-key")


def _secrets(code: str = CODE) -> list[str]:
    key = P.derive_key(code)
    return [code, P.format_code(code), code.lower(), P.format_code(code).lower(), key.hex()]


def _key_fields(line: str) -> list[str]:
    return line.split()[:2]


@pytest.fixture
def worker_key(tmp_path: Path) -> Path:
    """The host's worker key pair; the private half must never be sent."""
    key = tmp_path / "keys" / "id_ed25519"
    key.parent.mkdir()
    key.write_text(
        f"-----BEGIN OPENSSH PRIVATE KEY-----\n{PRIVATE_MARKER}\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    key.chmod(0o600)
    (key.parent / "id_ed25519.pub").write_text(GRANTED_KEY + "\n")
    return key


# --- a fake worker behind the opener -----------------------------------------------------


class _Response(io.BytesIO):
    """What urlopen returns: a readable body that records how it was read."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status
        self.code = status
        self.headers = email.message.Message()
        self.read_sizes: list[int | None] = []
        self.bytes_read = 0

    def getcode(self) -> int:
        return self.status

    def read(self, size: int | None = -1) -> bytes:  # type: ignore[override]
        self.read_sizes.append(size)
        data = super().read(size)
        self.bytes_read += len(data)
        return data


def _http_error(url: str, status: int, headers: dict[str, str] | None = None) -> Exception:
    hdrs = email.message.Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return urllib.error.HTTPError(url, status, "refused", hdrs, io.BytesIO(b""))


class _Worker:
    """A worker as the protocol describes it, with ways to misbehave (``mode``)."""

    def __init__(
        self,
        mode: str = "good",
        *,
        code: str = CODE,
        hostname: str = "shidashi-worker",
        addresses: tuple[str, ...] = (WORKER_IP,),
        echo_name: bool = True,
        status: int = 200,
        headers: dict[str, str] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.mode, self.code, self.hostname = mode, code, hostname
        self.addresses, self.echo_name = addresses, echo_name
        self.status, self.headers, self.error = status, headers, error
        self.requests: list[dict[str, Any]] = []
        self.hello: dict[str, Any] | None = None
        self.tag: str | None = None
        self.response: _Response | None = None

    def __call__(self, req: Any, data: Any = None, timeout: Any = None, **_k: Any) -> Any:
        url = getattr(req, "full_url", req)
        body = getattr(req, "data", None) or data
        method = req.get_method() if hasattr(req, "get_method") else "POST"
        ctype = req.get_header("Content-type") if hasattr(req, "get_header") else None
        self.requests.append(
            {"url": url, "body": body, "timeout": timeout, "method": method, "ctype": ctype}
        )
        if self.error is not None:
            raise self.error
        if self.status != 200:
            raise _http_error(url, self.status, self.headers)
        doc = json.loads(body)
        self.hello, self.tag = doc["payload"], doc["mac"]
        trusted = self.hello["mode"] == "trusted"
        nonce = self.hello["nonce"]
        if self.mode == "other nonce":
            nonce = P.new_nonce()
        elif self.mode == "nonce in another case":
            swapped = nonce.swapcase()
            nonce = swapped if swapped != nonce else ("B" if nonce[0] == "A" else "A") + nonce[1:]
        hostname = (self.echo_name and self.hello.get("name")) or self.hostname
        payload: dict[str, Any] = {
            "v": 1,
            "nonce": nonce,
            "worker_nonce": P.new_nonce(),
            "host_key": WORKER_HOST_KEY,
            "hostname": hostname,
            "addresses": list(self.addresses),
            "cpu_flags": ["avx2", "bmi2", "sse4_2"],
            "image": "20261006T1200",
        }
        key = P.derive_key(OTHER_CODE if self.mode == "forged mac" else self.code)
        kind = "hello" if self.mode == "hello-kind mac" else "welcome"
        tag: str | None = None if trusted else P.mac(key, kind, payload)
        if self.mode == "null mac":  # a downgrade: a code-mode answer without its MAC
            tag = None
        if self.mode == "tampered after mac":
            payload["host_key"] = _ed25519_line("an-impostors-sshd-key", "root@x")
        if self.mode == "bad hostname":
            payload["hostname"] = "Evil Host"
            tag = None if trusted else P.mac(key, kind, payload)
        if self.mode == "reordered":  # the same welcome, other key order and spacing
            answer = json.dumps(
                {"mac": tag, "payload": dict(reversed(list(payload.items())))}, indent=2
            ).encode()
        else:
            answer = json.dumps({"payload": payload, "mac": tag}).encode()
        if self.mode == "oversized":
            answer += b" " * (1 << 20)
        elif self.mode == "exactly max body":
            answer += b" " * (16384 - len(answer))
        elif self.mode == "not json":
            answer = b"<html>502 Bad Gateway</html>"
        self.response = _Response(200, answer)
        return self.response


def _target(address: str = WORKER_IP, port: int = 8765, name: str | None = None) -> Any:
    return kyomei.Target(address=address, port=port, name=name)


def _pair(worker: _Worker, worker_key: Path, *, code: str | None = CODE, **kw: Any) -> Any:
    kw.setdefault("name", "bentoo-lab")
    target = kw.pop("target", None) or _target()
    return kyomei.pair_with(target, worker_key, code=code, opener=worker, **kw)


# ===================================================================================
# pair_with -- task 3.1
# ===================================================================================


def test_pair_with_posts_one_code_mode_hello_with_a_ten_second_timeout(worker_key: Path) -> None:
    worker = _Worker()
    _pair(worker, worker_key, target=_target(port=9000))
    assert len(worker.requests) == 1
    request = worker.requests[0]
    assert request["url"] == f"http://{WORKER_IP}:9000/kyomei/v1"
    assert request["method"] == "POST"
    assert request["timeout"] == 10
    assert (request["ctype"] or "").startswith("application/json")
    hello = worker.hello
    assert hello is not None
    assert set(hello) == {"v", "mode", "nonce", "authorized_key", "name"}
    assert hello["v"] == 1 and hello["mode"] == "code"
    assert _key_fields(hello["authorized_key"]) == _key_fields(GRANTED_KEY)
    assert hello["name"] == "bentoo-lab"
    assert len(base64.urlsafe_b64decode(hello["nonce"] + "==")) == 16
    # the MAC binds the whole payload under the code's key, as a hello
    assert worker.tag == P.mac(P.derive_key(CODE), "hello", hello)
    assert PRIVATE_MARKER.encode() not in request["body"]


@pytest.mark.parametrize("typed", ["k7m4-q2xp", "K7M4 Q2XP", "k7M4q2Xp"])
def test_pair_with_derives_the_key_from_the_normalized_code(worker_key: Path, typed: str) -> None:
    worker = _Worker()
    paired = _pair(worker, worker_key, code=typed)
    assert worker.tag == P.mac(P.derive_key(CODE), "hello", worker.hello)
    assert paired.trusted is False


def test_pair_with_draws_a_fresh_nonce_per_hello(worker_key: Path) -> None:
    nonces = set()
    for _ in range(5):
        worker = _Worker()
        _pair(worker, worker_key)
        assert worker.hello is not None
        nonces.add(worker.hello["nonce"])
    assert len(nonces) == 5


def test_pair_with_sends_a_trusted_hello_without_a_mac_when_there_is_no_code(
    worker_key: Path,
) -> None:
    worker = _Worker()
    paired = _pair(worker, worker_key, code=None, name=None)
    assert worker.hello is not None
    assert worker.hello["mode"] == "trusted"
    assert worker.tag is None
    assert worker.hello["name"] is None
    assert paired.trusted is True
    assert _key_fields(paired.welcome.host_key) == _key_fields(WORKER_HOST_KEY)


# Who the worker is -- hostile halves first: the worker's own claims (another hostname,
# another address list) never override what the host chose or where it connected.


def test_pair_with_keeps_the_given_name_over_the_hostname_the_worker_claims(
    worker_key: Path,
) -> None:
    paired = _pair(_Worker(echo_name=False, hostname="shidashi-worker"), worker_key)
    assert paired.welcome.hostname == "shidashi-worker"
    assert paired.name == "bentoo-lab"


def test_pair_with_records_where_it_connected_not_what_the_worker_lists(
    worker_key: Path,
) -> None:
    """R2.8: across a VPN the worker's own LAN addresses are not reachable."""
    paired = _pair(_Worker(addresses=("10.9.9.9",)), worker_key, target=_target("10.8.0.2"))
    assert paired.address == "10.8.0.2"
    assert tuple(paired.welcome.addresses) == ("10.9.9.9",)


def test_pair_with_an_unnamed_pairing_takes_the_workers_hostname(worker_key: Path) -> None:
    paired = _pair(_Worker(hostname="spare-box"), worker_key, name=None)
    assert paired.name == "spare-box"


def test_pair_with_returns_the_authenticated_welcome(worker_key: Path) -> None:
    worker = _Worker()
    paired = _pair(worker, worker_key)
    assert isinstance(paired, kyomei.Paired)
    welcome = paired.welcome
    assert worker.hello is not None
    assert welcome.nonce == worker.hello["nonce"]
    assert _key_fields(welcome.host_key) == _key_fields(WORKER_HOST_KEY)
    assert tuple(welcome.cpu_flags) == ("avx2", "bmi2", "sse4_2")
    assert welcome.image == "20261006T1200"
    assert paired.name == "bentoo-lab"
    assert paired.address == WORKER_IP
    assert paired.trusted is False


# The welcome's authentication (R2.4) -- hostile halves first: welcomes that only LOOK
# like the right one are refused; then the right welcome in another JSON shape passes.


@pytest.mark.parametrize(
    "mode",
    [
        "nonce in another case",
        "other nonce",
        "forged mac",
        "hello-kind mac",
        "tampered after mac",
        "null mac",
    ],
)
def test_pair_with_refuses_a_welcome_that_does_not_authenticate_the_worker(
    worker_key: Path, mode: str
) -> None:
    with pytest.raises(kyomei.PairingNotAuthenticated) as err:
        _pair(_Worker(mode), worker_key)
    assert "authenticat" in str(err.value).lower()


def test_pair_with_refuses_a_trusted_welcome_that_does_not_echo_its_nonce(
    worker_key: Path,
) -> None:
    with pytest.raises(kyomei.PairingNotAuthenticated):
        _pair(_Worker("other nonce"), worker_key, code=None)


def test_pair_with_accepts_the_right_welcome_in_another_key_order_and_spacing(
    worker_key: Path,
) -> None:
    paired = _pair(_Worker("reordered"), worker_key)
    assert _key_fields(paired.welcome.host_key) == _key_fields(WORKER_HOST_KEY)


@pytest.mark.parametrize("mode", ["oversized", "not json", "bad hostname"])
def test_pair_with_refuses_a_malformed_welcome(worker_key: Path, mode: str) -> None:
    with pytest.raises((P.ProtocolError, kyomei.PairingError)):
        _pair(_Worker(mode), worker_key)


def test_pair_with_reads_at_most_one_byte_past_the_cap(worker_key: Path) -> None:
    worker = _Worker("oversized")
    with pytest.raises((P.ProtocolError, kyomei.PairingError)):
        _pair(worker, worker_key)
    assert worker.response is not None
    sizes = worker.response.read_sizes
    assert sizes and all(s is not None and 0 < s <= 16385 for s in sizes), sizes
    assert worker.response.bytes_read <= 16385


def test_pair_with_accepts_a_welcome_of_exactly_sixteen_kib(worker_key: Path) -> None:
    paired = _pair(_Worker("exactly max body"), worker_key)
    assert paired.name == "bentoo-lab"


# Refusals and outages (R1.10, R1.11) -- hostile half first: an HTTPError IS a URLError,
# so a refusal caught as an outage would read "unreachable".


@pytest.mark.parametrize("status", [400, 403])
def test_pair_with_maps_a_refusal_to_pairing_refused_not_to_unreachable(
    worker_key: Path, status: int
) -> None:
    with pytest.raises(kyomei.PairingRefused) as err:
        _pair(_Worker(status=status), worker_key)
    assert not isinstance(err.value, kyomei.WorkerUnreachable)
    assert err.value.status == status
    assert err.value.retry_after is None


def test_pair_with_maps_a_locked_worker_to_pairing_refused_with_its_retry_after(
    worker_key: Path,
) -> None:
    with pytest.raises(kyomei.PairingRefused) as err:
        _pair(_Worker(status=423, headers={"Retry-After": "27"}), worker_key)
    assert err.value.status == 423
    assert err.value.retry_after == 27


def test_pair_with_maps_a_500_to_a_failed_install_on_the_worker(worker_key: Path) -> None:
    with pytest.raises(kyomei.PairingError) as err:
        _pair(_Worker(status=500), worker_key)
    assert not isinstance(err.value, (kyomei.PairingRefused, kyomei.WorkerUnreachable))
    assert "install" in str(err.value).lower()


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
        urllib.error.URLError(TimeoutError("timed out")),
        TimeoutError("timed out"),
        ConnectionResetError(104, "Connection reset by peer"),
        OSError(113, "No route to host"),
    ],
    ids=["refused-socket", "url-timeout", "socket-timeout", "reset", "no-route"],
)
def test_pair_with_maps_an_outage_to_worker_unreachable_naming_where(
    worker_key: Path, error: BaseException
) -> None:
    with pytest.raises(kyomei.WorkerUnreachable) as err:
        _pair(_Worker(error=error), worker_key, target=_target(port=9000))
    assert err.value.address == WORKER_IP
    assert err.value.port == 9000
    assert WORKER_IP in str(err.value) and "9000" in str(err.value)


def test_pair_with_errors_share_one_base() -> None:
    for cls in (
        kyomei.WorkerUnreachable,
        kyomei.PairingRefused,
        kyomei.PairingNotAuthenticated,
        kyomei.PairingNotProven,
    ):
        assert issubclass(cls, kyomei.PairingError), cls


def test_pair_with_never_logs_or_reports_the_code_its_key_or_a_body(
    worker_key: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    texts: list[str] = []
    for worker in (
        _Worker(),
        _Worker("forged mac"),
        _Worker("other nonce"),
        _Worker(status=403),
        _Worker(status=423, headers={"Retry-After": "30"}),
        _Worker(status=500),
        _Worker(error=urllib.error.URLError(TimeoutError("timed out"))),
    ):
        try:
            paired = _pair(worker, worker_key, code="k7m4-q2xp")
            texts += [repr(paired), str(paired)]
        except Exception as err:  # every failure's message is checked below
            texts += [str(err), repr(err)]
    texts += [record.getMessage() for record in caplog.records]
    for text in texts:
        for secret in (*_secrets(), PRIVATE_MARKER):
            assert secret not in text, f"{secret!r} leaked into: {text}"


# ===================================================================================
# choose, parse_address, default_address, trust_param -- task 3.2
# ===================================================================================


def _found(name: str, address: str, port: int = 8765, trusted: str = "no") -> Any:
    txt = {"v": "1", "image": "20261006T1200", "trusted": trusted}
    return mdns.Found(name=name, address=address, port=port, txt=txt)


class _Asker:
    """Scripted answers for ``ask``/``confirm``; records every prompt."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, text: str = "", *_a: Any, **_k: Any) -> Any:
        self.prompts.append(str(text))
        assert self.answers, f"asked once too often: {text!r}"
        return self.answers.pop(0)


def _never(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("the person was asked something here")


def test_choose_with_nothing_found_names_the_option_that_reaches_across_networks() -> None:
    with pytest.raises(kyomei.PairingError) as err:
        kyomei.choose([], ask=_never, confirm=_never)
    assert "--address" in str(err.value)


def test_choose_asks_to_confirm_a_single_worker_naming_it() -> None:
    confirm = _Asker(True)
    target = kyomei.choose([_found("bentoo-lab", WORKER_IP)], ask=_never, confirm=confirm)
    assert (target.address, target.port, target.name) == (WORKER_IP, 8765, "bentoo-lab")
    assert len(confirm.prompts) == 1
    assert "bentoo-lab" in confirm.prompts[0] and WORKER_IP in confirm.prompts[0]


def test_choose_a_no_to_the_single_worker_cancels() -> None:
    with pytest.raises(kyomei.PairingError) as err:
        kyomei.choose([_found("bentoo-lab", WORKER_IP)], ask=_never, confirm=_Asker(False))
    assert "cancel" in str(err.value).lower()


def test_choose_tells_apart_two_machines_with_the_same_name_by_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hostile: both fresh workers carry the image's default hostname."""
    found = [
        _found("shidashi-worker", "192.168.15.7"),
        _found("shidashi-worker", "192.168.15.8", trusted="yes"),
        _found("spare-box", "192.168.15.9", 9000),
    ]
    ask = _Asker("0", "4", "two", "", "2")
    target = kyomei.choose(found, ask=ask, confirm=_never)
    assert (target.address, target.port, target.name) == ("192.168.15.8", 8765, "shidashi-worker")
    assert len(ask.prompts) == 5  # re-asked after each invalid answer
    shown = capsys.readouterr().out + "\n".join(ask.prompts)
    for item in found:
        assert item.address in shown, item
    assert "spare-box" in shown
    assert "20261006T1200" in shown
    assert "trusted" in shown.lower()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("192.168.15.6", ("192.168.15.6", 8765)),
        ("10.8.0.2:9000", ("10.8.0.2", 9000)),
        ("10.8.0.2:1", ("10.8.0.2", 1)),
        ("10.8.0.2:65535", ("10.8.0.2", 65535)),
    ],
)
def test_parse_address_accepts_an_ipv4_with_an_optional_port(
    text: str, expected: tuple[str, int]
) -> None:
    target = kyomei.parse_address(text)
    assert (target.address, target.port) == expected
    assert target.name is None


@pytest.mark.parametrize(
    "text",
    [
        "bentoo-lab",
        "bentoo-lab.local",
        "bentoo-lab:8765",
        "::1",
        "[::1]:8765",
        "fe80::1",
        "192.168.15.6:0",
        "192.168.15.6:65536",
        "192.168.15.6:",
        ":8765",
        "192.168.15.6:abc",
        "192.168.15.6/24",
        "192.168.015.6",  # octal to inet_aton: another machine
        "256.1.1.1",
        "",
    ],
)
def test_parse_address_refuses_what_is_not_ipv4_and_a_port(text: str) -> None:
    with pytest.raises(ValueError):
        kyomei.parse_address(text)


def _fake_ip(stdout: str, calls: list[list[str]], rc: int = 0) -> Callable[..., Any]:
    def _run(argv: Any, *_a: Any, **kw: Any) -> subprocess.CompletedProcess:
        assert isinstance(argv, list), "argv list, never a shell string"
        calls.append([str(a) for a in argv])
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        err = "RTNETLINK answers: Network is unreachable\n" if rc else ""
        if text:
            return subprocess.CompletedProcess(argv, rc, stdout, err)
        return subprocess.CompletedProcess(argv, rc, stdout.encode(), err.encode())

    return _run


def test_default_address_is_the_default_routes_source(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    route = "1.1.1.1 via 192.168.15.1 dev enp5s0 src 192.168.15.5 uid 1000 \n    cache \n"
    monkeypatch.setattr(subprocess, "run", _fake_ip(route, calls))
    assert kyomei.default_address() == "192.168.15.5"
    assert calls and calls[0][:4] == ["ip", "-4", "route", "get"]


@pytest.mark.parametrize("rc", [0, 2])
def test_default_address_without_a_route_raises(monkeypatch: pytest.MonkeyPatch, rc: int) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_ip("", [], rc))
    with pytest.raises(kyomei.PairingError):
        kyomei.default_address()


def test_trust_param_carries_the_hosts_ip_and_its_worker_keys_fingerprint(
    worker_key: Path,
) -> None:
    param = kyomei.trust_param(worker_key, "192.168.15.5")
    assert param == f"shidashi.trust=192.168.15.5,{_fingerprint(GRANTED_KEY)}"


def test_trust_param_is_read_back_by_the_protocols_parser(worker_key: Path) -> None:
    """Derived value: what the host renders is what the worker's parser compares."""
    trust = P.parse_trust("quiet " + kyomei.trust_param(worker_key, "192.168.15.5") + "\n")
    assert trust is not None
    assert trust.address == "192.168.15.5"
    assert trust.fingerprint == P.fingerprint(GRANTED_KEY)


def test_trust_param_differs_for_another_key_with_the_same_comment(tmp_path: Path) -> None:
    key = tmp_path / "other" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("private\n")
    (key.parent / "id_ed25519.pub").write_text(
        _ed25519_line("another-host", "shidashi-worker-key") + "\n"
    )
    mine = tmp_path / "mine" / "id_ed25519"
    mine.parent.mkdir()
    mine.write_text("private\n")
    (mine.parent / "id_ed25519.pub").write_text(GRANTED_KEY + "\n")
    assert kyomei.trust_param(key, "192.168.15.5") != kyomei.trust_param(mine, "192.168.15.5")


# ===================================================================================
# complete -- task 3.3
# ===================================================================================


@pytest.fixture
def wdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    path = config.workers_dir()
    path.mkdir(parents=True)
    return path


class _Proof:
    """Stands in for remote.check: records each target and what was on disk then."""

    def __init__(self, wdir: Path, error: BaseException | None = None) -> None:
        self.wdir, self.error = wdir, error
        self.entries: list[Any] = []
        self.known_hosts_then: list[str] = []
        self.registry_then: dict[str, Any] = {}

    def __call__(self, entry: Any, *_a: Any, **_kw: Any) -> None:
        self.entries.append(entry)
        kh = self.wdir / "known_hosts"
        self.known_hosts_then = kh.read_text().splitlines() if kh.exists() else []
        self.registry_then = workers.load_registry(self.wdir / "workers.json")
        if self.error is not None:
            raise self.error


@pytest.fixture
def proof(wdir: Path, monkeypatch: pytest.MonkeyPatch) -> _Proof:
    fake = _Proof(wdir)
    monkeypatch.setattr(remote, "check", fake)
    monkeypatch.setattr(kyomei, "check", fake, raising=False)
    return fake


class _Keygen:
    """``ssh-keygen -lf -`` answered from the key it is fed; nothing runs."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, *_a: Any, **kw: Any) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        out, err = "", ""
        if argv[0] == "ssh-keygen" and any(a.startswith("-l") for a in argv):
            src = kw.get("input")
            if src is None:
                src = Path(argv[-1]).read_text()
            src = src.decode() if isinstance(src, bytes) else src
            out = f"256 {_fingerprint(src)} root@shidashi-worker (ED25519)\n"
        if self.rc:
            out, err = "", "(stdin) is not a public key file.\n"
        if text:
            return subprocess.CompletedProcess(argv, self.rc, out, err)
        return subprocess.CompletedProcess(argv, self.rc, out.encode(), err.encode())


def _welcome(**over: Any) -> Any:
    fields: dict[str, Any] = {
        "nonce": P.new_nonce(),
        "worker_nonce": P.new_nonce(),
        "host_key": WORKER_HOST_KEY,
        "hostname": "shidashi-worker",
        "addresses": ("192.168.15.7",),
        "cpu_flags": ("avx2", "bmi2", "sse4_2"),
        "image": "20261006T1200",
    }
    fields.update(over)
    return P.Welcome(**fields)


def _paired(
    name: str = "bentoo-lab", ip: str = "192.168.15.7", trusted: bool = False, **over: Any
) -> Any:
    return kyomei.Paired(welcome=_welcome(**over), name=name, address=ip, trusted=trusted)


def _complete(paired: Any, wdir: Path, **kw: Any) -> Any:
    """The one call site: ``complete(paired, registry_path, *, runner, clock, sleep,
    proof_window)`` with known_hosts beside the registry."""
    kw.setdefault("runner", _Keygen())
    return kyomei.complete(paired, wdir / "workers.json", **kw)


class _Clock:
    """A fake monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _keys_for(known_hosts: Path, name: str) -> list[list[str]]:
    found = []
    for line in known_hosts.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            hosts, *key = line.split()
            if name in hosts.split(","):
                found.append(key[:2])
    return found


def test_complete_pins_registers_then_proves_the_pin_once(wdir: Path, proof: _Proof) -> None:
    before = dt.datetime.now(dt.UTC)
    entry = _complete(_paired(), wdir)
    assert entry.name == "bentoo-lab"
    assert entry.address == "192.168.15.7"
    assert _key_fields(entry.host_key) == _key_fields(WORKER_HOST_KEY)
    assert entry.host_key_fingerprint == _fingerprint(WORKER_HOST_KEY)
    assert tuple(entry.cpu_flags) == ("avx2", "bmi2", "sse4_2")
    assert entry.image == "20261006T1200"
    paired_at = dt.datetime.fromisoformat(entry.paired_at)
    assert paired_at.utcoffset() == dt.timedelta(0)
    assert before - dt.timedelta(seconds=5) <= paired_at <= dt.datetime.now(dt.UTC)
    assert _keys_for(wdir / "known_hosts", "bentoo-lab") == [_key_fields(WORKER_HOST_KEY)]
    assert workers.load_registry(wdir / "workers.json") == {"bentoo-lab": entry}
    # R4.4: exactly one proof, and the pin was on disk when it ran; the proof's transport
    # is built from the paths complete() wrote, carrying the pinned fingerprint
    assert len(proof.entries) == 1
    target = proof.entries[0]
    assert isinstance(target, remote.Remote)
    assert (target.name, target.address) == ("bentoo-lab", "192.168.15.7")
    assert target.known_hosts == wdir / "known_hosts"
    assert target.key == wdir / "id_ed25519"
    assert target.expected_fingerprint == _fingerprint(WORKER_HOST_KEY)
    assert any(
        ln.split()[:3] == ["bentoo-lab", *_key_fields(WORKER_HOST_KEY)]
        for ln in proof.known_hosts_then
    )
    assert "bentoo-lab" in proof.registry_then


def test_complete_pins_under_the_pairings_name_never_the_hostname_the_worker_claims(
    wdir: Path, proof: _Proof
) -> None:
    """Hostile: the welcome says "shidashi-worker", the pairing is "bentoo-lab"; the
    welcome lists 10.9.9.9, the host connected to 192.168.15.7 (R2.8)."""
    paired = _paired(name="bentoo-lab", ip="192.168.15.7", addresses=("10.9.9.9",))
    entry = _complete(paired, wdir)
    assert entry.name == "bentoo-lab"
    assert entry.address == "192.168.15.7"
    assert _keys_for(wdir / "known_hosts", "shidashi-worker") == []
    assert set(workers.load_registry(wdir / "workers.json")) == {"bentoo-lab"}
    assert proof.entries[0].address == "192.168.15.7"


def test_complete_an_unnamed_pairing_is_recorded_under_the_workers_hostname(
    wdir: Path, proof: _Proof
) -> None:
    """pair_with already named it after the hostname; complete keeps that name."""
    entry = _complete(_paired(name="spare-box", hostname="spare-box"), wdir)
    assert entry.name == "spare-box"
    assert _keys_for(wdir / "known_hosts", "spare-box") == [_key_fields(WORKER_HOST_KEY)]


def test_complete_replaces_a_names_entry_and_key_and_keeps_a_neighbour_on_the_same_ip(
    wdir: Path, proof: _Proof
) -> None:
    old_key = _ed25519_line("bentoo-lab-before-reinstall")
    neighbour_key = _ed25519_line("lab2")
    neighbour = workers.WorkerEntry(
        name="lab2",
        address="192.168.15.7",  # DHCP gave lab2 the address bentoo-lab now has
        host_key=neighbour_key,
        host_key_fingerprint=_fingerprint(neighbour_key),
        paired_at="2026-10-01T09:00:00+00:00",
        cpu_flags=("avx2",),
        image="20261001T0900",
    )
    stale = neighbour.model_copy(
        update={
            "name": "bentoo-lab",
            "address": "192.168.15.3",
            "host_key": old_key,
            "host_key_fingerprint": _fingerprint(old_key),
        }
    )
    workers.save_registry(wdir / "workers.json", {"bentoo-lab": stale, "lab2": neighbour})
    (wdir / "known_hosts").write_text(f"bentoo-lab {old_key}\nlab2 {neighbour_key}\n")

    entry = _complete(_paired(), wdir)

    registry = workers.load_registry(wdir / "workers.json")
    assert set(registry) == {"bentoo-lab", "lab2"}
    assert registry["bentoo-lab"] == entry
    assert registry["lab2"] == neighbour
    assert _keys_for(wdir / "known_hosts", "bentoo-lab") == [_key_fields(WORKER_HOST_KEY)]
    assert _keys_for(wdir / "known_hosts", "lab2") == [_key_fields(neighbour_key)]


def test_complete_pins_a_trusted_pairing_the_same_way(wdir: Path, proof: _Proof) -> None:
    """R7.5: trust on first use -- the key the welcome carries is the one pinned."""
    entry = _complete(_paired(trusted=True), wdir)
    assert entry.host_key_fingerprint == _fingerprint(WORKER_HOST_KEY)
    assert _keys_for(wdir / "known_hosts", "bentoo-lab") == [_key_fields(WORKER_HOST_KEY)]
    assert len(proof.entries) == 1


@pytest.mark.parametrize("failure", ["unreachable", "mismatch"])
def test_complete_does_not_report_paired_when_the_proof_fails(
    wdir: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    error: BaseException
    if failure == "unreachable":
        error = remote.RemoteUnreachable("bentoo-lab", "192.168.15.7")
    else:
        error = remote.HostKeyMismatch(
            "bentoo-lab", _fingerprint(WORKER_HOST_KEY), _fingerprint(_ed25519_line("x"))
        )
    fake = _Proof(wdir, error)
    monkeypatch.setattr(remote, "check", fake)
    monkeypatch.setattr(kyomei, "check", fake, raising=False)
    clock = _Clock()
    with pytest.raises(kyomei.PairingNotProven) as err:
        _complete(_paired(), wdir, clock=clock, sleep=clock.sleep)
    assert "bentoo-lab" in workers.load_registry(wdir / "workers.json")  # the entry stays
    if failure == "mismatch":
        assert len(fake.entries) == 1  # a changed key is never retried
    else:
        assert len(fake.entries) > 1  # retried while sshd may still be starting...
        assert clock.now - 1000.0 >= 15.0  # ...for the whole window, on the fake clock
        assert clock.now - 1000.0 < 17.0
    assert "not proven" in str(err.value).lower()


def test_complete_reads_the_proof_window_constant_per_call(
    wdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _Proof(wdir, remote.RemoteUnreachable("bentoo-lab", "192.168.15.7"))
    monkeypatch.setattr(remote, "check", fake)
    monkeypatch.setattr(kyomei, "check", fake, raising=False)
    monkeypatch.setattr(kyomei, "PROOF_WINDOW", 3.0)
    clock = _Clock()
    with pytest.raises(kyomei.PairingNotProven):
        _complete(_paired(), wdir, clock=clock, sleep=clock.sleep)
    assert 3.0 <= clock.now - 1000.0 < 5.0


def test_complete_waits_for_a_worker_whose_sshd_is_still_starting(
    wdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[Any] = []

    def _check(entry: Any, *_a: Any, **_kw: Any) -> None:
        attempts.append(entry)
        if len(attempts) == 1:
            raise remote.RemoteUnreachable("bentoo-lab", "192.168.15.7", "connection refused")

    monkeypatch.setattr(remote, "check", _check)
    monkeypatch.setattr(kyomei, "check", _check, raising=False)
    clock = _Clock()
    entry = _complete(_paired(), wdir, clock=clock, sleep=clock.sleep)
    assert entry.name == "bentoo-lab"
    assert len(attempts) == 2
    assert clock.sleeps and sum(clock.sleeps) < 15.0


def test_complete_raises_a_pairing_error_and_pins_nothing_when_ssh_keygen_fails(
    wdir: Path, proof: _Proof
) -> None:
    with pytest.raises(kyomei.PairingError) as err:
        _complete(_paired(), wdir, runner=_Keygen(rc=255))
    assert "not a public key" in str(err.value)
    assert not (wdir / "known_hosts").exists()
    assert workers.load_registry(wdir / "workers.json") == {}
    assert proof.entries == []


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH's ssh-keygen")
def test_complete_fingerprints_with_the_real_ssh_keygen(wdir: Path, proof: _Proof) -> None:
    """Fidelity: the default runner and OpenSSH's own output format."""
    entry = kyomei.complete(_paired(), wdir / "workers.json")
    assert entry.host_key_fingerprint == _fingerprint(WORKER_HOST_KEY)


# ===================================================================================
# contract -- task 6.1: the host's real client against the worker's real session
# ===================================================================================


@pytest.fixture
def kw(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The worker's module, imported from the rootfs as the image runs it."""
    monkeypatch.syspath_prepend(str(WORKER_LIB))
    for name in ("kyomei_worker", "kyomei_protocol", "worker_disk"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("kyomei_worker")


def _session(kw: Any, trust: Any = None) -> Any:
    return kw.WorkerSession(
        host_key=WORKER_HOST_KEY,
        hostname="shidashi-worker",
        addresses=("192.168.15.7", "10.0.0.4"),
        cpu_flags=("avx2", "bmi2", "sse4_2"),
        image="20261006T1200",
        trust=trust,
        code_factory=lambda: CODE,
    )


def _opener_for(session: Any, peer: str, sent: list[bytes]) -> Callable[..., Any]:
    """An in-memory HTTP hop: the request body goes straight to ``session.handle``."""

    def _open(req: Any, data: Any = None, timeout: Any = None, **_k: Any) -> Any:
        body = getattr(req, "data", None) or data
        sent.append(body)
        status, answer, headers = session.handle(body, peer)
        if status != 200:
            raise _http_error(getattr(req, "full_url", ""), status, dict(headers))
        return _Response(status, answer)

    return _open


def test_contract_every_field_crosses_from_the_worker_to_the_registry(
    worker_key: Path, wdir: Path, proof: _Proof, kw: Any
) -> None:
    session = _session(kw)
    sent: list[bytes] = []
    # mDNS answer -> choose -> Target
    found = _found("shidashi-worker", "192.168.15.7")
    target = kyomei.choose([found], ask=_never, confirm=_Asker(True))
    assert (target.address, target.port, target.name) == (found.address, found.port, found.name)

    paired = kyomei.pair_with(
        target,
        worker_key,
        code="k7m4-q2xp",
        name="bentoo-lab",
        opener=_opener_for(session, "192.168.15.5", sent),
    )

    # hello -> Granted: the key and the name the host sent land on the worker
    assert len(sent) == 1
    hello = json.loads(sent[0])["payload"]
    granted = session.result
    assert granted is not None
    assert _key_fields(granted.key) == _key_fields(hello["authorized_key"])
    assert _key_fields(granted.key) == _key_fields(GRANTED_KEY)
    assert granted.name == "bentoo-lab"
    assert granted.trusted is False
    assert granted.peer == "192.168.15.5"

    # welcome -> Paired -> WorkerEntry: every field the worker sends is one the host keeps
    welcome = paired.welcome
    assert welcome.hostname == "bentoo-lab"  # the worker took the name it was given
    assert tuple(welcome.addresses) == ("192.168.15.7", "10.0.0.4")
    entry = _complete(paired, wdir)
    assert entry.name == "bentoo-lab"
    assert entry.address == target.address  # where the host connected (R2.8)
    assert _key_fields(entry.host_key) == _key_fields(welcome.host_key)
    assert _key_fields(entry.host_key) == _key_fields(WORKER_HOST_KEY)
    assert entry.host_key_fingerprint == _fingerprint(WORKER_HOST_KEY)
    assert tuple(entry.cpu_flags) == tuple(welcome.cpu_flags) == ("avx2", "bmi2", "sse4_2")
    assert entry.image == welcome.image == "20261006T1200"

    # what story 010 reads from the registry
    assert {"name", "address", "host_key", "host_key_fingerprint", "cpu_flags"} <= set(
        workers.WorkerEntry.model_fields
    )
    rem = remote.Remote.for_worker(entry)
    assert (rem.name, rem.address) == ("bentoo-lab", "192.168.15.7")
    assert rem.expected_fingerprint == _fingerprint(WORKER_HOST_KEY)


def test_contract_the_hosts_trust_param_is_what_the_worker_trusts(
    worker_key: Path, wdir: Path, proof: _Proof, kw: Any, tmp_path: Path
) -> None:
    """Derived value: the parameter the host renders, parsed by the worker's own copy of
    the protocol, admits this host's key from this host's address -- and nothing else."""
    worker_protocol = sys.modules["kyomei_protocol"]
    trust = worker_protocol.parse_trust(
        "quiet " + kyomei.trust_param(worker_key, "192.168.15.5") + "\n"
    )

    # hostile first: the same comment on another key, from the trusted address
    impostor = tmp_path / "impostor" / "id_ed25519"
    impostor.parent.mkdir()
    impostor.write_text("private\n")
    (impostor.parent / "id_ed25519.pub").write_text(
        _ed25519_line("impostor", "shidashi-worker-key") + "\n"
    )
    refused = _session(kw, trust)
    with pytest.raises(kyomei.PairingRefused) as err:
        kyomei.pair_with(
            _target("192.168.15.7"),
            impostor,
            code=None,
            name=None,
            opener=_opener_for(refused, "192.168.15.5", []),
        )
    assert err.value.status == 403
    assert refused.result is None

    # hostile: the right key from another address
    elsewhere = _session(kw, trust)
    with pytest.raises(kyomei.PairingRefused):
        kyomei.pair_with(
            _target("192.168.15.7"),
            worker_key,
            code=None,
            name=None,
            opener=_opener_for(elsewhere, "192.168.15.50", []),
        )
    assert elsewhere.result is None

    # the plain case
    session = _session(kw, trust)
    paired = kyomei.pair_with(
        _target("192.168.15.7"),
        worker_key,
        code=None,
        name=None,
        opener=_opener_for(session, "192.168.15.5", []),
    )
    assert paired.trusted is True
    assert paired.name == "shidashi-worker"
    assert session.result is not None and session.result.trusted is True
    entry = _complete(paired, wdir)
    assert entry.host_key_fingerprint == _fingerprint(WORKER_HOST_KEY)
