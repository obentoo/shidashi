"""Tests of shidashi.workers -- the worker registry and the name-keyed known_hosts pin.

``workers.json`` is the contract story 010 reads; ``known_hosts`` is read by ssh through
``HostKeyAlias=<name>``, so a name must never end with two keys (R4.3): ssh accepts a
host whose key matches ANY line naming it, and a left-over key lets an impostor in.

Requirements exercised: R4.1, R4.2, R4.3.
"""

import base64
import hashlib
import json
import os
import stat
import struct
from pathlib import Path

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


def _entry(name: str = "bentoo-lab", seed: str = "k1", address: str = "192.168.15.7"):
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
        entry.name = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        workers.WorkerEntry(**entry.model_dump(), password="bentoo")


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
