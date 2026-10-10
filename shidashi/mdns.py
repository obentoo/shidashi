"""Find the workers announced on the LAN: one DNS-SD browse over multicast DNS.

A worker waiting to be paired announces ``<hostname>._shidashi-kyomei._tcp.local``
through systemd-resolved. :func:`browse` sends ONE PTR query to 224.0.0.251:5353 from
an ephemeral port with the QU bit set, so responders answer by unicast to that port
(RFC 6762 "legacy unicast"); it never binds UDP 5353, which resolved or avahi may hold
and which would need no less than the whole mDNS stack to share. systemd-resolved
answers such a query with the PTR alone, so the SRV, TXT and A are then asked of the
machine that answered, by unicast (bentoo-lab, 2026-10-07).

A provisioned worker announces ``<N>._shidashi-worker._tcp.local`` instead; :func:`find`
is the same browse over that service, narrowed to the one instance named N.

:func:`build_query` and :func:`parse_answers` are pure. Every packet is untrusted
input: :func:`parse_answers` bounds every length and every compression chain and
returns what it could read -- never raises, never loops.
"""

import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shidashi.kyomei_protocol import SERVICE

MDNS_GROUP = ("224.0.0.251", 5353)
# A worker booted with a provisioned identity announces ``<N>._shidashi-worker._tcp``
# (story 020) -- a service of its own, never the pairing service.
WORKER_SERVICE = "_shidashi-worker._tcp.local"

_PTR, _A, _TXT, _SRV = 12, 1, 16, 33
_CLASS_IN_QU = 0x8001  # class IN with the "unicast response" bit
_QR = 0x8000  # the header bit that makes a packet a response
_MAX_HOPS = 16  # compression pointers followed per name
_MAX_NAME = 255
_RECV_SIZE = 9000
_FOLLOW_UP_WAIT = 1.0  # seconds a peer gets to answer the SRV/TXT/A questions


class _Malformed(Exception):
    """Inside the parser only: the packet stops making sense here."""


@dataclass(frozen=True)
class Found:
    """One announced worker: instance name, IPv4 address, port and TXT pairs."""

    name: str
    address: str
    port: int
    txt: tuple[tuple[str, str], ...]


def build_query(service: str = SERVICE) -> bytes:
    """One PTR question for ``<service>.local``, ID 0, class IN with the QU bit."""
    return _query([(f"{service}.local", _PTR)])


def _query(questions: list[tuple[str, int]]) -> bytes:
    header = struct.pack(">HHHHHH", 0, 0, len(questions), 0, 0, 0)
    body = b"".join(_encode_name(n) + struct.pack(">HH", t, _CLASS_IN_QU) for n, t in questions)
    return header + body


def parse_answers(packet: bytes, service: str = SERVICE) -> list[Found]:
    """The instances of ``service`` a response announces; ``[]`` when it is not one.

    PTR -> SRV -> TXT -> A are matched by name (case-insensitively) across the answer
    and additional sections. An instance whose SRV target has no A record cannot be
    dialed and is skipped. A packet that breaks off mid-way yields what came before.
    """
    return _assemble(_response_records(packet), service)


def _response_records(packet: bytes) -> list[tuple[str, int, object]]:
    """The records of a response; ``[]`` for anything that is not one."""
    if len(packet) < 12:
        return []
    _ident, flags, qd, an, ns, ar = struct.unpack_from(">HHHHHH", packet)
    if not flags & _QR:
        return []
    return _records(packet, qd, an + ns + ar)


def _instances(records: list[tuple[str, int, object]], service: str) -> list[str]:
    """The full instance names the PTR records of ``service`` point at."""
    owner = f"{service}.local".lower()
    return [
        rdata
        for name, rtype, rdata in records
        if rtype == _PTR and name == owner and isinstance(rdata, str)
        and rdata.endswith("." + owner)
    ]  # fmt: skip


