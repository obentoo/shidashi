"""Tests of shidashi.mdns -- the host's one-shot DNS-SD browse (task 2.3).

``build_query`` and ``parse_answers`` are pure; ``browse`` takes a socket factory, so no
packet ever leaves this machine. The answers are built here byte by byte in the shape
systemd-resolved sends to a legacy-unicast (QU) query: the question echoed, the PTR in
the answer section, SRV/TXT/A (and the AAAA/NSEC it adds) in the additional section,
names compressed, the cache-flush bit on the unique records. No capture of a real
resolved answer was available when this was written: when one is, add it beside these.

Every packet is untrusted input (design, Security): ``parse_answers`` never raises and
never hangs, whatever it is fed.

Requirements exercised: R1.1 (name, IPv4 address and port of every worker that
answered, within --wait), R1.2 (nothing found is an empty list, not an error).
"""

import contextlib
import socket
import struct
import threading
import types
from collections.abc import Callable
from typing import Any

import pytest

from shidashi import mdns

SERVICE = "_shidashi-kyomei._tcp"
TXT = ("v=1", "image=20261006T1200", "trusted=no")


# --- a DNS packet builder (names compressed as a responder does) -----------------------


def _labels(*labels: str, ptr: int | None = None) -> bytes:
    out = b"".join(bytes([len(label.encode())]) + label.encode() for label in labels)
    return out + (struct.pack(">H", 0xC000 | ptr) if ptr is not None else b"\x00")


def _ptr(offset: int) -> bytes:
    return struct.pack(">H", 0xC000 | offset)


class _Packet:
    def __init__(self, *, flags: int = 0x8400, ident: int = 0) -> None:
        self.flags, self.ident = flags, ident
        self.counts = {"qd": 0, "an": 0, "ns": 0, "ar": 0}
        self.body = bytearray()

    def here(self) -> int:
        return 12 + len(self.body)

    def question(self, name: bytes, qtype: int = 12, qclass: int = 0x8001) -> int:
        offset = self.here()
        self.body += name + struct.pack(">HH", qtype, qclass)
        self.counts["qd"] += 1
        return offset

    def record(
        self,
        section: str,
        owner: bytes,
        rtype: int,
        rdata: bytes | Callable[[int], bytes],
        *,
        rclass: int = 0x0001,
        ttl: int = 10,
    ) -> tuple[int, int]:
        """Append a record; returns (owner offset, rdata offset)."""
        owner_offset = self.here()
        rdata_offset = owner_offset + len(owner) + 10
        data = rdata(rdata_offset) if callable(rdata) else rdata
        self.body += owner + struct.pack(">HHIH", rtype, rclass, ttl, len(data)) + data
        self.counts[section] += 1
        return owner_offset, rdata_offset

    def bytes(self) -> bytes:
        c = self.counts
        header = struct.pack(">HHHHHH", self.ident, self.flags, c["qd"], c["an"], c["ns"], c["ar"])
        return header + bytes(self.body)


def _txt(strings: tuple[str, ...]) -> bytes:
    return b"".join(bytes([len(s.encode())]) + s.encode() for s in strings)


