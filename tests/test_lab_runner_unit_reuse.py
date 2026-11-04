"""``lab/vm/builder.sh weekly`` clears the runner unit and, on a VM it did not boot, ends the
runner rather than the VM (story 032, task 13.1; R6.1, R6.5).

Contract (story.md R6.1, R6.5; design.md "lab/vm/builder.sh weekly", Error Handling "`weekly`
host side", "Runner never picks the job"): on a VM that was already up, ``weekly`` stops *and
clears* the runner unit ``shidashi-runner`` so the next cycle can start it again, removes
``/run/shidashi/jitconfig`` and leaves the VM up; the same clearing runs before
``systemd-run`` for a unit an earlier cycle left behind. Once the runner unit was attempted, an
abort on a VM this run did not boot -- a ``systemd-run`` failure, a runner that never exits
within ``WEEKLY_TIMEOUT`` -- ends the runner the same way, exits 1, and stops the VM only when
ending the runner fails.

Why a faithful systemd model (the 2026-11-04 audit gap): the real runner exits 100 + the job
result, so its ``RemainAfterExit=yes`` transient unit always ends ``failed``. On systemd 262
``systemctl stop`` on a failed unit returns 0 and leaves it loaded and failed, and a later
``systemd-run --unit=<same>`` fails "already loaded"; only ``systemctl reset-failed`` unloads
it. The shared ``FakeBuilderGuest`` does not model this (its ``systemd-run`` never refuses,
its ``stop``/``reset-failed`` are no-ops), so :class:`SystemdUnitReuseModel` wraps the guest's
``systemd-run`` and ``systemctl`` fakes for these tests only (the shared module is untouched):

- a unit is *loaded* while its record ``<guest state>/units/<unit>.json`` exists -- the shared
  fake already reports ``LoadState=not-found`` without it, so ``show``, ``is-active`` and
  ``is-failed`` (delegated to the shared fake) reflect every change below;
- ``systemd-run --unit=U`` refuses (exit 1, "was already loaded") while ``U`` is loaded,
  whatever its state; otherwise it runs the shared fake;
- ``systemctl stop U``: not loaded -> exit 5, "Unit U.service not loaded." (as systemd does);
  running, or exited successfully under ``RemainAfterExit`` -> unloaded, exit 0; failed ->
  exit 0, still loaded and reported ``failed`` at once;
- ``systemctl reset-failed U``: failed -> unloaded, exit 0; not loaded -> exit 1; otherwise a
  no-op, exit 0;
- two switches inject failures: ``systemd-run`` fails outright, and ``stop`` fails (exit 1,
  unit kept) on a loaded unit that is still running.

Every decision of the model is appended to ``<guest state>/unit-reuse.jsonl`` in the order the
guest made it. A guest command can run only while the fake host's VM is up (``vm run`` refuses
otherwise), so a guest event always precedes a ``vm stop`` of the same cycle.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._fake_builder_guest import (
    JITCONFIG,
    SANDBOX_REASON,
    FakeBuilderHost,
    guest_sandbox_works,
)
from tests.test_lab_runner_weekly_ownership import RUNNER_UNIT, calls_of, vm_marker

pytestmark = pytest.mark.skipif(not guest_sandbox_works(), reason=SANDBOX_REASON)

_REUSE_SYSTEMD_RUN = """
import json, os, sys
STATE, KEPT = %(state)r, %(kept)r
args, unit, i = sys.argv[1:], None, 0
while i < len(args) and args[i].startswith("-"):
    a = args[i]
    if a.startswith("--unit="):
        unit = a.split("=", 1)[1]
    elif a in ("-u", "--unit") and i + 1 < len(args):
        unit = args[i + 1]; i += 1
    i += 1
unit = unit.removesuffix(".service") if unit else None

def event(outcome):
    with open(os.path.join(STATE, "unit-reuse.jsonl"), "a") as log:
        log.write(json.dumps({"tool": "systemd-run", "unit": unit, "outcome": outcome}) + "\\n")

