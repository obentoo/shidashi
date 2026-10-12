"""Tests of shidashi.locks -- the advisory, process-scoped file lock (story 006, task 1.1).

Contract (design.md v2, cross-story contract C2): ``hold(path, *, what, wait=True)`` is a
context manager taking ``flock(LOCK_EX)`` on ``path`` and writing the holder's pid into
it as a FIXED-WIDTH record (``RECORD_WIDTH`` = 11 bytes: ten zero-padded digits and a
newline) in one ``pwrite`` at offset 0 -- never empty, never a mix of two pids. A lock
file that does not exist yet is created already written: a temp file with the record,
then ``os.link`` to the lock path (EEXIST: open the existing one). When the lock is
taken it reports ``f"waiting for {what} (pid {pid})"`` once on stderr and blocks
(``wait=True``) or raises ``LockBusy(what, pid)`` (``wait=False``). A pid that cannot
be read is named "pid unknown". ``hold`` is re-entrant per process,
keyed by the resolved path (a module-level registry): a nested ``hold`` of the same
lock -- through any spelling, from any object -- takes no second flock and the lock
stays held until the outermost block ends. ``holder_pid(path)`` reads the first record back.
The kernel releases the lock when its holder dies.

v3 (shared mode, R3.12): ``hold(..., shared=True)`` takes ``LOCK_SH`` instead of
``LOCK_EX``. Several shared holders hold the lock at once; an exclusive request waits for
EVERY shared holder, a shared request waits for an exclusive holder, and both report the
same ``waiting for {what} (pid N)`` line. The registry records the mode: a nested hold of
a path already held in the other mode raises ``locks.LockError`` (no upgrade -- flock
conversion is not atomic).

The other holder is always a REAL second process (``python -c``), never a thread of
this one: flock belongs to an open file description, and only another process
proves the lock is exclusive between processes.
"""

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import locks
from shidashi.locks import LockBusy, hold, holder_pid

#: Another process holding ``argv[1]`` (``what`` = ``argv[2]``) until its stdin gets a
#: line; ``argv[3]`` == "shared" takes it shared (v3), anything else exclusive.
_HOLDER = """
import sys
from pathlib import Path
from shidashi import locks
kw = {"shared": True} if sys.argv[3] == "shared" else {}
with locks.hold(Path(sys.argv[1]), what=sys.argv[2], **kw):
    print("held", flush=True)
    sys.stdin.readline()
print("released", flush=True)
"""


@contextlib.contextmanager
def _held_elsewhere(
    path: Path, what: str = "test lock", *, shared: bool = False
) -> Iterator[subprocess.Popen[str]]:
    """``path`` locked by another process for the block; yields that process."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path), what, "shared" if shared else "exclusive"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        line = proc.stdout.readline()
        if line != "held\n":
            proc.wait(timeout=10)
            pytest.fail(f"the holder never took {path}: {proc.stderr.read()}")
        yield proc
    finally:
        if proc.poll() is None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write("\n")
                proc.stdin.flush()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


class _Background(threading.Thread):
    """``fn`` in a daemon thread: a call that blocks forever cannot hang the suite."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self.fn = fn
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self.fn()
        except BaseException as err:  # re-raised by finish()
            self.error = err

    def finish(self, timeout: float = 20) -> Any:
        self.join(timeout)
        assert not self.is_alive(), "still blocked after the lock was released"
        if self.error is not None:
            raise self.error
        return self.result


