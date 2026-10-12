"""A waiter never names a dead ``flock`` holder (story 006, task 9.1; R3.9).

Contract (design.md v5, "Lock primitive" -- shared holders never hide a live holder):

- a waiter whose record names this process or a dead one looks for another holder of the
  file in ``/proc/locks`` (FLOCK entries on its ``dev:inode``);
- a ``/proc/locks`` holder is named only when it is ALIVE (``os.kill(pid, 0)``;
  ``PermissionError`` counts as alive); a dead one -- e.g. ``vm start`` that exited after
  handing its fd to QEMU: the kernel keeps the creator's pid on the lock -- is skipped and
  the scan goes on; no live holder left -> ``pid unknown``, in the wait line
  (``waiting for <what> (pid unknown)``) and in ``LockBusy`` alike;
- a live record pid is named as before.

Hostile halves first: a dead creator whose lock lives on in another process (named today),
a dead entry listed BEFORE a live one (the scan stopped at it), a live pid this user cannot
signal (not "dead"). Then the benign half: a live record pid.

The dead-creator fixture is real (a process flocks, hands its fd to a child and exits); the
ordering fixtures replace ``/proc/locks`` with a file listing the entries in a fixed order,
on the lock file's real ``dev:inode``, while this process really holds the ``flock`` through
another open file description.
"""

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import shidashi
from shidashi import locks

_REPO = Path(shidashi.__file__).resolve().parents[1]
_WHAT = "toolbox v3-systemd"

#: Flock argv[1], hand the fd to a child that lives until its stdin closes, print the
#: child's pid and exit: the lock outlives its creator, whose pid /proc/locks keeps.
_HAND_OFF = r"""
import fcntl, os, subprocess, sys
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
child = subprocess.Popen(
    [sys.executable, "-c", "import sys; sys.stdin.readline()"], pass_fds=[fd]
)
print(child.pid, flush=True)
"""


def _dead_pid() -> int:
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait(timeout=10)
    return done.pid


def _write_record(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"{pid:010d}\n".encode())


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    return tmp_path / "toolbox" / "v3-systemd.lock"


@contextlib.contextmanager
def _held_by_dead_creator(lock: Path) -> Iterator[tuple[int, int]]:
    """The lock held by a live child whose creator is dead; yields (creator, child)."""
    creator = subprocess.Popen(
        [sys.executable, "-c", _HAND_OFF, str(lock)],
        cwd=_REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert creator.stdout is not None and creator.stdin is not None
    child = int(creator.stdout.readline())
    creator.wait(timeout=10)  # reaped: its pid names no process any more
    try:
        yield creator.pid, child
    finally:
        creator.stdin.close()  # the child reads EOF and exits
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)


