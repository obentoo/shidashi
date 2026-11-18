"""A job has priority over the dev VMs on its worker (shidashi/vm_dev_evict.py).

Both halves: on the worker, ``vm stop --every-dev`` ends every session marked dev and
no other; on the host, ``worker.job`` -- run against tests/_fake_worker.py's worker --
sees the dev sessions in its opening probe, stops them with the tree it has just
shipped, names them in its audit trail, and does not start beside one that will not
stop. ``worker.ship_head``, which the dev VM relay ships its own tree with, is checked
here too: it is the same push and the same ``git archive`` a job makes.
"""

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import audit, config, ownership, remote, vm, vm_dev_evict, worker
from shidashi.cli import app
from tests._dev_relay import InMemoryTrail
from tests._fake_worker import FakeWorker, seed_host_cache

_SPEC = {"iso": "/b.iso", "uefi": False, "cid": 61, "memory": "1G", "cpus": 1}
ASSEMBLE = ["assemble", "v3", "kde", "systemd"]
FACTORY = ["factory", "v3", "minimal", "systemd"]


def _session(root: Path, name: str, *, dev: bool) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(json.dumps({**_SPEC, "dev": dev}))
    (directory / "work.qcow2").write_bytes(b"a disk")
    return directory


# --- the probe's list ---------------------------------------------------------------------


