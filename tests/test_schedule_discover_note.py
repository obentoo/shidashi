"""Unit tests of the plan note of a worker ``schedule.discover`` cannot reach (R3.7, R3.8).

Contract (design.md v4, story 014, "Node discovery"):

- R3.7: a worker whose address cannot be resolved gets a plan note naming a fix that
  ``shidashi build`` accepts -- "build with --local, or reach it once with
  ``shidashi worker status <name> --address ADDR``" -- never "pass --address ADDR"
  (``build`` takes no ``--address``). ``remote.resolve_address``'s own text is unchanged:
  the ``worker`` commands do take ``--address``.
- R3.8: when the probe at the RECORDED address fails and the one mDNS retry (lookup, then
  one more probe within the same deadline) also fails, or is still running at the
  deadline, the note keeps the recorded-address failure AND the retry's.

Only ``worker.status`` (keyed by the address it is sent to) and ``remote.find`` (mDNS)
are faked; the registry, ``remote.Remote`` and ``remote.resolve_address`` are real.
"""

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from shidashi import remote, schedule, worker, workers

LAB = "bentoo-lab"
RECORDED = "192.0.2.10"
FOUND = "192.0.2.77"
FIX_BUILD_TAKES = f"shidashi worker status {LAB} --address"


def _status(**over: Any) -> worker.WorkerStatus:
    """A real story 010 status of a ready worker (work disk mounted, no job running)."""
    values: dict[str, Any] = {
        "name": LAB,
        "reachable": True,
        "reason": None,
        "cpu_model": "AMD Ryzen 7 5700X 8-Core Processor",
        "threads": 16,
        "cpu_flags": ("avx2", "fma"),
        "runnable_arches": ("v1", "v2", "v3"),
        "max_target": "v3",
        "load1": 0.25,
        "work_free": 800 << 30,
        "accepts_jobs": True,
        "image": "20261004",
        "trunks": (),
        "jobs": (),
        "trunk_keys": (),
    }
    values.update(over)
    return worker.WorkerStatus(**values)


def _silent(address: str, why: str) -> Callable[[], worker.WorkerStatus]:
    """How ``worker.status`` answers for a worker that does not answer at ``address``."""
    return lambda: worker.WorkerStatus(
        name=LAB, reachable=False, reason=f"{LAB} ({address}) is unreachable: {why}"
    )


@dataclass
class Lab:
    """One paired worker; how it answers at each address, and what mDNS says."""

    wdir: Path
    answers: dict[str, Callable[[], Any]] = field(default_factory=dict)
    probed: list[str] = field(default_factory=list)
    looked_up: list[str] = field(default_factory=list)
    mdns: Callable[[], str | None] = lambda: None
    release: threading.Event = field(default_factory=threading.Event)

    def pair(self, address: str | None) -> None:
        self.wdir.mkdir(parents=True, exist_ok=True)
        (self.wdir / "id_ed25519").write_text("not a real key\n")
        (self.wdir / "known_hosts").write_text(f"{LAB} ssh-ed25519 AAAAC3NzaFAKE\n")
        entry = workers.WorkerEntry(
            name=LAB,
            address=address,
            host_key="ssh-ed25519 AAAAC3NzaFAKE",
            host_key_fingerprint=f"SHA256:{LAB}-pin",
            paired_at="2026-10-05T10:00:00Z",
        )
        workers.save_registry(self.wdir / "workers.json", {LAB: entry})

    def hang(self) -> Any:
        self.release.wait(10)
        return None


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Lab]:
    state = Lab(wdir=tmp_path / "user-data" / "shidashi" / "worker")
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))

    def status(remote_: Any, *_a: object, **_kw: object) -> Any:
        state.probed.append(remote_.address)
        return state.answers[remote_.address]()

    def find(name: str, **_kw: Any) -> str | None:
        state.looked_up.append(name)
        return state.mdns() if name == LAB else None

    monkeypatch.setattr(worker, "status", status)
    monkeypatch.setattr(remote, "find", find)
    yield state
    state.release.set()  # unblock a probe or lookup left hanging by a deadline test


