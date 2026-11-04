"""``lab/vm/builder.sh weekly`` stops the builder VM only when it booted it (story 032, task
12.1; R6.1).

Contract (story.md R6.1; design.md "lab/vm/builder.sh weekly", Error Handling "`weekly` host
side"): when the VM was down, ``weekly`` boots it and stops it after the runner exits. When the
VM was already up -- a ``builder.sh job`` may be running there -- ``weekly`` leaves it up:
instead of ``vm stop`` it stops the runner unit ``shidashi-runner`` and removes
``/run/shidashi/jitconfig`` in the guest, so the next cycle can reuse the unit name and no
runner config outlives the cycle. Either way the host's releases are pruned (``release prune
--keep 4 --purge-pruned``) and the exit code is the runner's. On the VM-was-up path a failure
to stop the unit or remove the config is reported and exits 1; prune still runs.

Runs the real script in ``FakeBuilderHost`` (tests/_fake_builder_guest.py). The fake host
keeps the VM's state in ``<tmp_path>/vm-running`` (``vm start`` creates it, ``vm stop``
removes it, ``vm run`` refuses without it): creating it before the cycle is "the VM was
already up".
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests._fake_builder_guest import (
    JITCONFIG,
    SANDBOX_REASON,
    FakeBuilderHost,
    guest_sandbox_works,
)

pytestmark = pytest.mark.skipif(not guest_sandbox_works(), reason=SANDBOX_REASON)

RUNNER_UNIT = "shidashi-runner"
#: a guest command that stops the runner unit, whatever flags it carries
STOPS_UNIT = re.compile(r"systemctl\b[^;&|]*\bstop\b[^;&|]*\b" + RUNNER_UNIT + r"\b")


@pytest.fixture
def weekly_host(tmp_path: Path) -> FakeBuilderHost:
    return FakeBuilderHost(tmp_path)


def vm_marker(tmp_path: Path) -> Path:
    """Where ``FakeBuilderHost`` records that the VM is running (see its ``_HOST_PYTHON``)."""
    return tmp_path / "vm-running"


def calls_of(calls: list[dict[str, Any]], *prefix: str) -> list[int]:
    return [i for i, c in enumerate(calls) if c["args"][: len(prefix)] == list(prefix)]


def option(args: list[str], name: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def guest_unit_stops(host: FakeBuilderHost) -> list[list[str]]:
    """Every ``systemctl stop shidashi-runner[.service]`` the guest ran."""
    log = host.guest.state / "systemctl.jsonl"
    if not log.is_file():
        return []
    stops = []
    for line in log.read_text(encoding="utf-8").splitlines():
        argv = json.loads(line)["argv"]
        words = [a.removesuffix(".service") for a in argv if not a.startswith("-")]
        if words[:1] == ["stop"] and RUNNER_UNIT in words[1:]:
            stops.append(argv)
    return stops


def stops_the_unit(call: dict[str, Any]) -> bool:
    return call["args"][:2] == ["vm", "run"] and bool(STOPS_UNIT.search(" ".join(call["args"])))


def assert_pruned(calls: list[dict[str, Any]], host: FakeBuilderHost) -> None:
    pruned = calls_of(calls, "release", "prune")
    assert len(pruned) == 1, f"release prune did not run exactly once: {calls}"
    args = calls[pruned[0]]["args"]
    assert option(args, "--keep") == "4", args
    assert "--purge-pruned" in args, args
    assert str(host.out / "releases") in " ".join(args), args


@pytest.mark.parametrize("status", [0, 3], ids=["runner-passed", "runner-failed"])
def test_weekly_leaves_a_vm_it_did_not_boot_running_and_stops_only_the_runner(
    weekly_host: FakeBuilderHost, tmp_path: Path, status: int
) -> None:
    vm_marker(tmp_path).touch()
    weekly_host.runner.exit_with(status)
    done = weekly_host.builder("weekly")
    output = done.stdout + done.stderr
    calls = weekly_host.calls()

    # Hostile half first: the VM this cycle did not boot must not be powered off.
    assert calls_of(calls, "vm", "stop") == [], f"weekly stopped a VM it did not boot: {calls}"
    assert calls_of(calls, "vm", "start") == [], f"weekly booted a VM that was up: {calls}"
    assert vm_marker(tmp_path).exists(), "the VM is no longer up after the cycle"

    assert len(weekly_host.runner.runs()) == 1, "the runner did not run exactly once"
    assert guest_unit_stops(weekly_host), f"the guest never ran `systemctl stop {RUNNER_UNIT}`"
    left = weekly_host.guest.root / JITCONFIG.removeprefix("/")
    assert not left.exists(), f"{JITCONFIG} is still in the guest after the cycle"

    dispatched = calls_of(calls, "ci", "dispatch")
    stopped_unit = [i for i, c in enumerate(calls) if stops_the_unit(c)]
    assert dispatched and stopped_unit and dispatched[0] < stopped_unit[-1], calls

    assert_pruned(calls, weekly_host)
    # Hostile halves: a failed runner is not a passed cycle, nor a passed one a failed one.
    assert done.returncode == status, output


def test_weekly_boots_a_vm_that_was_down_and_stops_it_after_the_runner(
    weekly_host: FakeBuilderHost, tmp_path: Path
) -> None:
    # The converse half: the VM this cycle booted must not be left running.
    assert not vm_marker(tmp_path).exists()
    weekly_host.runner.exit_with(3)
    done = weekly_host.builder("weekly")
    calls = weekly_host.calls()

    started = calls_of(calls, "vm", "start")
    stopped = calls_of(calls, "vm", "stop")
    dispatched = calls_of(calls, "ci", "dispatch")
    assert started and stopped and dispatched, calls
    assert started[0] < dispatched[0] < stopped[-1], calls
    assert not vm_marker(tmp_path).exists(), "weekly left running a VM it booted"
    assert_pruned(calls, weekly_host)
    assert done.returncode == 3, done.stdout + done.stderr


def test_weekly_whose_unit_stop_fails_on_a_vm_it_did_not_boot_exits_1_and_still_prunes(
    weekly_host: FakeBuilderHost, tmp_path: Path
) -> None:
    vm_marker(tmp_path).touch()
    # `systemctl stop` fails in the guest; every other systemctl verb is the fake's.
    real = weekly_host.guest.bin / "systemctl"
    kept = real.with_name("systemctl.fake")
    real.rename(kept)
    real.write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do [ "$a" = stop ] && { echo "Failed to stop unit" >&2; exit 5; }; done\n'
        f'exec {kept} "$@"\n',
        encoding="utf-8",
    )
    real.chmod(0o755)
    weekly_host.runner.exit_with(0)

    done = weekly_host.builder("weekly")
    output = done.stdout + done.stderr
    calls = weekly_host.calls()

    stop_attempts = [c for c in calls if stops_the_unit(c)]
    assert stop_attempts, f"weekly never tried to stop {RUNNER_UNIT} in the guest: {calls}"
    # A passed runner with a unit that would not stop is not a passed cycle.
    assert done.returncode == 1, output
    assert_pruned(calls, weekly_host)
