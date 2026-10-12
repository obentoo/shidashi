"""Tests of the checkpoint store under its lock (story 006, task 2.1).

Contract (design.md): every read-modify-write of a ``<arch>-<init>`` store --
``save``, ``claim``, ``release_step``, ``release``, ``retain``, ``drop``, ``prune`` --
runs under the store lock ``root/.lock``; ``Store.locked()`` takes it for a caller
that must make several calls atomically, and is re-entrant within one process -- by
the lock registry keyed by the resolved path (C2), so two ``Store`` objects for one
root nest too; a wait is reported on stderr ("waiting for checkpoint store <name>
(pid N)", R3.9);
``save`` snapshots and writes the manifest inside one critical section, so a prune
never sees a snapshot without its manifest; the manifest's temp file is
``<name>.json.<pid>.tmp``, never a name shared between processes.

Concurrency is between REAL processes (``python -c``) over the directory-copy
backend (tests/_fake_btrfs.py), without root and without btrfs.
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import shidashi
from shidashi import checkpoint
from shidashi.checkpoint import INSTALL, PACKAGES, PARTIAL, Store
from tests._fake_btrfs import FakeBackend

#: The repository root: the children import ``tests._fake_btrfs`` from it.
_ROOT = Path(shidashi.__file__).resolve().parents[1]

_HOLDER = """
import sys
from pathlib import Path
from shidashi import locks
with locks.hold(Path(sys.argv[1]), what="test holder"):
    print("held", flush=True)
    sys.stdin.readline()
"""


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(_ROOT), env.get("PYTHONPATH")) if p)
    return env


@contextlib.contextmanager
def _held_elsewhere(path: Path) -> Iterator[subprocess.Popen[str]]:
    """``path`` flock-held by another process for the block; yields that process."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
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


def _rootfs(tmp_path: Path, marker: str = "r") -> Path:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir(exist_ok=True)
    (rootfs / "marker").write_text(marker)
    return rootfs


def _store(tmp_path: Path, name: str = "v3-systemd") -> Store:
    root = tmp_path / "checkpoints" / name
    root.mkdir(parents=True)
    return Store(root, FakeBackend())


def _taken_elsewhere(path: Path) -> bool:
    """Whether another process can take ``path`` right now (wait=False)."""
    probe = (
        "import sys\nfrom pathlib import Path\nfrom shidashi import locks\n"
        "try:\n"
        "    with locks.hold(Path(sys.argv[1]), what='probe', wait=False):\n"
        "        pass\n"
        "except locks.LockBusy:\n"
        "    sys.exit(3)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe, str(path)], capture_output=True, text=True, env=_child_env()
    )
    assert done.returncode in (0, 3), done.stderr
    return done.returncode == 0


# --- Store.locked() ---------------------------------------------------------------------


def test_locked_holds_the_store_lock_beside_the_checkpoints(tmp_path: Path) -> None:
    from shidashi import locks

    store = _store(tmp_path)
    with store.locked():
        assert locks.holder_pid(store.root / ".lock") == os.getpid()
        assert not _taken_elsewhere(store.root / ".lock")
    assert _taken_elsewhere(store.root / ".lock")


def test_locked_is_reentrant_within_one_process(tmp_path: Path) -> None:
    """The assembler holds the lock across find -> restore -> claim; claim (and
    save, which claims) take it again inside: that must not deadlock."""
    store = _store(tmp_path)
    rootfs = _rootfs(tmp_path)

    def nested() -> checkpoint.Mark:
        with store.locked(), store.locked():
            store.save(rootfs, INSTALL, "fp", image="minimal", run_id=None)
            mark = store.find(INSTALL, "fp")
            assert mark is not None
            return store.claim(mark, "worker")

    claimed = _Background(nested)
    claimed.start()
    mark = claimed.finish(timeout=10)
    assert mark.images == ("minimal", "worker")
    assert _taken_elsewhere(store.root / ".lock")  # released once the outer block ends


# --- every mutation waits for the store lock (R3.1) --------------------------------------


def _prepared(tmp_path: Path) -> tuple[Store, Path, checkpoint.Mark]:
    store = _store(tmp_path)
    rootfs = _rootfs(tmp_path)
    mark = store.save(rootfs, INSTALL, "old", image="minimal", run_id=None)
    store.save(rootfs, PACKAGES, "p", image="minimal", run_id=None)
    return store, rootfs, mark


