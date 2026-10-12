"""The toolbox held for its whole use (story 006, task 4.3; R3.4, R3.5, R3.12, R3.9).

Contract (design.md v3, "Toolbox in use"): ``toolbox.using(tarball, rootfs)`` is a
context manager yielding a bool. It holds ``<rootfs>.lock`` SHARED for the whole block
when the stamp beside the tree names ``tarball``; otherwise it takes the lock EXCLUSIVE
only to (re-)extract -- never converting a held lock -- and then holds it shared for the
block. Several users of one tarball hold it at once and extract once; a re-extraction
for another tarball waits for every user of the old tree (one wait line on stderr,
``waiting for toolbox <arch>-<init> (pid N)``), and the tree is never removed under a
user. A failed extraction leaves no stamp and frees the lock. The assembler holds it
from the ``toolbox`` step through the last tool use (``publish.write_contents``) and
releases it on a failure there.

The extraction (``restore_fork_point``, root-only) is faked; the other user or
extractor is always a REAL second process.
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

from shidashi import publish, toolbox
from tests.test_assembler import (  # noqa: F401 -- the autouse fixtures _Wired relies on
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _Wired,
)

#: captured at import, before ``_toolbox_stubbed`` replaces them for the assembler tests
_REAL_ENSURE = toolbox.ensure_rootfs
_REAL_USING = getattr(toolbox, "using", None)

#: Another process using the tree of ``argv[1]`` from ``argv[2]``: its fake extraction
#: writes ``usr/bin/mksquashfs`` naming the tarball; inside the block it says "using",
#: waits for a line, then checks its tool is still the one it started with.
_USER = """
import sys
from pathlib import Path
from shidashi import toolbox

def restore(tarball, rootfs):
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "bin" / "mksquashfs").write_text(tarball.name)
    (rootfs.parent / f"extracted-by-{tarball.name}-{__import__('os').getpid()}").touch()

toolbox.restore_fork_point = restore
tar, root = Path(sys.argv[1]), Path(sys.argv[2])
with toolbox.using(tar, root) as extracted:
    tool = root / "usr" / "bin" / "mksquashfs"
    before = tool.read_text()
    print(f"using {extracted}", flush=True)
    sys.stdin.readline()
    print("intact" if tool.is_file() and tool.read_text() == before else "wiped", flush=True)
"""

#: Another process re-extracting through the factory's direct call.
_EXTRACTOR = """
import sys
from pathlib import Path
from shidashi import toolbox

def restore(tarball, rootfs):
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "bin" / "mksquashfs").write_text(tarball.name)

toolbox.restore_fork_point = restore
print(toolbox.ensure_rootfs(Path(sys.argv[1]), Path(sys.argv[2])), flush=True)
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


def _mode(path: Path) -> str:
    """``free``, ``shared`` or ``exclusive``: how ``path``'s flock is held right now,
    probed from a fresh open file description (this process's holders count too)."""
    if not path.exists():
        return "free"
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return "free"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "exclusive"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "shared"
    finally:
        os.close(fd)


def _lock_of(rootfs: Path) -> Path:
    return rootfs.with_name(f"{rootfs.name}.lock")


def _stamp_of(rootfs: Path) -> Path:
    return rootfs.with_name(f"{rootfs.name}.stamp")


def _tarball(tmp_path: Path, name: str) -> Path:
    tar = tmp_path / "fork-points" / name
    tar.parent.mkdir(parents=True, exist_ok=True)
    tar.write_bytes(name.encode())
    return tar


def _using() -> Callable[..., Any]:
    using = getattr(toolbox, "using", None)
    assert using is not None, "module 'shidashi.toolbox' has no attribute 'using'"
    return using  # type: ignore[no-any-return]


