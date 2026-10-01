"""Unit tests of shidashi.audit -- the run's audit trail (events, manifest, report)."""

import datetime
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _run(tmp_path: Path, clock: _Clock | None = None) -> audit.Run:
    return audit.Run(
        tmp_path / "run1",
        command="assemble",
        argv=["shidashi", "assemble", "v3", "kde", "systemd"],
        clock=clock or _Clock(),
        wall=lambda: datetime.datetime(2026, 9, 30, 1, 2, 3, tzinfo=datetime.UTC),
        usage=lambda: (1.0, 0.5, 2048),
    )


def test_redact_argv_masks_the_value_of_secret_looking_names() -> None:
    argv = ["env", "FEATURES=-ccache", "GITHUB_TOKEN=abc", "--password=x", "DB_PASS=y", "emerge"]
    assert audit.redact_argv(argv) == [
        "env",
        "FEATURES=-ccache",
        "GITHUB_TOKEN=***",
        "--password=***",
        "DB_PASS=***",
        "emerge",
    ]


def test_a_step_records_start_and_end_with_duration_path_and_results(tmp_path: Path) -> None:
    clock = _Clock()
    run = _run(tmp_path, clock)
    with run.step("assemble"), run.step("install", targets=3) as step:
        clock.now += 42.5
        step.add(packages=1458)
    run.close("ok")

    events = audit.read_events(tmp_path / "run1" / "events.jsonl")
    kinds = [(e["kind"], e["step"]) for e in events]
    assert kinds == [
        ("run.start", ""),
        ("step.start", "assemble"),
        ("step.start", "assemble/install"),
        ("step.end", "assemble/install"),
        ("step.end", "assemble"),
        ("run.end", ""),
    ]
    end = events[3]
    assert end["duration_s"] == 42.5 and end["status"] == "ok"
    assert end["packages"] == 1458 and events[2]["targets"] == 3
    assert end["cpu_user_s"] == 0.0 and end["children_max_rss_kib"] == 2048


