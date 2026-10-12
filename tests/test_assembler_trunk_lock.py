"""The trunk installed once for concurrent flavors (story 006, task 3.1).

Contract (design.md): the trunk is looked up and, when missing, built under the
trunk lock ``<store root>/trunk-<fp[:24]>.lock`` (``fp``: the trunk's install
fingerprint). The first assemble holds it while it installs and saves the trunk;
a second one of a flavor with the same trunk blocks on it -- saying which lock and
which pid it waits for (R3.9) -- then looks the checkpoint up AGAIN, finds the one
the first saved and restores it (R3.3). A restored trunk releases the lock at once;
a failed trunk install releases it too, so no waiter stays blocked -- and that waiter,
finding no checkpoint after the wait, installs the trunk itself. Lock order (C2,
R3.10): image -> trunk -> store; a waiter for the trunk lock holds NO store lock, so
it can never deadlock with the holder's ``store.save``. A failed assemble keeps its
checkpoints and its scratch rootfs (R2.3). The trunk lock's ExitStack is closed in
``Assembler.assemble``'s ``finally`` too, so a failure between the lookup and the
``trunk`` step never leaves the lock held -- the in-process ``--serial`` loop would
otherwise keep it for the rest of the build. A restored or built trunk is claimed by the
flavor too, and the flavor's ``retain`` keeps it, so the trunk manifest lists every
flavor that grew from it.

v3 (story 019): the lock is held across ``configure`` -- the image's plan judged on its
``<rootfs>.judge`` copy -- and the ``trunk`` step's judgement of the trunk's own plan,
until the trunk checkpoint is saved. A stale refusal of either plan raises
``AssemblerError`` and releases the lock without saving a trunk checkpoint. A waiter that
restores the trunk runs no before-trunk judgement (no ``.judge`` copy) and judges its own
plan in the ``install`` step.

The assemble is the ``_trunked`` one of tests/test_assembler.py (fake container,
fake btrfs store, the kde/gnome chain over a desktop trunk). The other holder of a
lock is a real second process. The story 019 cases answer each ``--pretend`` by the
configuration of the rootfs it runs in (its world file), as
tests/test_assembler_image_before_trunk.py does, with the same vte-flagging judge.
"""

import contextlib
import fcntl
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import shidashi.assembler as asm
from shidashi import binpkgs, checkpoint
from shidashi.assembler import AssemblerError, iso_emerge_argv
from shidashi.container import CommandResult
from tests.test_assembler import (  # noqa: F401 -- the autouse fixtures _Wired relies on
    _chain,
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _trunked,
    _Wired,
)

_TRUNK_KEY = "znver5-desktop-systemd"

_HOLDER = """
import sys
from pathlib import Path
from shidashi import locks
with locks.hold(Path(sys.argv[1]), what="trunk desktop"):
    print("held", flush=True)
    sys.stdin.readline()
"""


@contextlib.contextmanager
def _held_elsewhere(path: Path) -> Iterator[subprocess.Popen[str]]:
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

    def finish(self, timeout: float = 30) -> Any:
        self.join(timeout)
        assert not self.is_alive(), "the assemble is still blocked"
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


def _trunk_held(wired: _Wired) -> bool:
    return any(_locked(p) for p in wired.store.root.glob("trunk-*.lock"))


def _trunk_mark(wired: _Wired) -> checkpoint.Mark:
    (mark,) = [m for m in wired.store.marks(checkpoint.INSTALL) if _TRUNK_KEY in m.images]
    return mark


def _label(argv: list[str]) -> str:
    if argv[-1:] == ["--pretend"]:
        return "pretend"
    if "--update" in argv:
        return "branch"
    if "--emptytree" in argv:
        return "install"
    return argv[0]


