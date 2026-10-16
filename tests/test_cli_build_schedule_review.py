"""Tech review of `shidashi build` across the workers (story 014, 4.2, fix cycle 1).

- A child whose argv asks for no ``--result-file`` -- a worker share (``@<worker>``) and
  the host's serial share (``schedule.SERIAL_KEY``) -- is not failed by having none: on
  exit 0 it ends ``done`` and its trail step is ``ok``, also when a signal stopped the
  build; a non-zero exit still fails it, and an assemble child that lost the result file
  it was asked for still fails.
- ``dirty_files`` runs git as root on the user's checkout: it takes no optional lock
  (``--no-optional-locks``, so the user's index is never rewritten root-owned) and runs
  no fsmonitor hook of the checkout (``-c core.fsmonitor=false``).
- A build whose plan cannot be recorded (the audit trail could not open) does not run;
  ``--plan`` still prints the plan.
"""

import json
import signal
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import audit, parallel, schedule
from shidashi.cli import app

runner = CliRunner()
pytestmark = pytest.mark.usefixtures("host_only_planner")

#: The children the fake ``execute`` starts through the CLI's runner: two worker shares,
#: the host's serial share, and one assemble child that was asked for a result file.
ARGVS = {
    "@bentoo-lab": ["true", "worker", "job"],
    "@lab-two": ["true", "worker", "job"],
    schedule.SERIAL_KEY: ["true", "build", "--serial"],
    "kde": ["true", "assemble", "--result-file", "/nowhere/kde.json"],
}


@dataclass
class Lab:
    root: Path
    exits: dict[str, int] = field(default_factory=dict)
    interrupt: int | None = None
    executed: int = 0


def _result(key: str, exit_code: int, error: str | None, logs: Path) -> parallel.ChildResult:
    return parallel.ChildResult(
        image=key,
        exit_code=exit_code,
        run_id=None,
        log=logs / f"{key}.log",
        duration_s=2.0,
        isos=(),
        error=error,
    )


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Lab]:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    state = Lab(root=tmp_path)

    def run_children(argvs: Any, *, logs: Path) -> list[parallel.ChildResult]:
        out = []
        for key in argvs:
            code = state.exits.get(key, 0)
            if state.interrupt is not None and code < 0:
                out.append(_result(key, code, "interrupted", logs))
            else:
                # what parallel._read_result says of a child that wrote no result file
                out.append(_result(key, code, "no result file", logs))
        if state.interrupt is not None:
            raise parallel.Interrupted(out, signum=state.interrupt)
        return out

    def execute(plan: Any, specs: Any, estimates: Any, **kw: Any) -> list[schedule.Outcome]:
        state.executed += 1
        try:
            kw["runner"](dict(ARGVS), logs=kw["logs"])
        except parallel.Interrupted as err:
            raise schedule.Interrupted([], [], err.signum) from err
        return [
            schedule.Outcome(
                image=key, node=node, ok=True, duration_s=1.0, estimate_s=1.0, log=None
            )
            for key, node in plan.assignments.items()
        ]

    monkeypatch.setattr(parallel, "run_children", run_children)
    monkeypatch.setattr(schedule, "execute", execute)
    yield state


def _steps(root: Path) -> dict[str, dict[str, Any]]:
    for path in sorted((root / "runs").glob("*/manifest.json")):
        manifest = json.loads(path.read_text())
        if manifest.get("command") == "build":
            return {str(s["step"]): s for s in manifest["steps"]}
    raise AssertionError("no build run")


def _build(*args: str) -> Any:
    return runner.invoke(
        app, ["build", "v3", "systemd", "--images", "minimal", "--skip-factory", *args]
    )


# --- children with no result file asked for (finding 1) ----------------------------------


def test_a_worker_and_the_serial_share_ending_0_are_done_and_recorded_ok(lab: Lab) -> None:
    lab.exits = {"@lab-two": 1}
    result = _build("--local")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    for key in ("@bentoo-lab", schedule.SERIAL_KEY):
        assert any(line.startswith(f"{key}  done") for line in lines), result.output
    assert any(line.startswith("@lab-two  failed") for line in lines), result.output
    # an assemble child asked for a result file and wrote none: still failed
    assert any(line.startswith("kde  failed") for line in lines), result.output
    steps = _steps(lab.root)
    assert steps["worker:bentoo-lab"]["status"] == "ok"
    assert steps["worker:bentoo-lab"]["error"] is None
    assert steps[f"assemble:{schedule.SERIAL_KEY}"]["status"] == "ok"
    assert steps["worker:lab-two"]["status"] == "error"
    assert steps["assemble:kde"]["status"] == "error"
    assert steps["assemble:kde"]["error"] == "no result file"


def test_an_interrupt_records_a_worker_that_had_ended_0_as_ok(lab: Lab) -> None:
    lab.interrupt = signal.SIGTERM
    lab.exits = {"@lab-two": -signal.SIGTERM, schedule.SERIAL_KEY: -signal.SIGTERM}
    result = _build("--local")
    assert result.exit_code == 128 + signal.SIGTERM, result.output
    lines = result.output.splitlines()
    assert any(line.startswith("@bentoo-lab  done") for line in lines), result.output
    assert any(line.startswith("@lab-two  failed") for line in lines), result.output
    steps = _steps(lab.root)
    assert steps["worker:bentoo-lab"]["status"] == "ok"
    assert steps["worker:lab-two"]["status"] == "error"
    assert steps["worker:lab-two"]["error"] == "interrupted"
    assert steps[f"assemble:{schedule.SERIAL_KEY}"]["status"] == "error"


# --- git as root on the user's checkout (finding 2) --------------------------------------


def test_dirty_files_takes_no_optional_lock_and_runs_no_fsmonitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def run(argv: Any, *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append([str(x) for x in argv])
        return subprocess.CompletedProcess(argv, 0, stdout=b"?? new.txt\0", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert schedule.dirty_files() == ["new.txt"]
    (argv,) = seen
    assert Path(argv[0]).name == "git"
    assert argv[1] == "--no-optional-locks"  # a git option: before any subcommand
    pairs = list(zip(argv, argv[1:], strict=False))
    assert ("-c", "core.fsmonitor=false") in pairs
    assert any(a == "-c" and b.startswith("safe.directory=") for a, b in pairs)
    assert argv.index("status") > max(i for i, a in enumerate(argv) if a == "-c")


# --- a plan that cannot be recorded (finding 3) ------------------------------------------


@pytest.fixture
def no_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise OSError(30, "Read-only file system", "/var/log/shidashi/runs")

    monkeypatch.setattr(audit, "run", refuse)


@pytest.mark.usefixtures("no_trail")
def test_a_build_whose_plan_cannot_be_recorded_does_not_run(lab: Lab) -> None:
    result = _build("--local")
    assert result.exit_code == 1, result.output
    assert lab.executed == 0
    text = " ".join(result.output.split())
    assert "plan" in text and "not recorded" in text, result.output
    assert "Read-only file system" in text


@pytest.mark.usefixtures("no_trail")
def test_plan_still_prints_without_a_trail(lab: Lab) -> None:
    result = _build("--local", "--plan")
    assert result.exit_code == 0, result.output
    assert "v3-minimal-systemd" in result.output
    assert lab.executed == 0
