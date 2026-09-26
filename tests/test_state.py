"""UNIT de shidashi.state (story 004 grupo 1) — modelos + persistência PUROS.

Tudo aqui é puro ou faz apenas I/O contra um tmp dir (sem root, sem nspawn):

* 1.1 modelos frozen pydantic v2 (``extra="forbid"``, coleções ``tuple``):
  ``EmergePlanEntry`` / ``PhaseDiff`` / ``BuildState`` (R4.1/R4.4/R6.1);
* 1.2 persistência: ``recipe_hash`` (SHA-256 estável + sensível à receita),
  ``save_state``/``load_state`` round-trip com escrita atômica (temp+rename),
  ``clear_state`` idempotente, ``is_stale`` (mismatch de snapshot ou hash)
  (R6.1/R6.2/R6.4).

Contrato derivado de design.md §state. Os símbolos de ``shidashi.state`` são
importados de forma tolerante (``try_import``) para não abortar a coleção do
pytest enquanto a impl não existe; cada teste fica Red no uso nomeando o símbolo
pendente (Red esperado da story 004).
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from tests._pending import try_import

EmergePlanEntry: Any = try_import("shidashi.state", "EmergePlanEntry")
PhaseDiff: Any = try_import("shidashi.state", "PhaseDiff")
BuildState: Any = try_import("shidashi.state", "BuildState")
recipe_hash: Any = try_import("shidashi.state", "recipe_hash")
load_state: Any = try_import("shidashi.state", "load_state")
save_state: Any = try_import("shidashi.state", "save_state")
clear_state: Any = try_import("shidashi.state", "clear_state")
is_stale: Any = try_import("shidashi.state", "is_stale")


def _recipe(*, flavor: str = "minimal", sets: tuple[str, ...] = ()) -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor=flavor,
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=sets,
        phases=(Phase(name="rebuild"), Phase(name="graphics")),
        portage_layers=("base", "arch/v3", "flavor/minimal", "init/systemd"),
    )


def _state(**over: Any) -> Any:
    base: dict[str, Any] = dict(
        arch="v3",
        flavor="minimal",
        init="systemd",
        snapshot="20260524",
        recipe_hash="deadbeef",
    )
    base.update(over)
    return BuildState(**base)


# --- 1.1 modelos frozen + extra=forbid + tuple ------------------------------


def test_emerge_plan_entry_fields_and_default_use_changes() -> None:
    entry = EmergePlanEntry(atom="media-video/ffmpeg-6.1.1", op="N")
    assert entry.atom == "media-video/ffmpeg-6.1.1"
    assert entry.op == "N"
    assert entry.use_changes == ()


def test_emerge_plan_entry_is_frozen_and_extra_forbid() -> None:
    entry = EmergePlanEntry(atom="x/y-1", op="R", use_changes=("sdl",))
    with pytest.raises(Exception):  # noqa: B017  (frozen → ValidationError)
        entry.op = "N"
    with pytest.raises(Exception):  # noqa: B017  (extra=forbid)
        EmergePlanEntry(atom="x/y-1", op="N", bogus=1)


def test_phase_diff_defaults_are_empty_tuples() -> None:
    diff = PhaseDiff(phase="rebuild", built=("a/b-1",))
    assert diff.phase == "rebuild"
    assert diff.built == ("a/b-1",)
    assert diff.unexpected_rebuilds == ()
    assert diff.use_changes == ()
    assert diff.blockers == ()


def test_build_state_defaults_and_collections_are_tuples() -> None:
    state = _state()
    assert state.seed_done is False
    assert state.completed_phases == ()
    assert state.accumulated_breaks == ()
    assert state.phase_diffs == ()
    full = _state(
        seed_done=True,
        completed_phases=("rebuild",),
        accumulated_breaks=(UseBreak(atom="x/y", flag="z"),),
        phase_diffs=(PhaseDiff(phase="rebuild", built=("a/b-1",)),),
    )
    assert full.completed_phases == ("rebuild",)
    assert isinstance(full.accumulated_breaks, tuple)
    assert full.phase_diffs[0].phase == "rebuild"


# --- seed_sha512 (R4.1/R4.2, story 005) -------------------------------------


def test_build_state_seed_sha512_defaults_empty() -> None:
    assert _state().seed_sha512 == ""


def test_build_state_seed_sha512_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    save_state(p, _state(seed_sha512="a" * 128))
    loaded = load_state(p)
    assert loaded is not None
    assert loaded.seed_sha512 == "a" * 128


def test_build_state_loads_legacy_json_without_seed_sha512(tmp_path: Path) -> None:
    # JSON antigo (sem o campo) ainda carrega sob extra="forbid", via o default.
    p = tmp_path / "legacy.json"
    p.write_text(
        '{"arch":"v3","flavor":"minimal","init":"systemd",'
        '"snapshot":"20260524","recipe_hash":"deadbeef"}',
        encoding="utf-8",
    )
    loaded = load_state(p)
    assert loaded is not None
    assert loaded.seed_sha512 == ""


def test_build_state_is_frozen_and_extra_forbid() -> None:
    state = _state()
    with pytest.raises(Exception):  # noqa: B017
        state.seed_done = True
    with pytest.raises(Exception):  # noqa: B017
        _state(bogus="x")


# --- 1.2 recipe_hash --------------------------------------------------------


def test_recipe_hash_is_stable_for_same_recipe() -> None:
    assert recipe_hash(_recipe()) == recipe_hash(_recipe())


def test_recipe_hash_changes_when_recipe_changes() -> None:
    assert recipe_hash(_recipe(flavor="minimal")) != recipe_hash(_recipe(flavor="kde"))


def test_recipe_hash_is_hex_sha256() -> None:
    digest = recipe_hash(_recipe())
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(ch in "0123456789abcdef" for ch in digest)


# --- 1.2 save/load/clear ----------------------------------------------------


def test_save_then_load_round_trips_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _state(
        seed_done=True,
        completed_phases=("rebuild",),
        phase_diffs=(PhaseDiff(phase="rebuild", built=("a/b-1",), blockers=("c/d",)),),
    )
    save_state(path, state)
    loaded = load_state(path)
    assert loaded == state


def test_load_state_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json") is None


def test_save_state_writes_atomically_no_partial_temp(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _state())
    assert path.exists()
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_clear_state_removes_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _state())
    assert path.exists()
    clear_state(path)
    assert not path.exists()
    clear_state(path)  # idempotente: não levanta
    assert not path.exists()


# --- 1.2 is_stale (R6.2) ----------------------------------------------------


def test_is_stale_false_when_snapshot_and_hash_match() -> None:
    state = _state(snapshot="SNAP", recipe_hash="HASH")
    assert is_stale(state, snapshot="SNAP", recipe_hash="HASH") is False


def test_is_stale_true_on_snapshot_mismatch() -> None:
    state = _state(snapshot="OLD", recipe_hash="HASH")
    assert is_stale(state, snapshot="NEW", recipe_hash="HASH") is True


def test_is_stale_true_on_recipe_hash_mismatch() -> None:
    state = _state(snapshot="SNAP", recipe_hash="OLD")
    assert is_stale(state, snapshot="SNAP", recipe_hash="NEW") is True
