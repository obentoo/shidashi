"""Producer -> consumer contracts of the two record files (story 029, task 6.1; R1.1, R2.2,
R4.1, R4.2, R5.1).

Each test drives the REAL producer and hands its file, untouched, to the REAL consumers:

1. the factory's record (``phases.run_phases`` over a fake stage container) is what
   ``binhost show`` classifies and what ``plan_publish`` publishes -- with the GOTCHA case:
   one ``(CPV, USE)`` that is kde's cut transient and minimal's final;
2. the quarantine list a real ``binhost gc`` writes is what the worker sync moves aside
   on the worker and leaves out of the pull.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, ownership, phases, worker
from shidashi.container import CommandResult
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from tests._fake_worker import FakeWorker, option_values
from tests._pending import try_import
from tests.test_binhost_cli_gc import install_fake_toolbox, path_of
from tests.test_binhost_cli_show import FFMPEG, GEN, QUARANTINE, binhost, kind_of, make_lab
from tests.test_binhost_refs import entry

load_record: Any = try_import("shidashi.binhost", "load_record")
parse_refs: Any = try_import("shidashi.binhost", "parse_refs")
plan_publish: Any = try_import("shidashi.publish_pool", "plan_publish")

NO_SDL = UseBreak(atom="media-video/ffmpeg", flag="sdl", enable=False)
CUT_FFMPEG = entry(FFMPEG, 2, use="x264", build_time=200)  # built under kde's cut
FINAL_FFMPEG = entry(FFMPEG, 3, use="sdl x264", build_time=300)  # kde's settled ffmpeg
CURL = entry("net-misc/curl-8.16.0", 1, build_time=100)


def _recipe() -> ResolvedRecipe:
    """base -> minimal (ships) -> desktop (cuts ffmpeg's sdl) -> kde (ships)."""
    return ResolvedRecipe(
        arch="v3",
        flavor="kde",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=("base", "graphics", "kde"),
        phases=(
            Phase(name="base", stage="base", sets=("base",), emptytree=True),
            Phase(name="minimal", stage="minimal", sets=("extra-system",), ships=True),
            Phase(name="desktop", stage="desktop", sets=("gpu",), use_break=(NO_SDL,)),
            Phase(name="flavor", stage="kde", sets=("kde",), ships=True),
        ),
        portage_layers=("base", "arch/v3", "flavor/kde", "init/systemd"),
    )


class _Stage:
    """minimal installs ffmpeg build 2 (no sdl: minimal does not want it); kde's settle
    re-merges ffmpeg with sdl, build 3."""

    def __init__(self, rootfs: Path, pkgdir: Path) -> None:
        self.rootfs = rootfs
        self.binds: tuple[tuple[Path, Path], ...] = ()
        self.binds_rw = ((pkgdir, Path("/var/cache/binpkgs")),)
        self._vdb("net-misc/curl-8.16.0", 1)

    #: the BUILD_TIME each instance's vdb entry carries -- the index's (R1.7)
    BUILT = {(FFMPEG, 2): 200, (FFMPEG, 3): 300, ("net-misc/curl-8.16.0", 1): 100}

    def _vdb(self, cpv: str, build: int) -> None:
        d = self.rootfs / "var/db/pkg" / cpv
        d.mkdir(parents=True, exist_ok=True)
        (d / "BUILD_ID").write_text(f"{build}\n", encoding="utf-8")
        (d / "BUILD_TIME").write_text(f"{self.BUILT[cpv, build]}\n", encoding="utf-8")

    def run(self, argv: Any, **_k: Any) -> CommandResult:
        argv = list(argv)
        if argv[:1] == ["emerge"] and "--pretend" not in argv:
            if "@extra-system" in argv:
                self._vdb(FFMPEG, 2)
            elif "--oneshot" in argv and "media-video/ffmpeg" in argv:
                self._vdb(FFMPEG, 3)  # the settle
        return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")


def test_the_factorys_record_is_what_show_classifies_and_publish_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GOTCHA cut_instances: ffmpeg build 2 is kde's cut transient AND minimal's final. The
    factory records both facts; ``show`` says final; ``plan_publish`` publishes it."""
    lab = make_lab(tmp_path, monkeypatch)
    lab.ebuild(FFMPEG)
    lab.ebuild("net-misc/curl-8.16.0")
    pkgdir = lab.pool(CUT_FFMPEG, FINAL_FFMPEG, CURL)
    monkeypatch.setattr(
        phases,
        "stale_binpkgs",
        lambda *_a, **_k: phases.StaleOutcome(None, phases.StalePlan(), {}, {"index": "x"}),
    )
    monkeypatch.setattr(phases, "check_binpkgs", lambda *_a, **_k: {"index": "absent"})
    monkeypatch.setattr(phases, "snapshot_fork_point", lambda _root, dest: dest)
    with audit.run(tmp_path / "runs", command="factory", argv=[]):
        phases.run_phases(
            _Stage(tmp_path / "rootfs", pkgdir),  # type: ignore[arg-type]
            _recipe(),
            emptytree=True,
            snapshot="S",
            pins="P",
            fork_points_dir=tmp_path / "forks",
        )
    record = load_record(pkgdir)  # the producer's own file, read back
    assert (FFMPEG, 2) in record.finals.get("minimal", frozenset())
    assert (FFMPEG, 2) in record.transients.get("kde", frozenset())
    assert (FFMPEG, 3) in record.finals.get("kde", frozenset())

    shown = binhost("show", "v3")
    assert shown.exit_code == 0, shown.output
    assert kind_of(shown.output, path_of(CUT_FFMPEG)) == "final"
    assert kind_of(shown.output, path_of(FINAL_FFMPEG)) == "final"

    refs = parse_refs((pkgdir / "Packages").read_text(encoding="utf-8"))
    planned = {r.path for r in plan_publish(refs, load_record(pkgdir))}
    assert planned == {path_of(CUT_FFMPEG), path_of(FINAL_FFMPEG), path_of(CURL)}


# --- the quarantine list: gc -> worker sync ----------------------------------------------


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    w.commit_recipes()
    yield w
    w.close()


def test_the_gcs_quarantine_list_is_what_the_worker_sync_moves_and_leaves_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fw: FakeWorker
) -> None:
    """A real gc on the host writes the list; a push carries it; the next pull moves
    exactly those paths aside on the worker (which still has them: push and pull never
    delete) and excludes exactly those paths -- and none of them comes back."""
    lab = make_lab(tmp_path, monkeypatch)  # the same cache the fake worker's host uses
    assert lab.cache == fw.host_cache
    install_fake_toolbox(tmp_path, monkeypatch)
    olds = [entry(f"dev-libs/foo-1.{n}", 1, build_time=n + 1) for n in range(4)]
    lab.pool(*olds)
    for block in olds:  # the worker has every file the host had before the gc
        rel = path_of(block)
        fw.put(f"cache/binpkgs/v3/{GEN}/{rel}", f"binpkg {rel}\n")

    gc = binhost("gc", "v3")  # keep=2: foo-1.0 and foo-1.1 go
    assert gc.exit_code == 0, gc.output
    written = lab.pkgdir() / QUARANTINE
    assert written.is_file(), "the gc wrote no quarantine list"
    listed = set(json.loads(written.read_text(encoding="utf-8"))["paths"])
    assert listed == {path_of(olds[0]), path_of(olds[1])}

    worker.push(fw.remote(), "v3", bwlimit=None)  # carries the list (a dotfile) along
    assert (fw.work / "cache" / "binpkgs" / "v3" / GEN / QUARANTINE).is_file()

    owner = ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker=fw.name,
            job="kde",
            commit=fw.head,
            since="2026-10-20T14:02:31Z",
            host_pid=None,
        ),
    )
    worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results", owner=owner)

    wpkg = fw.work / "cache" / "binpkgs" / "v3" / GEN
    assert {rel for rel in listed if (wpkg / rel).exists()} == set()
    assert {rel for rel in map(path_of, olds[2:]) if (wpkg / rel).exists()} == set(
        map(path_of, olds[2:])
    )
    excludes = {
        e
        for call in fw.rsync_calls()
        for e in option_values(call["argv"], "--exclude")
        if "/Packages" in option_values(call["argv"], "--exclude")
    }
    assert excludes - {"/Packages"} == {"/" + rel for rel in listed}
    assert not any((lab.pkgdir() / rel).exists() for rel in listed)
