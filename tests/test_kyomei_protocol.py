"""Tests of shidashi.kyomei_protocol -- the pairing's wire format and cryptography (v3).

The module is pure (no I/O) and stdlib-only, and it ships twice byte-for-byte: in the
``shidashi`` package (host) and in the worker image's rootfs (worker). Expected values
are computed here from the design's formulas with ``hashlib``/``hmac`` directly, so a
test never checks the module against itself.

v3 direction: the HELLO goes host -> worker (``mode``, ``nonce``, ``authorized_key``,
``name``), the WELCOME goes worker -> host (``nonce`` echoed, ``worker_nonce``,
``host_key``, ``hostname``, ``addresses``, ``cpu_flags``, ``image``). Both travel as
``{"payload": ..., "mac": hex | null}``: a hex MAC in code mode, ``null`` in trusted
mode. ``parse_hello``/``parse_welcome`` return ``(message, tag)``.

Selection: ``-k 'not copy'`` is task 1.1; ``-k copy`` is task 1.2 (unchanged from v2).

Requirements exercised: R1.6 (code alphabet, dash/case), R1.12 (RFC 1123 names),
R2.2 (MAC binding, kind separation), R2.6 (body cap and strict shape), R2.7 (plain
IPv4 outside loopback), R7.1 and R7.4 (the ``shidashi.trust`` parameter), R3.5 (the
worker copy).
"""

import base64
import dataclasses
import hashlib
import hmac
import importlib.util
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

from shidashi import kyomei_protocol as P


def _repo_root() -> Path:
    """The checkout: the first ancestor holding pyproject.toml and variants/worker."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


ROOT = _repo_root()
HOST_COPY = ROOT / "shidashi" / "kyomei_protocol.py"
WORKER_COPY = ROOT / "variants/worker/rootfs/usr/local/lib/shidashi/kyomei_protocol.py"

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE = "K7M4Q2XP"


def _ed25519_line(seed: str, comment: str = "k") -> str:
    """A syntactically valid ``ssh-ed25519`` public key line (deterministic)."""
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    """OpenSSH's form: SHA256 of the key blob, standard base64 without padding."""
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _key(code: str = CODE) -> bytes:
    return hashlib.sha256(b"shidashi-kyomei-v1\x00" + code.encode()).digest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _mac(key: bytes, kind: str, payload: Any) -> str:
    return hmac.new(key, kind.encode() + b"\x00" + _canonical(payload), "sha256").hexdigest()


HOST_NONCE = "AAECAwQFBgcICQoLDA0ODw"
WORKER_NONCE = "q83vEjRWeJASNFZ4kBI0Vg"
GRANTED_KEY = _ed25519_line("host-worker-key", "shidashi-worker-key")
WORKER_HOST_KEY = _ed25519_line("worker-sshd-host-key", "root@shidashi-worker")


def _hello_payload(**over: Any) -> dict[str, Any]:
    """Host -> worker: the key the host wants granted."""
    payload: dict[str, Any] = {
        "v": 1,
        "mode": "code",
        "nonce": HOST_NONCE,
        "authorized_key": GRANTED_KEY,
        "name": "bentoo-lab",
    }
    payload.update(over)
    return payload


def _welcome_payload(**over: Any) -> dict[str, Any]:
    """Worker -> host: its sshd host key and what it is."""
    payload: dict[str, Any] = {
        "v": 1,
        "nonce": HOST_NONCE,
        "worker_nonce": WORKER_NONCE,
        "host_key": WORKER_HOST_KEY,
        "hostname": "bentoo-lab",
        "addresses": ["192.168.15.6"],
        "cpu_flags": ["avx2", "bmi2", "sse4_2"],
        "image": "20261006T1200",
    }
    payload.update(over)
    return payload


