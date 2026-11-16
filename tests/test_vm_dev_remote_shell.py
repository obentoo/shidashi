"""The command lines a host sends a worker for a dev VM (shidashi/vm_dev_remote_shell.py).

They are text, so they are read as text first: the exact line for a start, a run and a
stop is pinned, because that line IS the contract with the Shidashi on the worker. Then
the two scripts that decide something on their own -- the probe and the stop of a
process group -- are run by a real ``/bin/sh`` against real processes: what they must
never do (signal a process that merely has the recorded number) cannot be read off a
string.
"""

import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

import pytest

from shidashi import process_state, vm_exec, worker
from shidashi import vm_dev_remote_shell as shell

_COMMIT = "5" * 40
_PINS = {"id": "p1", "gentoo": {"date": "20261101"}, "overlays": {}}
_ENV = (
    "SHIDASHI_CACHE=/mnt/work/cache SHIDASHI_SCRATCH=/mnt/work/scratch "
    f"SHIDASHI_RUNS=/mnt/work/out/runs PYTHONPATH=/mnt/work/src/{_COMMIT} "
    f"SHIDASHI_SOURCE_COMMIT={_COMMIT}"
)
_APP = "/mnt/work/runtime/venv/bin/python -c 'from shidashi.cli import app; app()'"
_SHIPPED = f"cd /mnt/work/src/{_COMMIT} && {_ENV} {_APP}"
#: The same Shidashi as a relay runs it: marked as the end of the chain.
_RELAYED = f"cd /mnt/work/src/{_COMMIT} && {_ENV} SHIDASHI_DEV_RELAYED=1 {_APP}"


def _inner(line: str) -> str:
    """The command ``vm_exec.guarded`` wrapped: the one word after its ``sh -c``."""
    words = shlex.split(line.split("; sh -c ", 1)[1].rsplit("; rc=$?", 1)[0])
    assert len(words) == 1
    return words[0]


# --- the shipped Shidashi ---------------------------------------------------------------


def test_a_shipped_shidashi_runs_as_it_was_run_by_hand_on_the_worker() -> None:
    # 2026-11-15 on bentoo-lab: this directory, these variables, this interpreter
    assert worker.shipped_shell(_COMMIT, ["vm", "stop", "--name", "exp1"]) == (
        f"{_SHIPPED} vm stop --name exp1"
    )


def test_a_job_unit_runs_under_the_same_environment() -> None:
    argv = worker.job_unit_argv("fac", _COMMIT, ["doctor"])
    setenv = [word.removeprefix("--setenv=") for word in argv if word.startswith("--setenv=")]
    assert " ".join(setenv) == _ENV
    assert argv[-4:] == [*_APP.split(" ", 2)[:2], "from shidashi.cli import app; app()", "doctor"]


def test_every_argument_reaches_the_shipped_shidashi_as_one_word() -> None:
    hostile = ["vm", "run", "--", "echo 'a b'; rm -rf $HOME && `id`"]
    line = worker.shipped_shell(_COMMIT, hostile)
    assert shlex.split(line.split(" && ", 1)[1])[-4:] == hostile


# --- the three lines --------------------------------------------------------------------


def test_the_start_line_is_the_workers_own_vm_start_dev() -> None:
    line = shell.relayed_start_command(
        _COMMIT,
        "dev",
        iso="/mnt/work/cache/dev-isos/b.iso",
        pins=_PINS,
        memory="24G",
        cpus=16,
        max_life=12.0,
    )
    pins = '{"gentoo":{"date":"20261101"},"id":"p1","overlays":{}}'
    start = (
        f"{_RELAYED} vm start --dev --local --name dev --iso /mnt/work/cache/dev-isos/b.iso "
        f"--pins-json '{pins}' --memory 24G --max-life 12.0 --cpus 16"
    )
    assert _inner(line) == start
    pidfile = "/mnt/work/scratch/vm/dev.start.pid"
    assert line == f"mkdir -p /mnt/work/scratch/vm && {vm_exec.guarded(start, pidfile)}"


def test_the_start_line_leaves_the_workers_cpus_alone_when_none_are_known() -> None:
    line = shell.relayed_start_command(
        _COMMIT, "dev", iso="/i.iso", pins=_PINS, memory="8G", cpus=None, max_life=0.5
    )
    assert "--cpus" not in line and "--max-life 0.5" in _inner(line)
    assert "--disk-size" not in line


def test_the_start_line_passes_a_disk_size_on() -> None:
    line = shell.relayed_start_command(
        _COMMIT, "d", iso="/i.iso", pins=_PINS, memory="8G", cpus=2, max_life=1, disk_size="80G"
    )
    assert _inner(line).endswith("--max-life 1.0 --cpus 2 --disk-size 80G")


def test_a_worker_never_relays_further() -> None:
    # the worker's environment may name a worker too: --local ends the chain there
    line = shell.relayed_start_command(
        _COMMIT, "d", iso="/i.iso", pins=_PINS, memory="8G", cpus=None, max_life=1
    )
    assert " vm start --dev --local " in _inner(line)


