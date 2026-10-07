"""The kyomei pairing protocol -- its wire format and cryptography, and nothing else.

kyomei (共鳴, "resonance") pairs a worker with a host. The worker announces itself and
shows a one-time code on its console; the host sends a HELLO carrying the key it wants
granted, and the worker answers with a WELCOME carrying its sshd host key and what it
is. In code mode both messages carry an HMAC under a key derived from the code, so
neither end accepts the other's message without the person having read the code off
the worker's screen. The key comes from scrypt and a code lives at most ``CODE_TTL``
seconds: an impostor that answers a hello can only guess offline, slowly, and against
a code that is replaced before the search ends. In trusted mode (the worker booted with
``shidashi.trust=<host IPv4>,<host key fingerprint>``) the MAC is ``null`` and the
worker checks the peer's address and key fingerprint instead.

Both travel as ``{"payload": {...}, "mac": hex | null}``. The MAC covers
``kind + NUL + canonical(payload)``, so a hello never verifies as a welcome.

This module is pure (no I/O) and imports only the standard library: it ships twice,
byte-for-byte, in the ``shidashi`` package (host) and in the worker image's rootfs
under ``/usr/local/lib/shidashi/`` (worker), where no third-party package exists.
"""

import base64
import binascii
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from collections.abc import Mapping
from typing import Any, Literal

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
CODE_LEN = 8
MAX_BODY = 16384
PORT = 8765
SERVICE = "_shidashi-kyomei._tcp"
HOSTNAME_RE = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"  # one RFC 1123 label, lower case
#: How long the worker shows one code before drawing another, in seconds.
CODE_TTL = 600.0

_KEY_SALT = b"shidashi-kyomei-v1"
#: scrypt's cost: 32 MiB and ~45 ms per derivation -- once per code here, once per guess
#: for anyone trying codes offline against a captured or solicited hello.
_SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
_KEY_TYPE = "ssh-ed25519"
_MAC_RE = re.compile(r"[0-9a-f]{64}")
_LOOKALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})
_LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")
_TRUST_PARAM = "shidashi.trust"
_TRUST_FP_RE = re.compile(r"SHA256:[A-Za-z0-9+/_-]{43}")

_HELLO_FIELDS = frozenset({"v", "mode", "nonce", "authorized_key", "name"})
_WELCOME_FIELDS = frozenset(
    {"v", "nonce", "worker_nonce", "host_key", "hostname", "addresses", "cpu_flags", "image"}
)


class ProtocolError(Exception):
    """A message (or the trust parameter) that is not the protocol's shape."""


@dataclasses.dataclass(frozen=True)
class Hello:
    """Host -> worker: the key the host wants granted, and the name it offers."""

    mode: Literal["code", "trusted"]
    nonce: str
    authorized_key: str
    name: str | None


@dataclasses.dataclass(frozen=True)
class Welcome:
    """Worker -> host: the host's nonce echoed, the worker's sshd host key and facts."""

    nonce: str
    worker_nonce: str
    host_key: str
    hostname: str
    addresses: tuple[str, ...]
    cpu_flags: tuple[str, ...]
    image: str


@dataclasses.dataclass(frozen=True)
class Trust:
    """The ``shidashi.trust`` boot parameter: the one host paired without a code."""

    address: str
    fingerprint: str


# --- the code and its key ------------------------------------------------------------


def new_code() -> str:
    """Eight random alphabet characters (40 bits)."""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def format_code(code: str) -> str:
    """The code as shown on the worker's console: ``XXXX-XXXX``."""
    return f"{code[:4]}-{code[4:]}"


def normalize_code(text: str) -> str:
    """The code as typed -> its canonical form; ``ValueError`` when it cannot be one.

    Case and dashes/spaces are ignored, and Crockford's lookalikes read as the digit
    they look like (O -> 0, I and L -> 1). Anything else outside the alphabet (U
    included) is refused, never guessed.
    """
    code = text.upper().replace("-", "").replace(" ", "").translate(_LOOKALIKES)
    if len(code) != CODE_LEN or any(c not in ALPHABET for c in code):
        raise ValueError(f"a code is {CODE_LEN} characters of {ALPHABET}")
    return code


