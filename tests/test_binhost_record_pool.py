"""The factory records each shipped stage's finals and cut transients (story 029, task 2.1;
R1.1-R1.7).

Contract from design.md "Factory hook" and story.md R1: after a ``ships`` stage settles,
and before its fork-point snapshot, the PKGDIR bound into the stage gets, in
``.shidashi-pool.json``, the stage's finals -- every ``(cpv, BUILD_ID)`` installed in the
rootfs (``var/db/pkg/<cat>/<pf>/{BUILD_ID,BUILD_TIME}``) whose index entry has the same
BUILD_ID AND the same BUILD_TIME (R1.7: a vdb BUILD_ID from elsewhere, e.g. the releng
stage3, never names a local instance) -- and its transients -- every index instance of a
cut package whose USE has the cut flag in the cut's state, for every cut the settle
undid, including a cut declared by an earlier phase that does not ship (``base``). A vdb
entry without a ``BUILD_ID`` is left out; no PKGDIR bound -> no record and the build goes
on. Audited as a ``record-pool`` step with the counts.

Both factory runners are driven for real (``run_phases`` and ``run_phases_stepwise``);
only the container is fake: emerge "installs" by writing vdb entries, and the steps that
need Portage (stale-binpkgs, check-binpkgs) are stubbed as tests/test_phases.py does.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, phases
from shidashi.container import CommandResult
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from tests._pending import try_import
from tests.test_binhost_refs import entry, index

load_record: Any = try_import("shidashi.binhost", "load_record")
installed_instances: Any = try_import("shidashi.binhost", "installed_instances")
record_stage: Any = try_import("shidashi.binhost", "record_stage")

GEN = "20260823T153057Z"
RECORD = ".shidashi-pool.json"
PY = "dev-lang/python-3.14.7"
TRUNK_CUT = UseBreak(atom="dev-lang/python", flag="bluetooth", enable=False)

#: what the stage3/base leave installed: cpv -> (BUILD_ID, BUILD_TIME); None = no file
BASE_VDB: dict[str, tuple[int | None, int | None]] = {
    PY: (3, 200),
    "sys-apps/coreutils-9.8": (1, 100),
    "sys-apps/which-2.21-r1": (2, 100),
    # merged with FEATURES=-buildpkg: no BUILD_ID (GOTCHA vdb BUILD_ID)
    "sys-libs/bootstrapped-1.0": (None, 100),
    # R1.7, hostile half: the releng stage3's own BUILD_ID 1 -- the index has a build 1 of
    # this CPV too, but built here at another time: not that instance
    "sys-apps/stage3only-1.0": (1, 999),
    # R1.7: a BUILD_ID with no index entry at all
    "sys-libs/releng-2": (5, 50),
}
PLASMA = "kde-plasma/plasma-workspace-6.4.0"

INDEX = index(
    entry(PY, 2, use="ssl", build_time=100),  # the base's cut build: no bluetooth
    entry(PY, 3, use="bluetooth ssl", build_time=200),  # minimal's settled final
    entry("sys-apps/coreutils-9.8", 1, build_time=100),
    entry("sys-apps/which-2.21-r1", 2, build_time=100),
    entry(PLASMA, 1, build_time=300),
    entry("sys-apps/stage3only-1.0", 1, build_time=100),
)

MINIMAL_FINALS = {(PY, 3), ("sys-apps/coreutils-9.8", 1), ("sys-apps/which-2.21-r1", 2)}


def _recipe() -> ResolvedRecipe:
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
            Phase(
                name="base",
                stage="base",
                sets=("base",),
                emptytree=True,
                use_break=(TRUNK_CUT,),
            ),
            Phase(name="minimal", stage="minimal", sets=("extra-system",), ships=True),
            Phase(name="desktop", stage="desktop", sets=("gpu",)),
            Phase(name="flavor", stage="kde", sets=("kde",), ships=True),
        ),
        portage_layers=("base", "arch/v3", "flavor/kde", "init/systemd"),
    )


def _vdb(rootfs: Path, cpv: str, build: int | None, build_time: int | None) -> None:
    d = rootfs / "var/db/pkg" / cpv
    d.mkdir(parents=True, exist_ok=True)
    if build is not None:
        (d / "BUILD_ID").write_text(f"{build}\n", encoding="utf-8")
    if build_time is not None:
        (d / "BUILD_TIME").write_text(f"{build_time}\n", encoding="utf-8")


class _Stage:
    """The stage container: every emerge succeeds; the kde one installs plasma."""

    def __init__(self, rootfs: Path, pkgdir: Path | None) -> None:
        self.rootfs = rootfs
        self.binds: tuple[tuple[Path, Path], ...] = ()
        if pkgdir is not None:
            self.binds_rw = ((pkgdir, Path("/var/cache/binpkgs")),)
        for cpv, (build, build_time) in BASE_VDB.items():
            _vdb(rootfs, cpv, build, build_time)

    def run(self, argv: Any, **_k: Any) -> CommandResult:
        argv = list(argv)
        if argv[:1] == ["emerge"] and "--pretend" not in argv and "@kde" in argv:
            _vdb(self.rootfs, PLASMA, 1, 300)
        return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A PKGDIR with the index above, Portage-only steps stubbed, and a witness of the
    record at each fork-point snapshot."""
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    pkgdir = tmp_path / "cache" / "binpkgs" / "v3" / GEN
    pkgdir.mkdir(parents=True)
    (pkgdir / "Packages").write_text(INDEX, encoding="utf-8")
    monkeypatch.setattr(
        phases,
        "stale_binpkgs",
        lambda *_a, **_k: phases.StaleOutcome(None, phases.StalePlan(), {}, {"index": "x"}),
    )
    monkeypatch.setattr(phases, "check_binpkgs", lambda *_a, **_k: {"index": "absent"})
    at_snapshot: dict[str, Any] = {}

    def snapshot(_root: Path, dest: Path) -> Path:
        record = pkgdir / RECORD
        try:
            at_snapshot[dest.name] = json.loads(record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            at_snapshot[dest.name] = None
        except ValueError:
            at_snapshot[dest.name] = "unparsable"
        return dest

    monkeypatch.setattr(phases, "snapshot_fork_point", snapshot)
    return {"pkgdir": pkgdir, "rootfs": tmp_path / "rootfs", "at_snapshot": at_snapshot}


def _run(
    tmp_path: Path, container: _Stage, runner: str, **kw: Any
) -> list[dict[str, Any]]:  # the record-pool steps, in order
    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        if runner == "one-shot":
            phases.run_phases(
                container,  # type: ignore[arg-type]
                _recipe(),
                emptytree=True,
                snapshot="S",
                pins="P",
                fork_points_dir=tmp_path / "forks",
                **kw,
            )
        else:
            phases.run_phases_stepwise(
                container,  # type: ignore[arg-type]
                _recipe(),
                emptytree=True,
                completed=(),
                until=kw.get("stop_after"),
                snapshot="S",
                pins="P",
                fork_points_dir=tmp_path / "forks",
                state_path=tmp_path / "state.json",
            )
    events = audit.read_events(trail.path / "events.jsonl")
    # a step's data is what it was started with plus what it added (either is fine);
    # record-pool steps never nest, so the n-th start belongs to the n-th end
    starts = [
        e
        for e in events
        if e.get("kind") == "step.start" and str(e.get("step")).endswith("record-pool")
    ]
    manifest = audit.build_manifest(events)
    ended = [s for s in manifest["steps"] if str(s["step"]).endswith("record-pool")]
    return [{**start, **end} for start, end in zip(starts, ended, strict=False)]


RUNNERS = ["one-shot", "stepwise"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_each_shipped_stage_records_what_its_rootfs_installed_and_its_cut_builds(
    tmp_path: Path, factory: dict[str, Any], runner: str
) -> None:
    """R1.1, R1.2, R1.4, R1.7. Hostile halves: python build 3 (bluetooth on) is installed
    and NOT a transient; build 2 (the base's cut, settled at minimal) is a transient and NOT
    a final; the package merged without a BUILD_ID is in neither; a vdb BUILD_ID whose
    BUILD_TIME is not the index's, or with no index entry, is not a final; plasma,
    installed by kde, is not minimal's."""
    _run(tmp_path, _Stage(factory["rootfs"], factory["pkgdir"]), runner)
    record = load_record(factory["pkgdir"])
    assert record.finals.get("minimal") == frozenset(MINIMAL_FINALS)
    assert record.transients.get("minimal") == frozenset({(PY, 2)})
    assert record.finals.get("kde") == frozenset(MINIMAL_FINALS | {(PLASMA, 1)})
    assert not any(
        cpv.startswith("sys-libs/bootstrapped") for cpv, _b in record.finals.get("kde", ())
    )
    # stages that do not ship record nothing
    assert set(record.finals) == {"minimal", "kde"}


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_record_is_written_after_the_settle_and_before_the_fork_point(
    tmp_path: Path, factory: dict[str, Any], runner: str
) -> None:
    """design.md "Factory hook": the snapshot of a shipped stage already sees its record;
    the stages before it (base) see none."""
    _run(tmp_path, _Stage(factory["rootfs"], factory["pkgdir"]), runner)
    seen = factory["at_snapshot"]
    minimal = next((v for k, v in seen.items() if k.endswith("-minimal.tar")), "missing")
    base = next((v for k, v in seen.items() if k.endswith("-base.tar")), "missing")
    assert base is None, seen
    assert isinstance(minimal, dict) and "minimal" in minimal.get("stages", {}), seen


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_record_pool_step_is_audited_with_its_counts(
    tmp_path: Path, factory: dict[str, Any], runner: str
) -> None:
    """R1.6: one step per shipped stage, finals and transients counted."""
    steps = _run(tmp_path, _Stage(factory["rootfs"], factory["pkgdir"]), runner)
    assert len(steps) == 2, steps
    minimal, kde = steps
    assert (minimal.get("finals"), minimal.get("transients")) == (3, 1), minimal
    assert kde.get("finals") == 4, kde


def test_a_rebuilt_stage_replaces_its_record_and_leaves_the_other_stages(
    tmp_path: Path, factory: dict[str, Any]
) -> None:
    """R1.3: minimal is rebuilt alone (--stop-after minimal); kde's record survives as it
    was, minimal's stale entries are gone."""
    pkgdir = factory["pkgdir"]
    (pkgdir / RECORD).write_text(
        json.dumps(
            {
                "version": 1,
                "stages": {
                    "minimal": {"finals": [["sys-apps/gone-1", 1]], "transients": []},
                    "kde": {"finals": [[PLASMA, 9]], "transients": [[PY, 7]]},
                },
            }
        ),
        encoding="utf-8",
    )
    _run(tmp_path, _Stage(factory["rootfs"], pkgdir), "one-shot", stop_after="minimal")
    record = load_record(pkgdir)
    assert record.finals.get("minimal") == frozenset(MINIMAL_FINALS)
    assert record.finals.get("kde") == frozenset({(PLASMA, 9)})
    assert record.transients.get("kde") == frozenset({(PY, 7)})


def test_without_a_pkgdir_bound_the_build_goes_on_and_writes_no_record(
    tmp_path: Path, factory: dict[str, Any]
) -> None:
    """R1.5. The fixture's PKGDIR exists but is not bound into the first container: no
    record, and the build runs to its end. Bound into a second one, it is recorded -- the
    bind, not the directory's existence, decides."""
    steps = _run(tmp_path, _Stage(factory["rootfs"], None), "one-shot")
    assert list(tmp_path.rglob(RECORD)) == []
    assert all(s.get("finals") is None for s in steps)
    assert any(k.endswith("-kde.tar") for k in factory["at_snapshot"])  # ran to the end

    bound = tmp_path / "bound"
    bound.mkdir()
    _run(bound, _Stage(bound / "rootfs", factory["pkgdir"]), "one-shot")
    assert (factory["pkgdir"] / RECORD).is_file()


def test_a_damaged_record_stops_the_stage_naming_the_file_and_is_left_as_it_was(
    tmp_path: Path, factory: dict[str, Any]
) -> None:
    """R7.3, R7.4 at the factory: a record it cannot read is never replaced."""
    path = factory["pkgdir"] / RECORD
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(Exception) as err:
        _run(tmp_path, _Stage(factory["rootfs"], factory["pkgdir"]), "one-shot")
    assert RECORD in str(err.value)
    assert path.read_text(encoding="utf-8") == "{not json"


# --- the I/O helpers the hook calls (design.md "shidashi/binhost.py") ------------------------


def test_installed_instances_reads_build_id_and_build_time_and_skips_incomplete_entries(
    tmp_path: Path,
) -> None:
    """``(cpv, BUILD_ID) -> BUILD_TIME``; an entry missing either file, or holding a
    non-integer, is skipped (never guessed)."""
    for cpv, (build, build_time) in BASE_VDB.items():
        _vdb(tmp_path, cpv, build, build_time)
    _vdb(tmp_path, "app-misc/no-time-1", 4, None)
    _vdb(tmp_path, "app-misc/bad-id-1", None, 10)
    (tmp_path / "var/db/pkg/app-misc/bad-id-1/BUILD_ID").write_text("x\n", encoding="utf-8")
    assert installed_instances(tmp_path) == {
        (PY, 3): 200,
        ("sys-apps/coreutils-9.8", 1): 100,
        ("sys-apps/which-2.21-r1", 2): 100,
        ("sys-apps/stage3only-1.0", 1): 999,
        ("sys-libs/releng-2", 5): 50,
    }


def test_record_stage_keeps_only_installed_instances_the_index_has_at_the_same_build_time(
    tmp_path: Path,
) -> None:
    """R1.7 at the helper: the stage3's ``stage3only`` build 1 (BUILD_TIME 999) is not the
    index's build 1 (BUILD_TIME 100); the counts returned are the counts saved."""
    rootfs, pkgdir = tmp_path / "rootfs", tmp_path / "pkgdir"
    pkgdir.mkdir()
    (pkgdir / "Packages").write_text(INDEX, encoding="utf-8")
    for cpv, (build, build_time) in BASE_VDB.items():
        _vdb(rootfs, cpv, build, build_time)
    counts = record_stage(pkgdir, rootfs, "minimal", (TRUNK_CUT,))
    record = load_record(pkgdir)
    assert record.finals.get("minimal") == frozenset(MINIMAL_FINALS)
    assert record.transients.get("minimal") == frozenset({(PY, 2)})
    assert tuple(counts) == (3, 1)
