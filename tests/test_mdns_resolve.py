"""Discovery against what systemd-resolved really answers (story 009, found in task 6.3).

To a legacy-unicast query (an ephemeral source port, the QU bit) resolved answers the
PTR ONLY -- no SRV, TXT or A in the additional section (captured from bentoo-lab on
2026-10-07, worker ISO 2026.10.07). ``browse`` must then ask the instance's SRV and
TXT, and the SRV target's A, of the machine that answered: by unicast to that peer, so
two fresh workers sharing the image's default hostname keep their own addresses.

Requirements exercised: R1.1.
"""

import socket
import struct
import types
from typing import Any

import pytest

from shidashi import mdns

SERVICE = "_shidashi-kyomei._tcp"
INSTANCE = "shidashi-worker._shidashi-kyomei._tcp.local"

# the PTR-only answer bentoo-lab's resolved sent to the host's query (75 bytes)
RESOLVED_PTR_ONLY = bytes.fromhex(
    "0000 8400 0001 0001 0000 0000"  # id 0, response, 1 question, 1 answer
    " 10 5f7368696461736869 2d6b796f6d6569 04 5f746370 05 6c6f63616c 00 000c 0001"
    " c00c 000c 0001 00000078 0012 0f 7368696461736869 2d776f726b6572 c00c"
)


def _name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"


def _answer(owner: str, rtype: int, rdata: bytes) -> bytes:
    """A one-record response, names written in full (no compression)."""
    header = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    return header + _name(owner) + struct.pack(">HHIH", rtype, 0x8001, 120, len(rdata)) + rdata


def _srv(target: str, port: int = 8765) -> bytes:
    return _answer(INSTANCE, 33, struct.pack(">HHH", 0, 0, port) + _name(target))


def _txt(*items: str) -> bytes:
    return _answer(INSTANCE, 16, b"".join(bytes([len(i)]) + i.encode() for i in items))


def _a(host: str, address: str) -> bytes:
    return _answer(host, 1, socket.inet_aton(address))


def _questions(query: bytes) -> list[tuple[str, int]]:
    count = struct.unpack_from(">H", query, 4)[0]
    offset, out = 12, []
    for _ in range(count):
        labels = []
        while query[offset]:
            length = query[offset]
            labels.append(query[offset + 1 : offset + 1 + length].decode())
            offset += 1 + length
        qtype = struct.unpack_from(">H", query, offset + 1)[0]
        offset += 5
        out.append((".".join(labels).lower(), qtype))
    return out


class _Resolved:
    """A LAN of resolved responders: PTR-only to the browse, the rest when asked."""

    def __init__(self, workers: dict[str, dict[str, Any]]) -> None:
        self.workers = workers  # peer IP -> {"host": ..., "port": ..., "txt": (...)}
        self.sent: list[tuple[list[tuple[str, int]], tuple[str, int]]] = []
        self.inbox: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False
        self.binds: list[Any] = []

    def setsockopt(self, *_a: Any) -> None:
        return None

    def bind(self, address: Any) -> None:
        self.binds.append(address)

    def settimeout(self, _value: Any) -> None:
        return None

    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        questions = _questions(data)
        self.sent.append((questions, (address[0], address[1])))
        group = address[0] == "224.0.0.251"
        for peer, worker in self.workers.items():
            if not group and address[0] != peer:
                continue
            for qname, qtype in questions:
                if qtype == 12:
                    self.inbox.append((RESOLVED_PTR_ONLY, (peer, 5353)))
                elif qtype == 33 and qname == INSTANCE:
                    self.inbox.append((_srv(worker["host"], worker["port"]), (peer, 5353)))
                elif qtype == 16 and qname == INSTANCE:
                    self.inbox.append((_txt(*worker["txt"]), (peer, 5353)))
                elif qtype == 1 and qname == worker["host"]:
                    self.inbox.append((_a(worker["host"], peer), (peer, 5353)))
        return len(data)

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        if not self.inbox:
            raise TimeoutError("timed out")
        return self.inbox.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each read that finds nothing lets one second pass: no test waits for real."""
    now = {"t": 1000.0}

    def _monotonic() -> float:
        now["t"] += 0.05
        return now["t"]

    monkeypatch.setattr(mdns, "time", types.SimpleNamespace(monotonic=_monotonic))


def _browse(lan: _Resolved, wait: float = 1.0) -> list[Any]:
    return mdns.browse(SERVICE, wait, sock_factory=lambda *_a: lan)


def test_the_captured_answer_is_a_ptr_only_response() -> None:
    """The fixture is what resolved sent: one PTR, nothing to dial yet."""
    assert mdns.parse_answers(RESOLVED_PTR_ONLY) == []


def test_resolve_browse_asks_srv_txt_and_a_of_the_peer_that_answered_the_ptr() -> None:
    lan = _Resolved(
        {
            "192.168.15.6": {
                "host": "shidashi-worker.local",
                "port": 8765,
                "txt": ("v=1", "image=X", "trusted=no"),
            }
        }
    )
    found = _browse(lan)
    assert [(f.name, f.address, f.port) for f in found] == [
        ("shidashi-worker", "192.168.15.6", 8765)
    ]
    assert dict(found[0].txt) == {"v": "1", "image": "X", "trusted": "no"}
    follow_ups = [q for q, address in lan.sent if address != ("224.0.0.251", 5353)]
    assert follow_ups, "no follow-up query was sent"
    assert all(address == ("192.168.15.6", 5353) for _q, address in lan.sent[1:])
    assert lan.closed
    assert all(port == 0 for _host, port in lan.binds)  # still never UDP 5353


def test_resolve_two_workers_with_the_default_name_keep_their_own_addresses() -> None:
    """Hostile: both answer as shidashi-worker; each address comes from its own peer."""
    lan = _Resolved(
        {
            "192.168.15.7": {"host": "shidashi-worker.local", "port": 8765, "txt": ("v=1",)},
            "192.168.15.8": {"host": "shidashi-worker.local", "port": 9000, "txt": ("v=1",)},
        }
    )
    found = _browse(lan)
    assert sorted((f.address, f.port) for f in found) == [
        ("192.168.15.7", 8765),
        ("192.168.15.8", 9000),
    ]


def test_resolve_a_peer_that_never_answers_the_follow_up_is_left_out() -> None:
    lan = _Resolved({"192.168.15.6": {"host": "elsewhere.local", "port": 8765, "txt": ()}})
    lan.workers["192.168.15.6"]["host"] = "shidashi-worker.local"

    real_sendto = lan.sendto

    def _ptr_only(data: bytes, address: tuple[str, int]) -> int:
        if address[0] != "224.0.0.251":
            lan.sent.append((_questions(data), (address[0], address[1])))
            return len(data)  # the peer went silent after the PTR
        return real_sendto(data, address)

    lan.sendto = _ptr_only  # type: ignore[method-assign]
    assert _browse(lan) == []
    assert lan.closed
