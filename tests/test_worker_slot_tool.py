"""contrib/worker/slot: the worker's one CPU and memory budget, run as the script it is.

Every case runs the real bash script against a temporary SLOT_ROOT with a fixed budget,
so nothing depends on this host's processors or memory.
"""

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

SLOT = Path(__file__).resolve().parent.parent / "contrib" / "worker" / "slot"


@pytest.fixture
def slot_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        SLOT_ROOT=str(tmp_path / "slots"),
        SLOT_CPUS="16",
        SLOT_MEMORY_GIB="40",
        SLOT_POLL="0.1",
        SLOT_MIN_FREE_GIB="0",
    )
    return env


def run_slot(
    env: dict[str, str], *args: str, timeout: float = 20
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SLOT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def holder(slot_env: dict[str, str]) -> Iterator[subprocess.Popen[str]]:
    """A reservation of 12 cpus / 30 GiB held by a sleeping command until the test ends."""
    proc = subprocess.Popen(
        [
            "bash",
            str(SLOT),
            "run",
            "--owner",
            "zeo:build",
            "--cpus",
            "12",
            "--memory",
            "30",
            "--",
            "sleep",
            "30",
        ],
        env=slot_env,
        text=True,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    wait_for(lambda: "owner=zeo:build" in run_slot(slot_env, "list").stdout)
    yield proc
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def wait_for(cond: object, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():  # type: ignore[operator]
            return
        time.sleep(0.05)
    raise AssertionError("condition never held")


def test_run_passes_the_exit_code_and_the_descriptor(slot_env: dict[str, str]) -> None:
    done = run_slot(
        slot_env,
        "run",
        "--owner",
        "t",
        "--cpus",
        "2",
        "--memory",
        "1",
        "--",
        "bash",
        "-c",
        'test -e "/proc/self/fd/$SLOT_FD" && echo "$SLOT_CPUS_GRANTED"; exit 7',
    )
    assert done.returncode == 7
    assert done.stdout.strip() == "2"


def test_a_request_that_does_not_fit_is_refused_naming_the_holder(
    slot_env: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    done = run_slot(
        slot_env,
        "run",
        "--owner",
        "lab:x",
        "--cpus",
        "8",
        "--memory",
        "4",
        "--wait",
        "0",
        "--",
        "true",
    )
    assert done.returncode == 75
    assert "zeo:build(12cpu,30G)" in done.stderr
    assert "free 4 cpus, 10 GiB" in done.stderr


def test_a_request_that_fits_beside_a_holder_runs_at_once(
    slot_env: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    done = run_slot(
        slot_env,
        "run",
        "--owner",
        "lab:y",
        "--cpus",
        "4",
        "--memory",
        "10",
        "--wait",
        "0",
        "--",
        "true",
    )
    assert done.returncode == 0, done.stderr


def test_a_waiter_queues_once_and_runs_when_the_holder_ends(
    slot_env: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    waiter = subprocess.Popen(
        [
            "bash",
            str(SLOT),
            "run",
            "--owner",
            "ci:42",
            "--cpus",
            "8",
            "--memory",
            "8",
            "--",
            "echo",
            "ran",
        ],
        env=slot_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    assert waiter.poll() is None, "the waiter must still be queued"
    os.killpg(holder.pid, signal.SIGKILL)
    out, err = waiter.communicate(timeout=20)
    assert waiter.returncode == 0
    assert out.strip() == "ran"
    assert err.count("slot: queued ci:42") == 1
    assert "slot: granted ci:42" in err


def test_a_dead_holder_leaves_nothing_behind(slot_env: dict[str, str], tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [
            "bash",
            str(SLOT),
            "run",
            "--owner",
            "dies",
            "--cpus",
            "16",
            "--memory",
            "40",
            "--",
            "sleep",
            "30",
        ],
        env=slot_env,
        start_new_session=True,
    )
    wait_for(lambda: "owner=dies" in run_slot(slot_env, "list").stdout)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    done = run_slot(
        slot_env,
        "run",
        "--owner",
        "next",
        "--cpus",
        "16",
        "--memory",
        "40",
        "--wait",
        "0",
        "--",
        "true",
    )
    assert done.returncode == 0, done.stderr
    leases = sorted(p.name for p in (tmp_path / "slots" / "leases").iterdir())
    assert not any(
        "dies" in (tmp_path / "slots" / "leases" / n).read_text()
        for n in leases
        if n.endswith(".lease")
    )


def test_a_detached_child_keeps_the_reservation_after_the_command_exits(
    slot_env: dict[str, str],
) -> None:
    # the dev VM case: the command starts QEMU in its own session and exits; QEMU
    # inherited the descriptor, so the reservation lives as long as QEMU does
    pidfile = Path(slot_env["SLOT_ROOT"]).parent / "child.pid"
    done = run_slot(
        slot_env,
        "run",
        "--owner",
        "vm:dev",
        "--cpus",
        "16",
        "--memory",
        "24",
        "--",
        "bash",
        "-c",
        f"setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $! >{pidfile}",
    )
    assert done.returncode == 0
    child = int(pidfile.read_text())
    try:
        assert "owner=vm:dev" in run_slot(slot_env, "list").stdout
        refused = run_slot(
            slot_env,
            "run",
            "--owner",
            "lab",
            "--cpus",
            "1",
            "--memory",
            "1",
            "--wait",
            "0",
            "--",
            "true",
        )
        assert refused.returncode == 75
    finally:
        os.kill(child, signal.SIGKILL)
    wait_for(lambda: "no live reservation" in run_slot(slot_env, "list").stdout)


def test_budget_limits_and_usage_errors(slot_env: dict[str, str]) -> None:
    too_big = run_slot(
        slot_env, "run", "--owner", "big", "--cpus", "1", "--memory", "41", "--", "true"
    )
    assert too_big.returncode == 69
    clamped = run_slot(
        slot_env,
        "run",
        "--owner",
        "wide",
        "--cpus",
        "64",
        "--memory",
        "1",
        "--",
        "bash",
        "-c",
        'echo "$SLOT_CPUS_GRANTED"',
    )
    assert clamped.returncode == 0 and clamped.stdout.strip() == "16"
    every = run_slot(
        slot_env,
        "run",
        "--owner",
        "all",
        "--cpus",
        "all",
        "--memory",
        "1",
        "--",
        "bash",
        "-c",
        'echo "$SLOT_CPUS_GRANTED"',
    )
    assert every.stdout.strip() == "16"
    half = run_slot(
        slot_env,
        "run",
        "--owner",
        "half",
        "--cpus",
        "half",
        "--memory",
        "1",
        "--",
        "bash",
        "-c",
        'echo "$SLOT_CPUS_GRANTED"',
    )
    assert half.stdout.strip() == "8"
    for bad in (
        ["--cpus", "x", "--memory", "1"],
        ["--cpus", "1", "--memory", "1g"],
        ["--cpus", "1", "--memory", "1", "--owner", "bad owner"],
    ):
        args = ["run", "--owner", "o", *bad, "--", "true"]
        assert run_slot(slot_env, *args).returncode == 64, bad
    assert (
        run_slot(slot_env, "run", "--owner", "o", "--cpus", "1", "--memory", "1").returncode == 64
    )


def test_a_run_publishes_itself_for_other_consumers(slot_env: dict[str, str]) -> None:
    installed = Path(slot_env["SLOT_ROOT"]) / "slot"
    assert (
        run_slot(
            slot_env, "run", "--owner", "p", "--cpus", "1", "--memory", "1", "--", "true"
        ).returncode
        == 0
    )
    assert installed.read_bytes() == SLOT.read_bytes()
    assert os.access(installed, os.X_OK)
    # a newer installed copy is never replaced by an older one
    installed.write_text("#!/bin/sh\nSLOT_VERSION=99\n")
    installed.chmod(0o755)
    run_slot(slot_env, "run", "--owner", "p", "--cpus", "1", "--memory", "1", "--", "true")
    assert "SLOT_VERSION=99" in installed.read_text()


def test_a_work_disk_below_its_floor_refuses_every_run_at_once(slot_env: dict[str, str]) -> None:
    env = {**slot_env, "SLOT_MIN_FREE_GIB": "999999"}
    start = time.monotonic()
    done = run_slot(
        env, "run", "--owner", "big:job", "--cpus", "1", "--memory", "1", "--", "echo", "ran"
    )
    assert done.returncode == 75 and "ran" not in done.stdout
    assert "below its 999999 GiB floor" in done.stderr and "slot du" in done.stderr
    assert time.monotonic() - start < 5, "a disk floor must refuse, not wait"


def test_du_names_each_consumer_of_the_work_disk(slot_env: dict[str, str], tmp_path: Path) -> None:
    for name in ("zeo-lab", "lab", "cache"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f").write_bytes(b"x" * 4096)
    out = run_slot(slot_env, "du").stdout
    assert out.startswith(f"disk {tmp_path} free_gib=")
    assert "floor_gib=0" in out
    for name in ("zeo-lab", "lab", "cache"):
        assert any(row.startswith(f"use {name} ") for row in out.splitlines()), out
    assert "zeo lab.sh" in out and "shidashi" in out


def test_the_usage_is_the_whole_header(slot_env: dict[str, str]) -> None:
    done = run_slot(slot_env)
    assert done.returncode == 64
    assert "slot du" in done.stdout and "Do not edit the installed copy." in done.stdout