def _wait_until(condition: Callable[[], bool], timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# --- the holder's pid ---------------------------------------------------------------


def test_hold_writes_the_holders_pid(tmp_path: Path) -> None:
    path = tmp_path / "store.lock"
    with hold(path, what="checkpoint store v3-systemd"):
        assert holder_pid(path) == os.getpid()


def test_a_short_pid_over_a_longer_stale_one_reads_correctly(tmp_path: Path) -> None:
    """The file outlives its holder: a new holder whose pid is SHORTER than the one
    left behind must not leave a mix of both (``678`` over ``99999999`` reads
    ``67899999``) -- a waiter would name a process that does not exist."""
    path = tmp_path / "store.lock"
    path.write_text("9999999999999999\n")  # longer than a record, from an older format
    with hold(path, what="store"):
        assert holder_pid(path) == os.getpid()
        assert path.read_bytes()[: locks.RECORD_WIDTH] == f"{os.getpid():010d}\n".encode()


def test_the_pid_is_a_fixed_width_record(tmp_path: Path) -> None:
    path = tmp_path / "store.lock"
    with hold(path, what="store"):
        record = path.read_bytes()
        assert len(record) == locks.RECORD_WIDTH == 11
        assert record == f"{os.getpid():010d}\n".encode()


def test_a_new_lock_file_is_linked_into_place_already_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiter that opens the lock file the instant it appears must never read it
    empty: it comes into existence by a link of a temp file holding the record."""
    path = tmp_path / "trunk.lock"
    linked: list[bytes] = []
    real_link = os.link

    def link(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kw: object) -> None:
        if Path(dst) == path:
            linked.append(Path(src).read_bytes())
        real_link(src, dst, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(locks.os, "link", link)
    with hold(path, what="trunk desktop"):
        pass
    assert len(linked) == 1 and len(linked[0]) == locks.RECORD_WIDTH
    assert not [p for p in tmp_path.iterdir() if p != path], "a temp file was left behind"


def test_a_waiter_never_sees_a_new_lock_file_empty(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    seen_empty = threading.Event()
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            try:
                if path.stat().st_size == 0:
                    seen_empty.set()
            except FileNotFoundError:
                pass

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    for _ in range(50):
        with hold(path, what="x"):
            pass
        path.unlink()
    stop.set()
    watcher.join(5)
    assert not seen_empty.is_set()


def test_the_pid_named_is_the_current_holders_not_a_previous_one(tmp_path: Path) -> None:
    """Holder A comes and goes, holder B takes the lock: a waiter names B, never A."""
    path = tmp_path / "trunk.lock"
    with _held_elsewhere(path) as first:
        first_pid = first.pid
    with _held_elsewhere(path) as second:
        with pytest.raises(LockBusy) as busy, hold(path, what="trunk desktop", wait=False):
            pass
        assert busy.value.pid == second.pid
        assert busy.value.pid != first_pid


@pytest.mark.parametrize("content", [None, "", "\n", "not-a-pid\n", "12ab\n"])
def test_holder_pid_is_none_without_a_readable_pid(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "x.lock"
    if content is not None:
        path.write_text(content)
    assert holder_pid(path) is None


def test_the_lock_file_is_created_0644(tmp_path: Path) -> None:
    """design.md, Security: lock files carry only a pid and are mode 0644."""
    path = tmp_path / "x.lock"
    old = os.umask(0)
    try:
        with hold(path, what="x"):
            pass
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o644


# --- busy without waiting (the image lock, R3.6) ------------------------------------


def test_busy_without_wait_names_the_lock_and_its_holder(tmp_path: Path) -> None:
    path = tmp_path / "assemble" / "v3-kde-systemd.lock"
    path.parent.mkdir()
    with (
        _held_elsewhere(path, what="assemble v3-kde-systemd") as other,
        pytest.raises(LockBusy) as busy,
        hold(path, what="assemble v3-kde-systemd", wait=False),
    ):
        pytest.fail("entered a lock another process holds")
    assert busy.value.what == "assemble v3-kde-systemd"
    assert busy.value.pid == other.pid


def test_without_contention_wait_false_just_holds(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with hold(path, what="x", wait=False):
        assert holder_pid(path) == os.getpid()


# --- waiting (R3.9) -------------------------------------------------------------------


class _Stderr:
    """What this process wrote to stderr so far (threads included), accumulated.

    sys.stderr is swapped for a buffer that only ever grows: reading pytest's fd
    capture (readouterr) truncates it, and a line a waiter thread writes between
    that read and the truncate is lost.
    """

    def __init__(self, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        self.capfd, self._lock, self._parts = capfd, threading.Lock(), list[str]()
        monkeypatch.setattr(sys, "stderr", self)

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def read(self) -> str:
        captured = self.capfd.readouterr()
        assert "waiting for" not in captured.out, "the wait line went to stdout"
        with self._lock:
            return "".join(self._parts) + captured.err


def test_a_waiter_says_once_on_stderr_which_lock_and_pid_it_waits_for_then_gets_it(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "trunk-0123.lock"
    err = _Stderr(capfd, monkeypatch)
    entered = threading.Event()

    def wait_for_it() -> None:
        with hold(path, what="trunk desktop"):
            entered.set()
            assert holder_pid(path) == os.getpid()

    with _held_elsewhere(path, what="trunk desktop") as other:
        waiter = _Background(wait_for_it)
        waiter.start()
        assert _wait_until(lambda: "waiting for" in err.read()), "the waiter never said it"
        time.sleep(0.3)
        assert not entered.is_set(), "entered while another process held the lock"
    waiter.finish()
    assert entered.is_set()
    line = f"waiting for trunk desktop (pid {other.pid})"
    assert err.read().count(line) == 1  # once, not per retry


def test_an_uncontended_lock_says_nothing(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with hold(tmp_path / "x.lock", what="x"):
        pass
    captured = capfd.readouterr()
    assert "waiting" not in captured.err + captured.out


def test_hold_takes_no_on_wait_callback(tmp_path: Path) -> None:
    """C2 v2: the wait is always reported on stderr; there is no callback to forget."""
    with pytest.raises(TypeError):
        hold(tmp_path / "x.lock", what="x", on_wait=print)  # type: ignore[call-arg]


def test_waiting_blocks_until_release(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    entered = threading.Event()

    def wait_for_it() -> None:
        with hold(path, what="x"):
            entered.set()

    with _held_elsewhere(path):
        waiter = _Background(wait_for_it)
        waiter.start()
        time.sleep(0.5)
        assert not entered.is_set()
    waiter.finish()
    assert entered.is_set()


# --- release ----------------------------------------------------------------------------


def _taken_elsewhere(path: Path) -> bool:
    """Whether ANOTHER process can take ``path`` right now (wait=False)."""
    probe = (
        "import sys\nfrom pathlib import Path\nfrom shidashi import locks\n"
        "try:\n"
        "    with locks.hold(Path(sys.argv[1]), what='probe', wait=False):\n"
        "        pass\n"
        "except locks.LockBusy:\n"
        "    sys.exit(3)\n"
    )
    done = subprocess.run([sys.executable, "-c", probe, str(path)], capture_output=True, text=True)
    assert done.returncode in (0, 3), done.stderr
    return done.returncode == 0


def test_leaving_the_block_releases_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with hold(path, what="x"):
        assert not _taken_elsewhere(path)
    assert _taken_elsewhere(path)


def test_an_exception_in_the_block_releases_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError, match="install failed"), hold(path, what="x"):
        raise RuntimeError("install failed")
    assert _taken_elsewhere(path)


def test_a_holder_that_dies_releases_the_lock_without_cleanup(tmp_path: Path) -> None:
    """R3.8: SIGKILL leaves the file (and its pid) behind, never the lock."""
    path = tmp_path / "x.lock"
    with _held_elsewhere(path) as other:
        os.kill(other.pid, signal.SIGKILL)
        other.wait(timeout=10)
        assert path.exists()  # nothing removed the file
        with hold(path, what="x", wait=False):
            assert holder_pid(path) == os.getpid()


# --- which locks are the same lock (hostile halves first) -------------------------------


def test_same_named_locks_in_two_directories_are_two_locks(tmp_path: Path) -> None:
    """Wrongly collapsed: every store's lock is called ``.lock``; the v3-systemd
    store's holder must not block the v3-openrc store."""
    systemd = tmp_path / "checkpoints" / "v3-systemd" / ".lock"
    openrc = tmp_path / "checkpoints" / "v3-openrc" / ".lock"
    systemd.parent.mkdir(parents=True)
    openrc.parent.mkdir(parents=True)
    with _held_elsewhere(systemd), hold(openrc, what="checkpoint store v3-openrc", wait=False):
        assert holder_pid(openrc) == os.getpid()


def test_one_lock_reached_through_another_spelling_is_still_one_lock(tmp_path: Path) -> None:
    """Wrongly split: the same file through a symlinked directory and a ``..``
    detour is the same lock, and stays exclusive."""
    real = tmp_path / "scratch" / "assemble"
    real.mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    path = real / "v3-kde-systemd.lock"
    detour = tmp_path / "link" / ".." / "scratch" / "assemble" / "v3-kde-systemd.lock"
    with _held_elsewhere(path) as other:
        for spelling in (tmp_path / "link" / "v3-kde-systemd.lock", detour):
            with (
                pytest.raises(LockBusy) as busy,
                hold(spelling, what="assemble v3-kde-systemd", wait=False),
            ):
                pass
            assert busy.value.pid == other.pid


def test_two_distinct_locks_held_by_one_process_do_not_block_each_other(tmp_path: Path) -> None:
    with hold(tmp_path / "a.lock", what="a"), hold(tmp_path / "b.lock", what="b", wait=False):
        assert holder_pid(tmp_path / "a.lock") == holder_pid(tmp_path / "b.lock") == os.getpid()


def test_lock_busy_is_an_exception_carrying_what_and_pid() -> None:
    err = locks.LockBusy("assemble v3-kde-systemd", 4242)
    assert isinstance(err, Exception)
    assert (err.what, err.pid) == ("assemble v3-kde-systemd", 4242)
    unknown = locks.LockBusy("x", None)
    assert unknown.pid is None


# --- C2: default wait message, "pid unknown", re-entrancy -------------------------------


def test_an_unreadable_holder_pid_is_named_pid_unknown(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.6/R3.9 never print "pid None"."""
    path = tmp_path / "x.lock"
    err = _Stderr(capfd, monkeypatch)
    with _held_elsewhere(path, what="x"):
        path.write_text("not-a-pid\n")  # an unreadable record
        with (
            pytest.raises(LockBusy) as busy,
            hold(path, what="assemble v3-kde-systemd", wait=False),
        ):
            pass
        assert busy.value.pid is None
        assert "pid unknown" in str(busy.value)
        waiter = _Background(lambda: _enter_and_leave(path))
        waiter.start()
        assert _wait_until(lambda: "waiting for x" in err.read())
        assert "waiting for x (pid unknown)" in err.read()
        assert "None" not in err.read()
    waiter.finish()


def _enter_and_leave(path: Path) -> None:
    with hold(path, what="x"):
        pass


def test_a_nested_hold_of_the_same_lock_is_reentrant(tmp_path: Path) -> None:
    """C2: one flock per resolved path per process -- a nested hold neither deadlocks
    nor releases the lock when the inner block ends."""
    path = tmp_path / "store.lock"
    with hold(path, what="store"):
        with hold(path, what="store"):  # would deadlock with a second flock
            assert _taken_elsewhere(path) is False
        assert _taken_elsewhere(path) is False  # still held after the inner block
    assert _taken_elsewhere(path) is True


def test_reentrancy_follows_the_resolved_path_not_the_spelling(tmp_path: Path) -> None:
    """Two Store objects for one root, one reached through a symlink, nest safely."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    first, second = real / "x.lock", tmp_path / "alias" / ".." / "real" / "x.lock"
    done = _Background(lambda: _nest(first, second))
    done.start()
    done.finish(timeout=10)
    assert _taken_elsewhere(first) is True


def _nest(first: Path, second: Path) -> None:
    with hold(first, what="x"), hold(second, what="x"):
        pass


# --- v3: shared holders (R3.12) -- the exclusions first, then the sharing -------------


def _taken_elsewhere_in(path: Path, *, shared: bool) -> bool:
    """Whether ANOTHER process can take ``path`` right now in that mode (wait=False)."""
    probe = (
        "import sys\nfrom pathlib import Path\nfrom shidashi import locks\n"
        "try:\n"
        "    with locks.hold(Path(sys.argv[1]), what='probe', wait=False,"
        " shared=sys.argv[2] == 'shared'):\n"
        "        pass\n"
        "except locks.LockBusy:\n"
        "    sys.exit(3)\n"
    )
    mode = "shared" if shared else "exclusive"
    done = subprocess.run(
        [sys.executable, "-c", probe, str(path), mode], capture_output=True, text=True
    )
    assert done.returncode in (0, 3), done.stderr
    return done.returncode == 0


def test_an_exclusive_waiter_waits_for_every_shared_holder(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile half: the extraction must not slip in when the FIRST user leaves while a
    second one still runs its tools in the tree."""
    path = tmp_path / "toolbox" / "v3-systemd.lock"
    path.parent.mkdir()
    err = _Stderr(capfd, monkeypatch)
    entered = threading.Event()

    def extract() -> None:
        with hold(path, what="toolbox v3-systemd"):
            entered.set()

    with contextlib.ExitStack() as second_user:
        with _held_elsewhere(path, what="toolbox v3-systemd", shared=True):
            second_user.enter_context(_held_elsewhere(path, what="toolbox v3-systemd", shared=True))
            waiter = _Background(extract)
            waiter.start()
            assert _wait_until(lambda: "waiting for toolbox v3-systemd" in err.read())
            time.sleep(0.3)
            assert not entered.is_set(), "took it exclusive under two shared holders"
        # the first user left; the second still holds it shared
        time.sleep(0.5)
        assert not entered.is_set(), "took it exclusive while one shared holder remained"
    waiter.finish()
    assert entered.is_set()
    assert err.read().count("waiting for toolbox v3-systemd") == 1


def test_a_shared_waiter_waits_for_an_exclusive_holder(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile half: a user must not enter the tree while an extraction holds it."""
    path = tmp_path / "v3-systemd.lock"
    err = _Stderr(capfd, monkeypatch)
    entered = threading.Event()

    def use() -> None:
        with hold(path, what="toolbox v3-systemd", shared=True):
            entered.set()

    with _held_elsewhere(path, what="toolbox v3-systemd") as extractor:
        with (
            pytest.raises(LockBusy) as busy,
            hold(path, what="toolbox v3-systemd", wait=False, shared=True),
        ):
            pytest.fail("entered shared under an exclusive holder")
        assert busy.value.pid == extractor.pid
        waiter = _Background(use)
        waiter.start()
        assert _wait_until(lambda: "waiting for toolbox v3-systemd" in err.read())
        time.sleep(0.3)
        assert not entered.is_set(), "entered shared while another process held it exclusive"
    waiter.finish()
    assert entered.is_set()
    line = f"waiting for toolbox v3-systemd (pid {extractor.pid})"
    assert err.read().count(line) == 1


def test_a_shared_holder_keeps_an_exclusive_request_out(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with hold(path, what="x", shared=True):
        assert _taken_elsewhere_in(path, shared=False) is False
    assert _taken_elsewhere_in(path, shared=False) is True


def test_two_shared_holders_hold_the_lock_at_once_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "v3-systemd.lock"
    with _held_elsewhere(path, what="toolbox v3-systemd", shared=True) as other:
        assert other.poll() is None
        with hold(path, what="toolbox v3-systemd", wait=False, shared=True):
            assert _taken_elsewhere_in(path, shared=True) is True  # a third one too
            assert _taken_elsewhere_in(path, shared=False) is False


@pytest.mark.parametrize(("outer", "inner"), [(False, True), (True, False)])
def test_a_nested_hold_in_the_other_mode_raises_lock_error(
    tmp_path: Path, outer: bool, inner: bool
) -> None:
    """No upgrade nor downgrade of a held lock: flock conversion is not atomic -- in
    between, another process could take it. Both directions, any spelling."""
    path = tmp_path / "x.lock"
    (tmp_path / "alias").symlink_to(tmp_path)
    with hold(path, what="x", shared=outer):
        for spelling in (path, tmp_path / "alias" / "x.lock"):
            with (
                pytest.raises(locks.LockError, match="x.lock"),
                hold(spelling, what="x", shared=inner),
            ):
                pytest.fail("converted a held lock")
        # the outer hold is untouched by the refused nested one
        assert _taken_elsewhere_in(path, shared=False) is False
        assert _taken_elsewhere_in(path, shared=True) is outer
    assert _taken_elsewhere_in(path, shared=False) is True


def test_another_lock_in_the_other_mode_is_no_conversion(tmp_path: Path) -> None:
    """Wrongly collapsed: the mode check is per resolved path -- an exclusive store
    lock and a shared toolbox lock nest freely."""
    with (
        hold(tmp_path / "a.lock", what="a"),
        hold(tmp_path / "b.lock", what="b", shared=True, wait=False),
    ):
        assert _taken_elsewhere_in(tmp_path / "b.lock", shared=True) is True


def test_a_nested_shared_hold_is_reentrant(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with hold(path, what="x", shared=True):
        with hold(path, what="x", shared=True):
            pass
        assert _taken_elsewhere_in(path, shared=False) is False  # still held
    assert _taken_elsewhere_in(path, shared=False) is True


def test_lock_error_is_an_exception() -> None:
    assert issubclass(locks.LockError, Exception)