def _body(payload: dict[str, Any], kind: str, *, key: bytes | None = None) -> bytes:
    """A code-mode message: the payload and its hex MAC under ``key``."""
    return json.dumps({"payload": payload, "mac": _mac(key or _key(), kind, payload)}).encode()


def _trusted(payload: dict[str, Any]) -> bytes:
    """A trusted-mode message: the payload and a null MAC."""
    return json.dumps({"payload": payload, "mac": None}).encode()


# --- constants -------------------------------------------------------------------------


def test_the_protocol_constants() -> None:
    assert P.ALPHABET == CROCKFORD
    assert P.CODE_LEN == 8
    assert P.MAX_BODY == 16384
    assert P.PORT == 8765
    assert P.SERVICE == "_shidashi-kyomei._tcp"


# --- the code (R1.6) -------------------------------------------------------------------


def test_new_code_draws_eight_alphabet_characters_and_never_repeats_itself() -> None:
    codes = [P.new_code() for _ in range(300)]
    for code in codes:
        assert len(code) == 8
        assert set(code) <= set(CROCKFORD), code
    # 40 bits: 300 draws colliding would mean a broken source of randomness
    assert len(set(codes)) == len(codes)


def test_format_code_shows_two_groups_of_four() -> None:
    assert P.format_code(CODE) == "K7M4-Q2XP"


# Hostile halves first: codes that LOOK alike but are different stay different, then
# codes typed differently that ARE the same collapse to one -- then the plain case.


@pytest.mark.parametrize(
    ("typed", "other"),
    [
        ("K7M4-Q2XP", "K7M4-Q2XR"),  # one character apart
        ("K7M4-Q2XP", "Q2XP-K7M4"),  # same characters, other order
        ("V7M4-Q2XP", "W7M4-Q2XP"),  # neighbours in the alphabet
    ],
)
def test_normalize_code_keeps_near_identical_codes_apart(typed: str, other: str) -> None:
    assert P.normalize_code(typed) != P.normalize_code(other)
    assert P.derive_key(P.normalize_code(typed)) != P.derive_key(P.normalize_code(other))


@pytest.mark.parametrize(
    "typed",
    ["K7M4-Q2XP", "k7m4-q2xp", "K7M4Q2XP", "k7m4q2xp", "K7m4-q2Xp", " k7m4 q2xp ", "K7M4 Q2XP"],
)
def test_normalize_code_accepts_the_code_with_or_without_dash_in_either_case(typed: str) -> None:
    assert P.normalize_code(typed) == CODE


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("O0OO-0000", "00000000"),  # O reads as zero
        ("o0oo-0000", "00000000"),
        ("I1L1-il11", "11111111"),  # I and L read as one
    ],
)
def test_normalize_code_reads_crockford_lookalikes_as_their_digit(
    typed: str, canonical: str
) -> None:
    assert P.normalize_code(typed) == canonical


@pytest.mark.parametrize(
    "typed",
    [
        "K7M4-Q2XU",  # U is not in Crockford's alphabet: never silently mapped to V
        "K7M4-Q2X",  # seven characters
        "K7M4-Q2XPP",  # nine characters
        "K7M4-Q2X!",  # punctuation
        "K7M4_Q2XP",  # an underscore is not a dash
        "",
    ],
)
def test_normalize_code_refuses_what_is_not_eight_alphabet_characters(typed: str) -> None:
    with pytest.raises(ValueError):
        P.normalize_code(typed)


def test_derive_key_is_sha256_of_the_domain_tag_and_the_code() -> None:
    key = P.derive_key(CODE)
    assert isinstance(key, bytes)
    assert key == _key(CODE)
    assert len(key) == 32


# --- canonical encoding and MAC (R2.2) -------------------------------------------------


def test_canonical_sorts_keys_compacts_and_escapes_non_ascii() -> None:
    assert P.canonical({"b": 1, "a": "é", "c": [1, 2]}) == b'{"a":"\\u00e9","b":1,"c":[1,2]}'