def test_a_failing_step_is_recorded_as_an_error_and_the_exception_propagates(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    with pytest.raises(RuntimeError, match="boom"), run.step("depclean"):
        raise RuntimeError("boom")
    run.close("error", "RuntimeError: boom")
    manifest = json.loads((tmp_path / "run1" / "manifest.json").read_text())
    assert manifest["status"] == "error"
    assert manifest["steps"][0]["status"] == "error"
    assert manifest["steps"][0]["error"] == "RuntimeError: boom"


def test_the_manifest_and_report_summarise_commands_artifacts_and_metrics(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    run.input("stage3", {"sha512": "ab" * 64})
    with run.step("squashfs"):
        run.command(["mksquashfs", "/r", "/o"], exit_code=0, duration_s=159.0)
        run.command(["emerge", "--depclean"], exit_code=1, duration_s=2.0)
        run.metric("squashfs.ratio", 2.7)
    iso = tmp_path / "bentoo.iso"
    iso.write_bytes(b"ISO")
    run.artifact(iso, role="iso")
    run.close("ok")

    manifest = json.loads((tmp_path / "run1" / "manifest.json").read_text())
    assert manifest["command"] == "assemble" and manifest["status"] == "ok"
    assert manifest["inputs"]["stage3"] == {"sha512": "ab" * 64}
    assert manifest["commands"] == {"count": 2, "failed": 1, "duration_s": 161.0}
    import hashlib

    assert manifest["artifacts"] == [
        {
            "path": str(iso),
            "role": "iso",
            "size_bytes": 3,
            "sha256": hashlib.sha256(b"ISO").hexdigest(),
        }
    ]
    assert manifest["metrics"][0]["name"] == "squashfs.ratio"
    report = (tmp_path / "run1" / "report.md").read_text()
    assert "| `squashfs` | ok |" in report and "| iso | " in report


def test_a_run_killed_inside_a_step_leaves_it_unfinished_in_the_manifest(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    run._stack.append("install")  # entered, never left: the process died
    run.event("step.start")
    events = audit.read_events(tmp_path / "run1" / "events.jsonl")
    manifest = audit.build_manifest(events)
    assert manifest["status"] == "unfinished"
    assert manifest["steps"] == [
        {"step": "install", "started": events[-1]["ts"], "status": "unfinished"}
    ]


def test_outside_a_run_the_recorder_drops_everything() -> None:
    recorder = audit.current()
    recorder.command(["emerge"], exit_code=0)
    with recorder.step("x") as step:
        step.add(n=1)
    assert recorder.attach("x", {}) is None


def test_run_makes_itself_current_and_closes_with_the_blocks_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit, "repo_state", lambda: {"commit": "abc", "dirty": False})
    with audit.run(tmp_path, command="factory", argv=["shidashi"], inputs={"flavor": "kde"}) as r:
        assert audit.current() is r
    assert audit.current() is not r
    manifest = json.loads((r.path / "manifest.json").read_text())
    assert manifest["status"] == "ok"
    assert manifest["inputs"] == {"repository": {"commit": "abc", "dirty": False}, "flavor": "kde"}

    with pytest.raises(ValueError), audit.run(tmp_path, command="x", argv=[]) as failed:
        raise ValueError("nope")
    manifest = json.loads((failed.path / "manifest.json").read_text())
    assert manifest["status"] == "error" and manifest["error"] == "ValueError: nope"


def test_parse_emerge_log_times_each_merge_inside_the_window() -> None:
    log = "\n".join(
        [
            "1000:  >>> emerge (1 of 2) dev-libs/a-1.0 to /",
            "1003:  ::: completed emerge (1 of 2) dev-libs/a-1.0 to /",
            "1004:  >>> emerge (2 of 2) dev-libs/b-2.0 to /",
            "2000:  >>> emerge (1 of 1) dev-libs/c-3.0 to /",
            "2010:  ::: completed emerge (1 of 1) dev-libs/c-3.0 to /",
        ]
    )
    merges = audit.parse_emerge_log(log, since=900, until=1500)
    assert merges == {
        "dev-libs/a-1.0": {"started": 1000, "ended": 1003, "duration_s": 3},
        "dev-libs/b-2.0": {"started": 1004},  # never completed
    }


def test_harvest_packages_reads_the_vdb_with_its_own_use_flags(tmp_path: Path) -> None:
    entry = tmp_path / "var/db/pkg/media-video/pipewire-1.6.9"
    entry.mkdir(parents=True)
    for name, text in {
        "SIZE": "4423680\n",
        "SLOT": "0/0.4\n",
        "repository": "gentoo\n",
        "IUSE": "+ffmpeg bluetooth -doc\n",
        "USE": "amd64 bluetooth elibc_glibc ffmpeg\n",
    }.items():
        (entry / name).write_text(text)
    packages = audit.harvest_packages(
        tmp_path,
        merges={"media-video/pipewire-1.6.9": {"started": 1, "ended": 5, "duration_s": 4}},
        reused=["media-video/pipewire-1.6.9"],
    )
    assert packages == [
        {
            "atom": "media-video/pipewire-1.6.9",
            "slot": "0/0.4",
            "repository": "gentoo",
            "size_bytes": 4423680,
            "use": ["bluetooth", "ffmpeg"],
            "source": "binpkg",
            "merge": {"started": 1, "ended": 5, "duration_s": 4},
        }
    ]


def test_the_container_records_every_command_in_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shidashi.container import Container

    def fake_run(cmd: Any, **_k: Any) -> Any:
        return subprocess.CompletedProcess(cmd, 3, stdout="a\nb\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(audit, "repo_state", lambda: {})
    with audit.run(tmp_path, command="t", argv=[]) as r:
        Container(tmp_path / "rootfs").run(["emerge", "GITHUB_TOKEN=s"], check=False)
    commands = [e for e in audit.read_events(r.path / "events.jsonl") if e["kind"] == "command"]
    assert len(commands) == 1
    assert commands[0]["argv"] == ["emerge", "GITHUB_TOKEN=***"]
    assert commands[0]["exit_code"] == 3 and commands[0]["output_lines"] == 2
