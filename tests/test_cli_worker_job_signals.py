"""A stopped ``worker job`` ends its own audit run (story 014, R4.11; task 6.2).

``build`` stops a worker child with SIGTERM, and a closed terminal sends SIGHUP. Either
way the host-side ``worker job`` must record the end of its audit run with the signal and
exit ``128 + signum`` -- and send nothing to the worker: its job keeps running there,
resumable with ``worker logs -f`` and ``worker sync pull``.

Signal handlers only work on the main thread, so each case runs ``worker job`` in a real
subprocess: a small driver fakes the transport (``worker.job`` blocks, or returns at
once) and records anything that would reach the worker (ssh, rsync, or a worker/remote
call other than the faked job).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

import shidashi

REPO = Path(shidashi.__file__).resolve().parent.parent

DRIVER = textwrap.dedent(
    """
    import json, os, signal, subprocess, sys, time
    from pathlib import Path

    mode, ready, sent, report = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])

    from shidashi import cli, workers
    from shidashi import remote as remote_mod
    from shidashi import worker as worker_mod

    def record(what):
        with sent.open("a") as fh:
            fh.write(what + "\\n")

    # anything that would reach the worker is recorded, never run
    real_popen = subprocess.Popen

    class Recording(real_popen):
        def __init__(self, args, *a, **kw):
            argv = [args] if isinstance(args, (str, bytes)) else list(args)
            if argv and os.path.basename(str(argv[0])) in ("ssh", "rsync", "scp"):
                record("exec " + " ".join(map(str, argv)))
            super().__init__(args, *a, **kw)

    subprocess.Popen = Recording

    def recorder(name):
        def call(*a, **kw):
            record(name)
            raise RuntimeError(name + " must not be called")
        return call

    for name in ("poweroff", "logs", "run_command", "pull", "push", "status",
                 "job_state", "active_jobs", "job_active", "job_ended", "_start_unit"):
        if hasattr(worker_mod, name):
            setattr(worker_mod, name, recorder("worker." + name))
    for name in ("run", "stream", "put_tree", "check"):
        if hasattr(remote_mod, name):
            setattr(remote_mod, name, recorder("remote." + name))

    entry = workers.WorkerEntry(
        name="lab",
        address="192.0.2.10",
        host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFake",
        host_key_fingerprint="SHA256:fakefakefakefakefakefakefakefakefakefakefak",
        paired_at="2026-10-17T00:00:00Z",
    )
    cli._paired = lambda name: entry

    def previous_term(signum, frame):  # the handler in force before the command
        pass

    def previous_hup(signum, frame):
        pass

    if mode == "return":
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGHUP, previous_hup)

    def fake_job(remote, entry, job, args, **kw):
        during = {
            "term_routed": signal.getsignal(signal.SIGTERM) not in (previous_term, signal.SIG_DFL),
            "hup_routed": signal.getsignal(signal.SIGHUP) not in (previous_hup, signal.SIG_DFL),
        }
        ready.write_text(json.dumps(during))
        if mode == "block":
            while True:  # the job is running on the worker; the follow never ends
                time.sleep(0.05)
        return worker_mod.JobResult(
            exit_code=0, log=None, isos=(), run_ids=(), duration_s=0.0, followed=False
        )

    worker_mod.job = fake_job

    code = None
    try:
        cli.app(
            ["worker", "job", "lab", "probe", "--", "build", "--arch", "v3"],
            prog_name="shidashi",
        )
    except SystemExit as exit_:
        code = exit_.code
    finally:
        report.write_text(json.dumps({
            "code": code,
            "term_restored": signal.getsignal(signal.SIGTERM) is previous_term,
            "hup_restored": signal.getsignal(signal.SIGHUP) is previous_hup,
        }))
    sys.exit(code if isinstance(code, int) else 1)
    """
)


def _start(tmp_path: Path, mode: str) -> tuple[subprocess.Popen[bytes], dict[str, Path]]:
    paths = {
        "driver": tmp_path / "driver.py",
        "ready": tmp_path / "ready",
        "sent": tmp_path / "sent",
        "report": tmp_path / "report",
        "runs": tmp_path / "runs",
    }
    paths["driver"].write_text(DRIVER)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    env["SHIDASHI_RUNS"] = str(paths["runs"])
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["SHIDASHI_CACHE"] = str(tmp_path / "cache")
    env["SHIDASHI_SCRATCH"] = str(tmp_path / "scratch")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(paths["driver"]),
            mode,
            str(paths["ready"]),
            str(paths["sent"]),
            str(paths["report"]),
        ],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc, paths


def _wait_ready(proc: subprocess.Popen[bytes], ready: Path) -> None:
    deadline = time.monotonic() + 60
    while not ready.exists():
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(
                f"worker job ended before its job started: {proc.returncode}\n{err.decode()}"
            )
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail("worker job never reached its job")
        time.sleep(0.05)
    time.sleep(0.2)  # inside the follow's loop


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, str]:
    try:
        _out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("worker job did not end after the signal")
    return proc.returncode, err.decode(errors="replace")


def _the_run(runs: Path) -> list[dict[str, Any]]:
    """The events of the one ``worker-job`` audit run."""
    found = []
    for events in sorted(runs.glob("*/events.jsonl")):
        lines = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
        first = lines[0] if lines else {}
        if first.get("kind") == "run.start" and first.get("command") == "worker-job":
            found.append(lines)
    assert len(found) == 1, f"expected one worker-job run under {runs}, found {len(found)}"
    return found[0]


def _mentions(event: dict[str, Any], *needles: str) -> bool:
    ignored = {"ts", "t", "run", "kind", "step", "duration_s"}
    text = " ".join(str(v) for k, v in event.items() if k not in ignored)
    return any(needle in text for needle in needles)


@pytest.mark.parametrize(
    ("signum", "code", "name"),
    [(signal.SIGTERM, 143, "SIGTERM"), (signal.SIGHUP, 129, "SIGHUP")],
)
def test_a_signal_ends_the_audit_run_and_exits_128_plus_signum(
    tmp_path: Path, signum: int, code: int, name: str
) -> None:
    proc, paths = _start(tmp_path, "block")
    _wait_ready(proc, paths["ready"])

    proc.send_signal(signum)
    returncode, stderr = _finish(proc)

    assert returncode == code, f"{name}: exit {returncode}, expected {code}\n{stderr}"
    events = _the_run(paths["runs"])
    ends = [e for e in events if e.get("kind") == "run.end"]
    assert len(ends) == 1, f"{name}: the worker-job run has no end recorded"
    assert _mentions(ends[0], str(code), name), f"{name}: the end does not name it: {ends[0]}"
    run_dir = paths["runs"] / events[0]["run"]
    assert (run_dir / "manifest.json").is_file()  # the run was closed, not cut


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_sends_nothing_to_the_worker(tmp_path: Path, signum: int) -> None:
    proc, paths = _start(tmp_path, "block")
    _wait_ready(proc, paths["ready"])

    proc.send_signal(signum)
    _finish(proc)

    sent = paths["sent"].read_text() if paths["sent"].exists() else ""
    assert sent == "", f"the stopped worker job reached the worker:\n{sent}"


def test_the_previous_handlers_are_restored_after_a_normal_return(tmp_path: Path) -> None:
    proc, paths = _start(tmp_path, "return")
    returncode, stderr = _finish(proc)

    assert returncode == 0, stderr
    during = json.loads(paths["ready"].read_text())
    assert during == {"term_routed": True, "hup_routed": True}, (
        f"SIGTERM/SIGHUP were not routed during the command: {during}"
    )
    report = json.loads(paths["report"].read_text())
    assert report["term_restored"] is True
    assert report["hup_restored"] is True