def test_canonical_differs_when_any_value_differs_but_not_when_only_key_order_does() -> None:
    first = {"nonce": "abc", "hostname": "lab"}
    # hostile: one character apart must encode apart (else a MAC covers two payloads)
    assert P.canonical(first) != P.canonical({"nonce": "abd", "hostname": "lab"})
    assert P.canonical(first) != P.canonical({"nonce": "ABC", "hostname": "lab"})
    # hostile converse: the same mapping built in another order encodes the same
    assert P.canonical(first) == P.canonical({"hostname": "lab", "nonce": "abc"})


@pytest.mark.parametrize("kind", ["hello", "welcome"])
def test_mac_is_hmac_sha256_over_the_kind_a_nul_and_the_canonical_payload(kind: str) -> None:
    payload = _hello_payload() if kind == "hello" else _welcome_payload()
    assert P.mac(_key(), kind, payload) == _mac(_key(), kind, payload)


def test_a_hello_mac_never_verifies_as_a_welcome_nor_the_reverse() -> None:
    payload = _welcome_payload()
    hello_tag = P.mac(_key(), "hello", payload)
    welcome_tag = P.mac(_key(), "welcome", payload)
    assert hello_tag != welcome_tag
    assert P.verify(_key(), "welcome", payload, hello_tag) is False
    assert P.verify(_key(), "hello", payload, welcome_tag) is False
    assert P.verify(_key(), "hello", payload, hello_tag) is True
    assert P.verify(_key(), "welcome", payload, welcome_tag) is True


@pytest.mark.parametrize(
    "case",
    ["other code", "tampered payload", "truncated tag", "garbage tag", "empty tag"],
)
def test_verify_refuses_every_tag_that_was_not_made_for_this_key_and_payload(case: str) -> None:
    payload = _hello_payload()
    tag = _mac(_key(), "hello", payload)
    key = _key()
    if case == "other code":
        key = _key("K7M4Q2XR")
    elif case == "tampered payload":
        payload = _hello_payload(name="impostor")
    elif case == "truncated tag":
        tag = tag[:-2]
    elif case == "garbage tag":
        tag = "zz" * 32
    else:
        tag = ""
    assert P.verify(key, "hello", payload, tag) is False


def test_new_nonce_is_sixteen_random_bytes_in_base64url() -> None:
    nonces = [P.new_nonce() for _ in range(200)]
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    for nonce in nonces:
        assert isinstance(nonce, str)
        assert set(nonce) <= allowed, nonce
        assert len(base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4))) == 16
    assert len(set(nonces)) == len(nonces)


# --- fingerprint (shared by the host's trust_param and the worker's trusted check) ----
# Hostile halves first: two DIFFERENT keys under the same comment never share a
# fingerprint; the SAME key under another comment (or none) always does.


def test_fingerprint_tells_two_keys_with_the_same_comment_apart() -> None:
    one = _ed25519_line("key-one", "shidashi-worker-key")
    two = _ed25519_line("key-two", "shidashi-worker-key")
    assert P.fingerprint(one) != P.fingerprint(two)


def test_fingerprint_is_the_same_for_one_key_under_any_comment() -> None:
    bare = " ".join(GRANTED_KEY.split()[:2])
    assert P.fingerprint(bare) == P.fingerprint(GRANTED_KEY)
    assert P.fingerprint(bare + " another comment") == P.fingerprint(GRANTED_KEY)
    assert P.fingerprint(GRANTED_KEY + "\n") == P.fingerprint(GRANTED_KEY)


def test_fingerprint_matches_openssh_sha256_form() -> None:
    """Known answer: SHA256 of the key blob, base64 without padding (ssh-keygen -lf)."""
    assert P.fingerprint(GRANTED_KEY) == _fingerprint(GRANTED_KEY)
    assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", P.fingerprint(GRANTED_KEY))