if os.path.exists(os.path.join(STATE, "reuse-systemd-run-fails")):
    event("failed")
    print("Failed to start transient service unit: Connection timed out", file=sys.stderr)
    sys.exit(1)
if unit and os.path.isfile(os.path.join(STATE, "units", unit + ".json")):
    event("refused-already-loaded")
    print(f"Failed to start transient service unit: Unit {unit}.service was already loaded "
          "or has a fragment file.", file=sys.stderr)
    sys.exit(1)
event("started")
os.execv(KEPT, [KEPT, *args])
"""

_REUSE_SYSTEMCTL = """
import json, os, subprocess, sys
STATE, KEPT = %(state)r, %(kept)r
args = sys.argv[1:]
words = [a for a in args if not a.startswith("-")]
verb = words[0] if words else ""
if verb not in ("stop", "reset-failed"):
    os.execv(KEPT, [KEPT, *args])

def event(unit, outcome):
    with open(os.path.join(STATE, "unit-reuse.jsonl"), "a") as log:
        log.write(json.dumps({"tool": "systemctl", "verb": verb, "unit": unit,
                              "outcome": outcome}) + "\\n")

rc = 0
for unit in (w.removesuffix(".service") for w in words[1:]):
    record = os.path.join(STATE, "units", unit + ".json")
    if not os.path.isfile(record):
        event(unit, "not-loaded")
        print(f"Failed to {verb} {unit}.service: Unit {unit}.service not loaded.",
              file=sys.stderr)
        rc = 5 if verb == "stop" else 1
        continue
    data = json.load(open(record))
    status = data["status"]
    if verb == "stop":
        if status is None and os.path.exists(os.path.join(STATE, "reuse-stop-fails")):
            event(unit, "stop-failed-while-running")
            print(f"Job for {unit}.service canceled.", file=sys.stderr)
            rc = 1
        elif status is None or status == 0:
            os.unlink(record)
            event(unit, "unloaded")
        else:
            data["polls"] = 1_000_000  # reported failed from now on, as systemd reports it
            json.dump(data, open(record, "w"))
            event(unit, "kept-loaded-failed")
    elif status is not None and status != 0:
        os.unlink(record)
        event(unit, "unloaded")
    else:
        event(unit, "not-failed")
# the shared fake runs after the model decided: it logs the verb, and its own reset-failed
# then finds the record this model already removed
subprocess.run([KEPT, *args])
sys.exit(rc)
"""


class SystemdUnitReuseModel:
    """systemd 262's transient-unit lifetime over the shared guest fakes (module docstring)."""

    def __init__(self, host: FakeBuilderHost) -> None:
        self.guest = host.guest
        self.units = self.guest.state / "units"
        for tool, body in (("systemd-run", _REUSE_SYSTEMD_RUN), ("systemctl", _REUSE_SYSTEMCTL)):
            real = self.guest.bin / tool
            kept = real.with_name(tool + ".fake")
            real.rename(kept)
            values = {"state": str(self.guest.state), "kept": str(kept)}
            real.write_text(f"#!{sys.executable}\n" + body % values, encoding="utf-8")
            real.chmod(0o755)

    def seed_failed_unit(self, unit: str, status: int) -> None:
        """A unit an earlier cycle left loaded and failed (its runner exited ``status``)."""
        self.units.mkdir(parents=True, exist_ok=True)
        record = self.units / f"{unit}.json"
        record.write_text(json.dumps({"status": status, "polls": 1_000_000}), encoding="utf-8")

    def fail_systemd_run(self) -> None:
        (self.guest.state / "reuse-systemd-run-fails").touch()

    def fail_stop_while_running(self) -> None:
        (self.guest.state / "reuse-stop-fails").touch()

    def loaded(self, unit: str) -> bool:
        return (self.units / f"{unit}.json").is_file()

    def events(self) -> list[dict[str, Any]]:
        log = self.guest.state / "unit-reuse.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def outcomes(self, tool: str, *, unit: str = RUNNER_UNIT) -> list[str]:
        return [e["outcome"] for e in self.events() if e["tool"] == tool and e["unit"] == unit]


