"""The toolbox rootfs lock across ``binhost gc`` and ``binhost publish`` (story 029,
task 7.1; R3.13).

Through the real CLI over the labs of tests/test_binhost_cli_gc.py and
tests/test_binhost_cli_publish.py. ``toolbox.using`` is replaced by a ledger that stands for
the shared rootfs lock: it records when the hold is taken and released (with the pool as it
stood then) and how many holds are open each time the fake ``emaint`` / ``gpkg-sign`` runs.
Only observable behaviour is asserted: that the lock is held while the tools run, taken
before the first move, and released on EVERY way out -- never how the command wires it.

The release is recorded by an explicit ``__exit__``, not by a generator's ``finally``: a lock
the command forgot to release would otherwise be "released" when the garbage collector
finalizes the abandoned context manager, and the leak would stay green.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from shidashi import config, ownership, toolbox
from tests.test_binhost_cli_gc import (
    ALL,
    EVERY,
    PLANNED,
    FakeTools,
    install_fake_toolbox,
    path_of,
    pool_files,
)
from tests.test_binhost_cli_show import (
    FFMPEG,
    GEN,
    OTHER_GEN,
    Lab,
    binhost,
    make_lab,
    per_init_generation,
)
from tests.test_binhost_refs import entry
from tests.test_publish_pool import ENTRIES, FPR, extra_socket, publish_lab

# --- the ledger ----------------------------------------------------------------------------


@dataclass
class Ledger:
    """What happened to the lock, in order. ``events`` holds ``enter``, ``exit`` and
    ``run:<tool>``; ``open`` is the number of holds open now; ``seen`` the number open at
    each tool run; ``pool_at`` the build pool's files at each ``enter`` / ``exit``."""

    pool: Path
    events: list[str] = field(default_factory=list)
    entered: list[tuple[Path, Path]] = field(default_factory=list)
    seen: list[int] = field(default_factory=list)
    pool_at: dict[str, set[str]] = field(default_factory=dict)
    open: int = 0
    fail_to_enter: Exception | None = None

    def runs(self) -> list[str]:
        return [e for e in self.events if e.startswith("run:")]


class _Hold:
    """One ``toolbox.using(...)`` call: a context manager the command must close."""

    def __init__(self, ledger: Ledger, tarball: Path, rootfs: Path) -> None:
        self.ledger, self.tarball, self.rootfs = ledger, tarball, rootfs

    def __enter__(self) -> bool:
        ledger = self.ledger
        if not Path(self.tarball).is_file():  # as toolbox._require_tarball
            raise toolbox.ToolboxError(f"no toolbox at {self.tarball}")
        if ledger.fail_to_enter is not None:
            raise ledger.fail_to_enter
        ledger.events.append("enter")
        ledger.entered.append((Path(self.tarball), Path(self.rootfs)))
        ledger.pool_at["enter"] = _files(ledger.pool)
        ledger.open += 1
        return False

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.ledger.events.append("exit")
        self.ledger.pool_at["exit"] = _files(self.ledger.pool)
        self.ledger.open -= 1


def _files(pool: Path) -> set[str]:
    return pool_files(pool) if pool.is_dir() else set()


def install_ledger(monkeypatch: pytest.MonkeyPatch, pool: Path) -> Ledger:
    """After ``install_fake_toolbox``: ``using`` becomes the ledger, and every tool run of
    the fake toolbox is noted with the holds open at that moment."""
    ledger = Ledger(pool)

    def using(tar: Path, rootfs: Path, *_a: Any, **_k: Any) -> _Hold:
        return _Hold(ledger, tar, rootfs)

    def ran(argv: list[str]) -> None:
        for tool in ("emaint", "gpkg-sign"):
            if tool in argv:
                ledger.events.append(f"run:{tool}")
                ledger.seen.append(ledger.open)
                return

    monkeypatch.setattr(toolbox, "using", using)
    FakeTools.on_run.append(ran)
    return ledger


def rootfs_of(init: str) -> Path:
    """Where the command keeps the unpacked toolbox of v3 x ``init`` (the assembler's)."""
    return config.scratch_dir() / "toolbox" / f"v3-{init}"


def assert_held_once_across_the_tools(ledger: Ledger, init: str = "systemd") -> None:
    """One hold, on the rootfs the tools ran in, open at every tool run, then released."""
    assert ledger.events and ledger.events[0] == "enter", ledger.events
    assert ledger.events[-1] == "exit", ledger.events
    assert ledger.events.count("enter") == ledger.events.count("exit") == 1, ledger.events
    assert ledger.runs(), "no tool ran in the toolbox"
    assert ledger.seen == [1] * len(ledger.seen), ledger.events  # never 0 (unheld), never 2
    assert ledger.open == 0
    assert ledger.entered == [(FakeTools.tarball, rootfs_of(init))]
    assert FakeTools.made and FakeTools.made[-1].rootfs == rootfs_of(init)


