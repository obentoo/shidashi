"""``lab/vm/builder.sh weekly`` -- the host side of one weekly cycle (story 032, task 7.3;
R6.1, R6.2, R6.5).

Contract (story.md R6.1, R6.2, R6.5; design.md "lab/vm/builder.sh weekly", "Sequence
Diagrams", "Implementation Gotchas"): ``weekly`` boots the builder VM, mints one JIT runner
config (``shidashi ci jit-config``, the token through ``--token-file`` only), hands it to the
guest over ``vm run``'s stdin into ``/run/shidashi/jitconfig`` (0600) -- never in an
argument, never through a shared directory -- starts the runner as the transient unit
``shidashi-runner``, dispatches ``weekly-release.yml`` on ``main``, waits for the runner to
exit, stops the VM and prunes ``$STATE/out/releases`` to 4; it exits with the runner's
status. A runner that has not exited within ``WEEKLY_TIMEOUT`` stops the VM and exits 1.

Runs the real script in ``FakeBuilderHost`` (tests/_fake_builder_guest.py): every
``shidashi`` builder.sh runs is recorded, and ``vm run`` runs its command as root in
``FakeBuilderGuest`` with the stdin it was given; the runner is ``FakeActionsRunner``.
"""

import time
from typing import Any

import pytest

from tests._fake_builder_guest import (
    JITCONFIG,
    SANDBOX_REASON,
    FakeBuilderHost,
    guest_sandbox_works,
)

pytestmark = pytest.mark.skipif(not guest_sandbox_works(), reason=SANDBOX_REASON)


@pytest.fixture
def weekly_host(tmp_path: Any) -> FakeBuilderHost:
    return FakeBuilderHost(tmp_path)


def first(calls: list[dict[str, Any]], *prefix: str, after: int = -1) -> int:
    """The index of the first recorded ``shidashi`` call starting with ``prefix``."""
    for i, call in enumerate(calls):
        if i > after and call["args"][: len(prefix)] == list(prefix):
            return i
    raise AssertionError(f"no `shidashi {' '.join(prefix)}` after #{after}: {calls}")


def last(calls: list[dict[str, Any]], *prefix: str) -> int:
    found = [i for i, c in enumerate(calls) if c["args"][: len(prefix)] == list(prefix)]
    assert found, f"no `shidashi {' '.join(prefix)}`: {calls}"
    return found[-1]


def option(args: list[str], name: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


@pytest.mark.parametrize("status", [0, 3], ids=["runner-passed", "runner-failed"])
def test_weekly_runs_one_cycle_in_order_and_exits_with_the_runners_status(
    weekly_host: FakeBuilderHost, status: int
) -> None:
    weekly_host.runner.exit_with(status)
    done = weekly_host.builder("weekly")
    calls = weekly_host.calls()
    output = done.stdout + done.stderr
    # Hostile halves: a failed runner is not a passed cycle, nor a passed one a failed one.
    assert done.returncode == status, output

    booted = first(calls, "vm", "start")
    minted = first(calls, "ci", "jit-config")
    handed = next(
        (
            i
            for i, c in enumerate(calls)
            if c["args"][:2] == ["vm", "run"] and weekly_host.jit in c["stdin"]
        ),
        None,
    )
    assert handed is not None, f"the JIT config never reached the guest on stdin: {calls}"
    dispatched = first(calls, "ci", "dispatch")
    stopped = last(calls, "vm", "stop")
    assert booted < handed and minted < handed < dispatched < stopped, calls

    jit_args = calls[minted]["args"]
    assert option(jit_args, "--repo") == "obentoo/shidashi"
    assert option(jit_args, "--label") == "bentoo-builder"
    assert option(jit_args, "--runner-group-id")
    assert (option(jit_args, "--name") or "").startswith("builder-")
    assert option(jit_args, "--token-file")
    dispatch_args = calls[dispatched]["args"]
    assert option(dispatch_args, "--repo") == "obentoo/shidashi"
    assert option(dispatch_args, "--workflow") == "weekly-release.yml"
    assert option(dispatch_args, "--ref") == "main"
    assert option(dispatch_args, "--token-file")

    assert len(weekly_host.runner.runs()) == 1, "the runner did not run exactly once"
    assert weekly_host.runner.config_calls() == []
    units = [u["unit"] for u in weekly_host.guest.units_started()]
    assert "shidashi-runner" in units, units


def test_weekly_prunes_the_hosts_releases_to_four_after_the_vm_stopped(
    weekly_host: FakeBuilderHost,
) -> None:
    done = weekly_host.builder("weekly")
    assert done.returncode == 0, done.stdout + done.stderr
    calls = weekly_host.calls()
    pruned = first(calls, "release", "prune")
    assert first(calls, "vm", "stop") < pruned, calls
    args = calls[pruned]["args"]
    assert option(args, "--keep") == "4", args
    assert str(weekly_host.out / "releases") in args, args


def test_weekly_hands_the_jit_config_over_stdin_only_into_a_0600_file_in_run(
    weekly_host: FakeBuilderHost,
) -> None:
    done = weekly_host.builder("weekly")
    calls = weekly_host.calls()
    jit = weekly_host.jit
    carried = [c for c in calls if jit in c["stdin"]]
    assert len(carried) == 1 and carried[0]["args"][:2] == ["vm", "run"], calls
    for call in calls:
        assert jit not in " ".join(call["args"]), f"the JIT config was an argument: {call}"
    assert jit not in done.stdout + done.stderr
    # never through a shared directory: not in out/ (rw), the checkout (repo) or the cache
    for root in (weekly_host.out, weekly_host.checkout, weekly_host.cache):
        for path in root.rglob("*"):
            if path.is_file():
                assert jit not in path.read_text(errors="replace"), path
    seen = [u["jitconfig"] for u in weekly_host.guest.units_started()]
    left = weekly_host.guest.root / JITCONFIG.removeprefix("/")
    if left.is_file():
        seen.append(
            {"exists": True, "mode": left.stat().st_mode & 0o7777, "content": left.read_text()}
        )
    assert any(
        s.get("exists") and s.get("mode") == 0o600 and s.get("content", "").strip() == jit
        for s in seen
    ), f"{JITCONFIG} never held the config with mode 0600: {seen}"


def test_weekly_never_shows_or_passes_the_github_token(weekly_host: FakeBuilderHost) -> None:
    done = weekly_host.builder("weekly")
    assert done.returncode == 0, done.stdout + done.stderr
    token = weekly_host.token
    assert token not in done.stdout + done.stderr
    calls = weekly_host.calls()
    for command in (("ci", "jit-config"), ("ci", "dispatch")):
        made = [c for c in calls if c["args"][:2] == list(command)]
        assert made and all(option(c["args"], "--token-file") for c in made), calls
    for call in calls:
        assert token not in " ".join(call["args"]) and token not in call["stdin"], call
    assert not weekly_host.sudo_log.exists(), "weekly ran sudo"


def test_weekly_whose_runner_never_exits_stops_the_vm_and_exits_1(
    weekly_host: FakeBuilderHost,
) -> None:
    weekly_host.guest.settings(active_polls=0, never_exits=True)
    began = time.monotonic()
    done = weekly_host.builder("weekly", env={"WEEKLY_TIMEOUT": "3"}, timeout=180)
    assert done.returncode == 1, done.stdout + done.stderr
    assert time.monotonic() - began < 170
    calls = weekly_host.calls()
    assert first(calls, "vm", "start") < last(calls, "vm", "stop"), calls