def _probe(wired: _Wired, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object, bool]]:
    """(event, detail, trunk lock held) at each store lookup/save/restore and command."""
    seen: list[tuple[str, object, bool]] = []
    store = wired.store
    real_find, real_save, real_restore = store.find, store.save, store.restore

    def find(step: str, expected: str) -> object:
        seen.append(("find", (step, expected), _trunk_held(wired)))
        return real_find(step, expected)

    def save(rootfs: Path, step: str, fp: str, **kw: Any) -> object:
        seen.append(("save", kw.get("image"), _trunk_held(wired)))
        return real_save(rootfs, step, fp, **kw)

    def restore(mark: checkpoint.Mark, rootfs: Path) -> None:
        seen.append(("restore", mark.images, _trunk_held(wired)))
        real_restore(mark, rootfs)

    monkeypatch.setattr(store, "find", find)
    monkeypatch.setattr(store, "save", save)
    monkeypatch.setattr(store, "restore", restore)
    wired_container = asm.Container

    class _Probing(wired_container):  # type: ignore[misc,valid-type]
        def run(self, argv: list[str], **kw: object) -> object:
            seen.append(("run", _label(argv), _trunk_held(wired)))
            return super().run(argv, **kw)

    monkeypatch.setattr(asm, "Container", _Probing)
    return seen


# --- the first flavor: the lock from the lookup until the trunk is saved -------------------


def test_the_trunk_lock_is_held_from_the_lookup_until_the_trunk_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _trunked(tmp_path, monkeypatch)
    seen = _probe(wired, monkeypatch)
    wired.assemble(recipe=_chain("kde"))
    trunk = _trunk_mark(wired)

    names = sorted(p.name for p in wired.store.root.glob("trunk-*.lock"))
    assert names == [f"trunk-{trunk.fingerprint[:24]}.lock"]
    lookups = [
        held
        for event, detail, held in seen
        if event == "find"
        and detail
        == (
            checkpoint.INSTALL,
            trunk.fingerprint,
        )
    ]
    assert lookups and all(lookups), f"the trunk was looked up outside its lock: {seen}"
    assert ("run", "install", True) in seen, seen  # the trunk's install, under the lock
    assert ("save", _TRUNK_KEY, True) in seen, seen  # and its checkpoint
    branch = [held for event, detail, held in seen if event == "run" and detail == "branch"]
    assert branch == [False], f"the branch ran under the trunk lock: {seen}"
    assert not _trunk_held(wired)


def test_a_restored_trunk_releases_its_lock_before_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _trunked(tmp_path, monkeypatch)
    wired.assemble(recipe=_chain("kde"))
    seen = _probe(wired, monkeypatch)
    wired.assemble(recipe=_chain("gnome"))
    assert wired.step("trunk")["restored"] == _TRUNK_KEY
    restores = [held for event, detail, held in seen if event == "restore"]
    assert restores == [True], seen  # the trunk's restore, under its lock
    assert ("run", "install", True) not in seen and ("run", "install", False) not in seen
    branch = [held for event, detail, held in seen if event == "run" and detail == "branch"]
    assert branch == [False], seen
    assert not _trunk_held(wired)


def test_a_failed_trunk_install_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _trunked(tmp_path, monkeypatch)
    wired.fail_install = True
    with pytest.raises(subprocess.CalledProcessError):
        wired.assemble(recipe=_chain("kde"))
    locks_left = list(wired.store.root.glob("trunk-*.lock"))
    assert locks_left, "the trunk install never took the trunk lock"
    assert not any(_locked(p) for p in locks_left), "a waiter would stay blocked forever"
    # R2.3: the failed image keeps its scratch rootfs for debugging
    assert (tmp_path / "scratch" / "assemble" / "znver5-kde-systemd").is_dir()


# --- the second flavor: waits, then restores (R3.3, R3.9) ------------------------------------


def test_a_second_flavor_waits_for_the_trunk_then_restores_it_instead_of_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """kde's assemble (another process) holds the trunk lock while it installs the
    trunk; gnome's starts meanwhile, finds no trunk checkpoint and must wait. When
    kde saves the trunk and lets go, gnome restores it: one trunk install, not two."""
    wired = _trunked(tmp_path, monkeypatch)
    wired.assemble(recipe=_chain("kde"))  # only to learn the trunk's fingerprint
    trunk = _trunk_mark(wired)
    snapshot = trunk.path
    manifest = trunk.path.with_name(trunk.path.name + ".json")
    aside = tmp_path / "aside"
    aside.mkdir()
    # as if kde's assemble were still installing it: no trunk checkpoint yet
    shutil.move(snapshot, aside / snapshot.name)
    shutil.move(manifest, aside / manifest.name)
    assert wired.store.find(checkpoint.INSTALL, trunk.fingerprint) is None
    capfd.readouterr()

    lock = wired.store.root / f"trunk-{trunk.fingerprint[:24]}.lock"
    with _held_elsewhere(lock) as kde:
        gnome = _Background(lambda: wired.assemble(recipe=_chain("gnome")))
        gnome.start()
        time.sleep(1.0)
        assert gnome.is_alive(), "gnome did not wait for the trunk kde is installing"
        assert not wired.ran(iso_emerge_argv(_chain("gnome"))), "gnome installed the trunk too"
        # kde saves the trunk checkpoint, then lets go of the lock
        shutil.move(aside / snapshot.name, snapshot)
        shutil.move(aside / manifest.name, manifest)
    gnome.finish()

    assert not wired.ran(iso_emerge_argv(_chain("gnome")))  # the trunk was not installed again
    assert wired.extracted == 1  # no stage3 for gnome
    assert wired.step("trunk")["restored"] == _TRUNK_KEY
    said = capfd.readouterr()
    line = f"waiting for trunk desktop (pid {kde.pid})"
    assert line in said.err  # R3.9: on stderr, the child's log
    assert "waiting for trunk" not in said.out  # never on stdout