# --- gc ---------------------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Lab, Ledger]:
    lab = make_lab(tmp_path, monkeypatch)
    lab.ebuild("dev-libs/foo-2.0")
    lab.pool(*ALL)
    install_fake_toolbox(tmp_path, monkeypatch)
    return lab, install_ledger(monkeypatch, lab.pkgdir())


def test_gc_holds_the_rootfs_from_before_the_first_move_until_after_the_reindex(
    rig: tuple[Lab, Ledger],
) -> None:
    """R3.13: taken while the pool is still whole, open while ``emaint`` runs, released
    when the pool already lost its planned files."""
    lab, ledger = rig
    result = binhost("gc", "v3")
    assert result.exit_code == 0, result.output
    assert ledger.events == ["enter", "run:emaint", "exit"], ledger.events
    assert_held_once_across_the_tools(ledger)
    assert ledger.pool_at["enter"] == EVERY  # before the first move
    assert ledger.pool_at["exit"] == EVERY - PLANNED  # after the last one
    assert pool_files(lab.pkgdir()) == EVERY - PLANNED


def test_gc_holds_the_rootfs_of_its_own_init_not_systemds(
    rig: tuple[Lab, Ledger], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile half: a lock on v3's systemd rootfs does not protect an openrc reindex."""
    lab, ledger = rig
    per_init_generation(monkeypatch)
    ledger.pool = lab.pool(*ALL, generation=OTHER_GEN)
    result = binhost("gc", "v3", "--init", "openrc")
    assert result.exit_code == 0, result.output
    assert_held_once_across_the_tools(ledger, "openrc")
    assert rootfs_of("openrc") != rootfs_of("systemd")


def test_a_failed_reindex_still_releases_the_rootfs(rig: tuple[Lab, Ledger]) -> None:
    """R3.13 on a failure: the lock must not outlive the command."""
    _lab, ledger = rig
    FakeTools.env["FAKE_FAIL"] = "emaint"
    result = binhost("gc", "v3")
    assert result.exit_code == 1, result.output
    assert "fake index failure" in result.output
    assert ledger.events == ["enter", "run:emaint", "exit"], ledger.events
    assert ledger.open == 0


def test_a_failed_move_still_reindexes_under_the_lock_and_releases_it(
    rig: tuple[Lab, Ledger],
) -> None:
    """``<quarantine>/dev-libs`` is a FILE: every dev-libs move fails. The reindex of what
    did move runs while the lock is held, and the failure exit releases it."""
    lab, ledger = rig
    zz = [entry(f"app-arch/zz-1.{n}", 1, build_time=n + 1) for n in range(3)]
    lab.pool(*ALL, *zz)
    root = lab.cache / "quarantine" / "binpkgs" / "v3" / GEN
    root.mkdir(parents=True)
    (root / "dev-libs").write_text("in the way\n", encoding="utf-8")
    result = binhost("gc", "v3")
    assert result.exit_code == 1, result.output
    assert ledger.events == ["enter", "run:emaint", "exit"], ledger.events
    assert ledger.seen == [1]
    assert ledger.open == 0
    assert path_of(zz[0]) not in pool_files(lab.pkgdir())  # something did move, under the hold
    assert ledger.pool_at["enter"] == pool_files(lab.pkgdir()) | {path_of(zz[0])}


def test_a_lock_that_cannot_be_taken_refuses_and_moves_nothing(rig: tuple[Lab, Ledger]) -> None:
    """The rootfs cannot be used (``using`` raises): exit 1 naming the toolbox, no traceback,
    nothing moved, no tool run -- an index naming moved files fails the next emerge."""
    lab, ledger = rig
    ledger.fail_to_enter = toolbox.ToolboxError("cannot extract the rootfs: disk full")
    result = binhost("gc", "v3")
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "disk full" in " ".join(result.output.split())
    assert pool_files(lab.pkgdir()) == EVERY
    assert ledger.events == []
    assert FakeTools.calls("emaint") == []


def test_a_missing_toolbox_enters_nothing(rig: tuple[Lab, Ledger]) -> None:
    """R3.12 stays: refused before the first move, and no hold is ever taken."""
    lab, ledger = rig
    FakeTools.tarball.unlink()
    result = binhost("gc", "v3")
    assert result.exit_code == 1, result.output
    assert ledger.events == []
    assert pool_files(lab.pkgdir()) == EVERY


def test_a_dry_run_enters_nothing(rig: tuple[Lab, Ledger]) -> None:
    """Nothing is moved or reindexed, so nothing is worth locking -- with the toolbox present."""
    lab, ledger = rig
    result = binhost("gc", "v3", "--dry-run")
    assert result.exit_code == 0, result.output
    assert ledger.events == []
    assert pool_files(lab.pkgdir()) == EVERY


def test_a_gc_refused_for_a_busy_arch_enters_nothing(rig: tuple[Lab, Ledger]) -> None:
    """Hostile half: the hold is taken only once the arch's lock is ours, never before a
    refusal that moves nothing."""
    lab, ledger = rig
    ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker="bentoo-lab",
            job="fac-v3",
            commit="a" * 40,
            since="2026-10-20T10:00:00Z",
            host_pid=None,
        ),
    )
    result = binhost("gc", "v3")
    assert result.exit_code == 1, result.output
    assert ledger.events == []
    assert pool_files(lab.pkgdir()) == EVERY


