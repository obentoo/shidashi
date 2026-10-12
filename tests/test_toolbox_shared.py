"""The toolbox extracted at most once by concurrent assembles (story 006, task 4.1).

Contract (design.md, tasks.md 4.1): ``ensure_rootfs(tarball, rootfs)`` -- signature
unchanged -- holds the toolbox lock ``<rootfs>.lock`` (``scratch/toolbox/<arch>-<init>.lock``)
while it checks the stamp, removes and extracts; a second caller that waited finds
the first one's stamp and extracts nothing (R3.4).

v3: the lock ``ensure_rootfs`` holds is EXCLUSIVE (the factory's and the tests' direct
calls, and ``toolbox.using``'s extraction, task 4.3): users of the tree hold the same
lock SHARED for their whole use, so an extraction -- and its wait line -- waits for every
user. ``_locked`` probes with ``LOCK_SH | LOCK_NB`` from its own open file description:
it reports True only for an EXCLUSIVE holder.

The extraction (``restore_fork_point``, root-only) is faked; the other extractor is
a REAL second process running ``ensure_rootfs`` with its own fake extraction.
"""

import contextlib
import fcntl
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import toolbox

#: Another process extracting the toolbox: its fake extraction stops halfway until
#: its stdin says go.
_EXTRACTOR = """
import sys
from pathlib import Path
from shidashi import toolbox

def restore(tarball, rootfs):
    (rootfs / "usr").mkdir()
    (rootfs / "usr" / "half").write_text("extracting")
    print("extracting", flush=True)
    sys.stdin.readline()
    (rootfs / "usr" / "bin").mkdir()

toolbox.restore_fork_point = restore
print(toolbox.ensure_rootfs(Path(sys.argv[1]), Path(sys.argv[2])), flush=True)
"""

_HOLDER = """
import sys
from pathlib import Path
from shidashi import locks
kw = {"shared": True} if sys.argv[2:] == ["shared"] else {}
with locks.hold(Path(sys.argv[1]), what="toolbox", **kw):
    print("held", flush=True)
    sys.stdin.readline()
"""


class _Background(threading.Thread):
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
        assert not self.is_alive(), "still blocked"
        if self.error is not None:
            raise self.error
        return self.result