@contextlib.contextmanager
def _held_here(lock: Path) -> Iterator[int]:
    """This process holds ``lock`` through its own open description (not ``locks.hold``),
    so any ``locks.hold`` of it conflicts; yields the fd."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _fake_proc_locks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lock: Path, pids: list[int]
) -> None:
    """``/proc/locks`` lists one granted FLOCK entry per pid on ``lock``, in this order."""
    st = os.stat(lock)
    where = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    lines = [f"{n}: FLOCK  ADVISORY  WRITE {pid} {where} 0 EOF" for n, pid in enumerate(pids, 1)]
    fake = tmp_path / "proc-locks"
    fake.write_text("\n".join(lines) + "\n", encoding="ascii")
    monkeypatch.setattr(locks, "_PROC_LOCKS", fake)


def _busy_pid(lock: Path) -> locks.LockBusy:
    with pytest.raises(locks.LockBusy) as busy, locks.hold(lock, what=_WHAT, wait=False):
        pytest.fail("the lock was taken while another description holds it")
    return busy.value


def _wait_line(lock: Path, capfd: pytest.CaptureFixture[str], release: object) -> str:
    """The wait line a blocking ``hold`` prints, then ``release()`` lets it through."""
    errors: list[BaseException] = []

    def wait() -> None:
        try:
            with locks.hold(lock, what=_WHAT):
                pass
        except BaseException as err:  # re-raised below
            errors.append(err)

    waiter = threading.Thread(target=wait, daemon=True)
    capfd.readouterr()
    waiter.start()
    seen = ""
    try:
        deadline = time.monotonic() + 10
        while "waiting for" not in seen and time.monotonic() < deadline:
            time.sleep(0.05)
            seen += capfd.readouterr().err
    finally:
        release()  # type: ignore[operator]
        waiter.join(10)
    assert not waiter.is_alive(), "the waiter never got the lock once released"
    if errors:
        raise errors[0]
    lines = [line for line in seen.splitlines() if line.startswith("waiting for")]
    assert len(lines) == 1, f"expected one wait line: {seen!r}"
    return lines[0]


# --- hostile halves: a dead /proc/locks holder must never be named ------------------------


def test_a_dead_creator_whose_lock_lives_on_is_not_named_by_lock_busy(lock: Path) -> None:
    """``vm start`` exited after handing its fd to QEMU: /proc/locks still shows its pid.
    The record names another dead process; no live holder is listed -> ``pid unknown``."""
    _write_record(lock, _dead_pid())
    with _held_by_dead_creator(lock) as (creator, _child):
        busy = _busy_pid(lock)
    assert busy.pid != creator, f"LockBusy named the dead creator {creator}: {busy}"
    assert busy.pid is None, f"expected pid unknown: {busy}"
    assert "pid unknown" in str(busy), str(busy)


def test_a_dead_creator_whose_lock_lives_on_is_not_named_in_the_wait_line(
    lock: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _write_record(lock, _dead_pid())
    with _held_by_dead_creator(lock) as (creator, child):

        def release() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)

        line = _wait_line(lock, capfd, release)
    assert f"(pid {creator})" not in line, f"the wait line named the dead creator: {line}"
    assert line == f"waiting for {_WHAT} (pid unknown)", line


def test_a_dead_entry_is_skipped_for_a_live_one_listed_after_it(
    lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly collapsed: the first FLOCK entry is dead; the scan must go on to the live
    holder after it (the test's parent: alive, another process, signalable)."""
    _write_record(lock, _dead_pid())
    dead, live = _dead_pid(), os.getppid()
    with _held_here(lock):
        _fake_proc_locks(monkeypatch, tmp_path, lock, [dead, live])
        busy = _busy_pid(lock)
    assert busy.pid == live, f"expected the live holder {live}, not the dead {dead}: {busy}"


def test_a_pid_this_user_cannot_signal_is_named_after_a_dead_entry(
    lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly split: ``PermissionError`` from ``os.kill(1, 0)`` is a live process, not a
    dead one to skip -- pid 1 is named."""
    _write_record(lock, _dead_pid())
    dead = _dead_pid()
    with _held_here(lock):
        _fake_proc_locks(monkeypatch, tmp_path, lock, [dead, 1])
        busy = _busy_pid(lock)
    assert busy.pid == 1, f"expected pid 1 (alive, not signalable), got: {busy}"


def test_only_dead_entries_render_pid_unknown_in_lock_busy(
    lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_record(lock, _dead_pid())
    dead = [_dead_pid(), _dead_pid()]
    with _held_here(lock):
        _fake_proc_locks(monkeypatch, tmp_path, lock, dead)
        busy = _busy_pid(lock)
    assert busy.pid is None, f"named a dead holder: {busy}"
    assert str(busy) == f"{_WHAT} is held by pid unknown", str(busy)


def test_only_dead_entries_render_pid_unknown_in_the_wait_line(
    lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _write_record(lock, _dead_pid())
    dead = [_dead_pid(), _dead_pid()]
    with _held_here(lock) as fd:
        _fake_proc_locks(monkeypatch, tmp_path, lock, dead)
        line = _wait_line(lock, capfd, lambda: fcntl.flock(fd, fcntl.LOCK_UN))
    assert line == f"waiting for {_WHAT} (pid unknown)", line


# --- then a live holder is still named ------------------------------------------------------


def test_a_live_proc_locks_holder_is_still_named(
    lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_record(lock, _dead_pid())
    live = os.getppid()
    with _held_here(lock):
        _fake_proc_locks(monkeypatch, tmp_path, lock, [live])
        busy = _busy_pid(lock)
    assert busy.pid == live, str(busy)


def test_a_live_record_pid_is_named_as_before(
    lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The record names a live process other than this one: it is named, /proc/locks is
    not consulted (its dead entry would otherwise be a candidate)."""
    live = os.getppid()
    _write_record(lock, live)
    with _held_here(lock) as fd:
        _fake_proc_locks(monkeypatch, tmp_path, lock, [_dead_pid()])
        assert _busy_pid(lock).pid == live
        line = _wait_line(lock, capfd, lambda: fcntl.flock(fd, fcntl.LOCK_UN))
    assert line == f"waiting for {_WHAT} (pid {live})", line
