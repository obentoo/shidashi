"""A shared holder never hides another holder's pid (story 006, task 8.2; R3.9, R3.12).

Contract (design.md v4, "Lock primitive", "Toolbox extraction and shared use"):

- an EXCLUSIVE acquire always writes its pid (one fixed-width ``pwrite``);
- a SHARED acquire writes it only when the record is unreadable, names this process, or names
  no live process (``os.kill(pid, 0)`` -> ``ProcessLookupError``; ``PermissionError`` counts
  as live) -- so the record keeps naming a live holder however many shared holders follow;
- a waiter that reads its OWN pid or a DEAD one looks for another holder of the file in
  ``/proc/locks`` (FLOCK entries on the file's ``dev:inode``), else renders ``pid unknown``;
- ``toolbox.using`` takes its SHARED hold first again (shared -> check stamp -> yield |
  release -> exclusive -> re-check -> extract -> loop); the unlocked peek is gone, and the
  re-extraction's wait line names the tree's user, never the extractor itself.

Hostile halves first: the second shared holder that would overwrite the first, a live pid
this user cannot signal (not "dead"), the waiter that was itself the last shared holder,
and -- for ``/proc/locks`` -- another file's FLOCK holder and a POSIX lock on the same
inode (never named) against the same inode reached through a hard link (named).

Every other holder is a REAL second process driven over its stdin.
"""

import contextlib
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import shidashi
from shidashi import locks, toolbox

_REPO = Path(shidashi.__file__).resolve().parents[1]
_WHAT = "toolbox v3-systemd"

#: A lock user driven line by line: ``sh``/``ex`` hold (shared/exclusive), ``rel`` lets go,
#: ``raw-sh P`` flocks P shared WITHOUT writing a record, ``posix P`` takes a POSIX lock on P.
#: Each command answers ``ok <cmd>`` once done (``ex`` only once the lock is taken).
_CTL = """
import contextlib, fcntl, os, sys
from pathlib import Path
from shidashi import locks
path = Path(sys.argv[1])
what = sys.argv[2]
stack = contextlib.ExitStack()
fds = []
for line in sys.stdin:
    cmd, _, arg = line.strip().partition(" ")
    if cmd == "sh":
        stack.enter_context(locks.hold(path, what=what, shared=True))
    elif cmd == "ex":
        stack.enter_context(locks.hold(path, what=what))
    elif cmd == "rel":
        stack.close()
        stack = contextlib.ExitStack()
    elif cmd == "raw-sh":
        fds.append(os.open(arg, os.O_RDWR))
        fcntl.flock(fds[-1], fcntl.LOCK_SH)
    elif cmd == "posix":
        fds.append(os.open(arg, os.O_RDWR))
        fcntl.lockf(fds[-1], fcntl.LOCK_SH)
    print("ok", cmd, flush=True)
"""

#: Another process inside ``toolbox.using(argv[1], argv[2])`` until a line on its stdin.
_USER = """
import sys
from pathlib import Path
from shidashi import toolbox

def restore(tarball, rootfs):
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "bin" / "mksquashfs").write_text(tarball.name)

toolbox.restore_fork_point = restore
with toolbox.using(Path(sys.argv[1]), Path(sys.argv[2])):
    print("using", flush=True)
    sys.stdin.readline()
"""


