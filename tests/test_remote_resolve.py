"""``remote.resolve_address``: where to reach a worker (story 020, task 3.2).

A recorded address is used as it is. A worker with none -- a provisioned worker that
never answered yet -- is looked up by name over mDNS, for 5 s; nobody answering is a
:class:`remote.RemoteUnreachable` naming the worker, the timeout and the ``--address``
way to give it by hand. The finder is a parameter: no test sends a packet.

Entries are built with ``model_validate`` and the function is reached through
``getattr``: the file type-checks before and after tasks 1.2 and 3.2, and until then
each test fails on the missing function.

Requirements exercised: R3.2, R3.3.
"""

import base64
import hashlib
import struct
from typing import Any

import pytest

from shidashi import remote, workers


def _remote(attr: str) -> Any:
    """``remote.<attr>``, typed ``Any``: absent until task 3.2 adds it. Each test takes
    the function before building its entry, so it fails on the function first."""
    return getattr(remote, attr)


def _ed25519_line(seed: str) -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()}"


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _entry(name: str = "bentoo-lab", **over: Any) -> workers.WorkerEntry:
    """A provisioned worker as task 1.2 records it: no address, flags or image yet."""
    key = _ed25519_line(name)
    raw: dict[str, Any] = {
        "name": name,
        "host_key": key,
        "host_key_fingerprint": _fingerprint(key),
        "paired_at": "2026-10-10T12:00:00+00:00",
        "provisioned": True,
    }
    raw.update(over)
    return workers.WorkerEntry.model_validate(raw)


class _Finder:
    """``mdns.find`` as a table of names to addresses; records every lookup."""

    def __init__(self, answers: dict[str, str] | None = None, error: OSError | None = None) -> None:
        self.answers = answers or {}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, **kw: Any) -> str | None:
        self.calls.append((name, kw))
        if self.error is not None:
            raise self.error
        return self.answers.get(name)


def test_a_recorded_address_is_used_without_any_lookup() -> None:
    resolve_address = _remote("resolve_address")
    finder = _Finder({"bentoo-lab": "192.168.15.99"})
    entry = _entry(address="192.168.15.6")
    assert resolve_address(entry, finder=finder) == "192.168.15.6"
    assert finder.calls == []


def test_without_an_address_the_worker_is_looked_up_by_its_own_name_for_5_s() -> None:
    """Hostile: the lookup asks for N itself -- never a neighbour's answer."""
    resolve_address = _remote("resolve_address")
    finder = _Finder({"bentoo-lab2": "192.168.15.7", "bentoo-lab": "192.168.15.6"})
    assert resolve_address(_entry(), finder=finder) == "192.168.15.6"
    assert [name for name, _kw in finder.calls] == ["bentoo-lab"]
    assert finder.calls[0][1].get("timeout") == pytest.approx(5.0)


def test_nobody_answering_is_unreachable_naming_n_the_timeout_and_address() -> None:
    resolve_address = _remote("resolve_address")
    finder = _Finder({"bentoo-lab2": "192.168.15.7"})
    with pytest.raises(remote.RemoteUnreachable) as caught:
        resolve_address(_entry(), finder=finder)
    message = str(caught.value)
    assert "bentoo-lab" in message
    assert "5 s" in message
    assert "--address" in message
    assert "None" not in message  # the missing address is not printed as a value
    assert len(finder.calls) == 1  # one lookup, no second try


def test_a_socket_error_of_the_lookup_is_unreachable_carrying_its_cause() -> None:
    resolve_address = _remote("resolve_address")
    cause = OSError(101, "Network is unreachable")
    with pytest.raises(remote.RemoteUnreachable) as caught:
        resolve_address(_entry(), finder=_Finder(error=cause))
    assert "Network is unreachable" in str(caught.value)
    assert "bentoo-lab" in str(caught.value)
    assert caught.value.__cause__ is cause


def test_an_unreachable_lookup_is_never_read_as_a_host_key_mismatch() -> None:
    """The CLI never retries a mismatch; a failed lookup must not look like one."""
    resolve_address = _remote("resolve_address")
    with pytest.raises(remote.RemoteError) as caught:
        resolve_address(_entry(), finder=_Finder())
    assert not isinstance(caught.value, remote.HostKeyMismatch)