# --- names (R1.12) ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["bentoo-lab", "a", "0lab", "a" * 63, "lab-2"])
def test_hostname_re_accepts_rfc_1123_labels(name: str) -> None:
    assert re.fullmatch(P.HOSTNAME_RE, name)


@pytest.mark.parametrize(
    "name", ["Bentoo-Lab", "bad host", "lab,evil", "-lab", "lab-", "a" * 64, "", "lab.local"]
)
def test_hostname_re_refuses_what_is_not_an_rfc_1123_label(name: str) -> None:
    assert not re.fullmatch(P.HOSTNAME_RE, name)


# --- parse_hello (host -> worker) -------------------------------------------------------


def test_parse_hello_returns_a_code_mode_hello_and_its_tag() -> None:
    payload = _hello_payload()
    body = _body(payload, "hello")
    hello, tag = P.parse_hello(body)
    assert tag == json.loads(body)["mac"]
    assert hello.mode == "code"
    assert hello.nonce == HOST_NONCE
    assert hello.authorized_key == GRANTED_KEY
    assert hello.name == "bentoo-lab"
    with pytest.raises(dataclasses.FrozenInstanceError):
        hello.nonce = "other"  # type: ignore[misc]


def test_parse_hello_returns_a_trusted_hello_with_no_tag() -> None:
    hello, tag = P.parse_hello(_trusted(_hello_payload(mode="trusted")))
    assert tag is None
    assert hello.mode == "trusted"
    assert hello.authorized_key == GRANTED_KEY


def test_parse_hello_accepts_an_unnamed_hello() -> None:
    hello, _tag = P.parse_hello(_body(_hello_payload(name=None), "hello"))
    assert hello.name is None


def test_parse_hello_accepts_a_body_of_exactly_max_body_bytes() -> None:
    body = _body(_hello_payload(), "hello")
    padded = body + b" " * (16384 - len(body))  # JSON allows trailing whitespace
    assert len(padded) == 16384
    assert P.parse_hello(padded)[0].name == "bentoo-lab"


def _malformed_hellos() -> dict[str, bytes]:
    good = _hello_payload()
    body = _body(good, "hello")

    def with_payload(**over: Any) -> bytes:
        return _body(_hello_payload(**over), "hello")

    missing = dict(good)
    del missing["nonce"]
    extra_top = json.loads(body)
    extra_top["extra"] = 1
    no_mac = json.loads(body)
    del no_mac["mac"]
    return {
        "one byte over the cap": body + b" " * (16385 - len(body)),
        "not json": b"payload=1&mac=2",
        "not utf-8": b"\xff\xfe\x00{",
        "a json list": b"[1, 2]",
        "a payload that is not an object": json.dumps({"payload": [1], "mac": None}).encode(),
        "no mac key": json.dumps(no_mac).encode(),
        "a missing field": _body(missing, "hello"),
        "an extra payload field": with_payload(admin=True),
        "an extra top-level key": json.dumps(extra_top).encode(),
        "an unknown version": with_payload(v=2),
        "a string version": with_payload(v="1"),
        "an unknown mode": with_payload(mode="admin"),
        "an rsa key": with_payload(authorized_key="ssh-rsa AAAAB3NzaC1yc2E k"),
        # hostile lookalikes of the ed25519 prefix
        "an ed25519 certificate": with_payload(
            authorized_key="ssh-ed25519-cert-v01@openssh.com AAAAIHNzaC1lZDI1NTE5 k"
        ),
        "no space after the key type": with_payload(authorized_key="ssh-ed25519AAAAC3Nza k"),
        "a nonce that is not a string": with_payload(nonce=12345),
        "a code-mode hello with a null mac": json.dumps(
            {"payload": _hello_payload(), "mac": None}
        ).encode(),
        "a trusted-mode hello carrying a mac": _body(_hello_payload(mode="trusted"), "hello"),
    }