def _answer(
    workers: list[dict[str, Any]],
    *,
    split: bool = True,
    question: bool = True,
    service: tuple[str, str] = ("_shidashi-kyomei", "_tcp"),
    flags: int = 0x8400,
    a_owner_upper: bool = False,
    extras: bool = True,
) -> bytes:
    """One response answering for ``workers`` (dicts: name, address, port, txt)."""
    pkt = _Packet(flags=flags)
    if question:
        svc = pkt.question(_labels(*service, "local"))
        svc_owner = _ptr(svc)
    else:
        svc = pkt.here()  # the first PTR's owner is written in full, here
        svc_owner = _labels(*service, "local")
    local = svc + 1 + len(service[0]) + 1 + len(service[1])
    instances: list[int] = []
    for i, worker in enumerate(workers):
        owner = svc_owner if (question or i > 0) else _labels(*service, "local")
        if not question and i > 0:
            owner = _ptr(svc)
        _o, inst = pkt.record("an", owner, 12, _labels(worker["name"], ptr=svc))
        instances.append(inst)
    rest = "ar" if split else "an"
    for worker, inst in zip(workers, instances, strict=True):
        _o, srv = pkt.record(
            rest,
            _ptr(inst),
            33,
            struct.pack(">HHH", 0, 0, worker["port"]) + _labels(worker["name"], ptr=local),
            rclass=0x8001,
        )
        target = srv + 6
        pkt.record(rest, _ptr(inst), 16, _txt(worker.get("txt", TXT)), rclass=0x8001)
        a_owner = _labels(worker["name"].upper(), "LOCAL") if a_owner_upper else _ptr(target)
        pkt.record(rest, a_owner, 1, socket.inet_aton(worker["address"]), rclass=0x8001)
        if extras:
            pkt.record(rest, _ptr(target), 28, b"\xfe\x80" + b"\x00" * 13 + b"\x01", rclass=0x8001)
            pkt.record(rest, _ptr(inst), 47, _ptr(inst) + b"\x00\x05\x00\x00\x80\x00\x40")
    return pkt.bytes()


def _w(name: str, address: str, port: int = 8765, txt: tuple[str, ...] = TXT) -> dict[str, Any]:
    return {"name": name, "address": address, "port": port, "txt": txt}


def _key(found: Any) -> tuple[str, str, int, tuple[tuple[str, str], ...]]:
    return (found.name, found.address, found.port, tuple(sorted(dict(found.txt).items())))


def _parse_bounded(packet: bytes, seconds: float = 5.0) -> list[Any]:
    """parse_answers in a thread: a compression loop must not hang the suite."""
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["found"] = mdns.parse_answers(packet)
        except BaseException as err:  # the test reports it
            outcome["error"] = err

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "parse_answers did not return: a compression loop?"
    assert "error" not in outcome, f"parse_answers raised {outcome.get('error')!r}"
    found = outcome["found"]
    assert isinstance(found, list)
    for item in found:
        assert isinstance(item, mdns.Found)
        socket.inet_aton(item.address)  # a dotted IPv4 ssh can dial
        assert item.address.count(".") == 3
        assert 0 < item.port < 65536
    return found


# --- build_query ---------------------------------------------------------------------------


def test_build_query_is_one_ptr_question_with_the_unicast_response_bit() -> None:
    expected = (
        b"\x00\x00"  # ID 0 (RFC 6762 18.1)
        b"\x00\x00"  # a standard query
        b"\x00\x01\x00\x00\x00\x00\x00\x00"  # one question, no records
        b"\x10_shidashi-kyomei\x04_tcp\x05local\x00"
        b"\x00\x0c"  # PTR
        b"\x80\x01"  # class IN with the QU bit: answer me by unicast
    )
    assert mdns.build_query(SERVICE) == expected


# --- parse_answers: what a worker announces --------------------------------------------------


def test_parse_answers_reads_a_resolved_answer_to_name_address_port_and_txt() -> None:
    found = _parse_bounded(_answer([_w("bentoo-lab", "192.168.15.6")]))
    assert [_key(f) for f in found] == [
        (
            "bentoo-lab",
            "192.168.15.6",
            8765,
            (("image", "20261006T1200"), ("trusted", "no"), ("v", "1")),
        )
    ]


def test_parse_answers_reads_the_same_records_whatever_section_carries_them() -> None:
    worker = _w("bentoo-lab", "192.168.15.6", txt=("v=1", "image=X", "trusted=yes"))
    split = _parse_bounded(_answer([worker], split=True))
    together = _parse_bounded(_answer([worker], split=False))
    unsolicited = _parse_bounded(_answer([worker], split=False, question=False))
    assert [_key(f) for f in split] == [_key(f) for f in together] == [_key(f) for f in unsolicited]
    assert dict(split[0].txt)["trusted"] == "yes"