def _assemble(records: list[tuple[str, int, object]], service: str) -> list[Found]:
    """PTR -> SRV -> TXT -> A, matched by name; an instance without an address is skipped."""
    owner = f"{service}.local".lower()
    found: list[Found] = []
    for instance in _instances(records, service):
        srv = next((r for n, t, r in records if t == _SRV and n == instance), None)
        if not isinstance(srv, tuple):
            continue
        port, target = srv
        address = next((r for n, t, r in records if t == _A and n == target), None)
        if not isinstance(address, str) or port == 0:
            continue
        txt = next((r for n, t, r in records if t == _TXT and n == instance), ())
        label = instance[: -len(owner) - 1]
        found.append(Found(label, address, port, txt if isinstance(txt, tuple) else ()))
    return found


def browse(
    service: str,
    wait: float,
    *,
    sock_factory: Callable[..., Any] = socket.socket,
) -> list[Found]:
    """Query once, collect answers for ``wait`` seconds; deduplicated, sorted by name.

    The socket is bound to the address the multicast route leaves from, not to
    0.0.0.0: the query goes out of that one interface and the unicast answers come
    back to it, so nothing works less, and the port stays out of reach of the
    host's container and VM bridges. ``OSError`` when the host has no route.
    """
    sock = sock_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        address = _route_source(sock_factory)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        sock.bind((address, 0))
        sock.sendto(build_query(service), MDNS_GROUP)
        deadline = time.monotonic() + wait
        seen: dict[tuple[str, str, int], Found] = {}
        complete: dict[str, set[str]] = {}  # peer -> instances it answered in full
        pending: dict[str, list[str]] = {}  # peer -> instances it named by PTR alone
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                packet, peer = sock.recvfrom(_RECV_SIZE)
            except TimeoutError:
                break
            records = _response_records(packet)
            for item in _assemble(records, service):  # each packet on its own
                seen.setdefault((item.name, item.address, item.port), item)
                complete.setdefault(str(peer[0]), set()).add(_full(item.name, service))
            pending.setdefault(str(peer[0]), []).extend(_instances(records, service))
        for peer_ip, named in pending.items():
            missing = sorted(set(named) - complete.get(peer_ip, set()))
            for item in _resolve(sock, peer_ip, missing, service) if missing else ():
                seen.setdefault((item.name, item.address, item.port), item)
    finally:
        sock.close()
    return sorted(seen.values(), key=lambda f: (f.name, f.address, f.port))


def find(
    name: str,
    *,
    timeout: float,
    sock_factory: Callable[..., Any] = socket.socket,
) -> str | None:
    """The IPv4 address of the worker announced as ``name``; ``None`` when none answers.

    One :func:`browse` of :data:`WORKER_SERVICE` for ``timeout`` seconds. The instance
    name must equal ``name`` exactly, compared case-insensitively as DNS labels are: a
    worker whose name merely resembles it is another worker. ``OSError`` (no route to
    the mDNS group, a socket failure) is the caller's to report.
    """
    # browse() appends ".local" itself; passing it here would ask for "….local.local"
    service = WORKER_SERVICE.removesuffix(".local")
    wanted = name.lower()
    for item in browse(service, timeout, sock_factory=sock_factory):
        if item.name.lower() == wanted:
            return item.address
    return None


def _route_source(sock_factory: Callable[..., Any]) -> str:
    """The local address the kernel sends to the mDNS group from. I/O, no packet sent.

    A UDP ``connect`` only looks the route up. It is done on a socket of its own:
    connected, the browse socket would drop the unicast answers from the peers.
    """
    probe = sock_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(MDNS_GROUP)
        return str(probe.getsockname()[0])
    finally:
        probe.close()


def _full(name: str, service: str) -> str:
    return f"{name}.{service}.local".lower()


def _resolve(sock: Any, peer: str, instances: list[str], service: str) -> list[Found]:
    """Complete instances a peer named by PTR alone: systemd-resolved answers a
    legacy-unicast PTR query with the PTR only, so ask that peer -- by unicast, so two
    machines sharing a hostname keep their own address -- for each instance's SRV and
    TXT, then the A of each SRV target. Only that peer's follow-up answers are used."""
    owner = f"{service}.local".lower()
    records: list[tuple[str, int, object]] = [(owner, _PTR, i) for i in instances]
    records += _ask(sock, peer, [(i, rtype) for i in instances for rtype in (_SRV, _TXT)])
    targets = {r[1] for _n, t, r in records if t == _SRV and isinstance(r, tuple)}
    known = {n for n, t, _r in records if t == _A}
    if targets - known:
        records += _ask(sock, peer, [(t, _A) for t in sorted(targets - known)])
    return _assemble(records, service)


