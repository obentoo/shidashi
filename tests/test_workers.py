"""Tests of shidashi.workers -- the worker registry and the name-keyed known_hosts pin.

``workers.json`` is the contract story 010 reads; ``known_hosts`` is read by ssh through
``HostKeyAlias=<name>``, so a name must never end with two keys (R4.3): ssh accepts a
host whose key matches ANY line naming it, and a left-over key lets an impostor in.

A provisioned worker is recorded before it ever answered: no address, no CPU flags, no
image yet (R1.3); the first contact completes it (R3.5). Every ``workers.json`` written
before story 020 must still load.

Requirements exercised: R4.1, R4.2, R4.3; story 020: R1.3, R3.5.
"""

import base64
import hashlib
import json
import os
import stat
import struct
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shidashi import config, workers


def _ed25519_line(seed: str, comment: str = "") -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _entry(
    name: str = "bentoo-lab", seed: str = "k1", address: str = "192.168.15.7"
) -> workers.WorkerEntry:
    key = _ed25519_line(seed)
    return workers.WorkerEntry(
        name=name,
        address=address,
        host_key=key,
        host_key_fingerprint=_fingerprint(key),
        paired_at="2026-10-05T12:00:00+00:00",
        cpu_flags=("avx2", "bmi2"),
        image="20261005T1200",
    )


def _keys_for(known_hosts: Path, name: str) -> list[str]:
    """Every key ssh would accept for ``name`` (HostKeyAlias): its host-field list
    names it, whatever else the list carries."""
    found = []
    for line in known_hosts.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        hosts, *key = line.split()
        if name in hosts.split(","):
            found.append(" ".join(key[:2]))
    return found


# --- workers_dir --------------------------------------------------------------------


def test_workers_dir_lives_under_xdg_data_home_read_on_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "a"))
    assert config.workers_dir() == tmp_path / "a" / "shidashi" / "worker"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "b"))
    assert config.workers_dir() == tmp_path / "b" / "shidashi" / "worker"


def test_workers_dir_defaults_to_the_xdg_default_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.workers_dir() == tmp_path / ".local" / "share" / "shidashi" / "worker"


# --- WorkerEntry and the registry (R4.2) --------------------------------------------


def test_a_worker_entry_is_frozen_and_refuses_unknown_fields() -> None:
    entry = _entry()
    with pytest.raises(ValidationError):
        entry.name = "other"
    with pytest.raises(ValidationError):
        workers.WorkerEntry.model_validate({**entry.model_dump(), "password": "bentoo"})


def test_load_registry_of_an_absent_file_is_empty(tmp_path: Path) -> None:
    assert workers.load_registry(tmp_path / "workers.json") == {}


def test_save_then_load_round_trips_every_field_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "worker" / "workers.json"
    path.parent.mkdir()
    entries = {"bentoo-lab": _entry(), "spare": _entry("spare", "k2", "192.168.15.9")}
    workers.save_registry(path, entries)
    assert workers.load_registry(path) == entries
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["workers.json"]  # no temp left