def test_found_is_frozen() -> None:
    (found,) = _parse_bounded(_answer([_w("bentoo-lab", "192.168.15.6")]))
    with pytest.raises(AttributeError):
        found.address = "10.0.0.1"


# Identity of a worker inside a packet -- hostile halves first. Wrongly COLLAPSED: two
# machines that share the image's default name, or two workers in one packet, must keep
# their own address and port. Wrongly SPLIT: the A record whose owner differs from the
# SRV target only in letter case is the same host (DNS names compare case-insensitively).


def test_parse_answers_keeps_two_workers_of_one_packet_with_their_own_address_and_port() -> None:
    packet = _answer([_w("lab-one", "192.168.15.6", 8765), _w("lab-two", "192.168.15.7", 9000)])
    found = {f.name: (f.address, f.port) for f in _parse_bounded(packet)}
    assert found == {"lab-one": ("192.168.15.6", 8765), "lab-two": ("192.168.15.7", 9000)}


def test_parse_answers_matches_an_a_record_to_its_srv_target_in_any_letter_case() -> None:
    found = _parse_bounded(_answer([_w("bentoo-lab", "192.168.15.6")], a_owner_upper=True))
    assert [(f.name, f.address) for f in found] == [("bentoo-lab", "192.168.15.6")]


def test_parse_answers_skips_an_instance_without_an_address() -> None:
    """A SRV whose target no A record names cannot be dialed: it is not a Found."""
    pkt = _Packet()
    svc = pkt.question(_labels("_shidashi-kyomei", "_tcp", "local"))
    local = svc + 1 + 16 + 1 + 4
    _o, inst = pkt.record("an", _ptr(svc), 12, _labels("bentoo-lab", ptr=svc))
    pkt.record(
        "ar", _ptr(inst), 33, struct.pack(">HHH", 0, 0, 8765) + _labels("elsewhere", ptr=local)
    )
    pkt.record("ar", _labels("bentoo-lab", "local"), 1, socket.inet_aton("192.168.15.6"))
    assert _parse_bounded(pkt.bytes()) == []


# --- parse_answers: hostile packets never raise, never hang ----------------------------------


def _header(an: int = 1, flags: int = 0x8400) -> bytes:
    return struct.pack(">HHHHHH", 0, flags, 0, an, 0, 0)


def _hostile_packets() -> dict[str, bytes]:
    good = _answer([_w("bentoo-lab", "192.168.15.6")])
    rr = struct.pack(">HHIH", 12, 1, 10, 2)
    return {
        "empty": b"",
        "shorter than a header": b"\x00\x00\x84\x00\x00",
        "a pointer to itself": _header() + _ptr(12) + rr + _ptr(12),
        "two pointers to each other": _header() + _ptr(14) + _ptr(12) + rr + _ptr(12),
        "a pointer past the end": _header() + _ptr(0x3FFF) + rr + _ptr(12),
        "a label longer than the packet": _header() + b"\x3fabc",
        "a truncated record": good[: len(good) - 7],
        "an rdlength past the end": _header()
        + _labels("_shidashi-kyomei", "_tcp", "local")
        + struct.pack(">HHIH", 12, 1, 10, 0xFFFF)
        + b"\x0abentoo-lab",
        "more records counted than present": struct.pack(">HHHHHH", 0, 0x8400, 0, 40, 0, 40)
        + good[12:],
        # owner and rdata point at the end of a 20-hop chain (beyond the 16-hop bound)
        "a chain of twenty pointers": _header()
        + _ptr(65)
        + rr
        + _ptr(65)
        + b"\x00"
        + b"".join(_ptr(26 if k == 0 else 27 + 2 * (k - 1)) for k in range(20)),
        "an A record of five bytes": _header()
        + _labels("bentoo-lab", "local")
        + struct.pack(">HHIH", 1, 1, 10, 5)
        + b"\xc0\xa8\x0f\x06\x00",
    }


