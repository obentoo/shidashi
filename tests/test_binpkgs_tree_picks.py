"""What the tree picks in a slot: ``resolve_tree_picks`` and the resolver's third field.

The slot rule of ``shidashi.stale_plan`` asks ``portageq best_visible cat/pkg:SLOT`` for
each slot left with no fresh binpkg and compares the answer's cpv with the stale version.
Same harness as ``tests/test_binpkgs.py``, imported from it: the resolver runs for real,
in one interpreter and through the per-query loop, against a fake ``portageq`` -- and,
where there is a Portage, against this host's own tree.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, binpkgs
from shidashi.container import CommandResult
from shidashi.stale_plan import plan_stale, slot_atom_of
from tests.test_binpkgs import (
    SIMDUTF,
    _batch,
    _host_portageq_python,
    _LocalContainer,
    _ResolverEvents,
)

PY314, PY315 = "dev-lang/python:3.14", "dev-lang/python:3.15"
GONE = "app-emulation/spice:0"
_ANSWERS = {
    SIMDUTF: ("dev-cpp/simdutf-9.2.1", "0/36"),
    PY314: ("dev-lang/python-3.14.10_p1", "3.14"),
    PY315: ("dev-lang/python-3.15.2_p1-r1", "3.15"),
}
_SLOTS = [PY314, PY315, GONE]
_PICKS = {PY314: "dev-lang/python-3.14.10_p1", PY315: "dev-lang/python-3.15.2_p1-r1", GONE: ""}


def test_picks_are_the_best_visible_cpv_of_each_slot_in_one_interpreter(tmp_path: Path) -> None:
    container = _LocalContainer(tmp_path, _ANSWERS)
    got = binpkgs.resolve_tree_picks(container, _SLOTS, portageq=container.portageq)
    assert got == _PICKS  # the revision is part of the answer; nothing visible is ""
    (argv,) = container.calls
    assert argv[:4] == ["python3", "-X", "utf8", "-c"] and argv[-3:] == _SLOTS


def test_picks_through_the_per_query_loop_are_the_same_and_the_trail_says_so(
    tmp_path: Path,
) -> None:
    """A stage whose portageq does not load in one interpreter: the bash loop answers."""
    container = _LocalContainer(tmp_path, _ANSWERS)
    trail = _ResolverEvents()
    with audit.recording(trail):
        got = binpkgs.resolve_tree_picks(
            container, _SLOTS, known_visible=SIMDUTF, portageq=str(tmp_path / "no-portageq")
        )
    assert [call[0] for call in container.calls] == ["python3", "bash"]
    assert got == _PICKS
    [(kind, fields)] = trail.events
    assert kind == "binpkgs.portageq_per_query" and fields["queries"] == 4  # with the witness


def test_both_resolvers_print_the_cpv_as_the_third_field(tmp_path: Path) -> None:
    """The protocol: ``query<TAB>slot<TAB>cpv``, the same bytes either way."""
    one = _LocalContainer(tmp_path / "one", _ANSWERS)
    loop = _LocalContainer(tmp_path / "loop", _ANSWERS)
    batch = one.run(binpkgs.resolve_argv(_SLOTS, portageq=one.portageq))
    per_query = loop.run(["bash", "-c", binpkgs.resolve_script(_SLOTS)])
    assert batch.stdout == per_query.stdout
    assert batch.stdout.splitlines() == [
        f"{PY314}\t3.14\tdev-lang/python-3.14.10_p1",
        f"{PY315}\t3.15\tdev-lang/python-3.15.2_p1-r1",
        f"{GONE}\t\t",
    ]


def test_providers_still_read_the_slot_and_only_the_slot(tmp_path: Path) -> None:
    container = _LocalContainer(tmp_path, _ANSWERS)
    got = binpkgs.resolve_providers(container, [SIMDUTF, PY315], portageq=container.portageq)
    assert got == {SIMDUTF: "0/36", PY315: "3.15"}


def test_picks_nothing_visible_in_any_slot_asked_is_an_answer_not_an_error(
    tmp_path: Path,
) -> None:
    """Unlike the providers: every slot asked may be of a removed package. With the
    witness resolving, and with no witness at all, the empty answers stand."""
    gone = [GONE, "dev-libs/removed:2"]
    container = _LocalContainer(tmp_path, _ANSWERS)
    with_witness = binpkgs.resolve_tree_picks(
        container, gone, known_visible=SIMDUTF, portageq=container.portageq
    )
    assert with_witness == {GONE: "", "dev-libs/removed:2": ""}  # the witness is not returned
    assert container.calls[-1][-3:] == [*gone, SIMDUTF]
    assert binpkgs.resolve_tree_picks(container, gone, portageq=container.portageq) == with_witness


def test_picks_hostile_a_witness_that_no_longer_resolves_refuses_the_run(tmp_path: Path) -> None:
    """A broken Portage configuration exits 1 like "nothing visible": the provider this
    stage resolved a moment ago comes back empty too, and the run is refused."""
    container = _LocalContainer(tmp_path, {})  # portageq answers nothing at all
    with pytest.raises(binpkgs.BinpkgError) as err:
        binpkgs.resolve_tree_picks(
            container, _SLOTS, known_visible=SIMDUTF, portageq=container.portageq
        )
    assert SIMDUTF in str(err.value) and "no longer resolves" in str(err.value)


def test_picks_a_failed_run_is_a_binpkg_error_and_no_slot_asks_nothing(tmp_path: Path) -> None:
    container = _LocalContainer(tmp_path, {}, fail=True)
    with pytest.raises(binpkgs.BinpkgError) as err:
        binpkgs.resolve_tree_picks(container, _SLOTS)
    assert "portageq: boom" in err.value.output
    idle = _LocalContainer(tmp_path / "idle", {}, fail=True)
    assert binpkgs.resolve_tree_picks(idle, [], known_visible=SIMDUTF) == {}
    assert idle.calls == []


# --- against this host's Portage --------------------------------------------------------------

_HOST_PYTHON = _host_portageq_python()
_NO_PORTAGE = pytest.mark.skipif(_HOST_PYTHON is None, reason="no Portage on this host")
_VDB = Path("/var/db/pkg")
_REVISION = re.compile(r"-r\d+$")


class _HostStage:
    """Stands in for a stage's container on a Gentoo host: runs the command here, with
    the system interpreter -- the one that imports Portage -- as the stage's ``python3``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        command = list(argv)
        self.calls.append(list(command))
        if command[0] == "python3":
            assert _HOST_PYTHON is not None
            command[0] = _HOST_PYTHON
        done = subprocess.run(command, capture_output=True, text=True, check=False)
        if check and done.returncode != 0:
            raise subprocess.CalledProcessError(
                done.returncode, command, output=done.stdout, stderr=done.stderr
            )
        return CommandResult(done.returncode, done.stdout, done.stderr)