@contextlib.contextmanager
def _user_elsewhere(tar: Path, root: Path) -> Iterator[tuple[subprocess.Popen[str], list[str]]]:
    """A real second process inside ``using(tar, root)`` for the block; yields it and,
    after the block, what it said when it left (``intact`` or ``wiped``)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _USER, str(tar), str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    said: list[str] = []
    try:
        line = proc.stdout.readline()
        if not line.startswith("using "):
            proc.wait(timeout=10)
            pytest.fail(f"the user never entered the toolbox: {proc.stderr.read()}")
        said.append(line.strip())
        yield proc, said
    finally:
        if proc.poll() is None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write("\n")
                proc.stdin.flush()
            out, _err = proc.communicate(timeout=20)
            said.extend(out.split())


def _fake_restore(monkeypatch: pytest.MonkeyPatch, calls: list[Path]) -> None:
    def restore(tarball: Path, rootfs: Path) -> None:
        calls.append(tarball)
        (rootfs / "usr" / "bin").mkdir(parents=True)
        (rootfs / "usr" / "bin" / "mksquashfs").write_text(tarball.name)

    monkeypatch.setattr(toolbox, "restore_fork_point", restore)


# --- the exclusions first (hostile halves) -------------------------------------------


def test_a_re_extraction_waits_for_a_user_and_never_removes_the_tree_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """kde's assemble (another process) runs its tools from the tree of the old pins; this
    one needs the toolbox of new pins. It waits, says so once on stderr naming the user,
    and only after the user left replaces the tree; the user saw its tree intact."""
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    old, new = _tarball(tmp_path, "v3-systemd-OLD.tar"), _tarball(tmp_path, "v3-systemd-NEW.tar")
    calls: list[Path] = []
    _fake_restore(monkeypatch, calls)
    using = _using()
    with _user_elsewhere(old, root) as (user, said):
        assert _mode(_lock_of(root)) == "shared", "the user does not hold the tree shared"
        waiter = _Background(lambda: _enter(using, new, root))
        waiter.start()
        time.sleep(0.5)
        assert waiter.is_alive(), "the re-extraction did not wait for the tree's user"
        assert (root / "usr" / "bin" / "mksquashfs").read_text() == old.name
        assert calls == []
    waiter.finish()
    assert said[-1] == "intact", f"the tree was removed under its user: {said}"
    assert calls == [new]
    assert _stamp_of(root).read_text().startswith(f"{new.name} ")  # the stamp names it
    err = capfd.readouterr().err
    assert err.count("waiting for toolbox v3-systemd") == 1
    assert f"waiting for toolbox v3-systemd (pid {user.pid})" in err


def _enter(using: Callable[..., Any], tar: Path, root: Path) -> Any:
    with using(tar, root) as extracted:
        assert _mode(_lock_of(root)) == "shared"
        return extracted


def test_the_factorys_direct_extraction_waits_for_a_user_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This process uses the tree; another process calls ``ensure_rootfs`` with a rebuilt
    toolbox. Today's ``rmtree`` would wipe the tree mid-use; the user lock keeps it whole."""
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    old, new = _tarball(tmp_path, "v3-systemd-OLD.tar"), _tarball(tmp_path, "v3-systemd-NEW.tar")
    _fake_restore(monkeypatch, [])
    using = _using()
    tool = root / "usr" / "bin" / "mksquashfs"
    with using(old, root):
        other = subprocess.Popen(
            [sys.executable, "-c", _EXTRACTOR, str(new), str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(1.0)
            assert tool.is_file() and tool.read_text() == old.name, "wiped under its user"
            assert other.poll() is None, "the direct extraction did not wait"
        finally:
            if other.poll() is not None and other.returncode != 0:
                pytest.fail(f"the extractor failed: {other.communicate()[1]}")
    out, err = other.communicate(timeout=20)
    assert other.returncode == 0 and out.strip() == "True", err
    assert tool.read_text() == new.name


def test_a_failed_extraction_leaves_no_stamp_and_frees_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    tar = _tarball(tmp_path, "v3-systemd-SNAP.tar")

    def broken(tarball: Path, rootfs: Path) -> None:
        raise OSError(f"tar: {tarball}: unexpected end of file")

    monkeypatch.setattr(toolbox, "restore_fork_point", broken)
    using = _using()
    with pytest.raises(Exception, match="unexpected end of file|v3-systemd-SNAP.tar"):  # noqa: SIM117
        with using(tar, root):
            pytest.fail("entered a toolbox whose extraction failed")
    assert _lock_of(root).exists(), "the extraction never took the toolbox lock"
    assert _mode(_lock_of(root)) == "free"
    assert not _stamp_of(root).exists()
    calls: list[Path] = []
    _fake_restore(monkeypatch, calls)
    with using(tar, root):
        pass
    assert calls == [tar]  # the next use extracts again


# --- then the sharing ------------------------------------------------------------------


def test_two_users_of_one_tarball_hold_it_at_once_and_extract_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    tar = _tarball(tmp_path, "v3-systemd-SNAP.tar")
    calls: list[Path] = []
    _fake_restore(monkeypatch, calls)
    using = _using()
    with _user_elsewhere(tar, root) as (_user, said):
        second = _Background(lambda: _enter(using, tar, root))
        second.start()
        assert second.finish(timeout=5) is False, "the second user extracted or waited"
    assert said[-1] == "intact"
    extractions = list(root.parent.glob("extracted-by-*"))
    assert len(extractions) == 1 and calls == [], (extractions, calls)
    assert _mode(_lock_of(root)) == "free"


def test_two_toolboxes_are_two_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrongly collapsed: a user of v3-systemd never delays v3-openrc's extraction."""
    _fake_restore(monkeypatch, [])
    systemd = tmp_path / "scratch" / "toolbox" / "v3-systemd"
    openrc = tmp_path / "scratch" / "toolbox" / "v3-openrc"
    using = _using()
    with _user_elsewhere(_tarball(tmp_path, "v3-systemd.tar"), systemd):
        call = _Background(lambda: _enter(using, _tarball(tmp_path, "v3-openrc.tar"), openrc))
        call.start()
        call.finish(timeout=5)


# --- the assembler holds it from the toolbox step to the last tool use -------------------


def _real_toolbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo ``_toolbox_stubbed`` for the lock: the real ``using``/``ensure_rootfs`` with
    a fake extraction; the tools themselves stay the recorded ``_FakeTools``."""
    monkeypatch.setattr(toolbox, "ensure_rootfs", _REAL_ENSURE)
    if _REAL_USING is not None:
        monkeypatch.setattr(toolbox, "using", _REAL_USING)
    _fake_restore(monkeypatch, [])


def _toolbox_lock(tmp_path: Path) -> Path:
    found = list((tmp_path / "scratch" / "toolbox").glob("*.lock"))
    assert len(found) == 1, f"no single toolbox lock under scratch/toolbox: {found}"
    return found[0]


def test_the_assembler_holds_the_toolbox_from_its_step_to_after_write_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    _real_toolbox(monkeypatch)
    seen: dict[str, str] = {}
    lock_dir = tmp_path / "scratch" / "toolbox"

    def mode_now() -> str:
        found = list(lock_dir.glob("*.lock")) if lock_dir.is_dir() else []
        return _mode(found[0]) if len(found) == 1 else "no lock"

    from tests.test_assembler import _FakeTools

    real_versions, real_contents = _FakeTools.versions, publish.write_contents

    def versions(self: Any) -> dict[str, str]:
        seen["toolbox step"] = mode_now()
        return real_versions(self)

    def contents(*a: Any, **k: Any) -> Any:
        seen["write_contents"] = mode_now()
        return real_contents(*a, **k)

    monkeypatch.setattr(_FakeTools, "versions", versions)
    monkeypatch.setattr(publish, "write_contents", contents)
    wired.assemble(flavor="kde")
    assert seen == {"toolbox step": "shared", "write_contents": "shared"}, seen
    assert _mode(_toolbox_lock(tmp_path)) == "free"  # released after the last tool use


def test_a_failure_at_write_contents_releases_the_toolbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    _real_toolbox(monkeypatch)
    lock_dir = tmp_path / "scratch" / "toolbox"
    held: list[str] = []

    def broken(*_a: Any, **_k: Any) -> Any:
        found = list(lock_dir.glob("*.lock")) if lock_dir.is_dir() else []
        held.append(_mode(found[0]) if len(found) == 1 else "no lock")
        raise OSError("unsquashfs: disk full")

    monkeypatch.setattr(publish, "write_contents", broken)
    with pytest.raises(Exception, match="disk full"):
        wired.assemble(flavor="kde")
    assert held == ["shared"], f"the toolbox was not held when write_contents failed: {held}"
    assert _mode(_toolbox_lock(tmp_path)) == "free", "the failure left the toolbox held"