def test_the_run_line_hands_the_command_over_as_one_word() -> None:
    command = "cd /var/db/repos/staged && exec env -i 'A=b c' emerge -1 =dev-libs/foo-1.0"
    pidfile = shell.run_pidfile("dev", "ab12")
    line = shell.relayed_run_command(
        _COMMIT, "dev", command, timeout=0, stream=True, pidfile=pidfile
    )
    assert pidfile == "/mnt/work/scratch/vm/dev/relay-run-ab12.pid"
    run = f"{_RELAYED} vm run --name dev --timeout 0 --stream -- {shlex.quote(command)}"
    assert _inner(line) == run
    assert shlex.split(run)[-1] == command
    assert line.endswith(vm_exec.guarded(run, pidfile))


def test_the_run_line_keeps_the_timeout_and_the_mode_it_was_given() -> None:
    line = shell.relayed_run_command(
        _COMMIT, "dev", "true", timeout=600, stream=False, pidfile="/p"
    )
    assert _inner(line).endswith("vm run --name dev --timeout 600 -- true")


def test_the_run_line_says_a_session_is_gone_before_it_writes_into_it() -> None:
    line = shell.relayed_run_command(_COMMIT, "dev", "true", timeout=0, stream=True, pidfile="/p")
    check, _, rest = line.partition(" fi; ")
    assert check.startswith("if [ ! -e /mnt/work/scratch/vm/dev/session.json ]; then echo ")
    assert check.endswith(">&2; exit 1;") and "no longer on this worker" in check
    assert rest.startswith("echo $$ > /p; ")


def test_the_stop_line_ends_a_start_first_then_says_gone_or_stops() -> None:
    line = shell.relayed_stop_command(_COMMIT, "dev")
    starter = "/mnt/work/scratch/vm/dev.start.pid"
    assert line == (
        f"{shell.stop_group_script(starter, wait_s=10)}; "
        "if [ ! -e /mnt/work/scratch/vm/dev/session.json ]; then "
        f"echo {shell.GONE_MARK}; exit 0; fi; {_RELAYED} vm stop --name dev"
    )


# --- the probe, run -----------------------------------------------------------------------


def test_the_probe_reads_as_what_it_printed() -> None:
    read = shell.read_probe("work=mounted\nthreads=16\nsession=alive\n")
    assert read == shell.WorkerProbe(mounted=True, threads=16, alive=True, starting=False)
    assert shell.read_probe("threads=\nsession=starting\n") == shell.WorkerProbe(
        mounted=False, threads=None, alive=False, starting=True
    )
    assert shell.read_probe("threads=0\n").threads is None
    assert shell.read_probe("threads=sixteen\n").threads is None


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The worker's session directory, under a temporary root."""
    monkeypatch.setattr(shell, "SESSIONS", str(tmp_path / "vm"))
    (tmp_path / "vm").mkdir()
    return tmp_path / "vm"


def _sh(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, timeout=60)


def _named(tmp_path: Path, name: str, seconds: str = "60") -> subprocess.Popen[bytes]:
    """A live process whose name is ``name``: ``sleep`` under another name."""
    binary = tmp_path / name
    if not binary.exists():
        binary.symlink_to("/bin/sleep" if Path("/bin/sleep").exists() else "/usr/bin/sleep")
    return subprocess.Popen([str(binary), seconds])


def test_the_probe_sees_a_session_whose_qemu_runs(sessions: Path, tmp_path: Path) -> None:
    qemu = _named(tmp_path, "qemu-system-x86_64")
    try:
        (sessions / "dev").mkdir()
        (sessions / "dev" / "qemu.pid").write_text(f"{qemu.pid}\n")
        assert shell.read_probe(_sh(shell.probe_script("dev")).stdout).alive
        assert not shell.read_probe(_sh(shell.probe_script("other")).stdout).alive
    finally:
        qemu.kill()
        qemu.wait()
    assert not shell.read_probe(_sh(shell.probe_script("dev")).stdout).alive  # it ended


def test_the_probe_does_not_take_another_process_for_a_qemu(sessions: Path, tmp_path: Path) -> None:
    # the work disk outlives a reboot: the number in qemu.pid is then someone else's
    other = _named(tmp_path, "not-a-vm")
    try:
        (sessions / "dev").mkdir()
        (sessions / "dev" / "qemu.pid").write_text(f"{other.pid}\n")
        probe = shell.read_probe(_sh(shell.probe_script("dev")).stdout)
    finally:
        other.kill()
        other.wait()
    assert not probe.alive and not probe.starting


def test_the_probe_of_an_empty_pidfile_is_not_alive(sessions: Path) -> None:
    (sessions / "dev").mkdir()
    (sessions / "dev" / "qemu.pid").write_text("")
    done = _sh(shell.probe_script("dev"))
    assert done.returncode == 0 and not shell.read_probe(done.stdout).alive


# --- stopping a process group, run --------------------------------------------------------