def _installed_slot_atoms(pattern: str, limit: int) -> list[str]:
    """``cat/pkg:SLOT`` of up to ``limit`` installed packages matching ``pattern``: real
    atoms of this host, read from its vdb."""
    atoms: list[str] = []
    for slot_file in sorted(_VDB.glob(f"{pattern}/SLOT")):
        cpv = f"{slot_file.parent.parent.name}/{slot_file.parent.name}"
        slot = slot_file.read_text(encoding="utf-8").strip().split("/", 1)[0]
        atoms.append(f"{binpkgs.package_of(cpv)}:{slot}")
    return list(dict.fromkeys(atoms))[:limit]


def _python_slot_atoms() -> list[str]:
    """One atom per ``dev-lang/python`` 3.x slot with an ebuild in this host's tree (the
    slot is the version's first two components; ``python-0.3.N`` is the free-threaded
    build, in a slot of its own)."""
    repo = subprocess.run(
        ["portageq", "get_repo_path", "/", "gentoo"], capture_output=True, text=True, check=True
    ).stdout.strip()
    names = sorted(p.name for p in Path(repo, "dev-lang/python").glob("python-3.[0-9]*.ebuild"))
    slots = dict.fromkeys(".".join(n.removeprefix("python-").split(".")[:2]) for n in names)
    return [f"dev-lang/python:{slot}" for slot in slots]


def _stale_instance(cpv: str, atom: str) -> tuple[binpkgs.Instance, binpkgs.Stale]:
    """An instance of ``cpv`` in ``atom``'s slot, built against OpenSSL's old subslot."""
    dep = binpkgs.parse_slot_deps("dev-libs/openssl:0/3=")[0]
    name = cpv.split("/")[1]
    instance = binpkgs.Instance(
        cpv=cpv, build_id=1, path=f"{name}-1.gpkg.tar", slot_deps=(dep,), slot=atom.split(":")[1]
    )
    return instance, binpkgs.Stale(instance=instance, dep=dep, built="0/3", tree="0/4")