@pytest.mark.parametrize("case", sorted(_malformed_hellos()))
def test_parse_hello_refuses_every_body_that_is_not_the_protocols_shape(case: str) -> None:
    with pytest.raises(P.ProtocolError):
        P.parse_hello(_malformed_hellos()[case])


@pytest.mark.parametrize(
    "name",
    ["bad host", "lab,evil", "Bentoo-Lab", "-lab", "lab-", "a" * 64, "", "lab\nother", "lab\n"],
)
def test_parse_hello_refuses_a_name_that_is_not_an_rfc_1123_label(name: str) -> None:
    with pytest.raises(P.ProtocolError):
        P.parse_hello(_body(_hello_payload(name=name), "hello"))


# --- parse_welcome (worker -> host) ------------------------------------------------------


def test_parse_welcome_returns_every_field_and_its_tag() -> None:
    payload = _welcome_payload(addresses=["192.168.15.6", "10.0.0.4"])
    body = _body(payload, "welcome")
    welcome, tag = P.parse_welcome(body)
    assert tag == json.loads(body)["mac"]
    assert welcome.nonce == HOST_NONCE
    assert welcome.worker_nonce == WORKER_NONCE
    assert welcome.host_key == WORKER_HOST_KEY
    assert welcome.hostname == "bentoo-lab"
    assert tuple(welcome.addresses) == ("192.168.15.6", "10.0.0.4")
    assert tuple(welcome.cpu_flags) == ("avx2", "bmi2", "sse4_2")
    assert welcome.image == "20261006T1200"
    with pytest.raises(dataclasses.FrozenInstanceError):
        welcome.hostname = "other"  # type: ignore[misc]


def test_parse_welcome_returns_a_trusted_welcome_with_no_tag() -> None:
    welcome, tag = P.parse_welcome(_trusted(_welcome_payload()))
    assert tag is None
    assert welcome.host_key == WORKER_HOST_KEY


def _malformed_welcomes() -> dict[str, bytes]:
    good = _welcome_payload()
    body = _body(good, "welcome")

    def with_payload(**over: Any) -> bytes:
        return _body(_welcome_payload(**over), "welcome")

    missing = dict(good)
    del missing["worker_nonce"]
    no_mac = json.loads(body)
    del no_mac["mac"]
    return {
        "one byte over the cap": body + b" " * (16385 - len(body)),
        "not json": b"<html>502 Bad Gateway</html>",
        "no mac key": json.dumps(no_mac).encode(),
        "a mac that is not a string": json.dumps({"payload": good, "mac": 7}).encode(),
        "an extra field": with_payload(sudo=True),
        "a missing field": _body(missing, "welcome"),
        "an unknown version": with_payload(v=2),
        "an rsa host key": with_payload(host_key="ssh-rsa AAAAB3NzaC1yc2E k"),
        "an ed25519 certificate host key": with_payload(
            host_key="ssh-ed25519-cert-v01@openssh.com AAAAIHNzaC1lZDI1NTE5 k"
        ),
        "a null hostname": with_payload(hostname=None),
        "addresses not a list": with_payload(addresses="192.168.15.6"),
        "cpu_flags not a list": with_payload(cpu_flags="avx2 bmi2"),
        "an image that is not a string": with_payload(image=20261006),
    }


@pytest.mark.parametrize("case", sorted(_malformed_welcomes()))
def test_parse_welcome_refuses_every_body_that_is_not_the_protocols_shape(case: str) -> None:
    with pytest.raises(P.ProtocolError):
        P.parse_welcome(_malformed_welcomes()[case])


@pytest.mark.parametrize(
    "hostname",
    ["bad host", "lab,evil", "Bentoo-Lab", "-lab", "lab-", "a" * 64, "", "lab\nother"],
)
def test_parse_welcome_refuses_a_hostname_that_is_not_an_rfc_1123_label(hostname: str) -> None:
    """The hostname becomes a known_hosts first field and a HostKeyAlias (R1.12)."""
    with pytest.raises(P.ProtocolError):
        P.parse_welcome(_body(_welcome_payload(hostname=hostname), "welcome"))