def test_a_trunk_with_another_fingerprint_does_not_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly collapsed: the lock is the trunk's CONTENT (its fingerprint), not its
    stage name -- a holder of another desktop trunk (a changed configuration) must
    not block this one."""
    wired = _trunked(tmp_path, monkeypatch)
    other = wired.store.root / f"trunk-{'0' * 24}.lock"
    wired.store.root.mkdir(parents=True, exist_ok=True)
    with _held_elsewhere(other):
        kde = _Background(lambda: wired.assemble(recipe=_chain("kde")))
        kde.start()
        kde.finish(timeout=20)
    assert wired.step("trunk")["built"] == _TRUNK_KEY


def test_a_trunk_waiter_holds_no_store_lock_while_it_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2 lock order: the holder saves the trunk under the store lock; a waiter that
    held the store lock while waiting for the trunk lock would deadlock with it."""
    wired = _trunked(tmp_path, monkeypatch)
    wired.assemble(recipe=_chain("kde"))
    trunk = _trunk_mark(wired)
    aside = tmp_path / "aside"
    aside.mkdir()
    manifest = trunk.path.with_name(trunk.path.name + ".json")
    shutil.move(trunk.path, aside / trunk.path.name)
    shutil.move(manifest, aside / manifest.name)

    lock = wired.store.root / f"trunk-{trunk.fingerprint[:24]}.lock"
    with _held_elsewhere(lock):
        gnome = _Background(lambda: wired.assemble(recipe=_chain("gnome")))
        gnome.start()
        time.sleep(1.0)
        assert gnome.is_alive(), "gnome did not wait for the trunk"
        assert not _locked(wired.store.root / ".lock"), "waiting for the trunk under the store lock"
        shutil.move(aside / trunk.path.name, trunk.path)
        shutil.move(aside / manifest.name, manifest)
    gnome.finish()


def test_when_the_trunk_holder_fails_the_waiter_installs_the_trunk_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder lets go without saving (its install failed): the waiter looks the
    checkpoint up again, finds nothing, and installs the trunk -- never a hang, never a
    restore of nothing."""
    wired = _trunked(tmp_path, monkeypatch)
    wired.assemble(recipe=_chain("kde"))
    trunk = _trunk_mark(wired)
    wired.store.drop(trunk)  # the holder never saved it
    assert wired.store.find(checkpoint.INSTALL, trunk.fingerprint) is None

    lock = wired.store.root / f"trunk-{trunk.fingerprint[:24]}.lock"
    with _held_elsewhere(lock):
        gnome = _Background(lambda: wired.assemble(recipe=_chain("gnome")))
        gnome.start()
        time.sleep(1.0)
        assert gnome.is_alive()
    gnome.finish()
    assert wired.ran(iso_emerge_argv(_chain("gnome")))  # the trunk, installed by the waiter
    assert wired.step("trunk")["built"] == _TRUNK_KEY


def test_a_failure_before_the_trunk_step_releases_the_trunk_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage3 extraction fails after _find_trunk took the lock for a missing trunk:
    the lock must be free when assemble returns -- this process (the --serial loop) goes
    on to the next image, and another process must be able to take it."""
    wired = _trunked(tmp_path, monkeypatch)

    def broken_extract(tarball: Path, rootfs: Path) -> None:
        raise OSError("stage3 extraction failed")

    monkeypatch.setattr(asm, "extract_stage3", broken_extract)
    with pytest.raises(OSError, match="stage3 extraction failed"):
        wired.assemble(recipe=_chain("kde"))
    locks_left = list(wired.store.root.glob("trunk-*.lock"))
    assert locks_left, "the lookup never took the trunk lock"
    assert not _trunk_held(wired), "the trunk lock outlived the failed assemble"