@pytest.mark.parametrize("case", sorted(_hostile_packets()))
def test_parse_answers_never_raises_on_a_hostile_packet(case: str) -> None:
    _parse_bounded(_hostile_packets()[case])


def test_parse_answers_salvages_at_most_the_valid_part_of_a_truncated_packet() -> None:
    good = _answer([_w("bentoo-lab", "192.168.15.6")])
    found = _parse_bounded(good[: len(good) - 7])  # the trailing NSEC is cut short
    assert [(f.name, f.address, f.port) for f in found] in (
        [],
        [("bentoo-lab", "192.168.15.6", 8765)],
    )


def test_parse_answers_ignores_a_query_that_carries_records() -> None:
    """A query (QR bit clear) is not an answer, whatever records it carries."""
    packet = _answer([_w("bentoo-lab", "192.168.15.6")], flags=0x0000)
    assert _parse_bounded(packet) == []


def test_parse_answers_survives_every_truncation_and_every_corrupted_byte() -> None:
    good = _answer([_w("lab-one", "192.168.15.6"), _w("lab-two", "192.168.15.7")])
    for end in range(len(good)):
        _parse_bounded(good[:end], seconds=2.0)
    for i in range(12, len(good)):
        for byte in (0x00, 0x3F, 0xC0, 0xFF):
            mutated = bytearray(good)
            mutated[i] = byte
            _parse_bounded(bytes(mutated), seconds=2.0)


# --- browse with a fake socket and a fake clock ----------------------------------------------


#: The address the fake host's multicast route leaves from.
LAN_ADDRESS = "192.168.15.5"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _Socket:
    """A UDP socket that delivers ``schedule`` (seconds after the query) on a fake clock."""

    def __init__(self, clock: _Clock, schedule: list[tuple[float, bytes]], **fail: Any) -> None:
        self.clock, self.pending = clock, list(schedule)
        self.fail = fail
        self.options: list[tuple[int, int, Any]] = []
        self.binds: list[tuple[str, int]] = []
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.timeouts: list[float | None] = []
        self.timeout: float | None = None
        self.sent_at: float | None = None
        self.closed = False
        self.reads = 0
        self.connected: tuple[str, int] | None = None

    # -- socket API ------------------------------------------------------------------
    def setsockopt(self, level: int, option: int, value: Any, *_a: Any) -> None:
        self.options.append((level, option, value))

    def bind(self, address: tuple[str, int]) -> None:
        self.binds.append(tuple(address))  # type: ignore[arg-type]

    def connect(self, address: tuple[str, int]) -> None:
        # a UDP connect only looks the route up; ``no_route`` plays an offline host
        if self.fail.get("no_route"):
            raise OSError(101, "Network is unreachable")
        self.connected = (address[0], address[1])

    def getsockname(self) -> tuple[str, int]:
        return (LAN_ADDRESS, 40000)

    def sendto(self, data: bytes, *args: Any) -> int:
        address = args[-1]
        if self.fail.get("sendto"):
            raise OSError(101, "Network is unreachable")
        self.sent.append((bytes(data), tuple(address)))
        self.sent_at = self.clock.now
        return len(data)

    def settimeout(self, value: float | None) -> None:
        self.timeout = value
        self.timeouts.append(value)

    def setblocking(self, flag: bool) -> None:
        self.settimeout(None if flag else 0.0)

    def recvfrom(self, _size: int, *_flags: Any) -> tuple[bytes, tuple[str, int]]:
        self.reads += 1
        assert self.reads < 1000, "browse is spinning on the socket"
        assert self.sent_at is not None, "browse read before sending its query"
        deadline = float("inf") if self.timeout is None else self.clock.now + self.timeout
        if self.pending:
            at, packet = self.pending[0]
            if self.sent_at + at <= deadline:
                self.pending.pop(0)
                self.clock.now = max(self.clock.now, self.sent_at + at)
                return packet, ("192.168.15.6", 5353)
        if self.timeout is None:
            raise AssertionError("a blocking read with nothing left to read would hang")
        if self.timeout == 0:
            raise BlockingIOError(11, "Resource temporarily unavailable")
        self.clock.now = deadline
        raise TimeoutError("timed out")

    def recv(self, size: int, *flags: Any) -> bytes:
        return self.recvfrom(size, *flags)[0]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _Socket:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class _Factory:
    def __init__(self, sock: _Socket) -> None:
        self.sock = sock
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _Socket:
        self.calls.append(args + tuple(kwargs.values()))
        return self.sock


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """The module's clock, whichever way it imports it."""
    fake = _Clock()
    if isinstance(getattr(mdns, "time", None), types.ModuleType):
        monkeypatch.setattr(
            mdns,
            "time",
            types.SimpleNamespace(
                monotonic=fake.monotonic,
                time=fake.monotonic,
                perf_counter=fake.monotonic,
                sleep=fake.sleep,
            ),
        )
    for name in ("monotonic", "perf_counter", "time"):
        if callable(getattr(mdns, name, None)) and not isinstance(
            getattr(mdns, name), types.ModuleType
        ):
            monkeypatch.setattr(mdns, name, fake.monotonic)
    if callable(getattr(mdns, "sleep", None)):
        monkeypatch.setattr(mdns, "sleep", fake.sleep)
    return fake


