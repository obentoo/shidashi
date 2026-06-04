"""UNIT + INTEGRAÇÃO de shidashi.phases (story 004 — planejamento stepwise PURO +
orquestração privilegiada).

UNIT (determinista, CI não-Gentoo):
* 2.1 ``checkpoint_sequence`` (= ("seed", *phases, "settle")) e ``plan_phase_run``
  (pula completed, para após ``until`` inclusive, ``until="seed"`` ⇒ vazio,
  ``until`` inválido ⇒ ValueError listando os nomes válidos) — R1.1/R1.2/R1.4/
  R1.5/R2.5;
* 2.2 ``phase_snapshot_path`` (chave a-f-i-snapshot-phase) e ``latest_resumable``
  (última phase completed com tarball em disco, senão (None,None)) — R5.1/R5.2;
* 3.1 ``parse_emerge_plan`` (ops N/R/rR/U, deltas USE, [blocks], vazio em no-merge)
  — R4.1/R4.3;
* 3.2 ``compute_phase_diff`` (unexpected rebuild via prior_atoms, use_changes,
  blockers passthrough, phase limpa ⇒ listas vazias) — R4.1/R4.2;
* 5.1/5.2 (unit) routing de ``run_phases_stepwise`` com o Container monkeypatched
  e ``run_phase`` injetado: STOP após uma phase interrompe o laço; on_failure
  RETRY re-roda a mesma phase e ABORT levanta FactoryError; nenhum settle quando
  o stop é antecipado — R1.3/R2.2/R2.3/R3.1/R3.2/R3.3/R3.5/R5.3.

INTEGRAÇÃO (host-gated, Red DIFERIDO): o caminho privilegiado real
(nspawn + emerge + snapshot por fase) PULA em CI/sandbox não-root.

Contrato derivado de design.md §phases. Símbolos novos importados de forma
tolerante (``try_import``); cada teste unit fica Red no uso (Red esperado 004).
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from shidashi import phases
from shidashi.recipe import Phase, ResolvedRecipe, ResolvedUse
from tests._pending import try_import

checkpoint_sequence: Any = try_import("shidashi.phases", "checkpoint_sequence")
plan_phase_run: Any = try_import("shidashi.phases", "plan_phase_run")
parse_emerge_plan: Any = try_import("shidashi.phases", "parse_emerge_plan")
compute_phase_diff: Any = try_import("shidashi.phases", "compute_phase_diff")
phase_snapshot_path: Any = try_import("shidashi.phases", "phase_snapshot_path")
latest_resumable: Any = try_import("shidashi.phases", "latest_resumable")
run_phases_stepwise: Any = try_import("shidashi.phases", "run_phases_stepwise")
CheckpointDecision: Any = try_import("shidashi.phases", "CheckpointDecision")
FailureDecision: Any = try_import("shidashi.phases", "FailureDecision")
EmergePlanEntry: Any = try_import("shidashi.state", "EmergePlanEntry")

_NEEDS_HOST = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST, reason="exige root + systemd-nspawn + stage3 seedado (host Gentoo)"
)


def _recipe(
    *,
    flavor: str = "minimal",
    sets: tuple[str, ...] = (),
    phases_: tuple[Phase, ...] = (
        Phase(name="rebuild"),
        Phase(name="graphics"),
        Phase(name="apps"),
    ),
) -> ResolvedRecipe:
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
        use=ResolvedUse(enabled=(), disabled=()),
        sets=sets,
        phases=phases_,
        portage_layers=("base", "arch/v3", "flavor/minimal", "init/systemd"),
    )


# --- 2.1 checkpoint_sequence -------------------------------------------------


def test_checkpoint_sequence_is_seed_phases_settle() -> None:
    seq = checkpoint_sequence(_recipe())
    assert seq == ("seed", "rebuild", "graphics", "apps", "settle")


def test_checkpoint_sequence_seed_first_settle_last() -> None:
    seq = checkpoint_sequence(_recipe(phases_=(Phase(name="rebuild"),)))
    assert seq[0] == "seed"
    assert seq[-1] == "settle"


# --- 2.1 plan_phase_run ------------------------------------------------------


def test_plan_phase_run_all_when_none_completed_no_until() -> None:
    plan = plan_phase_run(_recipe(), completed=(), until=None)
    assert tuple(p.name for p in plan) == ("rebuild", "graphics", "apps")


def test_plan_phase_run_skips_completed() -> None:
    plan = plan_phase_run(_recipe(), completed=("rebuild",), until=None)
    assert tuple(p.name for p in plan) == ("graphics", "apps")


def test_plan_phase_run_stops_after_until_inclusive() -> None:
    plan = plan_phase_run(_recipe(), completed=(), until="graphics")
    assert tuple(p.name for p in plan) == ("rebuild", "graphics")


def test_plan_phase_run_until_seed_is_empty_plan() -> None:
    plan = plan_phase_run(_recipe(), completed=(), until="seed")
    assert tuple(plan) == ()


def test_plan_phase_run_skip_and_until_combine() -> None:
    plan = plan_phase_run(_recipe(), completed=("rebuild",), until="graphics")
    assert tuple(p.name for p in plan) == ("graphics",)


def test_plan_phase_run_invalid_until_raises_valueerror_listing_names() -> None:
    with pytest.raises(ValueError) as exc:
        plan_phase_run(_recipe(), completed=(), until="bogus")
    msg = str(exc.value)
    # mensagem lista os nomes válidos (seed + phases) para o usuário
    assert "rebuild" in msg
    assert "graphics" in msg
    assert "seed" in msg


# --- 2.2 phase_snapshot_path -------------------------------------------------


def test_phase_snapshot_path_key_composition(tmp_path: Path) -> None:
    path = phase_snapshot_path(
        _recipe(), snapshot="SNAP", phase="rebuild", fork_points_dir=tmp_path
    )
    assert path == tmp_path / "v3-minimal-systemd-SNAP-rebuild.tar"


def test_phase_snapshot_path_distinct_per_phase(tmp_path: Path) -> None:
    a = phase_snapshot_path(_recipe(), snapshot="S", phase="rebuild", fork_points_dir=tmp_path)
    b = phase_snapshot_path(_recipe(), snapshot="S", phase="graphics", fork_points_dir=tmp_path)
    assert a != b


# --- 2.2 latest_resumable ----------------------------------------------------


def test_latest_resumable_none_when_no_tarball(tmp_path: Path) -> None:
    phase, path = latest_resumable(
        _recipe(), snapshot="S", completed=("rebuild", "graphics"), fork_points_dir=tmp_path
    )
    assert phase is None
    assert path is None


def test_latest_resumable_picks_last_completed_with_tarball(tmp_path: Path) -> None:
    # snapshots de rebuild e graphics existem; o último completed com tarball é graphics
    (tmp_path / "v3-minimal-systemd-S-rebuild.tar").write_bytes(b"")
    (tmp_path / "v3-minimal-systemd-S-graphics.tar").write_bytes(b"")
    phase, path = latest_resumable(
        _recipe(), snapshot="S", completed=("rebuild", "graphics"), fork_points_dir=tmp_path
    )
    assert phase == "graphics"
    assert path == tmp_path / "v3-minimal-systemd-S-graphics.tar"


def test_latest_resumable_skips_completed_without_tarball(tmp_path: Path) -> None:
    # só rebuild tem tarball; graphics completed mas sem snapshot → cai para rebuild
    (tmp_path / "v3-minimal-systemd-S-rebuild.tar").write_bytes(b"")
    phase, path = latest_resumable(
        _recipe(), snapshot="S", completed=("rebuild", "graphics"), fork_points_dir=tmp_path
    )
    assert phase == "rebuild"
    assert path == tmp_path / "v3-minimal-systemd-S-rebuild.tar"


# --- 3.1 parse_emerge_plan ---------------------------------------------------

_EMERGE_VERBOSE = """\
These are the packages that would be merged, in order:

