"""Tests of the v4 hardening of kyomei (story 009, task 7).

Sections, each selected by its ``-k`` keyword (every test name carries exactly one of
them; no test name or parameter id carries two):

* ``protocol`` -- task 7.1: ``derive_key`` is scrypt (N=2^15, r=8, p=1, 32 bytes, salt
                  ``shidashi-kyomei-v1``) and ``CODE_TTL == 600``, in both copies (R2.11,
                  R2.12, R3.5);
* ``worker``   -- task 7.2: a code expires after ``CODE_TTL`` and a new one is drawn
                  without a failure, a 500 discards the code, the console block is 0600
                  (R2.5, R2.12, R2.13);
* ``host``     -- task 7.3: ``--trust-param --address`` routes towards the worker, and a
                  trusted re-pin of a known name with another key is confirmed first (R7.6,
                  R7.8, R7.9).

Expected values are computed here with ``hashlib.scrypt`` directly, never with the module
under test. Each scrypt derivation costs ~45 ms and 32 MiB, so the known answers are cached
and the tests derive as few keys as they can.

The fakes are the ones the v3 tests use: the worker's ``_Listening``/``_Runner``/``_Clock``
(tests/test_kyomei_worker.py) and the host's ``_Harness`` (tests/test_cli_kyomei.py).
"""

import datetime as dt
import functools
import hashlib
import importlib
import importlib.util
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import config, kyomei, workers
from shidashi import kyomei_protocol as P
from shidashi.cli import app
from tests.test_cli_kyomei import (
    WORKER_HOST_KEY as WELCOME_KEY,
)
from tests.test_cli_kyomei import (
    _ed25519_line,
    _fingerprint,
    _Harness,
    _out,
)
from tests.test_kyomei_worker import (
    CODE,
    CODE2,
    HOST,
    OTHER_CODE,
    WORKER_LIB,
    _authorized_keys,
    _Clock,
    _dnssd,
    _hello,
    _key_fields,
    _Listening,
    _Runner,
    _session,
    _worker_root,
)

ROOT_COPY = WORKER_LIB / "kyomei_protocol.py"
SALT = b"shidashi-kyomei-v1"
REAL_DEFAULT_ADDRESS = kyomei.default_address  # before any harness replaces it


@functools.cache
def _scrypt(code: str) -> bytes:
    """The design's K for a normalized code (R2.11), computed with hashlib directly."""
    return hashlib.scrypt(
        code.encode(), salt=SALT, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32
    )


# ===================================================================================
# protocol -- task 7.1: scrypt and the code's lifetime (R2.11, R2.12, R3.5)
# ===================================================================================


def test_protocol_derive_key_is_scrypt_with_the_pinned_parameters() -> None:
    key = P.derive_key(CODE)
    assert isinstance(key, bytes)
    assert len(key) == 32
    assert key == _scrypt(CODE)
    # hostile: neither the v3 key (one SHA-256) nor scrypt under the v3 NUL-ended tag
    assert key != hashlib.sha256(SALT + b"\x00" + CODE.encode()).digest()


def test_protocol_scrypt_keys_stay_apart_for_near_codes_and_agree_for_one_code_typed_twice() -> (
    None
):
    # hostile half 1: codes one character apart must never share a key
    assert P.derive_key(OTHER_CODE) == _scrypt(OTHER_CODE)
    assert _scrypt(OTHER_CODE) != _scrypt(CODE)
    # hostile half 2: the same code, typed another way, is the same key
    assert P.derive_key(P.normalize_code("k7m4 q2xp")) == _scrypt(CODE)
    # plain: the key is a pure function of the code
    assert P.derive_key(CODE) == P.derive_key(CODE)


def test_protocol_code_ttl_is_ten_minutes() -> None:
    assert P.CODE_TTL == 600.0
    assert isinstance(P.CODE_TTL, float)


