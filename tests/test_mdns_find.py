"""``mdns.find``: one worker, found by its name (story 020, task 3.1).

A worker booted with a provisioned identity announces ``<N>._shidashi-worker._tcp.local``
-- a service of its own, not the pairing service ``_shidashi-kyomei._tcp`` -- and
``find(N, timeout=...)`` returns the IPv4 address of the instance named exactly N
(DNS labels compare case-insensitively), or None when no such worker answers.

The LAN below answers the way systemd-resolved does (tests/test_mdns_resolve.py): the
PTR alone to the multicast browse, the SRV, TXT and A when asked by unicast. Its
packets are built with that module's helpers; its ``_Resolved`` fake is reused as is
for the pairing-service peer. The fake socket is handed to ``mdns.browse`` (the
legacy-unicast browse ``find`` is built on), so nothing reaches the network.

Every new name is reached through ``getattr``: the file type-checks before and after
the implementation, and until then each test fails on the missing attribute.

Requirements exercised: R3.1, R3.3.
"""

import socket
import struct
from collections.abc import Callable
from typing import Any

import pytest

from shidashi import mdns
from shidashi.kyomei_protocol import SERVICE as PAIRING_SERVICE
from tests.test_mdns_resolve import _answer, _name, _questions, _Resolved

WORKER_SERVICE = "_shidashi-worker._tcp.local"
_PTR, _A, _TXT, _SRV = 12, 1, 16, 33


def _mdns(attr: str) -> Any:
    """``mdns.<attr>``, typed ``Any``: absent until task 3.1 adds it."""
    return getattr(mdns, attr)