@_NO_PORTAGE
def test_picks_on_this_hosts_tree_a_revision_and_a_package_with_many_slots() -> None:
    """The real thing: this host's portageq, its tree and configuration, in one interpreter.
    The plan made from its answers excludes exactly the slots whose version it picks."""
    revised = _installed_slot_atoms("*/*-r[0-9]*", 30)
    pythons = _python_slot_atoms()
    nowhere = ["dev-lang/python:0.1", "dev-libs/no-such-package:0"]
    stage = _HostStage()
    picks = binpkgs.resolve_tree_picks(
        stage, [*revised, *pythons, *nowhere], known_visible="sys-apps/portage:0"
    )
    assert len(stage.calls) == 1 and stage.calls[0][0] == "python3"  # no per-query loop
    # every answer is a version of the package asked, or nothing
    assert all(
        not cpv or binpkgs.package_of(cpv) == atom.split(":")[0] for atom, cpv in picks.items()
    )
    assert [picks[atom] for atom in nowhere] == ["", ""]
    # a revision is part of the version the tree picks
    assert any(_REVISION.search(picks[atom]) for atom in revised), picks
    # one package, many slots: each slot has its own pick, inside that slot
    visible = {atom: picks[atom] for atom in pythons if picks[atom]}
    assert len(visible) >= 2 and len(set(visible.values())) == len(visible), picks
    for atom, cpv in visible.items():
        assert cpv.startswith(f"dev-lang/python-{atom.split(':')[1]}."), (atom, cpv)

    # the plan over real answers: the picked version's slot is excluded; the same version
    # with its revision dropped (or one added) is another version, superseded
    judged = {atom: cpv for atom, cpv in picks.items() if cpv}
    picked = [_stale_instance(cpv, atom) for atom, cpv in judged.items()]
    plan = plan_stale([f for _i, f in picked], [i for i, _f in picked], picks)
    assert set(plan.exclude) == set(judged) and plan.quarantine_now == ()
    twins = [
        _stale_instance(_REVISION.sub("", cpv) if _REVISION.search(cpv) else f"{cpv}-r99", atom)
        for atom, cpv in judged.items()
    ]
    twins.append(_stale_instance("dev-lang/python-0.1.0", "dev-lang/python:0.1"))
    plan = plan_stale([f for _i, f in twins], [i for i, _f in twins], picks)
    assert plan.exclude == () and plan.quarantine_after == ()
    assert {s.slot_atom: s.tree_picks for s in plan.superseded} == {
        **judged,
        "dev-lang/python:0.1": "",
    }
    assert all(slot_atom_of(s.instance) == s.slot_atom for s in plan.superseded)


@_NO_PORTAGE
def test_picks_on_this_host_are_the_same_bytes_and_answers_through_the_per_query_loop(
    tmp_path: Path,
) -> None:
    """A few real slots both ways: one interpreter, and one ``portageq`` per query (what a
    stage whose portageq does not load falls back to)."""
    (revised,) = _installed_slot_atoms("sys-apps/portage-[0-9]*", 1) or ["sys-apps/portage:0"]
    slots = [revised, *_python_slot_atoms()[-3:], "dev-libs/no-such-package:0"]
    assert _HOST_PYTHON is not None
    batch = _batch("", *slots, python=_HOST_PYTHON)
    per_query = subprocess.run(
        ["bash", "-c", binpkgs.resolve_script(slots)], capture_output=True, text=True, check=False
    )
    assert (batch.returncode, batch.stdout) == (per_query.returncode, per_query.stdout)
    lines = [line.split("\t") for line in batch.stdout.splitlines()]
    assert [line[0] for line in lines] == slots and all(len(line) == 3 for line in lines)
    assert lines[-1][1:] == ["", ""] and lines[0][2].startswith("sys-apps/portage-"), lines
    assert sum(1 for line in lines[1:-1] if line[2]) >= 2, lines  # python's newest slots

    direct = binpkgs.resolve_tree_picks(_HostStage(), slots, known_visible=revised)
    fallback_stage = _HostStage()
    trail = _ResolverEvents()
    with audit.recording(trail):
        fallback = binpkgs.resolve_tree_picks(
            fallback_stage, slots, known_visible=revised, portageq=str(tmp_path / "no-portageq")
        )
    assert [call[0] for call in fallback_stage.calls] == ["python3", "bash"]
    assert fallback == direct == {line[0]: line[2] for line in lines}
    assert [kind for kind, _fields in trail.events] == ["binpkgs.portageq_per_query"]