@pytest.fixture
def reuse_host(tmp_path: Path) -> FakeBuilderHost:
    return FakeBuilderHost(tmp_path)


@pytest.fixture
def unit_model(reuse_host: FakeBuilderHost) -> SystemdUnitReuseModel:
    return SystemdUnitReuseModel(reuse_host)


def jitconfig_left(host: FakeBuilderHost) -> bool:
    return (host.guest.root / JITCONFIG.removeprefix("/")).exists()


def weekly_lines(done: Any) -> list[str]:
    return [line for line in (done.stdout + done.stderr).splitlines() if "weekly:" in line]


def test_two_weekly_cycles_on_a_vm_left_up_both_dispatch_and_exit_with_the_runners_status(
    reuse_host: FakeBuilderHost, unit_model: SystemdUnitReuseModel, tmp_path: Path
) -> None:
    vm_marker(tmp_path).touch()
    reuse_host.runner.exit_with(102)  # a failed job: the unit ends `failed`, as on the real VM
    first = reuse_host.builder("weekly")
    first_calls = reuse_host.calls()
    reuse_host.runner.exit_with(100)  # a passed job
    second = reuse_host.builder("weekly")
    calls = reuse_host.calls()
    second_calls = calls[len(first_calls) :]
    output = "\n".join(d.stdout + d.stderr for d in (first, second))

    # Hostile half first: no cycle powers off a VM it did not boot, nor boots one that was up.
    assert calls_of(calls, "vm", "stop") == [], f"weekly stopped a VM it did not boot:\n{output}"
    assert calls_of(calls, "vm", "start") == [], calls
    assert vm_marker(tmp_path).exists(), "the VM is no longer up after two cycles"
    assert "refused-already-loaded" not in unit_model.outcomes("systemd-run"), (
        f"the second cycle could not start {RUNNER_UNIT}: the first left it loaded\n{output}"
    )
    assert calls_of(first_calls, "ci", "dispatch"), f"cycle 1 never dispatched:\n{output}"
    assert calls_of(second_calls, "ci", "dispatch"), f"cycle 2 never dispatched:\n{output}"
    assert len(reuse_host.runner.runs()) == 2, f"the runner did not run once per cycle:\n{output}"
    assert first.returncode == 102, output
    # Hostile half: cycle 2's status is its own runner's, never the leftover unit's 102.
    assert second.returncode == 0, output
    assert not unit_model.loaded(RUNNER_UNIT), f"{RUNNER_UNIT} is still loaded after cycle 2"
    assert not jitconfig_left(reuse_host), f"{JITCONFIG} is still in the guest"


def test_a_failed_unit_left_by_an_earlier_cycle_is_cleared_before_systemd_run(
    reuse_host: FakeBuilderHost, unit_model: SystemdUnitReuseModel, tmp_path: Path
) -> None:
    vm_marker(tmp_path).touch()
    unit_model.seed_failed_unit(RUNNER_UNIT, 102)
    reuse_host.runner.exit_with(101)  # distinct from the leftover's 102, from 0 and from 1
    done = reuse_host.builder("weekly")
    output = done.stdout + done.stderr
    calls = reuse_host.calls()
    events = unit_model.events()

    assert calls_of(calls, "vm", "stop") == [], f"weekly stopped a VM it did not boot:\n{output}"
    assert unit_model.outcomes("systemd-run") == ["started"], f"{events}\n{output}"
    cleared = [
        i
        for i, e in enumerate(events)
        if e["tool"] == "systemctl" and e["unit"] == RUNNER_UNIT and e["outcome"] == "unloaded"
    ]
    started = [i for i, e in enumerate(events) if e["tool"] == "systemd-run"]
    assert cleared and cleared[0] < started[0], f"the leftover unit was not cleared first: {events}"
    assert calls_of(calls, "ci", "dispatch"), f"weekly never dispatched:\n{output}"
    assert len(reuse_host.runner.runs()) == 1, output
    # Hostile half: the leftover unit's 102 is not this cycle's result.
    assert done.returncode == 101, output