def _listed(scratch: Path) -> list[str]:
    done = subprocess.run(
        ["/bin/sh", "-c", vm_dev_evict.list_script(str(scratch))],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stderr == ""
    return vm_dev_evict.listed_dev_sessions(done.stdout.splitlines())


def test_the_probe_lists_the_dev_sessions_and_no_other(tmp_path: Path) -> None:
    sessions = tmp_path / "vm"
    assert _listed(tmp_path) == []  # no session at all: no error either
    _session(sessions, "bentoo-build-41-1", dev=True)
    _session(sessions, "test-bios", dev=False)  # a boot test's: not a dev session
    _session(sessions, "h-proof-a", dev=True)
    (sessions / "empty").mkdir()
    (sessions / "expiry.jsonl").write_text("{}\n")
    assert _listed(tmp_path) == ["bentoo-build-41-1", "h-proof-a"]


def test_only_lines_naming_a_dev_session_are_read_as_one() -> None:
    lines = ["work=mounted", "devvm=a", "shidashi-job-x.service loaded", "devvm=b-2", ""]
    assert vm_dev_evict.listed_dev_sessions(lines) == ["a", "b-2"]


# --- the worker's side --------------------------------------------------------------------


def test_every_dev_session_is_ended_and_named_and_a_plain_one_is_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _session(tmp_path, "dev-a", dev=True)
    _session(tmp_path, "dev-b", dev=True)
    plain = _session(tmp_path, "mine", dev=False)
    (tmp_path / "expiry.jsonl").write_text("{}\n")
    assert vm_dev_evict.stop_every_dev_session(tmp_path) == 0
    said = capsys.readouterr()
    assert said.out == "devvm=dev-a\ndevvm=dev-b\n" and said.err == ""
    # ended as `vm stop` ends them: directory and work disk gone
    assert sorted(p.name for p in tmp_path.iterdir()) == ["expiry.jsonl", "mine"]
    assert (plain / "work.qcow2").is_file()


def test_a_session_that_will_not_end_is_said_and_the_others_still_go(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("dev-a", "dev-b", "dev-c"):
        _session(tmp_path, name, dev=True)
    ended: list[str] = []

    def end(directory: Path) -> None:
        if directory.name == "dev-b":
            raise vm.VmError("QEMU (pid 7) is still running: it was kept")
        ended.append(directory.name)

    assert vm_dev_evict.stop_every_dev_session(tmp_path, end_session=end) == 1
    said = capsys.readouterr()
    assert ended == ["dev-a", "dev-c"] and said.out == "devvm=dev-a\ndevvm=dev-c\n"
    assert said.err == (
        "error: dev session dev-b was not stopped: QEMU (pid 7) is still running: it was kept\n"
    )


def test_no_session_directory_at_all_is_nothing_to_stop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert vm_dev_evict.stop_every_dev_session(tmp_path / "none") == 0
    assert capsys.readouterr().out == ""


def test_the_hidden_option_stops_the_dev_sessions_of_the_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _session(vm.session_dir("x").parent, "dev-a", dev=True)
    kept = _session(vm.session_dir("x").parent, "bentoo", dev=False)
    result = CliRunner().invoke(app, ["vm", "stop", "--every-dev"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "devvm=dev-a\n"
    assert not vm.session_dir("dev-a").exists() and kept.is_dir()


# --- the host's side: evict ---------------------------------------------------------------


def _target() -> remote.Remote:
    return remote.Remote("bentoo-lab", "192.0.2.10", Path("/k"), Path("/kh"))


def test_evict_runs_the_command_and_records_who_was_stopped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def answering(_target: remote.Remote, command: str, **kw: Any) -> remote.RemoteResult:
        sent.append((command, kw))
        return remote.RemoteResult(command, 0, "devvm=dev-a\ndevvm=dev-b\n", "", 1.0)

    monkeypatch.setattr(vm_dev_evict, "run", answering)
    trail = InMemoryTrail()
    assert vm_dev_evict.evict(_target(), "the stop line", trail) == ["dev-a", "dev-b"]
    assert [command for command, _kw in sent] == ["the stop line"]
    assert sent[0][1]["input"] == ""  # never the caller's stdin
    assert trail.events == [
        {
            "kind": "worker.job.dev_sessions_stopped",
            "worker": "bentoo-lab",
            "sessions": ["dev-a", "dev-b"],
            "exit_code": 0,
        }
    ]
    assert "stopped the dev VM sessions dev-a, dev-b on bentoo-lab" in capsys.readouterr().err


def test_evict_refuses_to_go_on_when_a_session_was_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(_target: remote.Remote, command: str, **_kw: Any) -> remote.RemoteResult:
        err = "error: dev session dev-b was not stopped: QEMU (pid 7) is still running\n"
        return remote.RemoteResult(command, 1, "devvm=dev-a\n", err, 1.0)

    monkeypatch.setattr(vm_dev_evict, "run", failing)
    trail = InMemoryTrail()
    with pytest.raises(remote.SyncError) as kept:
        vm_dev_evict.evict(_target(), "the stop line", trail)
    assert kept.value.step == "stop the dev sessions" and kept.value.exit_code == 1
    assert "dev-b was not stopped" in kept.value.stderr_tail
    assert trail.events[0]["sessions"] == ["dev-a"] and trail.events[0]["exit_code"] == 1


# --- the host's side: worker.job ----------------------------------------------------------


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    seed_host_cache(w)
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path / "runs"))
    yield w
    w.close()


def _job(fw: FakeWorker, name: str, args: list[str]) -> tuple[Any, list[dict[str, Any]]]:
    """``worker.job``'s result and the events of its audit run."""
    with audit.run(config.runs_dir(), command="worker-job", argv=["worker", "job"]) as run:
        result = worker.job(
            fw.remote(),
            fw.entry(),
            name,
            args,
            allow_dirty=False,
            follow=True,
            results=fw.base / "results" / name,
        )
    return result, audit.read_events(run.path / "events.jsonl")


def test_a_job_stops_the_dev_sessions_first_and_its_trail_names_them(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fw.put("scratch/vm/bentoo-build-7-1/session.json", json.dumps({**_SPEC, "dev": True}))
    fw.put("scratch/vm/test-bios/session.json", json.dumps({**_SPEC, "dev": False}))
    seen: list[dict[str, Any]] = []

    def stopping(target: remote.Remote, command: str, **_kw: Any) -> remote.RemoteResult:
        before = fw.ssh_commands()
        seen.append(
            {
                "command": command,
                "shipped": any("tar -x" in c for c in before),
                "started": any("systemd-run" in c for c in before),
            }
        )
        return remote.RemoteResult(command, 0, "devvm=bentoo-build-7-1\n", "", 0.5)

    monkeypatch.setattr(vm_dev_evict, "run", stopping)
    result, events = _job(fw, "kde", ASSEMBLE)
    assert result.exit_code == 0 and fw.job_invocations()
    # once, with the tree the job has just shipped, before its unit exists
    assert seen == [
        {
            "command": worker.shipped_shell(fw.head, ["vm", "stop", "--every-dev"]),
            "shipped": True,
            "started": False,
        }
    ]
    (stopped,) = [e for e in events if e["kind"] == "worker.job.dev_sessions_stopped"]
    assert (stopped["worker"], stopped["sessions"]) == ("bentoo-lab", ["bentoo-build-7-1"])


def test_a_job_on_a_worker_without_dev_sessions_stops_nobody(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fw.put("scratch/vm/test-bios/session.json", json.dumps({**_SPEC, "dev": False}))

    def never(*_a: Any, **_kw: Any) -> remote.RemoteResult:
        raise AssertionError("a job with no dev session to stop went looking for one")

    monkeypatch.setattr(vm_dev_evict, "run", never)
    result, events = _job(fw, "kde", ASSEMBLE)
    assert result.exit_code == 0
    assert not [e for e in events if e["kind"] == "worker.job.dev_sessions_stopped"]
    assert not any("--every-dev" in c for c in fw.ssh_commands())


def test_a_job_does_not_start_beside_a_dev_session_that_will_not_stop(fw: FakeWorker) -> None:
    """The stop runs for real in the fake worker's shell, where the shipped tree is the
    fake checkout: its ``vm stop --every-dev`` fails, as a session that is kept does."""
    fw.put("scratch/vm/dev-a/session.json", json.dumps({**_SPEC, "dev": True}))
    with pytest.raises(remote.SyncError) as kept:
        _job(fw, "fac-v3", FACTORY)
    assert kept.value.step == "stop the dev sessions" and kept.value.exit_code != 0
    stop = [c for c in fw.ssh_commands() if "--every-dev" in c]
    assert stop == [worker.shipped_shell(fw.head, ["vm", "stop", "--every-dev"])]
    assert not any("systemd-run" in c for c in fw.ssh_commands()), "the unit was started"
    assert fw.job_invocations() == []
    assert ownership.current("v3") is None  # the lock a job that never started must not keep


# --- worker.ship_head ---------------------------------------------------------------------


def test_ship_head_sends_the_checkouts_head_and_the_venv(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    commit, sent = worker.ship_head(fw.remote())
    assert commit == fw.head and sent > 0
    assert (fw.work / "src" / commit / "shidashi" / "cli.py").read_text() == "# committed\n"
    assert (fw.work / "runtime" / "venv" / "pyvenv.cfg").is_file()
    assert capfd.readouterr().err == ""


def test_ship_head_ships_head_and_warns_of_what_is_not_committed(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.dirty()
    commit, _sent = worker.ship_head(fw.remote())
    assert commit == fw.head
    assert (fw.work / "src" / commit / "shidashi" / "cli.py").read_text() == "# committed\n"
    said = capfd.readouterr().err
    assert said.startswith("warning: shipping HEAD; the uncommitted changes to ")
    assert "shidashi/cli.py" in said and "are not included" in said
    assert "--allow-dirty" not in said  # `vm start` has no such option to name


def test_ship_head_refuses_a_worker_without_its_work_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = FakeWorker.install(tmp_path, monkeypatch, mounted=False)
    try:
        with pytest.raises(remote.SyncError, match="is not mounted on bentoo-lab"):
            worker.ship_head(w.remote())
        assert w.rsync_calls() == []
    finally:
        w.close()
