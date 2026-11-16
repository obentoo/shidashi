"""``shidashi vm start --dev`` choosing where its VM runs, and the start on a worker as
bentoolkit sees it: one JSON line on stdout, the documented exit codes, one line on
stderr for a failure (shidashi/vm_dev_remote_cli.py, shidashi/vm_dev_cli.py).

The worker is a ``FakeWorkerShell``; shipping the code is stood in for. The last two
tests run the CLI in a child process and end it with a signal while the worker is still
booting the VM: what a start that is stopped -- or killed outright -- leaves behind.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import config, remote, vm, vm_dev, vm_dev_record, vm_dev_remote, worker
from shidashi.cli import app
from tests._dev_relay import FakeShipHead
from tests._fake_worker_shell import SHIPPED, FakeWorkerShell
from tests.test_vm_dev import _info

_REPO = Path(__file__).resolve().parents[1]
_ISO_NAME = "bentoo-2026.11.12-minimal-systemd-v3.iso"


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorkerShell]:
    shell = FakeWorkerShell.install(tmp_path, monkeypatch)
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: _info())
    monkeypatch.setattr(worker, "ship_head", FakeShipHead(shell))
    yield shell
    shell.close()


@pytest.fixture
def iso(tmp_path: Path) -> Path:
    path = tmp_path / "isos" / _ISO_NAME
    path.parent.mkdir()
    path.write_bytes(b"an ISO")
    return path


@pytest.fixture
def local_starts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """``vm_dev.start_dev`` stood in for: each call is a VM "started on this host"."""
    started: list[dict[str, Any]] = []

    def start_here(**kw: Any) -> vm_dev.DevVm:
        started.append(kw)
        return vm_dev.DevVm(kw["name"], 43, Path("/i.iso"), "p1", [], Path("/s"))

    monkeypatch.setattr(vm_dev, "start_dev", start_here)
    return started


def _start(*args: str) -> Any:
    return CliRunner().invoke(app, ["vm", "start", "--dev", *args])


# --- where the VM runs --------------------------------------------------------------------


def test_worker_runs_the_vm_there_and_prints_one_json_line(
    lab: FakeWorkerShell, iso: Path, local_starts: list[dict[str, Any]]
) -> None:
    result = _start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso))
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 1, "stdout carries exactly one line: bentoolkit parses it"
    line = json.loads(lines[0])
    assert (line["name"], line["ready"], line["worker"]) == ("dev", True, "bentoo-lab")
    assert set(line) >= {"name", "cid", "iso", "pin", "binhost", "staged", "ready", "expires"}
    assert Path(line["iso"]).name == _ISO_NAME  # what bentoolkit names the VM by
    assert local_starts == [] and (lab.sessions / "dev" / "session.json").is_file()


def test_the_variable_alone_selects_the_worker(
    lab: FakeWorkerShell,
    iso: Path,
    local_starts: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIDASHI_DEV_WORKER", "bentoo-lab")
    assert config.dev_worker() == "bentoo-lab"
    result = _start("--name", "dev", "--iso", str(iso))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["worker"] == "bentoo-lab" and local_starts == []


def test_local_overrides_the_variable(
    lab: FakeWorkerShell,
    iso: Path,
    local_starts: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIDASHI_DEV_WORKER", "bentoo-lab")
    result = _start("--local", "--name", "dev", "--iso", str(iso))
    assert result.exit_code == 0, result.output
    assert "worker" not in json.loads(result.stdout)
    assert [kw["name"] for kw in local_starts] == ["dev"] and lab.ssh_calls() == []
    assert local_starts[0]["memory"] == "8G" and local_starts[0]["cpus"] == 8


def test_the_worker_option_wins_over_the_variable(
    lab: FakeWorkerShell, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_DEV_WORKER", "some-other-box")
    result = _start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso))
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_variable_is_no_worker(
    lab: FakeWorkerShell,
    local_starts: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("SHIDASHI_DEV_WORKER", value)
    assert config.dev_worker() is None
    assert _start("--name", "dev").exit_code == 0
    assert len(local_starts) == 1 and lab.ssh_calls() == []


def test_without_option_or_variable_the_vm_runs_here(
    lab: FakeWorkerShell, local_starts: list[dict[str, Any]]
) -> None:
    assert _start("--name", "dev").exit_code == 0
    assert len(local_starts) == 1 and lab.ssh_calls() == []


def test_a_relayed_start_never_relays_again(
    lab: FakeWorkerShell, local_starts: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # on the worker: its own environment names a worker, and the relay's mark is set
    monkeypatch.setenv("SHIDASHI_DEV_WORKER", "bentoo-lab")
    monkeypatch.setenv("SHIDASHI_DEV_RELAYED", "1")
    assert _start("--name", "dev").exit_code == 0
    assert len(local_starts) == 1 and lab.ssh_calls() == []


# --- usage errors: exit 2, before any contact -----------------------------------------------


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["--dev", "--worker", "bentoo-lab", "--local"], "--worker and --local exclude each other"),
        (["x.iso", "--worker", "bentoo-lab"], "--worker needs --dev"),
        (["x.iso", "--local"], "--local needs --dev"),
        (["--dev", "--worker", "bentoo-lab", "--name", "a/b"], "cannot name a session on a worker"),
        (["--dev", "--worker", "bentoo-lab", "--name", "-x"], "cannot name a session on a worker"),
        (["--dev", "--worker", "bentoo-lab", "--pins-json", "{}"], "--pins-json is what a relay"),
        (["--dev", "--worker", "bentoo-lab", "--display", "sdl"], "--display shows a VM on this"),
        (["--dev", "--worker", "bentoo-lab", "--max-life", "0"], "--max-life"),
        (["--dev", "--worker", "bentoo-lab", "--uefi"], "--dev decides --uefi itself"),
    ],
    ids=["both", "worker-plain", "local-plain", "slash", "dash", "pins", "display", "life", "uefi"],
)
def test_a_usage_error_exits_2_and_contacts_nobody(
    lab: FakeWorkerShell, local_starts: list[dict[str, Any]], args: list[str], said: str
) -> None:
    result = CliRunner().invoke(app, ["vm", "start", *args])
    assert result.exit_code == 2, result.output
    assert said in result.output
    assert lab.ssh_calls() == [] and local_starts == []


def test_the_help_names_the_two_options_and_hides_the_relays_own() -> None:
    start = " ".join(CliRunner().invoke(app, ["vm", "start", "--help"]).output.split())
    assert "--worker" in start and "--local" in start and "SHIDASHI_DEV_WORKER" in start
    assert "--pins-json" not in start
    assert "--every-dev" not in CliRunner().invoke(app, ["vm", "stop", "--help"]).output


# --- failures: one line on stderr, the documented code ----------------------------------------


def _failed(result: Any, code: int = 1) -> str:
    assert result.exit_code == code, result.output
    assert result.stdout == "", "a failed start prints no line for bentoolkit to parse"
    return str(result.stderr)


def test_a_worker_that_is_not_paired_is_the_error_worker_run_gives(
    lab: FakeWorkerShell, iso: Path
) -> None:
    said = _failed(_start("--worker", "nobody", "--name", "dev", "--iso", str(iso)))
    assert said == "error: no paired worker named 'nobody' (paired: bentoo-lab)\n"
    assert lab.ssh_calls() == []


def test_a_worker_that_does_not_answer_exits_1(lab: FakeWorkerShell, iso: Path) -> None:
    lab.unreachable()
    said = _failed(_start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso)))
    assert said.startswith("error: ") and "is unreachable" in said
    assert vm_dev_record.read_record("dev") is None


def test_a_running_job_exits_1_and_names_it(lab: FakeWorkerShell, iso: Path) -> None:
    lab.running_job("release-v3")
    said = _failed(_start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso)))
    assert said.count("\n") == 1 and "shidashi-job-release-v3" in said
    assert vm_dev_record.read_record("dev") is None


def test_no_iso_to_boot_exits_3(lab: FakeWorkerShell) -> None:
    said = _failed(_start("--worker", "bentoo-lab", "--name", "dev"), code=vm_dev.NO_ISO_EXIT)
    assert vm_dev.NO_ISO_EXIT == 3 and "no minimal ISO for v3/systemd" in said


def test_a_start_that_fails_there_exits_1_with_the_workers_reason(
    lab: FakeWorkerShell, iso: Path
) -> None:
    lab.plan("start", exit=1, stderr="error: the guest setup failed (exit 1): no disk\n")
    said = _failed(_start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso)))
    assert "vm start --dev failed on bentoo-lab (exit 1)" in said
    assert "the guest setup failed (exit 1): no disk" in said
    assert vm_dev_record.read_record("dev") is None


def test_a_name_recorded_on_a_worker_refuses_a_vm_on_this_host(
    lab: FakeWorkerShell, iso: Path, local_starts: list[dict[str, Any]]
) -> None:
    assert _start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso)).exit_code == 0
    for args in (["--dev", "--local", "--name", "dev"], [str(iso), "--name", "dev", "--no-wait"]):
        result = CliRunner().invoke(app, ["vm", "start", *args])
        said = _failed(result)
        assert "'dev' is recorded on worker bentoo-lab" in said
        assert "shidashi vm stop --name dev" in said
    assert local_starts == []


# --- reaching the worker as `worker run` does -------------------------------------------------


def test_only_a_first_contact_that_got_no_answer_is_tried_at_another_address(
    lab: FakeWorkerShell, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[str] = []

    def moved(target: remote.Remote, request: Any, **_kw: Any) -> dict[str, object]:
        tried.append(target.address)
        if target.address == lab.address:
            raise vm_dev_remote.NoFirstAnswer(target.name, target.address, "No route to host")
        return {"name": request.name, "ready": True, "worker": target.name}

    monkeypatch.setattr(vm_dev_remote, "start_on_worker", moved)
    monkeypatch.setattr(remote, "find", lambda _name, **_kw: "192.0.2.77")
    result = _start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso))
    assert result.exit_code == 0, result.output
    assert tried == [lab.address, "192.0.2.77"]


def test_a_worker_lost_after_the_start_began_is_not_started_twice(
    lab: FakeWorkerShell, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[str] = []

    def lost(target: remote.Remote, _request: Any, **_kw: Any) -> dict[str, object]:
        tried.append(target.address)
        raise remote.RemoteUnreachable(target.name, target.address, "Connection reset")

    monkeypatch.setattr(vm_dev_remote, "start_on_worker", lost)
    monkeypatch.setattr(remote, "find", lambda _name, **_kw: "192.0.2.77")
    said = _failed(_start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso)))
    assert tried == [lab.address] and "Connection reset" in said


# --- the audit trail --------------------------------------------------------------------------


def test_a_start_on_a_worker_is_an_audited_run_with_no_secret_in_it(
    lab: FakeWorkerShell, iso: Path
) -> None:
    assert _start("--worker", "bentoo-lab", "--name", "dev", "--iso", str(iso)).exit_code == 0
    (run,) = Path(os.environ["SHIDASHI_RUNS"]).iterdir()
    text = (run / "events.jsonl").read_text()
    events = [json.loads(line) for line in text.splitlines()]
    assert events[0]["command"] == "vm-dev-worker"
    kinds = [event["kind"] for event in events]
    assert "vm.dev.worker.probe" in kinds and "vm.dev.worker.start" in kinds
    steps = [event["step"] for event in events if event["kind"] == "step.end"]
    assert steps == ["vm.dev.worker.ship", "vm.dev.worker.iso", "vm.dev.worker.boot"]
    (sent,) = [e for e in events if e["kind"] == "step.end" and e["step"].endswith(".iso")]
    assert (sent["action"], sent["bytes_sent"]) == ("sent", 6) and sent["duration_s"] >= 0
    assert "not a real key" not in text and "id_ed25519" not in text  # the worker's key
    assert (events[-1]["kind"], events[-1]["status"]) == ("run.end", "ok")


# --- a start that is stopped, or killed, while the worker boots -------------------------------

_CHILD = (
    "import signal\n"
    "from shidashi import vm, worker\n"
    "from tests.test_vm_dev import _info\n"
    # a test run in the background starts with SIGINT ignored: Ctrl+C must raise here
    "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
    "vm.read_build_info = lambda _iso: _info()\n"
    f"worker.ship_head = lambda _remote, **_kw: ({SHIPPED!r}, 0)\n"
    "from shidashi.cli import app\n"
    "app()\n"
)


def _starting_child(lab: FakeWorkerShell, iso: Path) -> subprocess.Popen[bytes]:
    """The CLI starting ``dev`` on the worker, held there while the "VM boots"."""
    lab.plan("start", hold_s=4)
    lab.shipped_tree()
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, "vm", "start", "--dev", "--worker", "bentoo-lab"]
        + ["--name", "dev", "--iso", str(iso)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )
    deadline = time.monotonic() + 60
    while not lab.shidashi_calls():
        assert time.monotonic() < deadline and child.poll() is None, "the start never began"
        time.sleep(0.05)
    return child


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_start_stopped_while_the_worker_boots_leaves_nothing_anywhere(
    lab: FakeWorkerShell, iso: Path, signum: signal.Signals
) -> None:
    child = _starting_child(lab, iso)
    try:
        assert vm_dev_record.read_record("dev") is not None  # written before the boot
        child.send_signal(signum)
        code = child.wait(timeout=60)
        assert child.stdout is not None and child.stdout.read() == b""
    finally:
        child.kill()
        child.wait()
    assert code == (130 if signum == signal.SIGINT else 128 + signum)
    assert vm_dev_record.read_record("dev") is None
    time.sleep(4.5)  # past the moment the worker would have brought the VM up
    assert not (lab.sessions / "dev").exists(), "the worker went on and started the VM"
    assert not (lab.sessions / "dev.start.pid").exists()


def test_a_start_killed_outright_leaves_a_record_that_vm_stop_cleans_up(
    lab: FakeWorkerShell, iso: Path
) -> None:
    """bentoolkit cancels with SIGKILL, then calls ``vm stop``: nothing can be undone by
    the start, so the stop must find what it began -- and end it before the VM is up."""
    child = _starting_child(lab, iso)
    child.kill()
    child.wait()
    record = vm_dev_record.read_record("dev")
    assert record is not None and (record.worker, record.expires) == ("bentoo-lab", None)
    stopped = CliRunner().invoke(app, ["vm", "stop", "--name", "dev"])
    assert stopped.exit_code == 0, stopped.output
    assert vm_dev_record.read_record("dev") is None
    time.sleep(4.5)
    assert not (lab.sessions / "dev").exists(), "the VM came up after its stop"