def test_a_systemd_run_failure_on_a_vm_left_up_exits_1_naming_the_step_without_vm_stop(
    reuse_host: FakeBuilderHost, unit_model: SystemdUnitReuseModel, tmp_path: Path
) -> None:
    vm_marker(tmp_path).touch()
    unit_model.fail_systemd_run()
    reuse_host.runner.exit_with(0)
    done = reuse_host.builder("weekly")
    output = done.stdout + done.stderr
    calls = reuse_host.calls()

    assert calls_of(calls, "vm", "stop") == [], f"weekly stopped a VM it did not boot:\n{output}"
    assert vm_marker(tmp_path).exists()
    assert unit_model.outcomes("systemd-run") == ["failed"], output
    assert calls_of(calls, "ci", "dispatch") == [], "weekly dispatched with no runner started"
    assert done.returncode == 1, output
    named = [line for line in weekly_lines(done) if "start" in line and RUNNER_UNIT in line]
    assert named, f"no `weekly:` line names starting {RUNNER_UNIT}:\n{output}"
    # The converse: the step that failed is the start, not the clearing that preceded it.
    assert not any("clear" in line for line in named), output


def test_a_timeout_on_a_vm_left_up_ends_the_runner_exits_1_and_leaves_the_vm_up(
    reuse_host: FakeBuilderHost, unit_model: SystemdUnitReuseModel, tmp_path: Path
) -> None:
    vm_marker(tmp_path).touch()
    reuse_host.guest.settings(active_polls=0, never_exits=True)
    done = reuse_host.builder("weekly", env={"WEEKLY_TIMEOUT": "3"}, timeout=180)
    output = done.stdout + done.stderr
    calls = reuse_host.calls()

    # Hostile half first: a timeout on a VM this run did not boot must not power it off.
    assert calls_of(calls, "vm", "stop") == [], f"weekly stopped a VM it did not boot:\n{output}"
    assert vm_marker(tmp_path).exists()
    assert calls_of(calls, "ci", "dispatch"), output
    assert "unloaded" in unit_model.outcomes("systemctl"), (
        f"the runner was not ended in the guest: {unit_model.events()}\n{output}"
    )
    assert not unit_model.loaded(RUNNER_UNIT), f"{RUNNER_UNIT} is still loaded"
    assert not jitconfig_left(reuse_host), f"{JITCONFIG} is still in the guest"
    assert done.returncode == 1, output


def test_when_ending_the_runner_fails_during_the_timeout_abort_vm_stop_is_the_fallback(
    reuse_host: FakeBuilderHost, unit_model: SystemdUnitReuseModel, tmp_path: Path
) -> None:
    vm_marker(tmp_path).touch()
    reuse_host.guest.settings(active_polls=0, never_exits=True)
    unit_model.fail_stop_while_running()
    done = reuse_host.builder("weekly", env={"WEEKLY_TIMEOUT": "3"}, timeout=180)
    output = done.stdout + done.stderr
    calls = reuse_host.calls()

    assert calls_of(calls, "vm", "start") == [], calls
    # The fallback follows an attempt: a guest command runs only while the VM is up.
    assert "stop-failed-while-running" in unit_model.outcomes("systemctl"), (
        f"weekly never tried to end the runner before the VM: {unit_model.events()}\n{output}"
    )
    assert calls_of(calls, "vm", "stop"), (
        f"the runner could not be ended, yet the VM is up:\n{output}"
    )
    assert not vm_marker(tmp_path).exists()
    assert done.returncode == 1, output