def test_a_failed_save_leaves_the_previous_registry_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "workers.json"
    workers.save_registry(path, {"bentoo-lab": _entry()})
    before = path.read_bytes()

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", _fail)
    with pytest.raises(OSError):
        workers.save_registry(path, {"spare": _entry("spare", "k2")})
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "content",
    ["{not json", '["a list"]', json.dumps({"bentoo-lab": {"name": "bentoo-lab"}})],
)
def test_a_malformed_registry_raises_naming_its_path_and_is_not_rewritten(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "workers.json"
    path.write_text(content)
    with pytest.raises(workers.RegistryError, match=str(path)):
        workers.load_registry(path)
    assert path.read_text() == content


# --- pin (R4.1, R4.3) ---------------------------------------------------------------
# Hostile halves first: names that look alike but are other workers keep their lines,
# then lines that name THIS worker in another shape all go -- then the plain case.


def test_pin_keeps_the_lines_of_other_workers_with_lookalike_names(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    others = [
        f"lab2 {_ed25519_line('lab2')}",
        f"lab-old {_ed25519_line('lab-old')}",
        f"xlab {_ed25519_line('xlab')}",
        f"other {_ed25519_line('other', 'lab')}",  # the name only as a key comment
        f"192.168.15.7 {_ed25519_line('by-address')}",  # same address, not the name
    ]
    known_hosts.write_text("\n".join(others) + "\n")
    new = _ed25519_line("lab-new")
    workers.pin(known_hosts, "lab", new)
    lines = known_hosts.read_text().splitlines()
    for line in others:
        assert line in lines, f"pin('lab') removed another worker's line: {line}"
    assert _keys_for(known_hosts, "lab") == [" ".join(new.split()[:2])]


def test_pin_removes_every_older_key_for_the_name_whatever_its_shape(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        f"lab {_ed25519_line('old-1')}\n"
        f"lab,192.168.15.7 {_ed25519_line('old-2')}\n"  # a host list naming it
        f"192.168.15.8,lab {_ed25519_line('old-3')}\n"
        "lab ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTY=\n"  # other type
    )
    new = _ed25519_line("lab-new")
    workers.pin(known_hosts, "lab", new)
    assert _keys_for(known_hosts, "lab") == [" ".join(new.split()[:2])]


def test_pin_appends_one_name_and_key_line_and_keeps_comments(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("# workers paired by shidashi kyomei\n")
    key = _ed25519_line("lab")
    workers.pin(known_hosts, "lab", key)
    lines = known_hosts.read_text().splitlines()
    assert lines[0] == "# workers paired by shidashi kyomei"
    assert [ln.split()[:3] for ln in lines[1:]] == [["lab", *key.split()[:2]]]


def test_pin_creates_an_absent_file_and_repinning_the_same_key_stays_one_line(
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    key = _ed25519_line("lab")
    workers.pin(known_hosts, "lab", key)
    workers.pin(known_hosts, "lab", key)
    assert _keys_for(known_hosts, "lab") == [" ".join(key.split()[:2])]
    assert known_hosts.read_text().endswith("\n")


# --- A registry entry that exists before the first boot (story 020: R1.3, R3.5) -----

KEY = _ed25519_line("provisioned-k1")


def _provisioned(entry: workers.WorkerEntry) -> object:
    """The entry's ``provisioned`` flag (read through the dump: type-checks before 020)."""
    return entry.model_dump()["provisioned"]


def _provisioned_raw(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "bentoo-lab",
        "host_key": KEY,
        "host_key_fingerprint": _fingerprint(KEY),
        "paired_at": "2026-10-10T12:00:00+00:00",
        "provisioned": True,
    }
    raw.update(over)
    return raw


def test_a_misspelt_field_is_still_refused_not_read_as_a_missing_address() -> None:
    """Hostile: with ``address`` optional, a typo (``addres``) must not quietly load as
    an entry with no address -- unknown fields stay refused."""
    with pytest.raises(ValidationError) as caught:
        workers.WorkerEntry.model_validate(_provisioned_raw(addres="192.168.15.7"))
    # the typo is the ONLY complaint: every other field of a provisioned entry is valid
    assert [(e["type"], e["loc"]) for e in caught.value.errors()] == [
        ("extra_forbidden", ("addres",))
    ]


def test_an_absent_address_and_a_null_address_are_the_same_entry() -> None:
    """Hostile (the converse): ``"address": null`` and no ``address`` key both mean
    "not known yet"."""
    absent = workers.WorkerEntry.model_validate(_provisioned_raw())
    null = workers.WorkerEntry.model_validate(_provisioned_raw(address=None))
    assert absent == null
    assert absent.address is None


def test_a_provisioned_entry_needs_no_address_flags_or_image() -> None:
    entry = workers.WorkerEntry.model_validate(_provisioned_raw())
    assert _provisioned(entry) is True
    assert entry.address is None
    assert entry.cpu_flags == ()
    assert entry.image == ""
    assert entry.host_key_fingerprint == _fingerprint(KEY)


def test_a_provisioned_entry_is_still_frozen() -> None:
    entry = workers.WorkerEntry.model_validate(_provisioned_raw())
    with pytest.raises(ValidationError):
        entry.address = "192.168.15.7"


def test_a_pre_020_registry_loads_unchanged_and_not_provisioned(tmp_path: Path) -> None:
    """A ``workers.json`` written before story 020 (every field set, no ``provisioned``)."""
    path = tmp_path / "workers.json"
    old = {
        "bentoo-lab": {
            "name": "bentoo-lab",
            "address": "192.168.15.7",
            "host_key": KEY,
            "host_key_fingerprint": _fingerprint(KEY),
            "paired_at": "2026-10-05T12:00:00+00:00",
            "cpu_flags": ["avx2", "bmi2"],
            "image": "20261005T1200",
        }
    }
    path.write_text(json.dumps(old, indent=1) + "\n")
    entry = workers.load_registry(path)["bentoo-lab"]
    assert entry.address == "192.168.15.7"
    assert entry.cpu_flags == ("avx2", "bmi2")
    assert entry.image == "20261005T1200"
    assert _provisioned(entry) is False


def test_a_provisioned_entry_round_trips_through_the_registry_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "workers.json"
    entry = workers.WorkerEntry.model_validate(_provisioned_raw())
    workers.save_registry(path, {"bentoo-lab": entry})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["bentoo-lab"]["address"] is None
    loaded = workers.load_registry(path)["bentoo-lab"]
    assert loaded == entry
    assert _provisioned(loaded) is True


def test_a_provisioned_entry_completed_on_first_contact_round_trips(tmp_path: Path) -> None:
    """R3.5: the address, flags and image filled in later are kept like any entry's."""
    path = tmp_path / "workers.json"
    entry = workers.WorkerEntry.model_validate(_provisioned_raw())
    done = entry.model_copy(
        update={"address": "192.168.15.42", "cpu_flags": ("avx2",), "image": "20261010T1200"}
    )
    workers.save_registry(path, {"bentoo-lab": done})
    loaded = workers.load_registry(path)["bentoo-lab"]
    assert (loaded.address, loaded.cpu_flags, loaded.image) == (
        "192.168.15.42",
        ("avx2",),
        "20261010T1200",
    )