class _Clock:
    """``mdns``'s monotonic clock: moves only when a read waits (or by a hair per call)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        self.t += 0.001  # a loop that never waits still ends
        return self.t


class _WorkerLan:
    """Workers announcing ``_shidashi-worker._tcp`` through systemd-resolved: each
    answers the multicast PTR browse with its PTR alone, then its SRV/TXT and its
    host's A when asked by unicast. A read with nothing to deliver lets the socket's
    timeout pass on the clock, as a real one would."""

    def __init__(self, clock: _Clock, peers: dict[str, str]) -> None:
        self.clock = clock
        self.peers = peers  # peer IP -> instance label, in the case it announces
        self.sent: list[tuple[list[tuple[str, int]], tuple[str, int]]] = []
        self.inbox: list[tuple[bytes, tuple[str, int]]] = []
        self.binds: list[Any] = []
        self.closed = False
        self._timeout = 0.0

    @staticmethod
    def _instance(label: str) -> str:
        return f"{label}.{WORKER_SERVICE}"

    @staticmethod
    def _host(peer: str) -> str:
        return f"node-{peer.rsplit('.', 1)[-1]}.local"  # never the instance label

    def setsockopt(self, *_a: Any) -> None:
        return None

    def bind(self, address: Any) -> None:
        self.binds.append(address)

    def connect(self, _address: Any) -> None:
        return None

    def getsockname(self) -> tuple[str, int]:
        return ("192.168.15.5", 40000)

    def settimeout(self, value: Any) -> None:
        self._timeout = float(value or 0.0)

    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        questions = _questions(data)
        self.sent.append((questions, (address[0], address[1])))
        group = address[0] == "224.0.0.251"
        for peer, label in self.peers.items():
            if not group and address[0] != peer:
                continue
            instance, host = self._instance(label), self._host(peer)
            for qname, qtype in questions:
                if qtype == _PTR and qname == WORKER_SERVICE:
                    reply = _answer(WORKER_SERVICE, _PTR, _name(instance))
                elif qtype == _SRV and qname == instance.lower():
                    reply = _answer(instance, _SRV, struct.pack(">HHH", 0, 0, 22) + _name(host))
                elif qtype == _TXT and qname == instance.lower():
                    reply = _answer(instance, _TXT, b"\x03v=1")
                elif qtype == _A and qname == host:
                    reply = _answer(host, _A, socket.inet_aton(peer))
                else:
                    continue
                self.inbox.append((reply, (peer, 5353)))
        return len(data)

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        if not self.inbox:
            self.clock.t += self._timeout  # the whole wait passes, then the timeout
            raise TimeoutError("timed out")
        return self.inbox.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(mdns, "time", fake)
    return fake


@pytest.fixture
def plug(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[..., Any]], None]:
    """Hand ``factory`` to every ``mdns.browse`` as its socket factory."""
    real = mdns.browse

    def _plug(factory: Callable[..., Any]) -> None:
        def _browse(*args: Any, **kw: Any) -> list[mdns.Found]:
            kw["sock_factory"] = factory
            return real(*args, **kw)

        monkeypatch.setattr(mdns, "browse", _browse)

    return _plug


def _lan(
    clock: _Clock, plug: Callable[[Callable[..., Any]], None], peers: dict[str, str]
) -> _WorkerLan:
    lan = _WorkerLan(clock, peers)
    plug(lambda *_a: lan)
    return lan


def test_worker_service_is_its_own_service_not_the_pairing_one() -> None:
    assert _mdns("WORKER_SERVICE") == WORKER_SERVICE
    assert _mdns("WORKER_SERVICE") != f"{PAIRING_SERVICE}.local"


# --- hostile first: names that must NOT answer for N --------------------------------


@pytest.mark.parametrize("other", ["bentoo-lab2", "my-bentoo-lab", "bentoo"])
def test_find_a_worker_whose_name_only_resembles_n_does_not_answer_for_n(
    clock: _Clock, plug: Callable[[Callable[..., Any]], None], other: str
) -> None:
    """Hostile (wrong collapse): a prefix, a suffix or a shorter name is another worker."""
    _lan(clock, plug, {"192.168.15.7": other})
    assert _mdns("find")("bentoo-lab", timeout=5.0) is None


def test_find_picks_n_among_workers_with_neighbouring_names(
    clock: _Clock, plug: Callable[[Callable[..., Any]], None]
) -> None:
    """Hostile: N is neither the first answer nor the first name in order -- the
    instance named exactly N wins, and its neighbours keep their own addresses."""
    _lan(
        clock,
        plug,
        {
            "192.168.15.5": "aaa-first",
            "192.168.15.7": "bentoo-lab2",
            "192.168.15.6": "bentoo-lab",
        },
    )
    assert _mdns("find")("bentoo-lab", timeout=5.0) == "192.168.15.6"
    assert _mdns("find")("bentoo-lab2", timeout=5.0) == "192.168.15.7"


def test_find_ignores_a_worker_of_that_name_on_the_pairing_service(
    clock: _Clock, plug: Callable[[Callable[..., Any]], None]
) -> None:
    """Hostile: a worker in its pairing window announces ``shidashi-worker`` on
    ``_shidashi-kyomei._tcp`` (the captured resolved answer); it is not the provisioned
    worker of that name and must not answer for it."""
    pairing = _Resolved(
        {"192.168.15.6": {"host": "shidashi-worker.local", "port": 8765, "txt": ("v=1",)}}
    )
    plug(lambda *_a: pairing)
    assert _mdns("find")("shidashi-worker", timeout=5.0) is None


@pytest.mark.parametrize("asked", ["bentoo-lab", "BENTOO-LAB", "Bentoo-Lab"])
def test_find_matches_n_whatever_the_case_of_either_side(
    clock: _Clock, plug: Callable[[Callable[..., Any]], None], asked: str
) -> None:
    """Hostile (wrong split): DNS labels are case-insensitive -- the same worker
    announced as ``Bentoo-Lab`` is N however N is spelled."""
    _lan(clock, plug, {"192.168.15.6": "Bentoo-Lab"})
    assert _mdns("find")(asked, timeout=5.0) == "192.168.15.6"


# --- benign ---------------------------------------------------------------------------


def test_find_returns_the_address_of_the_worker_named_n(
    clock: _Clock, plug: Callable[[Callable[..., Any]], None]
) -> None:
    lan = _lan(clock, plug, {"192.168.15.6": "bentoo-lab"})
    assert _mdns("find")("bentoo-lab", timeout=5.0) == "192.168.15.6"
    group_queries = [q for q, address in lan.sent if address == ("224.0.0.251", 5353)]
    assert group_queries == [[(WORKER_SERVICE, _PTR)]]  # one browse, of the worker service
    assert all(port == 0 for _host, port in lan.binds)  # never UDP 5353
    assert lan.closed


@pytest.mark.parametrize("timeout", [5.0, 2.0])
def test_find_returns_none_when_nothing_answers_within_the_timeout(
    clock: _Clock, plug: Callable[[Callable[..., Any]], None], timeout: float
) -> None:
    lan = _lan(clock, plug, {})
    started = clock.t
    assert _mdns("find")("bentoo-lab", timeout=timeout) is None
    assert clock.t - started == pytest.approx(timeout, abs=0.5)  # waited the timeout, no more
    assert lan.closed


def test_find_lets_a_socket_error_surface(plug: Callable[[Callable[..., Any]], None]) -> None:
    """No route to the mDNS group is the caller's to report, never a silent None."""

    def _no_route(*_a: Any) -> Any:
        raise OSError(101, "Network is unreachable")

    plug(_no_route)
    with pytest.raises(OSError, match="Network is unreachable"):
        _mdns("find")("bentoo-lab", timeout=5.0)