# Addresses (R2.7) -- hostile halves first: values that LOOK like a plain LAN IPv4 but
# are not (a prefix, a leading zero ssh would read as octal, the 127/8 block beyond
# 127.0.0.1) are refused; then values that merely CONTAIN "127" are plain addresses.


@pytest.mark.parametrize(
    "addresses",
    [
        ["192.168.15.6/24"],
        ["192.168.015.6"],  # inet_aton reads 015 as octal 13: another machine
        ["127.0.0.1"],
        ["127.1.2.3"],  # the whole 127.0.0.0/8 is loopback
        ["192.168.15.6", "127.0.0.1"],
        ["not-an-ip"],
        ["256.1.1.1"],
        ["::1"],
        ["fe80::1"],
        [" 192.168.15.6"],
        [],  # at least one address
    ],
)
def test_parse_welcome_refuses_an_address_that_is_not_a_plain_ipv4_outside_loopback(
    addresses: list[str],
) -> None:
    with pytest.raises(P.ProtocolError):
        P.parse_welcome(_body(_welcome_payload(addresses=addresses), "welcome"))


def test_parse_welcome_accepts_addresses_that_only_contain_127() -> None:
    payload = _welcome_payload(addresses=["10.127.0.1", "192.168.127.1", "128.0.0.1"])
    welcome, _tag = P.parse_welcome(_body(payload, "welcome"))
    assert tuple(welcome.addresses) == ("10.127.0.1", "192.168.127.1", "128.0.0.1")


def test_a_hello_body_is_never_accepted_as_a_welcome_nor_the_reverse() -> None:
    with pytest.raises(P.ProtocolError):
        P.parse_welcome(_body(_hello_payload(), "hello"))
    with pytest.raises(P.ProtocolError):
        P.parse_hello(_body(_welcome_payload(), "welcome"))


# --- parse_trust (R7.1, R7.4) ------------------------------------------------------------


def _fingerprint_with_plus_and_slash() -> tuple[str, str]:
    """A key whose OpenSSH fingerprint carries both '+' and '/' (standard base64)."""
    for i in range(10_000):
        line = _ed25519_line(f"trusted-host-{i}", "shidashi-worker-key")
        fp = _fingerprint(line)
        if "+" in fp and "/" in fp:
            return line, fp
    raise RuntimeError("no fingerprint with '+' and '/' in 10000 seeds")


TRUSTED_LINE, TRUSTED_FP = _fingerprint_with_plus_and_slash()


@pytest.mark.parametrize(
    "cmdline",
    [
        "",
        "BOOT_IMAGE=/vmlinuz root=live:CDLABEL=BENTOO rd.live.image quiet splash\n",
        # hostile lookalikes: other parameters whose names merely contain ours
        f"myshidashi.trust=192.168.15.5,{TRUSTED_FP}",
        f"shidashi.trusted=192.168.15.5,{TRUSTED_FP}",
        f"shidashi.trust.v2=192.168.15.5,{TRUSTED_FP}",
    ],
)
def test_parse_trust_is_none_when_the_parameter_is_absent(cmdline: str) -> None:
    assert P.parse_trust(cmdline) is None


@pytest.mark.parametrize(
    "template",
    [
        "shidashi.trust={value}",
        "BOOT_IMAGE=/vmlinuz quiet shidashi.trust={value} splash",
        "root=live:CDLABEL=BENTOO shidashi.trust={value}\n",  # /proc/cmdline ends in \n
    ],
)
def test_parse_trust_reads_the_address_and_the_fingerprint_anywhere_on_the_line(
    template: str,
) -> None:
    trust = P.parse_trust(template.format(value=f"192.168.15.5,{TRUSTED_FP}"))
    assert trust is not None
    assert trust.address == "192.168.15.5"
    # byte-identical: the worker compares it with fingerprint(authorized_key)
    assert trust.fingerprint == TRUSTED_FP
    assert trust.fingerprint == P.fingerprint(TRUSTED_LINE)


