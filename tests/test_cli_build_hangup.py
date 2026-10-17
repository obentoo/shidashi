"""A hangup during `shidashi build` ends the build's run with its resume commands
recorded (story 014, 6.3; R4.10, R4.7).

When the terminal hangs up (SIGHUP) the build is stopped like any interrupt, but nothing
it prints can reach anyone: every write to stdout and stderr fails with ``EIO`` (a
hung-up tty). What remains is the build's audit run, so it must hold:

- its end event (``run.end``, status ``interrupted``) -- a run without one looks like a
  build still running;
- an ``interrupted`` step naming the signal, the images the signal stopped and, for
  every worker job left running, ``shidashi worker logs <w> <job> -f`` and
  ``shidashi worker sync pull <w> --arch <arch> --job <job>``;

and the command exits ``128 + SIGHUP`` = 129. A SIGINT (Ctrl+C, terminal alive) still
exits 130 and prints the same resume commands (R4.7).

A hangup is rarely ONE signal: the kernel hangs up the terminal's foreground group and a
``sudo`` in between relays its own SIGHUP too (2026-10-17: the build's last event was
the start of a step it was recording, a second after the session closed; no ``run.end``).
So a second SIGHUP arrives here while the build handles the first -- at its first write
to the dead terminal. Under the default disposition that signal kills the process with no
unwinding; in this process it is modelled by :class:`Killed`, raised from the signal
handler this test installs as the build's starting disposition. A build that survives a
repeated hangup never lets it reach that handler.

The build is driven by the fakes of tests/test_cli_build_schedule.py: its ``execute``
raises ``schedule.Interrupted(outcomes, jobs, signum)``. Here the terminal hangs up
inside ``execute``, before the interrupt is raised: from then on ``sys.stdout`` and
``sys.stderr`` are streams whose every write raises ``OSError(EIO)`` and that have no
descriptor to repoint.
"""

import errno
import io
import json
import signal
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from shidashi import schedule
from tests.test_cli_build_schedule import HOST, LAB, WORKER, Lab, _build, _node, lab

__all__ = ["lab"]  # the fixture, reused

ARCH = "v3"
LAB_TWO = "lab-two"
WORKER_TWO = _node(LAB_TWO, "worker", 16, 0.5)

#: Each worker job the signal left running, with its worker -- two, so a command that
#: pairs one worker with the other's job is caught.
JOBS = {LAB: "build-abc123", LAB_TWO: "build-def456"}

STOPPED = "v3-minimal-systemd"  # stopped by the signal on the host
FINISHED = "v3-kde-systemd"  # had ended on its own before the signal: not stopped


class Killed(BaseException):  # noqa: N818 -- what SIGHUP's default action does
    """The process killed by a SIGHUP left at its default disposition."""


class HungUp(io.TextIOBase):
    """A hung-up terminal: every write raises ``EIO``; nothing is buffered, so a flush
    has nothing to send. No descriptor: it cannot be pointed at /dev/null. The first
    write also brings the hangup's second SIGHUP (``on_first_write``)."""

    def __init__(self, on_first_write: Callable[[], None]) -> None:
        self.attempts = 0
        self._on_first_write = on_first_write

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, _s: str) -> int:
        self.attempts += 1
        self._on_first_write()
        raise OSError(errno.EIO, "Input/output error")

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


@dataclass
class Hangup:
    """What a hung-up build did: its exit, the writes it tried, and whether the second
    SIGHUP reached the default disposition (``killed``)."""

    streams: list[HungUp] = field(default_factory=list)
    second_sighup_sent: bool = False
    killed: bool = False
    exit_code: int | None = None
    exception: BaseException | None = None

    @property
    def attempts(self) -> int:
        return sum(s.attempts for s in self.streams)


def _interrupt(signum: int) -> schedule.Interrupted:
    return schedule.Interrupted(
        [
            schedule.Outcome(
                image=STOPPED,
                node=HOST.name,
                ok=False,
                duration_s=10.0,
                estimate_s=90.0,
                log=Path("/var/tmp/minimal.log"),
                error="interrupted",
            ),
            schedule.Outcome(
                image=FINISHED,
                node=HOST.name,
                ok=True,
                duration_s=50.0,
                estimate_s=90.0,
                log=None,
                isos=(Path("/var/tmp/iso/bentoo-v3-kde.iso"),),
            ),
        ],
        [
            schedule.WorkerJob(worker=w, job=j, results=Path(f"/var/tmp/wr/{w}/{j}"))
            for w, j in JOBS.items()
        ],
        signum,
    )


def _resume_commands(worker: str, job: str) -> tuple[str, str]:
    return (
        f"shidashi worker logs {worker} {job} -f",
        f"shidashi worker sync pull {worker} --arch {ARCH} --job {job}",
    )