_MUTATIONS: dict[str, Callable[[Store, Path, checkpoint.Mark], object]] = {
    "save": lambda s, r, _m: s.save(r, INSTALL, "new", image="minimal", run_id=None),
    "claim": lambda s, _r, m: s.claim(m, "worker"),
    "release_step": lambda s, _r, _m: s.release_step(INSTALL, "minimal"),
    "release": lambda s, _r, _m: s.release("minimal"),
    "retain": lambda s, _r, _m: s.retain("minimal", ["p"]),
    "drop": lambda s, _r, m: s.drop(m),
    "prune": lambda s, _r, _m: s.prune(),
}


@pytest.mark.parametrize("method", sorted(_MUTATIONS))
def test_every_mutation_of_the_store_waits_for_its_lock(tmp_path: Path, method: str) -> None:
    store, rootfs, mark = _prepared(tmp_path)
    before = sorted(p.name for p in store.root.iterdir() if p.name != ".lock")
    with _held_elsewhere(store.root / ".lock"):
        call = _Background(lambda: _MUTATIONS[method](store, rootfs, mark))
        call.start()
        time.sleep(0.5)  # an unlocked mutation of the fake store takes milliseconds
        assert call.is_alive(), f"Store.{method} ran while another process held the store lock"
        after = sorted(p.name for p in store.root.iterdir() if p.name != ".lock")
        assert after == before, f"Store.{method} changed the store before taking its lock"
    call.finish()


def test_two_stores_do_not_wait_for_each_other(tmp_path: Path) -> None:
    """Wrongly collapsed: the v3-openrc store's save must not wait for a holder of
    the v3-systemd store (one lock per store, not one per scratch)."""
    systemd = _store(tmp_path, "v3-systemd")
    openrc = _store(tmp_path, "v3-openrc")
    rootfs = _rootfs(tmp_path)
    with _held_elsewhere(systemd.root / ".lock"):
        call = _Background(lambda: openrc.save(rootfs, INSTALL, "fp", image="minimal", run_id=None))
        call.start()
        mark = call.finish(timeout=5)
    assert mark.images == ("minimal",)


# --- no image lost across processes (R3.1) ------------------------------------------------

#: A writer: claims ``argv[3]`` images ``<argv[2]>-<k>`` on checkpoint INSTALL/fp of the
#: store at ``argv[1]``, half through ``save`` (find + claim inside) and half through
#: ``locked()`` around find -> claim (the assembler's pattern). Waits for "go" on stdin.
_WRITER = """
import sys
from pathlib import Path
from shidashi.checkpoint import INSTALL, Store
from tests._fake_btrfs import FakeBackend
root, prefix, count, rootfs = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
store = Store(root, FakeBackend())
print("ready", flush=True)
sys.stdin.readline()
for k in range(count):
    image = f"{prefix}-{k}"
    if k % 2:
        store.save(rootfs, INSTALL, "fp", image=image, run_id=None)
    else:
        with store.locked():
            mark = store.find(INSTALL, "fp")
            assert mark is not None
            store.claim(mark, image)
print("done", flush=True)
"""


def test_concurrent_claims_from_several_processes_lose_no_image(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rootfs = _rootfs(tmp_path)
    store.save(rootfs, INSTALL, "fp", image="seed", run_id=None)
    writers = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(store.root), f"w{n}", "20", str(rootfs)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_child_env(),
        )
        for n in range(4)
    ]
    try:
        for proc in writers:
            assert proc.stdout is not None
            assert proc.stdout.readline() == "ready\n"
        for proc in writers:  # as close to together as the parent can make it
            assert proc.stdin is not None
            proc.stdin.write("go\n")
            proc.stdin.flush()
        for proc in writers:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
    finally:
        for proc in writers:
            if proc.poll() is None:
                proc.kill()
    mark = store.find(INSTALL, "fp")
    assert mark is not None
    expected = {"seed", *(f"w{n}-{k}" for n in range(4) for k in range(20))}
    assert set(mark.images) == expected, f"lost: {sorted(expected - set(mark.images))}"
    assert len(mark.images) == len(expected)  # and none twice


# --- prune never sees a half-saved checkpoint (R3.2) -----------------------------------------

#: A saver whose snapshot stops between the snapshot and the manifest until stdin says go.
_SLOW_SAVER = """
import sys
from pathlib import Path
from shidashi.checkpoint import INSTALL, Store
from tests._fake_btrfs import FakeBackend

class Slow(FakeBackend):
    def snapshot(self, source, dest, *, readonly):
        super().snapshot(source, dest, readonly=readonly)
        if readonly:
            print("snapshotted", flush=True)
            sys.stdin.readline()

store = Store(Path(sys.argv[1]), Slow())
store.save(Path(sys.argv[2]), INSTALL, "fp", image="kde", run_id="r1")
print("saved", flush=True)
"""


