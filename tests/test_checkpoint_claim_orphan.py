"""A claim never brings a vanished checkpoint back (story 006, task 8.1; R3.1, R3.8, R3.11).

Contract (design.md v4, "Checkpoint store under lock"): ``Store.claim(mark, image)`` returns
``Mark | None``. It re-reads the manifest under the store lock; when the manifest is gone --
another process dropped the checkpoint after ``mark`` was read -- it returns None and writes
NOTHING: writing ``mark`` back would leave a manifest whose snapshot is gone, which ``prune``
cannot remove (it reads as a valid manifest) and which every later lookup would trust.
Callers treat None as "not claimed": ``_find_trunk`` installs the trunk, the settle re-claim
skips the trunk the flavor's own save removed. A live manifest is claimed as before: the image
is added to the list read under the lock, an image another process added is kept.

Hostile halves first: a vanished manifest whose stale ``mark`` already lists the image
(nothing would seem to need writing, yet the answer must still be None), a snapshot left
without its manifest; then the converse -- a manifest dropped and saved AGAIN under the
same name is live, never "vanished".

The store runs over tests/_fake_btrfs.py; the "other process" that drops a checkpoint is a
real second process. The assembler cases are the ``_trunked`` assemble of
tests/test_assembler.py.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import shidashi
from shidashi import checkpoint
from shidashi.assembler import iso_emerge_argv
from tests._fake_btrfs import FakeBackend
from tests.test_assembler import (  # noqa: F401 -- the autouse fixtures _Wired relies on
    _chain,
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _trunked,
    _Wired,
)

_REPO = Path(shidashi.__file__).resolve().parents[1]
_TRUNK_KEY = "znver5-desktop-systemd"
_KDE = "znver5-kde-systemd"
_FP = "a" * 64
_OTHER_FP = "b" * 64

#: Another process dropping the checkpoint of fingerprint argv[2] from the store argv[1].
_DROPPER = """
import sys
from pathlib import Path
from shidashi import checkpoint
from tests._fake_btrfs import FakeBackend
store = checkpoint.Store(Path(sys.argv[1]), FakeBackend())
(mark,) = [m for m in store.marks() if m.fingerprint == sys.argv[2]]
store.drop(mark)
"""


def _store(tmp_path: Path) -> checkpoint.Store:
    return checkpoint.Store(tmp_path / "ckpt", FakeBackend())


def _rootfs(tmp_path: Path) -> Path:
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True, exist_ok=True)
    (rootfs / "etc" / "os-release").write_text("NAME=Bentoo\n")
    return rootfs


def _manifest(mark: checkpoint.Mark) -> Path:
    return mark.path.with_name(mark.path.name + ".json")


def _dropped_elsewhere(store: checkpoint.Store, fingerprint: str) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _DROPPER, str(store.root), fingerprint],
        cwd=_REPO,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, f"the other process could not drop it: {done.stderr}"


def _orphans(store: checkpoint.Store) -> list[str]:
    """Manifests whose snapshot is gone: what a claim must never leave behind."""
    return [m.path.name for m in store.marks() if not m.path.is_dir()]


# --- hostile halves: a vanished manifest is never written back ----------------------------


def test_a_claim_after_another_process_dropped_the_checkpoint_returns_none(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    mark = store.save(_rootfs(tmp_path), checkpoint.INSTALL, _FP, image="kde", run_id="r1")
    _dropped_elsewhere(store, _FP)
    assert not _manifest(mark).exists() and not mark.path.exists()

    claimed = store.claim(mark, "gnome")

    assert claimed is None, f"a vanished checkpoint was claimed: {claimed}"
    assert not _manifest(mark).exists(), "the claim wrote the dropped manifest back"
    assert list(store.root.glob("*.json")) == []
    assert list(store.root.glob("*.tmp")) == []


def test_a_vanished_manifest_whose_stale_mark_already_lists_the_image_is_still_none(
    tmp_path: Path,
) -> None:
    """Wrongly collapsed: "already listed in the mark I hold" is not "listed in the store".
    The settle re-claim holds exactly such a mark -- read before the flavor's own save
    dropped the trunk -- and must learn that it claimed nothing."""
    store = _store(tmp_path)
    mark = store.save(_rootfs(tmp_path), checkpoint.INSTALL, _FP, image="kde", run_id="r1")
    _dropped_elsewhere(store, _FP)

    assert store.claim(mark, "kde") is None
    assert not _manifest(mark).exists()


def test_a_snapshot_left_without_its_manifest_is_not_claimed_and_prune_takes_it(
    tmp_path: Path,
) -> None:
    """A drop interrupted between the manifest and the snapshot (manifest first): the
    snapshot alone is an unfinished checkpoint, never one a claim may complete."""
    store = _store(tmp_path)
    mark = store.save(_rootfs(tmp_path), checkpoint.INSTALL, _FP, image="kde", run_id="r1")
    _manifest(mark).unlink()

    assert store.claim(mark, "gnome") is None
    assert not _manifest(mark).exists(), "the claim completed a half-dropped checkpoint"
    assert store.prune() == [mark.path.name]
    assert not mark.path.exists()


def test_after_a_refused_claim_prune_removes_nothing_extra(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rootfs = _rootfs(tmp_path)
    gone = store.save(rootfs, checkpoint.INSTALL, _FP, image="kde", run_id="r1")
    kept = store.save(rootfs, checkpoint.PACKAGES, _OTHER_FP, image="gnome", run_id="r2")
    _dropped_elsewhere(store, _FP)
    before = store.marks()
    assert [m.fingerprint for m in before] == [_OTHER_FP]

    store.claim(gone, "gnome")

    assert store.prune() == []
    assert store.marks() == before, "the claim left an orphan manifest prune cannot remove"
    assert _orphans(store) == []
    assert kept.path.is_dir()


# --- the callers ----------------------------------------------------------------------------


def test_a_waiter_whose_trunk_vanished_under_its_claim_installs_the_trunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gnome finds and restores the trunk kde saved; before its claim the trunk is dropped
    (another holder judged it stale). Restored-from-nothing is not a trunk: gnome installs
    the trunk itself, and no manifest without its snapshot is left in the store."""
    wired = _trunked(tmp_path, monkeypatch)
    wired.assemble(recipe=_chain("kde"))
    (trunk,) = [m for m in wired.store.marks(checkpoint.INSTALL) if _TRUNK_KEY in m.images]
    real_restore = wired.store.restore
    vanished: list[checkpoint.Mark] = []

    def restore(mark: checkpoint.Mark, rootfs: Path) -> None:
        real_restore(mark, rootfs)
        if mark.fingerprint == trunk.fingerprint and not vanished:
            # dropped between the restore and the claim
            _manifest(mark).unlink()
            checkpoint.remove_tree(mark.path, wired.store.backend)
            vanished.append(mark)

    monkeypatch.setattr(wired.store, "restore", restore)
    wired.assemble(recipe=_chain("gnome"))

    assert vanished, "the trunk was never restored: the fixture did not run"
    assert wired.ran(iso_emerge_argv(_chain("gnome"))), "the trunk was not installed"
    assert wired.step("trunk").get("built") == _TRUNK_KEY, wired.step("trunk")
    assert _orphans(wired.store) == []
    (rebuilt,) = [m for m in wired.store.marks(checkpoint.INSTALL) if _TRUNK_KEY in m.images]
    assert rebuilt.path.is_dir()