def _ask(sock: Any, peer: str, questions: list[tuple[str, int]]) -> list[tuple[str, int, object]]:
    """Send ``questions`` to ``peer``:5353 and keep what that peer answers within a second."""
    sock.sendto(_query(questions), (peer, 5353))
    deadline = time.monotonic() + _FOLLOW_UP_WAIT
    out: list[tuple[str, int, object]] = []
    while (left := deadline - time.monotonic()) > 0:
        sock.settimeout(left)
        try:
            packet, source = sock.recvfrom(_RECV_SIZE)
        except TimeoutError:
            break
        if str(source[0]) == peer:
            out.extend(_response_records(packet))
            if len(out) >= len(questions):
                break
    return out


# --- the wire ----------------------------------------------------------------------


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _records(packet: bytes, questions: int, count: int) -> list[tuple[str, int, object]]:
    """``(owner, type, rdata)`` of every record read before the packet broke off.

    rdata is the target name (PTR), ``(port, target)`` (SRV), TXT pairs, or a dotted
    address (A); other types are skipped by their length.
    """
    out: list[tuple[str, int, object]] = []
    offset = 12
    try:
        for _ in range(questions):
            _name, offset = _read_name(packet, offset)
            offset = _need(packet, offset, 4)
        for _ in range(count):
            owner, offset = _read_name(packet, offset)
            end = _need(packet, offset, 10)
            rtype, _rclass, _ttl, length = struct.unpack_from(">HHIH", packet, offset)
            start, offset = end, _need(packet, end, length)
            rdata = _rdata(packet, rtype, start, length)
            if rdata is not None:
                out.append((owner, rtype, rdata))
    except _Malformed:
        return out  # salvage: the records read so far stand on their own
    return out


def _rdata(packet: bytes, rtype: int, start: int, length: int) -> object:
    end = start + length
    if rtype == _A:
        return socket.inet_ntoa(packet[start:end]) if length == 4 else None
    if rtype == _PTR:
        name, _next = _read_name(packet, start, limit=end)
        return name
    if rtype == _SRV:
        if length < 7:
            return None
        _prio, _weight, port = struct.unpack_from(">HHH", packet, start)
        target, _next = _read_name(packet, start + 6, limit=end)
        return (port, target)
    if rtype == _TXT:
        return _txt(packet[start:end])
    return None


def _txt(data: bytes) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(data):
        length = data[i]
        chunk = data[i + 1 : i + 1 + length]
        if len(chunk) < length:
            break
        key, _sep, value = chunk.decode("utf-8", "replace").partition("=")
        if key:
            pairs.append((key.lower(), value))
        i += 1 + length
    return tuple(pairs)


def _read_name(packet: bytes, offset: int, *, limit: int | None = None) -> tuple[str, int]:
    """The (lower-cased) name at ``offset`` and the offset just after it."""
    end = len(packet) if limit is None else min(limit, len(packet))
    labels: list[str] = []
    after: int | None = None
    hops = total = 0
    while True:
        if offset >= end:
            raise _Malformed
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= end:
                raise _Malformed
            hops += 1
            if hops > _MAX_HOPS:
                raise _Malformed
            if after is None:
                after = offset + 2
            offset = ((length & 0x3F) << 8) | packet[offset + 1]
            end = len(packet)  # a pointer may lead outside the rdata
            continue
        if length & 0xC0:
            raise _Malformed
        if length == 0:
            return ".".join(labels).lower(), offset + 1 if after is None else after
        label = packet[offset + 1 : offset + 1 + length]
        total += length + 1
        if len(label) < length or total > _MAX_NAME:
            raise _Malformed
        labels.append(label.decode("utf-8", "replace"))
        offset += 1 + length


def _need(packet: bytes, offset: int, size: int) -> int:
    if offset + size > len(packet):
        raise _Malformed
    return offset + size