def test_every_flavor_grown_from_the_trunk_is_in_its_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _trunked(tmp_path, monkeypatch)
    wired.assemble(recipe=_chain("kde"))
    wired.assemble(recipe=_chain("gnome"))
    trunk = _trunk_mark(wired)
    assert set(trunk.images) == {_TRUNK_KEY, "znver5-kde-systemd", "znver5-gnome-systemd"}
    # a later run of kde keeps its claim on the trunk (retain keeps the trunk's fingerprint)
    wired.assemble(recipe=_chain("kde"))
    assert "znver5-kde-systemd" in _trunk_mark(wired).images


# --- v3: story 019's judgement under the trunk lock -------------------------------------

#: the trunk's world, as ``_trunked`` composes it (``world_atoms``)
_TRUNK_WORLD = "app-misc/trunk\n"
_VTE = binpkgs.Instance(
    cpv="x11-libs/vte-0.84.1", build_id=1, path="x11-libs/vte/vte-0.84.1-1.gpkg.tar"
)
#: clean plans carry the tokens the fake install prints, so a restored trunk stays valid
_CLEAN_TRUNK_PLAN = (
    "[binary   N    ] x/a-1-1::gentoo  0 KiB\n[binary   N    ] x/b-2:0::gentoo  0 KiB\n"
)
_CLEAN_IMAGE_PLAN = (
    "[binary   N    ] x/a-1-1::gentoo  0 KiB\n[binary   N    ] x/c-3-1::gentoo  0 KiB\n"
)
_STALE = "[binary   N    ] x11-libs/vte-0.84.1-1::gentoo  0 KiB\n"


def _vte_judge(
    container: object, instances: object, plan_text: str
) -> tuple[list[binpkgs.Stale], list[binpkgs.SonameStale]]:
    if "x11-libs/vte-0.84.1" not in plan_text:
        return [], []
    needs = binpkgs.Soname(category="x86_64", name="libsimdutf.so.34")
    return [], [binpkgs.SonameStale(instance=_VTE, needs=needs, offered=("libsimdutf.so.36",))]


def _resolving(
    wired: _Wired,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trunk_plan: str = _CLEAN_TRUNK_PLAN,
    image_plan: str = _CLEAN_IMAGE_PLAN,
) -> list[tuple[str, bool]]:
    """Every ``--pretend`` answered by its rootfs's configuration; returns, per pretend,
    (the rootfs's name, whether the trunk lock was held at that moment)."""
    pretends: list[tuple[str, bool]] = []
    base: type = vars(asm)["Container"]  # the _Wired fake, patched in by _trunked

    class _Resolver(base):  # type: ignore[misc]
        def run(self, argv: list[str], **kw: object) -> object:
            if argv[:1] == ["emerge"] and "--pretend" in argv:
                rootfs = Path(self.rootfs)
                world = rootfs / "var" / "lib" / "portage" / "world"
                configured = world.read_text() if world.is_file() else ""
                pretends.append((rootfs.name, _trunk_held(wired)))
                wired.runs.append(list(argv))
                if configured == _TRUNK_WORLD:
                    return CommandResult(0, trunk_plan, "")
                return CommandResult(0, image_plan if configured else "", "")
            return super().run(argv, **kw)

    monkeypatch.setattr(asm, "Container", _Resolver)
    monkeypatch.setattr(asm, "judge_plan", _vte_judge)
    return pretends


def _trunk_marks(wired: _Wired) -> list[checkpoint.Mark]:
    return [m for m in wired.store.marks(checkpoint.INSTALL) if _TRUNK_KEY in m.images]