def test_the_settle_re_claim_skips_a_trunk_the_flavors_own_save_removed(
    tmp_path: Path,
) -> None:
    """The settle sequence on the store: the trunk claims read before the save, the
    flavor's INSTALL save (which drops the trunk it was the only image of), the re-claim.
    The re-claim returns None and writes nothing."""
    store = _store(tmp_path)
    rootfs = _rootfs(tmp_path)
    store.save(rootfs, checkpoint.INSTALL, _FP, image=_TRUNK_KEY, run_id="trunk")
    store.claim(store.marks(checkpoint.INSTALL)[0], _KDE)
    store.release_step(checkpoint.INSTALL, _TRUNK_KEY, keep=None)  # kde: its only image
    with store.locked():
        trunk_claims = [m for m in store.marks(checkpoint.INSTALL) if _KDE in m.images]
        assert [m.images for m in trunk_claims] == [(_KDE,)]
        store.save(rootfs, checkpoint.INSTALL, _OTHER_FP, image=_KDE, run_id="kde")
        reclaimed = [store.claim(m, _KDE) for m in trunk_claims]

    assert reclaimed == [None], f"the re-claim answered a trunk that is gone: {reclaimed}"
    assert [m.fingerprint for m in store.marks()] == [_OTHER_FP]
    assert _orphans(store) == []


def test_an_assemble_whose_settle_save_drops_its_trunk_succeeds_without_an_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same through the assembler: the trunk's own claim is released (by another
    process) before kde's INSTALL save, so that save drops the trunk; the settle re-claim
    skips it and the assemble ends with every manifest backed by its snapshot."""
    wired = _trunked(tmp_path, monkeypatch)
    store = wired.store
    real_save = store.save
    released: list[str] = []

    def save(rootfs: Path, step: str, fp: str, **kw: Any) -> checkpoint.Mark:
        if step == checkpoint.INSTALL and kw.get("image") == _KDE:
            released.extend(m.fingerprint for m in store.marks() if _TRUNK_KEY in m.images)
            store.release_step(checkpoint.INSTALL, _TRUNK_KEY)
        return real_save(rootfs, step, fp, **kw)

    monkeypatch.setattr(store, "save", save)
    wired.assemble(recipe=_chain("kde"))

    assert released, "kde's INSTALL save never ran: the fixture did not run"
    assert _orphans(store) == []
    assert all(m.fingerprint not in released for m in store.marks())


# --- then the benign: a live manifest is claimed as before ---------------------------------


def test_a_manifest_dropped_and_saved_again_is_live_and_claimed(tmp_path: Path) -> None:
    """Wrongly split: the same checkpoint name saved anew by another image after the drop
    is a live manifest -- the claim adds to it, it never answers None."""
    store = _store(tmp_path)
    rootfs = _rootfs(tmp_path)
    old = store.save(rootfs, checkpoint.INSTALL, _FP, image="kde", run_id="r1")
    _dropped_elsewhere(store, _FP)
    store.save(rootfs, checkpoint.INSTALL, _FP, image="wm", run_id="r2")

    claimed = store.claim(old, "gnome")

    assert claimed is not None
    assert claimed.images == ("wm", "gnome")
    assert claimed.run_id == "r2"
    assert store.find(checkpoint.INSTALL, _FP) == claimed


def test_a_live_manifest_is_claimed_keeping_an_image_another_process_added(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stale = store.save(_rootfs(tmp_path), checkpoint.INSTALL, _FP, image="kde", run_id="r1")
    store.claim(stale, "wm")  # another process, after `stale` was read

    claimed = store.claim(stale, "gnome")

    assert claimed is not None
    assert claimed.images == ("kde", "wm", "gnome")
    assert store.find(checkpoint.INSTALL, _FP) == claimed
    assert store.claim(claimed, "gnome") == claimed  # already listed: unchanged