def _guarded(pidfile: Path, command: str) -> subprocess.Popen[bytes]:
    """What sshd runs for a guarded line: a shell leading a session of its own."""
    line = vm_exec.guarded(command, str(pidfile))
    # a parent that waits, as sshd does: the shell is reaped the moment it ends
    child = subprocess.Popen(["/bin/sh", "-c", 'setsid -w /bin/sh -c "$0" & wait $!', line])
    deadline = time.monotonic() + 20
    while not (pidfile.is_file() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline, "the guarded shell never started"
        time.sleep(0.02)
    return child


def _group_gone(pgid: int) -> bool:
    return not process_state.group_running(pgid)


def test_stopping_a_group_ends_everything_in_it_and_waits(tmp_path: Path) -> None:
    pidfile = tmp_path / "run.pid"
    # a command that takes a moment to die: the script must still be waiting when it does
    child = _guarded(pidfile, "trap 'sleep 1; exit 0' TERM; sleep 60 & wait")
    pgid = int(pidfile.read_text())
    began = time.monotonic()
    done = _sh(shell.stop_group_script(str(pidfile), wait_s=20))
    took = time.monotonic() - began
    try:
        assert done.returncode == 0, done.stderr
        assert _group_gone(pgid), "the script returned while the group was still there"
        assert 0.9 < took < 15
        assert not pidfile.exists()
    finally:
        child.kill()
        child.wait()


def test_a_group_that_will_not_end_is_waited_for_no_longer_than_asked(tmp_path: Path) -> None:
    pidfile = tmp_path / "run.pid"
    child = _guarded(pidfile, "trap '' TERM; sleep 60")
    pgid = int(pidfile.read_text())
    try:
        began = time.monotonic()
        _sh(shell.stop_group_script(str(pidfile), wait_s=1))
        assert 0.9 < time.monotonic() - began < 8
        assert not pidfile.exists()
    finally:
        os.killpg(pgid, signal.SIGKILL)
        child.wait()


def test_a_number_left_by_a_dead_shell_signals_nobody(tmp_path: Path) -> None:
    # after a reboot the pidfile is still on the work disk and its pid is another's
    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)  # leads a group
    pidfile = tmp_path / "dev.start.pid"
    pidfile.write_text(f"{bystander.pid}\n")
    try:
        done = _sh(shell.stop_group_script(str(pidfile), wait_s=5))
        time.sleep(0.2)
        assert bystander.poll() is None, "a process that only had the number was signalled"
        assert done.returncode == 0 and not pidfile.exists()  # the stale file still goes
    finally:
        bystander.kill()
        bystander.wait()


def test_no_pidfile_is_nothing_to_stop(tmp_path: Path) -> None:
    done = _sh(shell.stop_group_script(str(tmp_path / "none.pid"), wait_s=5))
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""


def test_the_probe_sees_a_start_under_way_and_not_a_stale_one(
    sessions: Path, tmp_path: Path
) -> None:
    starter = Path(shell.start_pidfile("dev"))
    child = _guarded(starter, "sleep 60")
    try:
        assert shell.read_probe(_sh(shell.probe_script("dev")).stdout).starting
    finally:
        os.killpg(int(starter.read_text()), signal.SIGKILL)
        child.wait()
    starter.write_text(f"{os.getpid()}\n")  # a live process that is no start
    assert not shell.read_probe(_sh(shell.probe_script("dev")).stdout).starting


def test_a_group_left_with_only_unreaped_ended_processes_is_gone(tmp_path: Path) -> None:
    """Hostile: under an init that does not reap (PID 1 of the container ``act`` starts)
    a group's ended processes stay as zombies, and ``kill -0`` still answers for them;
    the stop must not wait for them until its timeout (2026-11-16)."""
    pidfile = tmp_path / "run.pid"
    # its parent is this test, which reaps it only at the end: the leader stays a zombie
    child = subprocess.Popen(
        ["/bin/sh", "-c", vm_exec.guarded("sleep 60", str(pidfile))], start_new_session=True
    )
    deadline = time.monotonic() + 20
    while not (pidfile.is_file() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline, "the guarded shell never started"
        time.sleep(0.02)
    pgid = int(pidfile.read_text())
    try:
        began = time.monotonic()
        done = _sh(shell.stop_group_script(str(pidfile), wait_s=20))
        took = time.monotonic() - began
        assert done.returncode == 0, done.stderr
        stat = Path(f"/proc/{child.pid}/stat").read_text()
        assert stat.rpartition(") ")[2].startswith("Z"), "the leader was meant to be a zombie"
        assert _group_gone(pgid)
        assert took < 5, f"the stop waited {took:.1f} s for processes that had ended"
        assert not pidfile.exists()
    finally:
        child.kill()
        child.wait()


def test_the_ssh_relay_line_runs_the_sessions_tree_as_the_end_of_the_chain() -> None:
    # `vm ssh-config`'s ProxyCommand: the worker's own relay, from the tree shipped for
    # the session, never relaying again (SHIDASHI_DEV_RELAYED)
    line = shell.relayed_ssh_relay_command(_COMMIT, "dev")
    assert line == f"{_RELAYED} vm ssh-relay --name dev"