def test_protocol_the_rootfs_copy_derives_the_same_scrypt_key_and_ttl() -> None:
    """The worker image's copy, loaded alone as it runs there, agrees with the design."""
    spec = importlib.util.spec_from_file_location("kyomei_protocol_rootfs_copy_v4", ROOT_COPY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.derive_key(CODE) == _scrypt(CODE)
    assert module.CODE_TTL == 600.0


# ===================================================================================
# worker -- task 7.2: the code's lifetime, a new code after a 500, 0600 (R2.5, R2.12, R2.13)
# ===================================================================================


@pytest.fixture
def kw(monkeypatch: pytest.MonkeyPatch) -> Any:
    """kyomei_worker imported from the rootfs, beside its own protocol copy."""
    monkeypatch.syspath_prepend(str(WORKER_LIB))
    for name in ("kyomei_worker", "kyomei_protocol", "worker_disk"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("kyomei_worker")


def test_worker_code_expires_after_code_ttl_and_a_new_one_is_drawn_without_a_failure(
    kw: Any,
) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock)  # the default lifetime: P.CODE_TTL
    assert session.code == CODE
    clock.now += 599.9
    assert session.expired() is False
    clock.now += 0.1  # exactly CODE_TTL after it was drawn
    assert session.expired() is True
    assert session.failures == 0  # expiring is not a failure
    assert session.locked_until is None
    session.rotate()  # what the listener does on expiry
    assert session.code == CODE2
    assert session.failures == 0
    assert session.expired() is False


def test_worker_session_honours_a_shortened_code_ttl(kw: Any) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock, code_ttl=5.0)
    clock.now += 4.9
    assert session.expired() is False
    clock.now += 0.1
    assert session.expired() is True


# Hostile halves first: expiry must not fire on a code that is locked, paired or just
# drawn (a rotation restarts the lifetime), nor be postponed by a refused guess.


def test_worker_a_locked_session_never_reads_as_expired(kw: Any) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock)
    for _ in range(3):
        session.handle(_hello(OTHER_CODE), HOST)
    assert session.locked_until is not None
    clock.now += 700
    assert session.expired() is False  # the lock's own rotation applies, not expiry


def test_worker_a_paired_session_never_reads_as_expired(kw: Any) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock)
    assert session.handle(_hello(CODE), HOST)[0] == 200
    clock.now += 700
    assert session.expired() is False


def test_worker_a_rotation_restarts_the_code_lifetime(kw: Any) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock)
    for _ in range(3):
        session.handle(_hello(OTHER_CODE), HOST)
    clock.now += 30  # the lockout passed: the listener rotates
    assert session.lock_passed()
    session.rotate()
    clock.now += 599  # 629 s after the first code, 599 s after this one
    assert session.expired() is False
    clock.now += 1
    assert session.expired() is True


def test_worker_a_refused_guess_does_not_postpone_expiry(kw: Any) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock)
    clock.now += 599
    assert session.handle(_hello(OTHER_CODE), HOST)[0] == 403
    clock.now += 1
    assert session.expired() is True


def test_worker_the_expired_code_no_longer_verifies_once_rotated(kw: Any) -> None:
    clock = _Clock()
    session = _session(kw, clock=clock)
    clock.now += 600
    assert session.expired() is True
    session.rotate()
    assert session.handle(_hello(CODE), HOST)[0] == 403  # the expired code is gone
    assert session.handle(_hello(CODE2), HOST)[0] == 200