def derive_key(code: str) -> bytes:
    """The MAC key ``K`` for a normalized code: scrypt, so offline guessing is costly."""
    return hashlib.scrypt(code.encode(), salt=_KEY_SALT, **_SCRYPT)


# --- encoding and MAC ----------------------------------------------------------------


def canonical(obj: Mapping[str, object]) -> bytes:
    """The one byte encoding both ends MAC: sorted keys, compact, ASCII only."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def mac(key: bytes, kind: str, payload: Mapping[str, object]) -> str:
    """HMAC-SHA256 over ``kind``, a NUL and the canonical payload, as hex."""
    return hmac.new(key, kind.encode() + b"\x00" + canonical(payload), "sha256").hexdigest()


def verify(key: bytes, kind: str, payload: Mapping[str, object], tag: str) -> bool:
    """Whether ``tag`` is this payload's MAC of this kind under ``key`` (constant time)."""
    return hmac.compare_digest(mac(key, kind, payload).encode(), tag.encode())


def new_nonce() -> str:
    """Sixteen random bytes in unpadded base64url."""
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()


def fingerprint(key_line: str) -> str:
    """OpenSSH's ``SHA256:`` fingerprint of a public key line (``ssh-keygen -lf``)."""
    blob = base64.b64decode(key_line.split()[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


# --- parsers -------------------------------------------------------------------------


def parse_hello(body: bytes) -> tuple[Hello, str | None]:
    """A hello body -> the hello and its tag; ``ProtocolError`` on any other shape."""
    payload, tag = _envelope(body, _HELLO_FIELDS, "hello")
    mode = payload["mode"]
    if mode not in ("code", "trusted"):
        raise ProtocolError("hello: mode is neither 'code' nor 'trusted'")
    if mode == "code" and tag is None:
        raise ProtocolError("hello: a code-mode hello carries a MAC")
    if mode == "trusted" and tag is not None:
        raise ProtocolError("hello: a trusted-mode hello carries no MAC")
    name = payload["name"]
    if name is not None:
        name = _label(name, "hello: name")
    hello = Hello(
        mode=mode,
        nonce=_nonce(payload["nonce"], "hello: nonce"),
        authorized_key=_ed25519(payload["authorized_key"], "hello: authorized_key"),
        name=name,
    )
    return hello, tag


def parse_welcome(body: bytes) -> tuple[Welcome, str | None]:
    """A welcome body -> the welcome and its tag; ``ProtocolError`` on any other shape.

    Whether a ``null`` tag is acceptable depends on the hello it answers, which only
    the caller knows: a code-mode caller treats it as unauthenticated.
    """
    payload, tag = _envelope(body, _WELCOME_FIELDS, "welcome")
    addresses = payload["addresses"]
    if not isinstance(addresses, list) or not addresses:
        raise ProtocolError("welcome: addresses is not a non-empty list")
    cpu_flags = payload["cpu_flags"]
    if not isinstance(cpu_flags, list) or not all(isinstance(f, str) for f in cpu_flags):
        raise ProtocolError("welcome: cpu_flags is not a list of strings")
    image = payload["image"]
    if not isinstance(image, str):
        raise ProtocolError("welcome: image is not a string")
    welcome = Welcome(
        nonce=_nonce(payload["nonce"], "welcome: nonce"),
        worker_nonce=_nonce(payload["worker_nonce"], "welcome: worker_nonce"),
        host_key=_ed25519(payload["host_key"], "welcome: host_key"),
        hostname=_label(payload["hostname"], "welcome: hostname"),
        addresses=tuple(_lan_ipv4(a) for a in addresses),
        cpu_flags=tuple(cpu_flags),
        image=image,
    )
    return welcome, tag


def parse_trust(cmdline: str) -> Trust | None:
    """The ``shidashi.trust`` parameter of a kernel command line, if it is there.

    ``None`` when absent; ``ProtocolError`` naming the reason when present but not
    ``<IPv4>,SHA256:<43 base64 characters>``. The last occurrence wins, as the
    kernel's own parameters do.
    """
    value = None
    for word in cmdline.split():
        param, sep, rest = word.partition("=")
        if sep and param == _TRUST_PARAM:
            value = rest
    if value is None:
        return None
    if not value:
        raise ProtocolError(f"{_TRUST_PARAM} is empty; expected <IPv4>,SHA256:<fingerprint>")
    address, comma, fp = value.partition(",")
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        parsed = None
    if parsed is None or str(parsed) != address:
        raise ProtocolError(f"{_TRUST_PARAM}: the host address is not a plain IPv4 address")
    if not comma or not _TRUST_FP_RE.fullmatch(fp):
        raise ProtocolError(
            f"{_TRUST_PARAM}: the host key fingerprint is not SHA256:<43 base64 characters>"
        )
    return Trust(address=address, fingerprint=fp)


def _envelope(body: bytes, fields: frozenset[str], kind: str) -> tuple[dict[str, Any], str | None]:
    """The common checks: size, JSON, the two top-level keys, version, exact fields."""
    if len(body) > MAX_BODY:
        raise ProtocolError(f"{kind}: body over {MAX_BODY} bytes")
    try:
        doc = json.loads(body.decode("utf-8"))
    except ValueError as err:  # UnicodeDecodeError and JSONDecodeError alike
        raise ProtocolError(f"{kind}: body is not JSON") from err
    if not isinstance(doc, dict) or set(doc) != {"payload", "mac"}:
        raise ProtocolError(f"{kind}: body is not {{payload, mac}}")
    payload, tag = doc["payload"], doc["mac"]
    if not isinstance(payload, dict):
        raise ProtocolError(f"{kind}: payload is not an object")
    if tag is not None and not (isinstance(tag, str) and _MAC_RE.fullmatch(tag)):
        raise ProtocolError(f"{kind}: mac is neither null nor a hex HMAC-SHA256")
    if set(payload) != fields:
        raise ProtocolError(f"{kind}: payload fields are not {sorted(fields)}")
    version = payload["v"]
    if type(version) is not int or version != 1:
        raise ProtocolError(f"{kind}: unknown protocol version")
    return payload, tag


def _nonce(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ProtocolError(f"{what} is not a nonce")
    return value


def _label(value: object, what: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(HOSTNAME_RE, value):
        raise ProtocolError(f"{what} is not an RFC 1123 label")
    return value


def _ed25519(value: object, what: str) -> str:
    """A single-line ``ssh-ed25519 <base64> [comment]`` whose blob decodes."""
    if not isinstance(value, str) or "\n" in value or not value.startswith(_KEY_TYPE + " "):
        raise ProtocolError(f"{what} is not an ssh-ed25519 public key")
    fields = value.split()
    try:
        base64.b64decode(fields[1], validate=True)
    except (IndexError, binascii.Error) as err:
        raise ProtocolError(f"{what} is not an ssh-ed25519 public key") from err
    return value


def _lan_ipv4(value: object) -> str:
    """A byte-exact dotted IPv4 outside 127.0.0.0/8 (no prefix, no leading zeros)."""
    if not isinstance(value, str):
        raise ProtocolError("welcome: an address is not a string")
    try:
        parsed = ipaddress.IPv4Address(value)
    except ValueError as err:
        raise ProtocolError("welcome: an address is not a plain IPv4 address") from err
    if str(parsed) != value or parsed in _LOOPBACK:
        raise ProtocolError("welcome: an address is not a plain IPv4 outside loopback")
    return value