def _browse(sock: _Socket, wait: float = 3.0) -> tuple[list[Any], _Factory]:
    factory = _Factory(sock)
    return mdns.browse(SERVICE, wait, sock_factory=factory), factory


def test_browse_sends_one_query_to_the_mdns_group_from_an_ephemeral_port(clock: _Clock) -> None:
    sock = _Socket(clock, [(0.2, _answer([_w("bentoo-lab", "192.168.15.6")]))])
    found, factory = _browse(sock)
    assert [(f.name, f.address, f.port) for f in found] == [("bentoo-lab", "192.168.15.6", 8765)]
    assert factory.calls and factory.calls[0][:2] == (socket.AF_INET, socket.SOCK_DGRAM)
    assert sock.sent == [(mdns.build_query(SERVICE), ("224.0.0.251", 5353))]
    # never UDP 5353 on the host: resolved or avahi may hold it, and it needs no root
    assert all(port == 0 for _addr, port in sock.binds), sock.binds
    # bound to the address the multicast route leaves from, not 0.0.0.0: answers
    # arrive there, and the host's container and VM bridges cannot reach the port
    assert sock.connected == ("224.0.0.251", 5353)
    assert sock.binds == [(LAN_ADDRESS, 0)], sock.binds
    opts = {(level, option): value for level, option, value in sock.options}
    assert opts.get((socket.IPPROTO_IP, socket.IP_MULTICAST_IF)) == socket.inet_aton(LAN_ADDRESS)
    ttl = opts.get((socket.IPPROTO_IP, socket.IP_MULTICAST_TTL))
    loop = opts.get((socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP))
    assert ttl in (255, b"\xff", struct.pack("b", -1), struct.pack("B", 255)), ttl
    assert loop in (0, b"\x00", False), loop
    assert sock.closed


def test_browse_collects_until_the_wait_passes_and_not_beyond(clock: _Clock) -> None:
    sock = _Socket(
        clock,
        [
            (0.5, _answer([_w("lab-early", "192.168.15.6")])),
            (2.0, _answer([_w("lab-late", "192.168.15.7")])),
            (3.5, _answer([_w("lab-too-late", "192.168.15.8")])),
        ],
    )
    found, _factory = _browse(sock, wait=3.0)
    assert [f.name for f in found] == ["lab-early", "lab-late"]
    assert sock.sent_at is not None
    elapsed = clock.now - sock.sent_at
    assert 3.0 - 1e-6 <= elapsed < 3.5, elapsed
    assert all(t is None or t <= 3.0 + 1e-6 for t in sock.timeouts), sock.timeouts