@pytest.fixture
def listening(tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Factory: ``listening(root=None, clock=None, code_ttl=None, **options)``.

    ``clock`` and ``code_ttl`` reach the WorkerSession that listen builds, so the
    lifetime runs on a fake clock and nothing waits ten minutes.
    """
    started: list[_Listening] = []

    def _make(
        root: Path | None = None,
        *,
        clock: _Clock | None = None,
        code_ttl: float | None = None,
        **options: Any,
    ) -> _Listening:
        real = kw.WorkerSession

        def _session_factory(*args: Any, **kwargs: Any) -> Any:
            if clock is not None:
                kwargs["clock"] = clock
            if code_ttl is not None:
                kwargs["code_ttl"] = code_ttl
            return real(*args, **kwargs)

        monkeypatch.setattr(kw, "WorkerSession", _session_factory)
        lst = _Listening(kw, root or _worker_root(tmp_path), monkeypatch, **options)
        started.append(lst.start())
        return lst

    yield _make
    for lst in started:  # a failed test must not leave a listener behind
        if lst.thread.is_alive():
            for server in lst.servers:
                server.server_close()


def test_worker_listen_shows_a_new_code_once_the_code_expires(listening: Any) -> None:
    clock = _Clock()
    lst = listening(clock=clock, code_ttl=5.0)
    first = lst.code()
    assert first is not None
    clock.now += 5.0
    lst.wait(lambda: lst.code() not in (None, first), "no new code after the code expired")
    second = lst.code()
    assert second is not None and second != first
    assert lst.thread.is_alive() and lst.accepts()  # the window stays open
    assert _dnssd(lst.root).exists()
    assert lst.post(_hello(first))[0] == 403  # the expired code is gone
    assert lst.post(_hello(second))[0] == 200
    lst.join()


def test_worker_listen_refuses_the_old_code_after_a_500_and_pairs_the_new_one(
    tmp_path: Path, listening: Any
) -> None:
    """R2.13: the hello that failed to install crossed the LAN; its code must die with it."""
    root = _worker_root(tmp_path)
    lst = listening(root, runner=_Runner(root, fail={"sshd -t": 1}))
    old = lst.code()
    assert old is not None
    assert lst.post(_hello(old))[0] == 500
    lst.wait(lambda: lst.code() not in (None, old), "no new code after the 500", seconds=5)
    new = lst.code()
    assert new is not None and new != old
    assert lst.post(_hello(old))[0] == 403  # a fresh hello under the old code
    assert lst.post(_hello(new))[0] == 200
    lst.join()
    blobs = [_key_fields(ln) for ln in _authorized_keys(root).read_text().splitlines()]
    assert len(blobs) == 1


def test_worker_console_block_stays_root_only_when_it_replaces_an_older_block(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _worker_root(tmp_path)
    runner = _Runner(root)
    monkeypatch.setattr(subprocess, "run", runner)
    issue = root / "run" / "issue.d" / "50-shidashi-kyomei.issue"
    issue.parent.mkdir(parents=True)
    issue.write_text("an older block, world-readable\n")
    issue.chmod(0o644)
    kw.console_show("K7M4-Q2XP  bentoo-lab", root=root, runner=runner)
    assert stat.S_IMODE(issue.stat().st_mode) == 0o600
    kw.console_show("M4K7-XPQ2  bentoo-lab", root=root, runner=runner)  # a re-show
    assert stat.S_IMODE(issue.stat().st_mode) == 0o600
    text = issue.read_text()
    assert "M4K7-XPQ2" in text
    assert "K7M4-Q2XP" not in text and "older block" not in text


# ===================================================================================
# host -- task 7.3: --trust-param --address and the trusted re-pin (R7.6, R7.8, R7.9)
# ===================================================================================

cli_runner = CliRunner()
NAME = "shidashi-worker"  # what the fake welcome names itself when no --name is given
# hostile: another sshd key under the SAME comment as the welcome's -- a different key
OLD_KEY = _ed25519_line("an-older-sshd-key", "root@shidashi-worker")


def _fake_ip(src: str | None, calls: list[list[str]]) -> Callable[..., Any]:
    """``ip -4 route get`` answering ``src`` (or no route); anything else runs for real."""
    real = subprocess.run

    def _run(argv: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(x) for x in argv]
        if argv[:1] != ["ip"]:
            return real(argv, *a, **kw)
        calls.append(argv)
        if src is None:
            return subprocess.CompletedProcess(argv, 2, "", "RTNETLINK answers: unreachable\n")
        route = f"{argv[-1]} via 10.0.0.1 dev wg0 src {src} uid 1000 \n    cache \n"
        return subprocess.CompletedProcess(argv, 0, route, "")

    return _run


def test_host_default_address_asks_the_route_towards_the_destination_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_ip("192.168.15.5", calls))
    assert kyomei.default_address() == "192.168.15.5"
    assert calls[-1] == ["ip", "-4", "route", "get", "1.1.1.1"]  # the default route
    assert kyomei.default_address(dest="10.8.0.2") == "192.168.15.5"
    assert calls[-1] == ["ip", "-4", "route", "get", "10.8.0.2"]


@pytest.mark.parametrize("dest", [None, "10.8.0.2"], ids=["default-route", "towards-dest"])
def test_host_default_address_without_a_route_names_the_address_option(
    monkeypatch: pytest.MonkeyPatch, dest: str | None
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_ip(None, []))
    with pytest.raises(kyomei.PairingError) as caught:
        if dest is None:
            kyomei.default_address()
        else:
            kyomei.default_address(dest=dest)
    assert "--address" in str(caught.value)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("COLUMNS", "200")
    return {"wdir": tmp_path / "xdg" / "shidashi" / "worker"}


@pytest.fixture
def existing_key(env: dict[str, Path]) -> Path:
    wdir = env["wdir"]
    wdir.mkdir(parents=True)
    key = wdir / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nexisting\n-----END\n")
    key.chmod(0o600)
    (wdir / "id_ed25519.pub").write_text(_ed25519_line("existing-key", "shidashi") + "\n")
    return key


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, env: dict[str, Path]) -> _Harness:
    return _Harness(monkeypatch)


def _kyomei(*args: str, input: str = "") -> Any:
    return cli_runner.invoke(app, ["kyomei", *args], input=input)


def _routes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace default_address by a recorder; returns the destinations it was asked."""
    asked: list[str] = []
    sources = {"1.1.1.1": "192.168.15.5", "10.8.0.2": "10.8.0.1"}

    def _route(dest: str = "1.1.1.1") -> str:
        asked.append(dest)
        return sources[dest]

    monkeypatch.setattr(kyomei, "default_address", _route)
    return asked


@pytest.mark.parametrize("address", ["10.8.0.2:9000", "10.8.0.2"], ids=["with-port", "bare"])
def test_host_trust_param_with_an_address_routes_towards_it(
    harness: _Harness, existing_key: Path, monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    asked = _routes(monkeypatch)
    result = _kyomei("--trust-param", "--address", address)
    out = _out(result)
    assert result.exit_code == 0, out
    assert asked == ["10.8.0.2"]  # its IPv4 only, never the port
    pub = (existing_key.parent / "id_ed25519.pub").read_text()
    assert f"shidashi.trust=10.8.0.1,{_fingerprint(pub)}" in out.split()
    assert not harness.discovered()
    assert harness.pair_calls == []


def test_host_trust_param_without_an_address_keeps_the_default_route(
    harness: _Harness, existing_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard (passes on v3): no --address is still the default route."""
    asked = _routes(monkeypatch)
    result = _kyomei("--trust-param")
    out = _out(result)
    assert result.exit_code == 0, out
    assert asked == ["1.1.1.1"]
    assert "shidashi.trust=192.168.15.5," in out


@pytest.mark.parametrize("args", [(), ("--address", "10.8.0.2")], ids=["bare", "with-address"])
def test_host_trust_param_without_a_route_exits_1_naming_the_address_option(
    harness: _Harness,
    existing_key: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    monkeypatch.setattr(kyomei, "default_address", REAL_DEFAULT_ADDRESS)
    monkeypatch.setattr(subprocess, "run", _fake_ip(None, []))
    result = _kyomei("--trust-param", *args)
    out = _out(result)
    assert result.exit_code == 1, out
    assert "--address" in out
    assert "Traceback" not in out
    assert "shidashi.trust=" not in out


# The trusted re-pin (R7.8). Hostile halves first: another key under the same comment IS
# another key (asked); the same key under another comment is NOT (not asked); a known
# entry under another name at the same address is not a replacement (not asked).


def _registered(wdir: Path, *entries: tuple[str, str, str]) -> None:
    """Record (name, address, host key) entries and pin their keys, as a pairing would."""
    registry: dict[str, workers.WorkerEntry] = {}
    for name, address, key in entries:
        registry[name] = workers.WorkerEntry(
            name=name,
            address=address,
            host_key=key,
            host_key_fingerprint=_fingerprint(key),
            paired_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC).isoformat(timespec="seconds"),
            cpu_flags=("avx2",),
            image="20261001T1200",
        )
        workers.pin(wdir / "known_hosts", name, key)
    workers.save_registry(wdir / "workers.json", registry)


def _registry() -> dict[str, workers.WorkerEntry]:
    return workers.load_registry(config.workers_dir() / "workers.json")


@pytest.mark.parametrize("answer", ["n\n", "\n", ""], ids=["no", "enter", "eof"])
def test_host_a_trusted_repin_of_a_known_name_with_another_key_asks_and_a_no_pins_nothing(
    harness: _Harness, existing_key: Path, env: dict[str, Path], answer: str
) -> None:
    _registered(env["wdir"], (NAME, "192.168.15.9", OLD_KEY))
    known_hosts = (env["wdir"] / "known_hosts").read_text()
    result = _kyomei("--trusted", "--address", "192.168.15.6", input=answer)
    out = _out(result)
    assert result.exit_code == 1, out
    assert "[y/N]" in out  # asked, and "no" is the default
    assert _fingerprint(OLD_KEY) in out  # the person sees what would be replaced
    assert "Traceback" not in out
    assert _registry()[NAME].host_key == OLD_KEY
    assert (env["wdir"] / "known_hosts").read_text() == known_hosts
    assert harness.proofs == []


def test_host_a_trusted_repin_of_a_known_name_confirmed_with_yes_replaces_the_pin(
    harness: _Harness, existing_key: Path, env: dict[str, Path]
) -> None:
    _registered(env["wdir"], (NAME, "192.168.15.9", OLD_KEY))
    result = _kyomei("--trusted", "--address", "192.168.15.6", input="y\n")
    out = _out(result)
    assert result.exit_code == 0, out
    assert "[y/N]" in out
    entry = _registry()[NAME]
    assert _key_fields(entry.host_key) == _key_fields(WELCOME_KEY)
    assert entry.address == "192.168.15.6"
    pinned = [ln for ln in (env["wdir"] / "known_hosts").read_text().splitlines() if ln]
    assert pinned == [f"{NAME} {' '.join(_key_fields(WELCOME_KEY))}"]
    assert len(harness.proofs) == 1


@pytest.mark.parametrize(
    ("args", "answers"),
    [
        (("--trusted", "--name", NAME, "--address", "192.168.15.6"), ""),
        (("--address", "192.168.15.6"), "k7m4-q2xp\n"),
    ],
    ids=["named", "code-mode"],
)
def test_host_a_repin_with_a_name_or_a_code_asks_nothing(
    harness: _Harness,
    existing_key: Path,
    env: dict[str, Path],
    args: tuple[str, ...],
    answers: str,
) -> None:
    """Guard (passes on v3): --name or a code already says which machine is meant."""
    _registered(env["wdir"], (NAME, "192.168.15.9", OLD_KEY))
    result = _kyomei(*args, input=answers)
    out = _out(result)
    assert result.exit_code == 0, out
    assert "[y/N]" not in out
    assert _key_fields(_registry()[NAME].host_key) == _key_fields(WELCOME_KEY)


def test_host_a_trusted_repin_of_the_same_key_under_another_comment_asks_nothing(
    harness: _Harness, existing_key: Path, env: dict[str, Path]
) -> None:
    """Guard (passes on v3): the same key is no replacement, whatever its comment."""
    same = " ".join([*_key_fields(WELCOME_KEY), "root@another-comment"])
    _registered(env["wdir"], (NAME, "192.168.15.6", same))
    result = _kyomei("--trusted", "--address", "192.168.15.6")
    out = _out(result)
    assert result.exit_code == 0, out
    assert "[y/N]" not in out


def test_host_a_trusted_pairing_beside_another_name_at_the_same_address_asks_nothing(
    harness: _Harness, existing_key: Path, env: dict[str, Path]
) -> None:
    """Guard (passes on v3): a replacement is by name; another name is left alone."""
    _registered(env["wdir"], ("bentoo-lab", "192.168.15.6", OLD_KEY))
    result = _kyomei("--trusted", "--address", "192.168.15.6")
    out = _out(result)
    assert result.exit_code == 0, out
    assert "[y/N]" not in out
    registry = _registry()
    assert set(registry) == {"bentoo-lab", NAME}
    assert registry["bentoo-lab"].host_key == OLD_KEY