Calculating dependencies... done!
[ebuild  N    ] media-libs/libsdl2-2.30.5:0/0::gentoo  USE="X (sound%) -wayland*"
[ebuild  R    ] media-libs/mesa-24.0.7:0/0::gentoo  USE="vulkan"
[ebuild rR    ] sys-apps/dbus-1.14.10:0::gentoo
[ebuild  U    ] sys-apps/portage-3.0.66 [3.0.65]  USE="(rsync-verify%*)"
[blocks B      ] <sys-libs/foo-2 ("<sys-libs/foo-2" is blocking sys-libs/bar-1)

>>> Emerging (1 of 4) media-libs/libsdl2-2.30.5
"""


def test_parse_emerge_plan_extracts_ops_per_atom() -> None:
    entries, blockers = parse_emerge_plan(_EMERGE_VERBOSE)
    by_op = {e.op for e in entries}
    assert {"N", "R", "rR", "U"} <= by_op
    atoms = {e.atom for e in entries}
    assert "media-libs/libsdl2-2.30.5" in atoms
    assert "sys-apps/dbus-1.14.10" in atoms


def test_parse_emerge_plan_captures_use_deltas() -> None:
    entries, _blockers = parse_emerge_plan(_EMERGE_VERBOSE)
    with_use = [e for e in entries if e.use_changes]
    # ao menos um entry carrega deltas de USE (sound% / -wayland* / rsync-verify%*)
    assert with_use, "esperado ao menos um EmergePlanEntry com use_changes"
    joined = " ".join(flag for e in with_use for flag in e.use_changes)
    assert "sound" in joined or "wayland" in joined or "rsync-verify" in joined


def test_parse_emerge_plan_captures_blockers() -> None:
    _entries, blockers = parse_emerge_plan(_EMERGE_VERBOSE)
    assert blockers, "esperado ao menos um blocker da linha [blocks B ...]"
    assert any("foo" in b for b in blockers)


def test_parse_emerge_plan_empty_on_no_merge() -> None:
    entries, blockers = parse_emerge_plan("Nothing to merge; quitting.\n")
    assert entries == ()
    assert blockers == ()


# --- 3.2 compute_phase_diff --------------------------------------------------


def test_compute_phase_diff_flags_unexpected_rebuild() -> None:
    entries = (
        EmergePlanEntry(atom="media-libs/mesa-24.0.7", op="R"),
        EmergePlanEntry(atom="media-video/ffmpeg-6.1.1", op="N"),
    )
    diff = compute_phase_diff("graphics", entries, (), prior_atoms=("media-libs/mesa-24.0.5",))
    # mesa foi construída numa phase anterior (mesma category/PN) e agora rebuild → unexpected
    assert any("mesa" in a for a in diff.unexpected_rebuilds)
    assert diff.phase == "graphics"


def test_compute_phase_diff_clean_phase_has_empty_rebuilds() -> None:
    entries = (EmergePlanEntry(atom="media-video/ffmpeg-6.1.1", op="N"),)
    diff = compute_phase_diff("graphics", entries, (), prior_atoms=())
    assert diff.unexpected_rebuilds == ()
    assert "media-video/ffmpeg-6.1.1" in diff.built


def test_compute_phase_diff_surfaces_use_changes_and_blockers() -> None:
    entries = (EmergePlanEntry(atom="media-libs/libsdl2-2.30.5", op="N", use_changes=("sound",)),)
    diff = compute_phase_diff(
        "graphics", entries, ("<sys-libs/foo-2 blocking bar",), prior_atoms=()
    )
    assert any("sound" in u for u in diff.use_changes)
    assert diff.blockers == ("<sys-libs/foo-2 blocking bar",)


# --- 5.1 enums ---------------------------------------------------------------


def test_checkpoint_decision_members() -> None:
    assert CheckpointDecision.CONTINUE == "CONTINUE"
    assert CheckpointDecision.STOP == "STOP"
    assert CheckpointDecision.SHELL == "SHELL"


def test_failure_decision_members() -> None:
    assert FailureDecision.RETRY == "RETRY"
    assert FailureDecision.ABORT == "ABORT"


# --- 5.2 (unit) routing de run_phases_stepwise (Container monkeypatched) -----


class _FakeContainer:
    """Container falso: registra cada ``emerge`` chamado; ``shell`` é no-op."""

    def __init__(self, *, fail_first: int = 0) -> None:
        self.rootfs = Path("/r")
        self.binds: tuple[Any, ...] = ()
        self.binds_rw: tuple[Any, ...] = ()
        self.emerge_calls: list[list[str]] = []
        self.shell_calls = 0
        self._fail_first = fail_first

    def run(self, argv: Any, **_k: Any) -> Any:
        from shidashi.container import CommandResult

        self.emerge_calls.append(list(argv))
        if len(self.emerge_calls) <= self._fail_first:
            import subprocess

            raise subprocess.CalledProcessError(1, list(argv), output="", stderr="boom")
        return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")

    def shell(self) -> None:
        self.shell_calls += 1


def _stepwise(container: Any, recipe: Any, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    # evita snapshot real + state I/O: monkeypatcha snapshot_fork_point e save_state.
    monkeypatch.setattr(phases, "snapshot_fork_point", lambda *_a, **_k: Path("/snap.tar"))
    import shidashi.state as state_mod

    monkeypatch.setattr(state_mod, "save_state", lambda *_a, **_k: None, raising=False)
    return run_phases_stepwise(
        container,
        recipe,
        emptytree=True,
        completed=(),
        until=None,
        snapshot="S",
        fork_points_dir=Path("/fp"),
        state_path=Path("/state.json"),
        **kw,
    )


def test_stepwise_checkpoint_stop_halts_loop_and_skips_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _FakeContainer()
    seen: list[str] = []

    def on_checkpoint(phase: str, _diff: Any) -> Any:
        seen.append(phase)
        return CheckpointDecision.STOP if phase == "rebuild" else CheckpointDecision.CONTINUE

    results = _stepwise(container, _recipe(), monkeypatch, on_checkpoint=on_checkpoint)
    # parou em rebuild: graphics/apps NÃO rodaram e NENHUM settle (R1.3/R2.3)
    assert seen == ["rebuild"]
    assert all("--newuse" not in c for c in container.emerge_calls)
    ran = [r.phase.name for r in results]
    assert "settle" not in ran
    assert "graphics" not in ran


def test_stepwise_failure_retry_then_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer(fail_first=1)  # 1ª emerge falha, 2ª passa
    failures: list[str] = []

    def on_failure(phase: str, _err: Any) -> Any:
        failures.append(phase)
        return FailureDecision.RETRY

    results = _stepwise(
        container,
        _recipe(phases_=(Phase(name="rebuild"),)),
        monkeypatch,
        on_failure=on_failure,
        on_checkpoint=lambda *_a: CheckpointDecision.CONTINUE,
    )
    # on_failure foi consultado e o emerge da MESMA phase foi re-rodado
    assert failures == ["rebuild"]
    assert container.emerge_calls[0] == container.emerge_calls[1]
    assert any(r.phase.name == "rebuild" for r in results)


def test_stepwise_failure_abort_raises_factory_error(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer(fail_first=99)  # sempre falha
    monkeypatch.setattr(phases, "snapshot_fork_point", lambda *_a, **_k: Path("/snap.tar"))
    import shidashi.state as state_mod

    monkeypatch.setattr(state_mod, "save_state", lambda *_a, **_k: None, raising=False)

    with pytest.raises(phases.FactoryError):
        run_phases_stepwise(
            container,
            _recipe(phases_=(Phase(name="rebuild"),)),
            emptytree=True,
            completed=(),
            until=None,
            snapshot="S",
            fork_points_dir=Path("/fp"),
            state_path=Path("/state.json"),
            on_failure=lambda *_a: FailureDecision.ABORT,
            on_checkpoint=lambda *_a: CheckpointDecision.CONTINUE,
        )


def test_stepwise_settle_runs_only_at_true_final_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer()
    # plano completo, todos CONTINUE → settle deve rodar exatamente uma vez no fim
    results = _stepwise(
        container,
        _recipe(phases_=(Phase(name="rebuild"),)),
        monkeypatch,
        on_checkpoint=lambda *_a: CheckpointDecision.CONTINUE,
    )
    assert results[-1].phase.name == "settle"


# --- INTEGRAÇÃO host-gated (Red DIFERIDO ao host privilegiado real) ----------


@_skip_privileged
def test_run_phases_stepwise_until_rebuild_persists_and_no_settle() -> None:
    # 5.2/5.1 (int): seed+rebuild reais, snapshot por fase escrito, state com
    # completed=("rebuild",), SEM settle, exit 0. Diferido ao host.
    pytest.skip("integração privilegiada: requer rootfs seedado real (Red diferido)")


@_skip_privileged
def test_run_phases_stepwise_resume_restores_and_runs_next() -> None:
    # R5.2 (int): resume restaura o snapshot da última phase completed e roda a próxima.
    pytest.skip("integração privilegiada: requer rootfs seedado real (Red diferido)")
