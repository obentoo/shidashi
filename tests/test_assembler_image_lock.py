"""One assemble per image (story 006, task 5.1).

Contract (design.md): ``Assembler.assemble`` holds ``scratch/assemble/<key>.lock``
with ``wait=False`` for its whole run; a second assemble of the same
``<arch>-<image>-<init>`` fails at once with ``AssemblerError("... is being
assembled by pid N")`` (R3.6), touching nothing of the running one; a holder whose
pid cannot be read is named "pid unknown" (C2). A failed assemble keeps its checkpoints
and its scratch rootfs (R2.3).

v3: the lock is taken BEFORE ``write_squashfs_exclude`` (right after the root and
toolbox-stage checks): it guards everything keyed by the image under
``scratch/assemble/`` -- ``<key>/``, ``<key>.judge``, ``<key>.squashfs-exclude``,
``<key>.*.squashfs``. A refused second assemble rewrites no exclude list and makes (or
removes) no ``.judge`` copy of the running one.
"""

import contextlib
import fcntl
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import shidashi.assembler as asm
from shidashi import image, publish
from shidashi.assembler import AssemblerError
from shidashi.image import ImageError
from tests.test_assembler import (  # noqa: F401 -- the autouse fixtures _Wired relies on
    _chain,
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _trunked,
    _Wired,
)

_HOLDER = """
import sys
from pathlib import Path
from shidashi import locks
with locks.hold(Path(sys.argv[1]), what="assemble"):
    print("held", flush=True)
    sys.stdin.readline()
"""


