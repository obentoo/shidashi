"""The assembler's find -> restore -> claim as one critical section (story 006, task 2.2).

Contract (design.md): the seed step's lookup of a checkpoint, its restore, its
validation and its claim (and the drop of a stale candidate) run inside
``store.locked()`` -- so two assembles resuming from one checkpoint both end in its
image list, and neither restores a checkpoint the other is dropping -- while the
long steps after the seed (install, initramfs, squashfs) run WITHOUT the store
lock, or concurrent assembles would run one after another. Lock order (C2, R3.10):
the image's own resume runs inside ``store.locked()`` and the store lock is let go
BEFORE ``_find_trunk``, which takes the trunk lock first and the store lock inside it
-- never the trunk lock inside the store lock.

The assemble is the ``_Wired`` one of tests/test_assembler.py (fake container, fake
btrfs store); "is the store lock held" is probed with a non-blocking flock on a
fresh descriptor, which sees this process's own lock too.
"""

import contextlib
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

import shidashi
import shidashi.assembler as asm
from shidashi import checkpoint, image
from tests.test_assembler import (  # noqa: F401 -- the autouse fixtures _Wired relies on
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _Wired,
)

_ROOT = Path(shidashi.__file__).resolve().parents[1]


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


def _probe_store(
    wired: _Wired, monkeypatch: pytest.MonkeyPatch, seen: list[tuple[str, bool]]
) -> Path:
    """Record, at each store call and container command, whether the store lock is held."""
    lock = wired.store.root / ".lock"
    store = wired.store
    for name in ("restore", "claim", "drop"):
        real = getattr(store, name)

        def probed(*args: object, _name: str = name, _real: object = real, **kw: object) -> object:
            seen.append((_name, _locked(lock)))
            return _real(*args, **kw)  # type: ignore[operator]

        monkeypatch.setattr(store, name, probed)

    wired_container = vars(asm)["Container"]

    class _Probing(wired_container):  # type: ignore[misc,valid-type]
        def run(self, argv: list[str], **kw: object) -> object:
            label = "pretend" if argv[-1:] == ["--pretend"] else argv[0]
            seen.append((label, _locked(lock)))
            return super().run(argv, **kw)

    monkeypatch.setattr(asm, "Container", _Probing)
    real_squashfs = image.make_squashfs

    def squashfs(*args: object, **kw: object) -> object:
        seen.append(("squashfs", _locked(lock)))
        return real_squashfs(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(image, "make_squashfs", squashfs)
    return lock


def test_a_resume_finds_restores_validates_and_claims_under_the_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    wired.assemble(flavor="kde")  # leaves the checkpoints worker resumes from
    seen: list[tuple[str, bool]] = []
    _probe_store(wired, monkeypatch, seen)

    wired.assemble(flavor="worker")
    assert wired.step("seed")["restored"] == checkpoint.PACKAGES
    first = {label: held for label, held in reversed(seen)}  # each label's FIRST probe
    assert first["restore"] is True, seen
    assert first["pretend"] is True, seen  # the validation of what was restored
    assert first["claim"] is True, seen


def test_the_store_lock_is_not_held_past_the_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly collapsed: holding the store lock through the install or the
    squashfs would serialize every concurrent assemble of the arch and init."""
    wired = _Wired(tmp_path, monkeypatch)
    seen: list[tuple[str, bool]] = []
    _probe_store(wired, monkeypatch, seen)
    wired.assemble(flavor="kde")  # a whole install: no checkpoint yet
    held = {label for label, is_held in seen if is_held}
    assert "emerge" not in held, seen
    assert "dracut" not in held, seen
    assert "squashfs" not in held, seen


def test_a_stale_checkpoint_is_restored_and_dropped_under_the_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    wired.assemble(flavor="kde")
    wired.write_index(a_build="2")  # one of its binpkgs rebuilt: both checkpoints stale
    seen: list[tuple[str, bool]] = []
    _probe_store(wired, monkeypatch, seen)
    wired.assemble(flavor="kde")
    assert len(wired.step("seed")["stale"]) == 2  # type: ignore[arg-type]
    restores = [held for label, held in seen if label == "restore"]
    drops = [held for label, held in seen if label == "drop"]
    assert restores[:2] == [True, True], seen
    assert drops[:2] == [True, True], seen


_CLAIMER = """
import sys
from pathlib import Path
from shidashi.checkpoint import PACKAGES, Store
from tests._fake_btrfs import FakeBackend
store = Store(Path(sys.argv[1]), FakeBackend())
store.save(Path(sys.argv[2]), PACKAGES, sys.argv[3], image=sys.argv[4], run_id=None)
"""


def test_two_assembles_resuming_from_one_checkpoint_both_end_in_its_image_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.1: while worker validates the checkpoint it restored, another PROCESS
    (gnome's assemble) claims the same checkpoint. Neither claim may be lost."""
    wired = _Wired(tmp_path, monkeypatch)
    wired.assemble(flavor="kde")
    (packages,) = wired.store.marks(checkpoint.PACKAGES)
    other_rootfs = tmp_path / "gnome-rootfs"
    other_rootfs.mkdir()
    env = dict(os.environ, PYTHONPATH=str(_ROOT))
    claimer: list[subprocess.Popen[str]] = []
    wired_container = vars(asm)["Container"]

    class _ConcurrentClaim(wired_container):  # type: ignore[misc,valid-type]
        def run(self, argv: list[str], **kw: object) -> object:
            if argv[-1:] == ["--pretend"] and not claimer:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _CLAIMER,
                        str(wired.store.root),
                        str(other_rootfs),
                        packages.fingerprint,
                        "znver5-gnome-systemd",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                claimer.append(proc)
                # unlocked, the other claim lands now, before worker's own claim;
                # under the store lock it waits for worker's critical section
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
            return super().run(argv, **kw)

    monkeypatch.setattr(asm, "Container", _ConcurrentClaim)
    wired.assemble(flavor="worker")
    assert claimer, "worker never validated a restored checkpoint"
    _, err = claimer[0].communicate(timeout=30)
    assert claimer[0].returncode == 0, err
    mark = wired.store.find(checkpoint.PACKAGES, packages.fingerprint)
    assert mark is not None
    assert set(mark.images) == {
        "znver5-kde-systemd",
        "znver5-worker-systemd",
        "znver5-gnome-systemd",
    }


def test_a_checkpoint_error_in_the_restore_propagates_and_frees_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    wired.assemble(flavor="kde")
    real_snapshot = wired.store.backend.snapshot

    def snapshot(source: Path, dest: Path, *, readonly: bool) -> None:
        if not readonly:  # a restore
            raise checkpoint.CheckpointError(f"btrfs subvolume snapshot {source} {dest}: busy")
        real_snapshot(source, dest, readonly=readonly)

    monkeypatch.setattr(wired.store.backend, "snapshot", snapshot)
    with pytest.raises(checkpoint.CheckpointError, match="busy"):
        wired.assemble(flavor="worker")
    lock = wired.store.root / ".lock"
    assert lock.exists(), "the resume never took the store lock"
    assert not _locked(lock), "a failed restore left the store locked"


def test_the_trunk_is_never_looked_up_under_the_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: _find_trunk blocks on the trunk lock; entering it with the store lock held
    deadlocks against a trunk holder that saves under the store lock."""
    from tests.test_assembler import _chain, _trunked

    wired = _trunked(tmp_path, monkeypatch)
    lock = wired.store.root / ".lock"
    held: list[bool] = []
    real = asm.Assembler._find_trunk

    def find_trunk(self: asm.Assembler, *args: object, **kw: object) -> object:
        held.append(_locked(lock))
        return real(self, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(asm.Assembler, "_find_trunk", find_trunk)
    wired.assemble(recipe=_chain("kde"))
    assert held == [False], "the trunk was looked up while the store lock was held"