def test_two_gcs_in_a_row_each_hold_and_release_their_own(rig: tuple[Lab, Ledger]) -> None:
    """Hostile half: a hold leaked by the first would show as an open count at the second."""
    _lab, ledger = rig
    assert binhost("gc", "v3").exit_code == 0
    assert binhost("gc", "v3", "--keep", "1").exit_code == 0
    assert ledger.events == ["enter", "run:emaint", "exit"] * 2, ledger.events
    assert ledger.seen == [1, 1]


# --- publish ----------------------------------------------------------------------------------


@pytest.fixture
def prig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Lab, Ledger]]:
    with extra_socket() as agent:
        lab = publish_lab(tmp_path, monkeypatch, agent, *ENTRIES)
        lab.record(finals={"minimal": [(FFMPEG, 3)]}, transients={"kde": [(FFMPEG, 2)]})
        yield lab, install_ledger(monkeypatch, lab.pkgdir())


def test_publish_holds_the_rootfs_across_the_signing_and_the_reindex(
    prig: tuple[Lab, Ledger],
) -> None:
    """R3.13 for the other tool run: ``gpkg-sign`` for each file, then ``emaint``, under
    one hold."""
    lab, ledger = prig
    result = binhost("publish", "v3", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert ledger.events == ["enter", "run:gpkg-sign", "run:emaint", "exit"], ledger.events
    assert_held_once_across_the_tools(ledger)
    assert (lab.cache / "publish" / "v3").is_dir()


def test_publish_holds_the_rootfs_of_its_own_init(
    prig: tuple[Lab, Ledger], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile half: ``--init openrc`` locks openrc's rootfs."""
    lab, ledger = prig
    per_init_generation(monkeypatch)
    other = lab.pool(*ENTRIES, generation=OTHER_GEN)
    (other / ".shidashi-pool.json").write_bytes((lab.pkgdir() / ".shidashi-pool.json").read_bytes())
    result = binhost("publish", "v3", "--key", FPR, "--init", "openrc")
    assert result.exit_code == 0, result.output
    assert_held_once_across_the_tools(ledger, "openrc")


@pytest.mark.parametrize("failing", ["gpkg-sign", "emaint"])
def test_publish_leaves_the_rootfs_when_a_step_is_refused(
    prig: tuple[Lab, Ledger], failing: str
) -> None:
    """R3.13 on a failure: the signing or the reindex fails, the lock is released."""
    _lab, ledger = prig
    FakeTools.env["FAKE_FAIL"] = failing
    result = binhost("publish", "v3", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert ledger.events[0] == "enter" and ledger.events[-1] == "exit", ledger.events
    assert f"run:{failing}" in ledger.events
    assert ledger.events.count("enter") == ledger.events.count("exit") == 1
    assert ledger.seen and set(ledger.seen) == {1}
    assert ledger.open == 0


def test_publish_with_nothing_to_publish_enters_nothing(prig: tuple[Lab, Ledger]) -> None:
    """Hostile half: a refusal that runs no tool (no recorded final) holds nothing."""
    lab, ledger = prig
    (lab.pkgdir() / ".shidashi-pool.json").unlink()
    result = binhost("publish", "v3", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert ledger.events == []


def test_publish_without_a_toolbox_enters_nothing(prig: tuple[Lab, Ledger]) -> None:
    _lab, ledger = prig
    FakeTools.tarball.unlink()
    result = binhost("publish", "v3", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert ledger.events == []


def test_publish_whose_lock_cannot_be_taken_is_refused_without_a_traceback(
    prig: tuple[Lab, Ledger],
) -> None:
    _lab, ledger = prig
    ledger.fail_to_enter = toolbox.ToolboxError("cannot extract the rootfs: disk full")
    result = binhost("publish", "v3", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "disk full" in " ".join(result.output.split())
    assert ledger.events == []
    assert FakeTools.calls("gpkg-sign") == []