def _locked(path: Path) -> bool:
    """Whether any open file description holds ``path``'s flock (this process's too)."""
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextlib.contextmanager
def _held_elsewhere(path: Path, *, shared: bool = False) -> Iterator[subprocess.Popen[str]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path), *(["shared"] if shared else [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        if proc.stdout.readline() != "held\n":
            proc.wait(timeout=10)
            pytest.fail(f"the holder never took {path}: {proc.stderr.read()}")
        yield proc
    finally:
        if proc.poll() is None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write("\n")
                proc.stdin.flush()
            proc.wait(timeout=10)


def _tarball(tmp_path: Path) -> Path:
    tar = tmp_path / "fork-points" / "v3-systemd-SNAP-toolbox.tar"
    tar.parent.mkdir(parents=True)
    tar.write_bytes(b"toolbox")
    return tar


def _lock_of(rootfs: Path) -> Path:
    return rootfs.with_name(f"{rootfs.name}.lock")


def test_the_extraction_runs_under_the_toolbox_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    held: list[bool] = []

    def restore(tarball: Path, rootfs: Path) -> None:
        held.append(_locked(_lock_of(rootfs)))
        (rootfs / "usr").mkdir()

    monkeypatch.setattr(toolbox, "restore_fork_point", restore)
    assert toolbox.ensure_rootfs(_tarball(tmp_path), root) is True
    assert held == [True]
    assert not _locked(_lock_of(root))  # released once extracted


def test_a_second_extractor_waits_and_then_extracts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.4: gnome's assemble reaches the toolbox while kde's (another process) is
    extracting it. Unlocked, gnome sees no stamp yet and wipes kde's half-made tree
    to extract again; locked, it waits and finds kde's stamp."""
    tar = _tarball(tmp_path)
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    mine: list[Path] = []

    def restore(tarball: Path, rootfs: Path) -> None:
        mine.append(rootfs)
        (rootfs / "usr").mkdir()

    monkeypatch.setattr(toolbox, "restore_fork_point", restore)
    other = subprocess.Popen(
        [sys.executable, "-c", _EXTRACTOR, str(tar), str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert other.stdin is not None and other.stdout is not None
    try:
        line = other.stdout.readline()
        assert line == "extracting\n", other.communicate(timeout=10)[1]
        second = _Background(lambda: toolbox.ensure_rootfs(tar, root))
        second.start()
        time.sleep(0.5)
        assert second.is_alive(), "the second extractor did not wait for the first"
        assert (root / "usr" / "half").is_file(), "the first extraction was wiped"
        other.stdin.write("go\n")
        other.stdin.flush()
        out, err = other.communicate(timeout=20)
        assert other.returncode == 0 and out.strip().endswith("True"), err
        extracted = second.finish()
    finally:
        if other.poll() is None:
            other.kill()
    assert extracted is False
    assert mine == []  # extracted once, by the first
    assert (root / "usr" / "bin").is_dir()


def test_two_toolboxes_do_not_wait_for_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly collapsed: the v3-openrc toolbox must not wait for a holder of the
    v3-systemd one (one lock per toolbox, beside it)."""
    monkeypatch.setattr(toolbox, "restore_fork_point", lambda _t, r: (r / "usr").mkdir())
    systemd = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    openrc = tmp_path / "scratch" / "toolbox" / "v3-openrc"
    tar = _tarball(tmp_path)
    with _held_elsewhere(_lock_of(systemd)):
        call = _Background(lambda: toolbox.ensure_rootfs(tar, openrc))
        call.start()
        assert call.finish(timeout=5) is True


def test_a_failed_extraction_releases_the_lock_and_leaves_no_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"

    def broken(tarball: Path, rootfs: Path) -> None:
        raise OSError(f"tar: {tarball}: unexpected end of file")

    monkeypatch.setattr(toolbox, "restore_fork_point", broken)
    tar = _tarball(tmp_path)
    with pytest.raises(Exception, match="unexpected end of file|v3-systemd-SNAP-toolbox.tar"):
        toolbox.ensure_rootfs(tar, root)
    assert _lock_of(root).exists(), "the extraction never took the toolbox lock"
    assert not _locked(_lock_of(root))
    assert not root.with_name(f"{root.name}.stamp").exists()  # never trusted later

    monkeypatch.setattr(toolbox, "restore_fork_point", lambda _t, r: (r / "usr").mkdir())
    assert toolbox.ensure_rootfs(tar, root) is True  # the next one extracts again


def test_a_wait_for_the_toolbox_says_which_toolbox_and_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.9 on the toolbox lock, reported on stderr with no on_wait passed."""
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    monkeypatch.setattr(
        toolbox, "restore_fork_point", lambda tarball, rootfs: (rootfs / "usr").mkdir()
    )
    tar = _tarball(tmp_path)
    with _held_elsewhere(_lock_of(root)) as holder:
        waiter = _Background(lambda: toolbox.ensure_rootfs(tar, root))
        waiter.start()
        time.sleep(0.5)
    waiter.finish()
    err = capfd.readouterr().err
    assert err.count("waiting for toolbox v3-systemd") == 1
    assert f"(pid {holder.pid})" in err


# --- v3: the extraction lock is the EXCLUSIVE one -----------------------------------------


def test_the_extraction_holds_the_toolbox_lock_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile half: a SHARED hold during the extraction would let a user's tools run in
    a half-made tree. Another process asking for it shared must be refused meanwhile."""
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    probe = (
        "import sys\nfrom pathlib import Path\nfrom shidashi import locks\n"
        "try:\n"
        "    with locks.hold(Path(sys.argv[1]), what='user', wait=False, shared=True):\n"
        "        pass\n"
        "except locks.LockBusy:\n"
        "    sys.exit(3)\n"
    )
    seen: list[int] = []

    def restore(tarball: Path, rootfs: Path) -> None:
        done = subprocess.run(
            [sys.executable, "-c", probe, str(_lock_of(rootfs))], capture_output=True, text=True
        )
        assert done.returncode in (0, 3), done.stderr
        seen.append(done.returncode)
        (rootfs / "usr").mkdir()

    monkeypatch.setattr(toolbox, "restore_fork_point", restore)
    assert toolbox.ensure_rootfs(_tarball(tmp_path), root) is True
    assert seen == [3], "a shared user got in during the extraction"


def test_an_extraction_waits_for_a_user_holding_the_tree_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A rebuilt toolbox (a stamp that no longer matches) must not be re-extracted while
    an assemble in another process runs its tools in the tree: the extraction waits,
    says so once on stderr naming the user's pid, and the tree stays whole meanwhile."""
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    old = tmp_path / "fork-points-old" / "v3-systemd-OLD-toolbox.tar"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old toolbox")

    def restore(tarball: Path, rootfs: Path) -> None:
        (rootfs / "usr" / "bin").mkdir(parents=True)
        (rootfs / "usr" / "bin" / "mksquashfs").write_text(tarball.name)

    monkeypatch.setattr(toolbox, "restore_fork_point", restore)
    assert toolbox.ensure_rootfs(old, root) is True
    new = _tarball(tmp_path)
    tool = root / "usr" / "bin" / "mksquashfs"
    with _held_elsewhere(_lock_of(root), shared=True) as user:
        waiter = _Background(lambda: toolbox.ensure_rootfs(new, root))
        waiter.start()
        time.sleep(0.5)
        assert waiter.is_alive(), "the extraction did not wait for the tree's user"
        assert tool.read_text() == old.name, "the tree was replaced under its user"
    assert waiter.finish() is True
    assert tool.read_text() == new.name
    err = capfd.readouterr().err
    assert err.count("waiting for toolbox v3-systemd") == 1
    assert f"waiting for toolbox v3-systemd (pid {user.pid})" in err