def test_parse_trust_keeps_the_fingerprints_letter_case() -> None:
    """Hostile: base64 is case-sensitive -- an upper-cased fingerprint is another key."""
    trust = P.parse_trust(f"shidashi.trust=192.168.15.5,{TRUSTED_FP}")
    assert trust is not None
    assert trust.fingerprint != TRUSTED_FP.upper()
    assert trust.fingerprint == TRUSTED_FP


def test_parse_trust_accepts_a_loopback_or_any_plain_ipv4_host() -> None:
    trust = P.parse_trust(f"shidashi.trust=127.0.0.1,{TRUSTED_FP}")
    assert trust is not None and trust.address == "127.0.0.1"


_BODY43 = TRUSTED_FP.removeprefix("SHA256:")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", ""),  # present but empty: any reason
        ("192.168.15.5", "fingerprint"),  # no comma, no fingerprint
        (f"bentoo-host,{TRUSTED_FP}", "address"),  # a name, not an IPv4
        (f"192.168.15.5/24,{TRUSTED_FP}", "address"),
        (f"192.168.015.5,{TRUSTED_FP}", "address"),
        (f"fe80::1,{TRUSTED_FP}", "address"),
        (f"192.168.15.5,{_BODY43}", "fingerprint"),  # no SHA256: prefix
        (f"192.168.15.5,MD5:{_BODY43}", "fingerprint"),
        (f"192.168.15.5,SHA256:{_BODY43[:-1]}", "fingerprint"),  # 42 characters
        (f"192.168.15.5,SHA256:{_BODY43}A", "fingerprint"),  # 44 characters
        (f"192.168.15.5,SHA256:{_BODY43}=", "fingerprint"),  # padded
        (f"192.168.15.5,SHA256:{_BODY43[:-1]}!", "fingerprint"),
    ],
)
def test_parse_trust_names_the_reason_when_the_parameter_is_malformed(
    value: str, reason: str
) -> None:
    with pytest.raises(P.ProtocolError) as err:
        P.parse_trust(f"quiet shidashi.trust={value} splash")
    message = str(err.value).lower()
    assert message.strip(), "the console must say why the parameter was ignored (R7.4)"
    words = {"address": ("address", "ipv4"), "fingerprint": ("fingerprint", "sha256"), "": ("",)}
    assert any(word in message for word in words[reason]), message


# --- the module itself ---------------------------------------------------------------


def test_the_module_imports_only_the_standard_library() -> None:
    """The worker image has Python 3.14 and no third-party package."""
    import ast

    tree = ast.parse(HOST_COPY.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import cannot run from the worker's rootfs"
            imported.add((node.module or "").split(".")[0])
    imported.discard("__future__")
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


# --- the worker's copy (task 1.2, unchanged from v2) ----------------------------------


def test_the_worker_copy_is_byte_identical_to_the_host_module() -> None:
    assert WORKER_COPY.is_file(), f"missing worker copy: {WORKER_COPY}"
    assert WORKER_COPY.read_bytes() == HOST_COPY.read_bytes(), (
        "the worker's kyomei_protocol.py drifted from the host's; re-sync with: "
        f"cp {HOST_COPY.relative_to(ROOT)} {WORKER_COPY.relative_to(ROOT)}"
    )


def test_the_worker_copy_loads_standalone_without_the_shidashi_package() -> None:
    """On the worker the module sits alone under /usr/local/lib/shidashi/."""
    spec = importlib.util.spec_from_file_location("kyomei_protocol_worker_copy", WORKER_COPY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.normalize_code("k7m4-q2xp") == CODE
    assert module.derive_key(CODE) == _key(CODE)