class _Proc:
    """A real second process; ``send`` waits for its ``ok``, ``send_nowait`` does not."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", *argv],
            cwd=_REPO,
            env={**os.environ, "PYTHONPATH": str(_REPO)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.pid = self.proc.pid

    def _line(self, stream: Any, timeout: float) -> str:
        ready, _, _ = select.select([stream], [], [], timeout)
        if not ready:
            return ""
        return str(stream.readline())

    def send_nowait(self, command: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def expect(self, line: str, timeout: float = 10) -> None:
        got = self._line(self.proc.stdout, timeout)
        if got.strip() != line:
            self.kill()
            assert self.proc.stderr is not None
            pytest.fail(
                f"pid {self.pid}: expected {line!r}, got {got!r}: {self.proc.stderr.read()}"
            )

    def send(self, command: str) -> None:
        self.send_nowait(command)
        self.expect(f"ok {command.split()[0]}")

    def err_line(self, timeout: float = 10) -> str:
        return self._line(self.proc.stderr, timeout).strip()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


@contextlib.contextmanager
def _procs() -> Iterator[Callable[..., _Proc]]:
    started: list[_Proc] = []

    def spawn(*argv: str) -> _Proc:
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    try:
        yield spawn
    finally:
        for proc in started:
            proc.kill()


def _ctl(spawn: Callable[..., _Proc], path: Path) -> _Proc:
    return spawn(_CTL, str(path), _WHAT)  # type: ignore[no-any-return]


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


# --- hostile halves: a shared holder must not overwrite a live holder ---------------------


def test_a_second_shared_holder_keeps_the_first_holders_record(lock: Path) -> None:
    with _procs() as spawn:
        a, b = _ctl(spawn, lock), _ctl(spawn, lock)
        a.send("sh")
        b.send("sh")
        assert locks.holder_pid(lock) == a.pid, "B's shared hold overwrote A, who still holds"


def test_a_live_pid_this_user_cannot_signal_is_not_dead(lock: Path) -> None:
    """Wrongly collapsed: ``PermissionError`` from ``os.kill(1, 0)`` is a live process."""
    _write_record(lock, 1)
    with locks.hold(lock, what=_WHAT, shared=True):
        assert locks.holder_pid(lock) == 1


def test_an_exclusive_waiter_behind_two_shared_holders_names_a_live_one_never_itself(
    lock: Path,
) -> None:
    """W was the last shared holder; it lets go and waits exclusive behind A and B (the
    toolbox's re-extraction). Its line must name A or B -- naming itself names no one."""
    with _procs() as spawn:
        a, b, w = _ctl(spawn, lock), _ctl(spawn, lock), _ctl(spawn, lock)
        a.send("sh")
        b.send("sh")
        w.send("sh")
        w.send("rel")
        w.send_nowait("ex")
        line = w.err_line()
        assert line.startswith(f"waiting for {_WHAT} (pid "), line
        assert f"(pid {w.pid})" not in line, f"the waiter named itself: {line}"
        assert line in {f"waiting for {_WHAT} (pid {p.pid})" for p in (a, b)}, line
        a.send("rel")
        b.send("rel")
        w.expect("ok ex")
        assert locks.holder_pid(lock) == w.pid


def test_a_waiter_reading_a_dead_pid_names_the_proc_locks_holder(
    lock: Path, tmp_path: Path
) -> None:
    """The record names a dead process; R holds the file (through a hard link, writing no
    record). Never named: X, an FLOCK holder of ANOTHER file, and P, a POSIX lock holder of
    THIS inode. Named: R -- the same inode by another name is the same lock."""
    _write_record(lock, _dead_pid())
    alias = tmp_path / "alias.lock"
    os.link(lock, alias)
    other = tmp_path / "other.lock"
    _write_record(other, 1)
    with _procs() as spawn:
        x, p, r = _ctl(spawn, other), _ctl(spawn, lock), _ctl(spawn, lock)
        w = _ctl(spawn, lock)
        x.send(f"raw-sh {other}")
        p.send(f"posix {lock}")
        r.send(f"raw-sh {alias}")
        w.send_nowait("ex")
        line = w.err_line()
        assert line == f"waiting for {_WHAT} (pid {r.pid})", (
            f"expected R={r.pid}, never X={x.pid}, P={p.pid}, W={w.pid}: {line}"
        )
        r.kill()
        w.expect("ok ex")


# --- then the record still follows the holders -----------------------------------------------


def test_when_the_recorded_holder_died_a_new_shared_holder_writes_itself(lock: Path) -> None:
    with _procs() as spawn:
        a, b = _ctl(spawn, lock), _ctl(spawn, lock)
        a.send("sh")
        b.send("sh")
        a.kill()
        with locks.hold(lock, what=_WHAT, shared=True):
            assert locks.holder_pid(lock) == os.getpid()


def test_an_unreadable_record_is_replaced_by_a_shared_holder(lock: Path) -> None:
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"garbage!!!\n")
    with locks.hold(lock, what=_WHAT, shared=True):
        assert locks.holder_pid(lock) == os.getpid()


def test_an_exclusive_acquire_always_writes_its_record(lock: Path) -> None:
    """Even over a live pid (1): exclusive means this process is the one holder."""
    _write_record(lock, 1)
    with locks.hold(lock, what=_WHAT):
        assert locks.holder_pid(lock) == os.getpid()
    with _procs() as spawn:
        a = _ctl(spawn, lock)
        a.send("sh")
        a.send("rel")  # alive, recorded, no longer holding
        with locks.hold(lock, what=_WHAT):
            assert locks.holder_pid(lock) == os.getpid()


# --- the toolbox: shared first, and the re-extraction's wait names the user ----------------


def _tarball(tmp_path: Path, name: str) -> Path:
    tar = tmp_path / "fork-points" / name
    tar.parent.mkdir(parents=True, exist_ok=True)
    tar.write_bytes(name.encode())
    return tar


class _Background(threading.Thread):
    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self.fn = fn
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.fn()
        except BaseException as err:  # re-raised by finish()
            self.error = err

    def finish(self, timeout: float = 20) -> None:
        self.join(timeout)
        assert not self.is_alive(), "still blocked"
        if self.error is not None:
            raise self.error


def test_the_toolbox_re_extraction_wait_names_the_user_never_the_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """This process extracted and used the old tree (the record names it); the user U, a
    real second process, now holds the tree shared. This process needs the new pins: its
    ``using`` takes the shared hold FIRST, finds the stamp stale, lets go and waits
    exclusive -- and its line names U, never this process."""
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    old, new = _tarball(tmp_path, "v3-systemd-OLD.tar"), _tarball(tmp_path, "v3-systemd-NEW.tar")

    def restore(tarball: Path, rootfs: Path) -> None:
        (rootfs / "usr" / "bin").mkdir(parents=True)
        (rootfs / "usr" / "bin" / "mksquashfs").write_text(tarball.name)

    monkeypatch.setattr(toolbox, "restore_fork_point", restore)
    with toolbox.using(old, root):
        pass
    real_hold = locks.hold
    modes: list[bool] = []

    def hold(path: Path, **kw: Any) -> Any:
        modes.append(bool(kw.get("shared", False)))
        return real_hold(path, **kw)

    with _procs() as spawn:
        user = spawn(_USER, str(old), str(root))
        user.expect("using")
        capfd.readouterr()
        monkeypatch.setattr(locks, "hold", hold)

        def extract() -> None:
            with toolbox.using(new, root):
                pass

        extractor = _Background(extract)
        extractor.start()
        time.sleep(1.0)
        assert extractor.is_alive(), "the re-extraction did not wait for the tree's user"
        user.send_nowait("")
        extractor.finish()
    err = capfd.readouterr().err
    assert f"waiting for {_WHAT} (pid {user.pid})" in err, err
    assert f"(pid {os.getpid()})" not in err, f"the extractor named itself: {err}"
    assert modes[:1] == [True], f"using's first hold was not shared: {modes}"
    assert (root / "usr" / "bin" / "mksquashfs").read_text() == new.name