@pytest.fixture
def hung_up_build(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], Hangup]]:
    """Run ``build`` whose terminal hangs up inside ``execute``, just before SIGHUP stops
    it; a second SIGHUP arrives at the build's first write to the dead terminal.

    The streams are swapped on ``sys`` directly: the CLI runner restores its own when
    the command returns. Its capture streams are kept referenced meanwhile -- dropped,
    they would be collected and close the buffers the runner reads at the end."""
    lab.nodes = [HOST, WORKER, WORKER_TWO]
    lab.interrupt = _interrupt(signal.SIGHUP)
    state = Hangup()
    captured: list[Any] = []
    inner = schedule.execute

    def second_sighup() -> None:
        if not state.second_sighup_sent:
            state.second_sighup_sent = True
            signal.raise_signal(signal.SIGHUP)

    def execute(*args: Any, **kw: Any) -> list[schedule.Outcome]:
        captured[:] = [sys.stdout, sys.stderr]
        state.streams[:] = [HungUp(second_sighup), HungUp(second_sighup)]
        sys.stdout, sys.stderr = state.streams
        return inner(*args, **kw)

    monkeypatch.setattr(schedule, "execute", execute)

    def default_action(_signum: int, _frame: object) -> None:
        raise Killed

    def run() -> Hangup:
        try:
            result = _build()
        except Killed as err:
            state.killed, state.exception = True, err
        else:
            state.exit_code, state.exception = result.exit_code, result.exception
        return state

    previous = signal.signal(signal.SIGHUP, default_action)
    try:
        yield run
    finally:
        signal.signal(signal.SIGHUP, previous)


def _build_events(root: Path) -> list[dict[str, Any]]:
    """The events of the build's audit run (one per line, a truncated last one skipped)."""
    for path in sorted((root / "runs").glob("*/events.jsonl")):
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if any(e.get("kind") == "run.start" and e.get("command") == "build" for e in events):
            return events
    raise AssertionError("no build run")


def _interrupted_step(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Every field the ``interrupted`` step recorded, at its start and its end; the step
    must have ended."""
    mine = [e for e in events if str(e.get("step", "")).split("/")[-1] == "interrupted"]
    kinds = [e.get("kind") for e in mine]
    assert "step.end" in kinds, (
        "no ended `interrupted` step in the build's run; its steps: "
        f"{[e.get('step') for e in events if e.get('kind') == 'step.end']}"
    )
    fields: dict[str, Any] = {}
    for event in mine:
        fields.update({k: v for k, v in event.items() if k not in ("ts", "t", "run", "kind")})
    return fields


def _as_text(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _lists_holding(fields: dict[str, Any], item: str) -> list[list[Any]]:
    """The step's list values (also a list kept as its string) that name ``item``."""
    found = []
    for value in fields.values():
        if isinstance(value, str) and value.startswith("["):
            try:
                value = json.loads(value.replace("'", '"'))
            except json.JSONDecodeError:
                continue
        if isinstance(value, list) and item in [str(v) for v in value]:
            found.append(value)
    return found


# --- a hangup (R4.10) --------------------------------------------------------------------


def test_a_hangup_exits_129_and_the_builds_run_has_its_end(
    lab: Lab, hung_up_build: Callable[[], Hangup]
) -> None:
    got = hung_up_build()
    assert got.attempts, "the build wrote nothing after the hangup"
    assert not got.killed, (
        "the hangup's second SIGHUP reached the default disposition while the build"
        " handled the first: the process would die there, its run without an end"
    )
    assert got.exit_code == 128 + signal.SIGHUP, (got.exit_code, repr(got.exception))
    events = _build_events(lab.root)
    ends = [e for e in events if e.get("kind") == "run.end"]
    assert len(ends) == 1, [e.get("kind") for e in events[-5:]]
    assert ends[0].get("status") == "interrupted", ends[0]


def test_a_hangup_records_the_signal_the_stopped_images_and_each_jobs_resume_commands(
    lab: Lab, hung_up_build: Callable[[], Hangup]
) -> None:
    got = hung_up_build()
    step = _interrupted_step(_build_events(lab.root))
    text = _as_text(step)

    # the signal, by name or by number
    assert "SIGHUP" in text or str(step.get("signal", step.get("signum"))) in ("1", "SIGHUP"), step

    # the stopped image -- and not the one that had ended on its own
    holding = _lists_holding(step, STOPPED)
    assert holding, f"no list naming the stopped image {STOPPED}: {step}"
    assert all(FINISHED not in [str(v) for v in lst] for lst in holding), holding

    # both commands of each job, each pairing the job with its own worker
    for worker, job in JOBS.items():
        for command in _resume_commands(worker, job):
            assert command in text, (command, step)
    for worker, job in zip(JOBS, reversed(JOBS.values()), strict=True):
        for command in _resume_commands(worker, job):
            assert command not in text, ("a worker paired with the other's job", command)
    assert not got.killed and got.exit_code == 129, (got.exit_code, repr(got.exception))


def test_the_step_is_recorded_even_when_every_terminal_write_fails(
    lab: Lab, hung_up_build: Callable[[], Hangup]
) -> None:
    got = hung_up_build()
    events = _build_events(lab.root)
    step = _interrupted_step(events)
    assert step.get("status", "ok") == "ok", step  # recorded, not failed by a dead terminal
    assert got.attempts > 0  # the build did try to write, and every write failed
    assert not got.killed and got.exit_code == 129, (got.exit_code, repr(got.exception))
    assert any(e.get("kind") == "run.end" for e in events)


# --- a Ctrl+C with the terminal alive (R4.7) ---------------------------------------------


def test_a_sigint_still_exits_130_and_prints_the_resume_commands(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER, WORKER_TWO]
    lab.interrupt = _interrupt(signal.SIGINT)
    result = _build()
    assert result.exit_code == 130, result.output
    for worker, job in JOBS.items():
        for command in _resume_commands(worker, job):
            assert command in result.output, (command, result.output)
    assert "minimal" in result.output and "interrupted" in result.output
    # the same record as a hangup's: the terminal that shows them may die later
    step = _interrupted_step(_build_events(lab.root))
    for worker, job in JOBS.items():
        for command in _resume_commands(worker, job):
            assert command in _as_text(step), (command, step)