@contextlib.contextmanager
def _held_elsewhere(path: Path) -> Iterator[subprocess.Popen[str]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
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


def _in_background(fn: Callable[[], Any], timeout: float = 20) -> BaseException | None:
    """Run ``fn`` in a daemon thread; return what it raised (None: it returned)."""
    box: list[BaseException | None] = [None]

    def run() -> None:
        try:
            fn()
        except BaseException as err:  # returned to the caller
            box[0] = err

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "the assemble waited instead of failing at once"
    return box[0]


def _locked(path: Path) -> bool:
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


def _assemble_dir(tmp_path: Path) -> Path:
    return tmp_path / "scratch" / "assemble"


def test_a_second_assemble_of_the_same_image_is_refused_naming_the_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    running_rootfs = _assemble_dir(tmp_path) / "znver5-kde-systemd"
    running_rootfs.mkdir(parents=True)
    (running_rootfs / "installing").write_text("the other assemble's work")
    with _held_elsewhere(_assemble_dir(tmp_path) / "znver5-kde-systemd.lock") as other:
        err = _in_background(lambda: wired.assemble(flavor="kde"))
    assert isinstance(err, AssemblerError), err
    assert f"znver5-kde-systemd is being assembled by pid {other.pid}" in str(err)
    # nothing of the running assemble was touched
    assert wired.extracted == 0
    assert wired.store.marks() == []
    assert (running_rootfs / "installing").read_text() == "the other assemble's work"


def test_the_image_lock_is_held_for_the_whole_assemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    lock = _assemble_dir(tmp_path) / "znver5-kde-systemd.lock"
    held: dict[str, bool] = {}
    real_squashfs, real_sums = image.make_squashfs, publish.update_sha256sums

    def squashfs(*a: Any, **k: Any) -> Any:
        held["squashfs"] = _locked(lock)
        return real_squashfs(*a, **k)

    def sums(*a: Any, **k: Any) -> Any:
        held["publish"] = _locked(lock)
        return real_sums(*a, **k)

    monkeypatch.setattr(image, "make_squashfs", squashfs)
    monkeypatch.setattr(publish, "update_sha256sums", sums)
    wired.assemble(flavor="kde")
    assert held == {"squashfs": True, "publish": True}
    assert not _locked(lock)  # released at the end


def test_other_images_and_inits_are_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly collapsed: kde under openrc, and gnome under systemd, are other
    images -- their holders must not refuse kde under systemd."""
    wired = _Wired(tmp_path, monkeypatch)
    with (
        _held_elsewhere(_assemble_dir(tmp_path) / "znver5-kde-openrc.lock"),
        _held_elsewhere(_assemble_dir(tmp_path) / "znver5-gnome-systemd.lock"),
    ):
        assert _in_background(lambda: wired.assemble(flavor="kde")) is None


def test_a_failed_assemble_releases_the_image_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    wired.fail_squashfs = True
    with pytest.raises(ImageError):
        wired.assemble(flavor="kde")
    lock = _assemble_dir(tmp_path) / "znver5-kde-systemd.lock"
    assert lock.exists(), "the assemble never took its image lock"
    assert not _locked(lock)
    # R2.3: releasing the image lock on failure removes nothing of the failed image
    assert (_assemble_dir(tmp_path) / "znver5-kde-systemd").is_dir()
    assert wired.store.marks(), "the failed image's checkpoints were dropped"
    wired.fail_squashfs = False
    wired.assemble(flavor="kde")  # the next run of the image is not refused


def test_an_unreadable_holder_pid_is_named_pid_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: never "pid None" in the refusal."""
    wired = _Wired(tmp_path, monkeypatch)
    lock = _assemble_dir(tmp_path) / "znver5-kde-systemd.lock"
    with _held_elsewhere(lock):
        lock.write_text("")  # the holder's pid unreadable
        err = _in_background(lambda: wired.assemble(flavor="kde"))
    assert isinstance(err, AssemblerError), err
    assert "znver5-kde-systemd is being assembled by pid unknown" in str(err)
    assert "None" not in str(err)


# --- v3: the lock before any scratch write -------------------------------------------------


def test_a_refused_assemble_rewrites_no_exclude_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running assemble's ``mksquashfs -ef`` reads its exclude list: a refused second
    one must not have rewritten it first."""
    wired = _Wired(tmp_path, monkeypatch)
    exclude = _assemble_dir(tmp_path) / "znver5-kde-systemd.squashfs-exclude"
    exclude.parent.mkdir(parents=True)
    exclude.write_text("the running assemble's list\n")
    with _held_elsewhere(_assemble_dir(tmp_path) / "znver5-kde-systemd.lock"):
        err = _in_background(lambda: wired.assemble(flavor="kde"))
    assert exclude.read_text() == "the running assemble's list\n", (
        "the refused assemble rewrote the running one's exclude list"
    )
    assert isinstance(err, AssemblerError), err


def test_a_refused_assemble_makes_and_removes_no_judge_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kde grows from a trunk with no checkpoint: an unrefused run would copy its seeded
    rootfs to ``<key>.judge`` (story 019) -- removing the running one's copy first."""
    wired = _trunked(tmp_path, monkeypatch)
    judge = _assemble_dir(tmp_path) / "znver5-kde-systemd.judge"
    judge.mkdir(parents=True)
    (judge / "being-judged").write_text("the running assemble's copy")
    copies: list[Path] = []
    backend = wired.store.backend
    real_snapshot = backend.snapshot

    def snapshot(source: Path, dest: Path, *, readonly: bool) -> None:
        copies.append(Path(dest))
        real_snapshot(source, dest, readonly=readonly)

    monkeypatch.setattr(backend, "snapshot", snapshot)
    with _held_elsewhere(_assemble_dir(tmp_path) / "znver5-kde-systemd.lock"):
        err = _in_background(lambda: wired.assemble(recipe=_chain("kde")))
    assert (judge / "being-judged").is_file(), "the refused assemble removed the running copy"
    assert not [c for c in copies if c.name.endswith(".judge")], copies
    assert isinstance(err, AssemblerError), err


def test_the_image_lock_is_held_when_the_exclude_list_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    lock = _assemble_dir(tmp_path) / "znver5-kde-systemd.lock"
    held: list[bool] = []
    real = asm.write_squashfs_exclude

    def write(patterns: tuple[str, ...], dest: Path) -> Path:
        held.append(_locked(lock))
        return real(patterns, dest)

    monkeypatch.setattr(asm, "write_squashfs_exclude", write)
    wired.assemble(flavor="kde")
    assert held == [True], "the exclude list was written before the image lock was taken"