@pytest.mark.parametrize("stale_in", ["image", "trunk"])
def test_a_stale_refusal_under_the_trunk_lock_releases_it_and_saves_no_trunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_in: str
) -> None:
    """kde has no trunk checkpoint: it takes the trunk lock, judges the image's plan on
    its ``.judge`` copy (configure), then the trunk's own plan (trunk step) -- both under
    the lock, so a waiter never judges or installs the same trunk at the same time. A
    stale binpkg in either plan refuses the assemble: the lock goes, no trunk is saved."""
    wired = _trunked(tmp_path, monkeypatch)
    assert (wired.binhost / "Packages").is_file()  # an index: plans are judged
    plans = (
        {"image_plan": _CLEAN_IMAGE_PLAN + _STALE}
        if stale_in == "image"
        else {"trunk_plan": _CLEAN_TRUNK_PLAN + _STALE}
    )
    pretends = _resolving(wired, monkeypatch, **plans)

    with pytest.raises(AssemblerError, match="x11-libs/vte-0.84.1"):
        wired.assemble(recipe=_chain("kde"))

    assert pretends, "no plan was judged"
    if stale_in == "image":
        assert [name for name, _ in pretends] == ["znver5-kde-systemd.judge"], pretends
    else:
        assert "znver5-kde-systemd" in [name for name, _ in pretends], pretends
    outside = [name for name, held in pretends if not held]
    assert not outside, f"plans judged outside the trunk lock: {outside} ({pretends})"
    assert list(wired.store.root.glob("trunk-*.lock")), "the lookup never took the trunk lock"
    assert not _trunk_held(wired), "the refusal left the trunk lock held"
    assert _trunk_marks(wired) == [], "a refused trunk was saved"
    assert _installs(wired.runs) == []  # nothing was installed


def test_a_clean_judgement_keeps_the_trunk_lock_until_the_trunk_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile half of the refusal: a clean judgement must not release the lock early --
    both pretends and the install run under it, and the trunk is saved."""
    wired = _trunked(tmp_path, monkeypatch)
    pretends = _resolving(wired, monkeypatch)
    seen = _probe(wired, monkeypatch)
    wired.assemble(recipe=_chain("kde"))
    before_install = pretends[:2]
    assert [name for name, _ in before_install] == [
        "znver5-kde-systemd.judge",
        "znver5-kde-systemd",
    ], pretends
    assert all(held for _, held in before_install), pretends
    assert ("save", _TRUNK_KEY, True) in seen, seen
    assert len(_trunk_marks(wired)) == 1
    assert not _trunk_held(wired)


def test_a_waiter_that_restores_the_trunk_judges_its_own_plan_in_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gnome waits while kde (another process) builds the trunk, then restores it: it
    makes no ``.judge`` copy (that judgement is only for an image whose trunk is built in
    the same run) and judges its own plan once, in the ``install`` step, in its rootfs."""
    wired = _trunked(tmp_path, monkeypatch)
    wired.assemble(recipe=_chain("kde"))  # only to learn the trunk's fingerprint
    trunk = _trunk_mark(wired)
    snapshot = trunk.path
    manifest = trunk.path.with_name(trunk.path.name + ".json")
    aside = tmp_path / "aside"
    aside.mkdir()
    shutil.move(snapshot, aside / snapshot.name)
    shutil.move(manifest, aside / manifest.name)
    pretends = _resolving(wired, monkeypatch)

    lock = wired.store.root / f"trunk-{trunk.fingerprint[:24]}.lock"
    with _held_elsewhere(lock):
        gnome = _Background(lambda: wired.assemble(recipe=_chain("gnome")))
        gnome.start()
        time.sleep(1.0)
        assert gnome.is_alive(), "gnome did not wait for the trunk kde is building"
        assert pretends == [], f"gnome judged a plan before the trunk was there: {pretends}"
        shutil.move(aside / snapshot.name, snapshot)
        shutil.move(aside / manifest.name, manifest)
    gnome.finish()

    assert wired.step("trunk")["restored"] == _TRUNK_KEY
    assert not any(name.endswith(".judge") for name, _ in pretends), pretends
    # the restored trunk's validity check and the install's judgement, both in gnome's rootfs
    assert pretends and {name for name, _ in pretends} == {"znver5-gnome-systemd"}, pretends
    assert wired.step("install")["stale_check"] == "judged"
    assert not [a for a in _installs(wired.runs) if "--emptytree" in a], "the trunk reinstalled"


def _installs(runs: list[list[str]]) -> list[list[str]]:
    return [a for a in runs if a[:1] == ["emerge"] and "--pretend" not in a]