def test_browse_returns_an_empty_list_when_nobody_answers(clock: _Clock) -> None:
    sock = _Socket(clock, [])
    found, _factory = _browse(sock, wait=1.0)
    assert found == []
    assert sock.closed


# Deduplication by (name, address, port) -- hostile halves first: two machines both
# called "shidashi-worker" (the image's default hostname) and one worker on two ports are
# DIFFERENT entries; the same worker answering twice is ONE entry.


def test_browse_keeps_two_machines_that_share_the_default_name(clock: _Clock) -> None:
    sock = _Socket(
        clock,
        [
            (0.1, _answer([_w("shidashi-worker", "192.168.15.7")])),
            (0.2, _answer([_w("shidashi-worker", "192.168.15.8")])),
            (0.3, _answer([_w("shidashi-worker", "192.168.15.8", 9000)])),
        ],
    )
    found, _factory = _browse(sock)
    assert sorted((f.name, f.address, f.port) for f in found) == [
        ("shidashi-worker", "192.168.15.7", 8765),
        ("shidashi-worker", "192.168.15.8", 8765),
        ("shidashi-worker", "192.168.15.8", 9000),
    ]


def test_browse_lists_a_worker_that_answered_twice_once(clock: _Clock) -> None:
    packet = _answer([_w("bentoo-lab", "192.168.15.6")])
    sock = _Socket(
        clock,
        [
            (0.1, packet),
            (0.4, packet),
            (1.0, _answer([_w("bentoo-lab", "192.168.15.6")], split=False)),
        ],
    )
    found, _factory = _browse(sock)
    assert [(f.name, f.address, f.port) for f in found] == [("bentoo-lab", "192.168.15.6", 8765)]


def test_browse_sorts_by_name(clock: _Clock) -> None:
    sock = _Socket(
        clock,
        [
            (0.1, _answer([_w("zeta", "192.168.15.9")])),
            (0.2, _answer([_w("alpha", "192.168.15.8"), _w("mid", "192.168.15.7")])),
        ],
    )
    found, _factory = _browse(sock)
    assert [f.name for f in found] == ["alpha", "mid", "zeta"]


def test_browse_skips_a_malformed_packet_and_keeps_listening(clock: _Clock) -> None:
    sock = _Socket(
        clock,
        [
            (0.1, b"\x00\x00\x84\x00\x00\x00\x00\x01" + b"\xc0\x0c" * 4),
            (0.5, _answer([_w("bentoo-lab", "192.168.15.6")])),
        ],
    )
    found, _factory = _browse(sock)
    assert [f.name for f in found] == ["bentoo-lab"]


def test_browse_ignores_another_services_instances(clock: _Clock) -> None:
    """A printer announcing _ipp._tcp on the same LAN is not a worker."""
    printer = _answer([_w("office-printer", "192.168.15.20", 631)], service=("_ipp", "_tcp"))
    sock = _Socket(clock, [(0.1, printer), (0.2, _answer([_w("bentoo-lab", "192.168.15.6")]))])
    found, _factory = _browse(sock)
    assert [f.name for f in found] == ["bentoo-lab"]


def test_browse_without_a_multicast_route_fails_and_closes_its_sockets(clock: _Clock) -> None:
    sock = _Socket(clock, [], no_route=True)
    with pytest.raises(OSError, match="unreachable"):
        _browse(sock)
    assert sock.closed and sock.sent == []


def test_browse_closes_its_socket_when_sending_fails(clock: _Clock) -> None:
    sock = _Socket(clock, [], sendto=True)
    with contextlib.suppress(OSError):  # failing is allowed; leaking the socket is not
        _browse(sock)
    assert sock.closed