def test_a_prune_never_removes_a_checkpoint_being_saved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rootfs = _rootfs(tmp_path, "installed")
    saver = subprocess.Popen(
        [sys.executable, "-c", _SLOW_SAVER, str(store.root), str(rootfs)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
    )
    assert saver.stdin is not None and saver.stdout is not None
    try:
        line = saver.stdout.readline()
        assert line == "snapshotted\n", saver.communicate(timeout=10)[1]
        # the snapshot exists, its manifest does not yet: a prune now would delete it
        pruning = _Background(store.prune)
        pruning.start()
        time.sleep(0.5)
        snapshots = [p for p in store.root.iterdir() if p.is_dir()]
        assert snapshots, "a concurrent prune removed the snapshot being saved"
        saver.stdin.write("go\n")
        saver.stdin.flush()
        out, err = saver.communicate(timeout=20)
        assert saver.returncode == 0 and "saved" in out, err
        removed = pruning.finish()
    finally:
        if saver.poll() is None:
            saver.kill()
    assert removed == []
    mark = store.find(INSTALL, "fp")
    assert mark is not None and mark.images == ("kde",)
    assert (mark.path / "marker").read_text() == "installed"


# --- the manifest's temp file is per process --------------------------------------------------


def _manifest_name(fp: str, step: str = INSTALL) -> str:
    return f"{step}-{fp[:24]}.json"


def test_another_writers_temp_file_is_neither_used_nor_clobbered(tmp_path: Path) -> None:
    """Wrongly collapsed: two processes writing one manifest through the same
    ``.json.tmp`` interleave on it. A temp file another process is still writing
    under the shared name stays untouched, and the manifest is still right."""
    store = _store(tmp_path)
    foreign = store.root / (_manifest_name("fp") + ".tmp")
    foreign.write_text("half of another process's manifest")
    store.save(_rootfs(tmp_path), INSTALL, "fp", image="minimal", run_id="r1")
    assert foreign.read_text() == "half of another process's manifest"
    raw = json.loads((store.root / _manifest_name("fp")).read_text())
    assert raw["images"] == ["minimal"] and raw["run_id"] == "r1"


def test_the_manifest_is_written_through_a_temp_file_named_by_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        replaced.append((Path(src).name, Path(dst).name))
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    store = _store(tmp_path)
    store.save(_rootfs(tmp_path), PARTIAL, "fp", image="kde", run_id=None)
    manifest = _manifest_name("fp", PARTIAL)
    assert (f"{manifest}.{os.getpid()}.tmp", manifest) in replaced
    leftovers = [p.name for p in store.root.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_prune_leaves_the_lock_files_beside_the_checkpoints_alone(tmp_path: Path) -> None:
    """The store lock and the trunk locks live in the store's directory, beside the
    snapshots. A prune that removed one while it is held would let a second holder
    in on a new file: two "exclusive" holders at once."""
    store = _store(tmp_path)
    store.save(_rootfs(tmp_path), INSTALL, "fp", image="minimal", run_id=None)
    trunk_lock = store.root / f"trunk-{'a' * 24}.lock"
    with _held_elsewhere(trunk_lock):
        removed = store.prune()
        assert removed == []
        assert trunk_lock.exists() and (store.root / ".lock").exists()
        assert not _taken_elsewhere(trunk_lock)  # still the same, still held


def test_two_store_objects_for_one_root_nest_without_deadlock(tmp_path: Path) -> None:
    """C2: the trunk step's branch.store and the seed step's store are two objects
    for one root; re-entrancy cannot be an instance counter."""
    first = _store(tmp_path)
    second = Store(first.root, first.backend)
    rootfs = _rootfs(tmp_path)

    def nested() -> None:
        with first.locked():
            second.save(rootfs, INSTALL, "fp", image="kde", run_id=None)

    done = _Background(nested)
    done.start()
    done.finish(timeout=10)
    assert first.find(INSTALL, "fp") is not None
    assert _taken_elsewhere(first.root / ".lock")


def test_a_wait_for_the_store_says_which_store_and_pid(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.9 on the store lock, with no on_wait passed by the caller."""
    store = _store(tmp_path)
    rootfs = _rootfs(tmp_path)
    store.save(rootfs, INSTALL, "fp", image="minimal", run_id=None)
    mark = store.find(INSTALL, "fp")
    assert mark is not None
    with _held_elsewhere(store.root / ".lock") as holder:
        waiter = _Background(lambda: store.claim(mark, "worker"))
        waiter.start()
        time.sleep(0.5)
    waiter.finish()
    err = capfd.readouterr().err
    assert err.count(f"waiting for checkpoint store {store.root.name}") == 1
    assert f"(pid {holder.pid})" in err
