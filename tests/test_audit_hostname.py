"""Unit tests of shidashi.audit -- every run records the machine that ran it (story 014, 2.1).

A worker's runs are pulled into the host's runs directory (story 010's sync); without
the hostname in the trail, the 5700X's durations would be read as the host's. The
manifest carries it as ``manifest["hostname"]`` (or, equivalently, the
``hostname`` input); a manifest of an older run, written before this field existed,
stays readable and simply has none.
"""

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit


def _manifest_hostname(manifest: dict[str, Any]) -> Any:
    return manifest.get("hostname") or (manifest.get("inputs") or {}).get("hostname")


def _open_and_close(runs: Path) -> Path:
    with audit.run(
        runs, command="assemble", argv=["shidashi", "assemble", "v3", "kde", "systemd"]
    ) as rec:
        with rec.step("install"):
            pass
        path = rec.path
    return path


_OLD_EVENTS = [
    {
        "ts": "2026-09-30T01:00:00.000+00:00",
        "t": 0,
        "run": "r-old",
        "kind": "run.start",
        "step": "",
        "command": "assemble",
        "argv": ["shidashi", "assemble", "v3", "kde", "systemd"],
        "pid": 1,
    },
    {
        "ts": "2026-09-30T01:10:00.000+00:00",
        "t": 600,
        "run": "r-old",
        "kind": "run.end",
        "step": "",
        "status": "ok",
        "duration_s": 600.0,
    },
]


def test_the_manifest_names_the_machine_and_an_older_run_has_none(tmp_path: Path) -> None:
    run_dir = _open_and_close(tmp_path / "runs")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert _manifest_hostname(manifest) == socket.gethostname()
    # a run recorded before the field existed stays readable, with no machine
    old = audit.build_manifest(_OLD_EVENTS)
    assert old["status"] == "ok" and old["command"] == "assemble"
    assert _manifest_hostname(old) is None


def test_the_events_of_a_run_name_the_machine_even_if_it_is_killed(tmp_path: Path) -> None:
    # a killed run has no manifest: its events alone must say where it ran
    run_dir = _open_and_close(tmp_path / "runs")
    events = audit.read_events(run_dir / "events.jsonl")
    host = socket.gethostname()
    named = [
        e
        for e in events
        if e.get("hostname") == host
        or (e.get("kind") == "input" and e.get("name") == "hostname" and e.get("value") == host)
    ]
    assert named, "no event records the hostname"
    # and it is known from the first events on, not only at the close
    assert events.index(named[0]) <= 3


def test_a_manifest_rebuilt_from_a_killed_runs_events_keeps_the_hostname(tmp_path: Path) -> None:
    run_dir = _open_and_close(tmp_path / "runs")
    events = audit.read_events(run_dir / "events.jsonl")
    # drop the close: as if the process died before writing the manifest
    alive = [e for e in events if e.get("kind") != "run.end"]
    assert _manifest_hostname(audit.build_manifest(alive)) == socket.gethostname()


def test_a_run_on_another_machine_records_that_machines_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # hostile collapse: two machines must never record the same name -- the
    # hostname is read when the run opens, not baked in
    import platform

    monkeypatch.setattr(socket, "gethostname", lambda: "bentoo-lab")
    monkeypatch.setattr(platform, "node", lambda: "bentoo-lab")
    run_dir = _open_and_close(tmp_path / "runs")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert _manifest_hostname(manifest) == "bentoo-lab"


def test_the_events_of_a_run_record_the_thread_count_of_its_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R2.7: a run's duration is scaled by --jobs / threads; the threads are the machine's
    import os

    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    run_dir = _open_and_close(tmp_path / "runs")
    events = audit.read_events(run_dir / "events.jsonl")
    threads = [
        e.get("value") for e in events if e.get("kind") == "input" and e.get("name") == "threads"
    ] or [e.get("threads") for e in events if "threads" in e]
    assert threads and threads[0] == 24, "no event records the machine's thread count"