def _lab_node(timeout: float = 10.0) -> schedule.Node:
    (node,) = [n for n in schedule.discover(local=False, timeout=timeout) if n.name == LAB]
    return node


# --- R3.7: the fix the note names is one `build` takes -----------------------------------


def test_an_unresolvable_workers_note_names_local_and_worker_status_address(lab: Lab) -> None:
    lab.pair(address=None)  # nobody answers over mDNS
    node = _lab_node()
    assert not node.reachable
    assert "mdns" in node.note.lower()  # the reason itself is kept
    assert "--local" in node.note
    assert FIX_BUILD_TAKES in node.note
    assert "pass --address ADDR" not in node.note  # `build` takes no --address


def test_a_failed_recorded_address_then_no_mdns_answer_names_a_fix_build_takes(
    lab: Lab,
) -> None:
    # the same resolution failure, reached through the recorded-address retry
    lab.pair(address=RECORDED)
    lab.answers[RECORDED] = _silent(RECORDED, "Connection timed out")
    node = _lab_node()
    assert not node.reachable
    assert "--local" in node.note
    assert FIX_BUILD_TAKES in node.note
    assert "pass --address ADDR" not in node.note


def test_resolve_address_own_text_still_tells_the_worker_commands_to_pass_address() -> None:
    entry = workers.WorkerEntry(
        name=LAB,
        address=None,
        host_key="ssh-ed25519 AAAAC3NzaFAKE",
        host_key_fingerprint=f"SHA256:{LAB}-pin",
        paired_at="2026-10-05T10:00:00Z",
    )
    with pytest.raises(remote.RemoteUnreachable) as caught:
        remote.resolve_address(entry, finder=lambda *_a, **_kw: None)
    assert "--address" in str(caught.value)
    assert "--local" not in str(caught.value)  # the build-only fix stays in the plan note


# --- R3.8: both reasons kept (hostile: the retry's reason replacing the first) -----------


@pytest.mark.parametrize("retry_fails", ["answers-unreachable", "raises"])
def test_a_failed_recorded_address_and_a_failed_retry_keep_both_reasons(
    lab: Lab, retry_fails: str
) -> None:
    lab.pair(address=RECORDED)
    lab.answers[RECORDED] = _silent(RECORDED, "No route to host")
    lab.mdns = lambda: FOUND
    if retry_fails == "answers-unreachable":
        lab.answers[FOUND] = _silent(FOUND, "Connection refused")
    else:

        def refuse() -> worker.WorkerStatus:
            raise remote.RemoteUnreachable(LAB, FOUND, "Connection refused")

        lab.answers[FOUND] = refuse
    node = _lab_node()
    assert lab.probed == [RECORDED, FOUND]
    assert not node.reachable
    assert RECORDED in node.note and "No route to host" in node.note  # the first reason
    assert FOUND in node.note and "Connection refused" in node.note  # the retry's


@pytest.mark.parametrize("still_running", ["mdns-lookup", "retry-probe"])
def test_a_retry_still_running_at_the_deadline_keeps_the_recorded_address_reason(
    lab: Lab, still_running: str
) -> None:
    lab.pair(address=RECORDED)
    lab.answers[RECORDED] = _silent(RECORDED, "No route to host")
    if still_running == "mdns-lookup":
        lab.mdns = lab.hang
    else:
        lab.mdns = lambda: FOUND
        lab.answers[FOUND] = lab.hang
    started = time.monotonic()
    node = _lab_node(timeout=0.5)
    assert time.monotonic() - started < 5
    assert not node.reachable
    assert RECORDED in node.note and "No route to host" in node.note  # not lost to the deadline
    assert "time" in node.note.lower()  # and the retry's: it timed out


# --- the benign half ---------------------------------------------------------------------


def test_a_worker_that_answers_on_the_retry_is_reachable_with_no_note(lab: Lab) -> None:
    lab.pair(address=RECORDED)
    lab.answers[RECORDED] = _silent(RECORDED, "No route to host")
    lab.mdns = lambda: FOUND
    lab.answers[FOUND] = _status
    node = _lab_node()
    assert lab.probed == [RECORDED, FOUND]
    assert node.reachable and node.note == ""
    assert node.threads == 16
