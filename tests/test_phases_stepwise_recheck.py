"""Unit tests of the re-check hook in the stepwise runner (story 016, task 4.2).

``run_phases_stepwise(..., on_phase_emerged=hook)`` calls the hook right after a
phase's emerge, outside the retry loop, before the after-steps, the persisted
progress and the checkpoint prompt (R4.2). A refusal propagates: the phase is
not persisted as completed (R4.4), the retry prompt never sees it (R4.6), and no
further emerge runs (R4.3). Without a hook the run is as before (R6.10).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shidashi import phases, state
from shidashi.generation import GenerationFingerprint, GenerationMismatchError, check_or_record
from shidashi.phases import CheckpointDecision, FailureDecision
from shidashi.recipe import Phase, ResolvedRecipe

pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

PINS = "p20260928.3fa9c2d1"

#: Taken once: a second _run in one test would otherwise wrap the first run's
#: wrapper, and the first run's events would collect the later runs' saves.
_REAL_SAVE = state.save_state


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="p",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(Phase(name="rebuild", emptytree=True), Phase(name="graphics"), Phase(name="apps")),
        portage_layers=(),
    )


BK = phases.build_key(_recipe())


def _fp(gcc: str) -> GenerationFingerprint:
    return GenerationFingerprint(
        arch="v3",
        profile="p",
        common_flags="-O2",
        chost="x86_64-pc-linux-gnu",
        llvm_slot="22",
        gcc=gcc,
        binutils="2.46.1",
        glibc="2.43",
    )


class _Container:
    def __init__(self, rootfs: Path, events: list[tuple[str, str]]) -> None:
        self.rootfs, self.events = rootfs, events

    def run(self, argv: Any, **_k: Any) -> Any:
        from shidashi.container import CommandResult

        argv = list(argv)
        self.events.append(("emerge" if "--pretend" not in argv else "check", argv[-1]))
        return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")

    def shell(self) -> None:
        pass


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook: Callable[[str], None] | None,
    *,
    pass_hook: bool = True,
    completed: tuple[str, ...] = (),
    on_failure: Any = None,
    events: list[tuple[str, str]] | None = None,
) -> tuple[list[tuple[str, str]], Path]:
    events = events if events is not None else []
    path = tmp_path / "state.json"

    def save(p: Path, s: state.BuildState) -> None:
        events.append(("save", ",".join(s.completed_phases)))
        _REAL_SAVE(p, s)

    def checkpoint(name: str, _diff: Any) -> CheckpointDecision:
        events.append(("checkpoint", name))
        return CheckpointDecision.CONTINUE

    monkeypatch.setattr(state, "save_state", save)
    monkeypatch.setattr(
        phases, "snapshot_fork_point", lambda _r, dest: events.append(("snap", dest.name))
    )
    extra = {"on_phase_emerged": hook} if pass_hook else {}
    phases.run_phases_stepwise(
        _Container(tmp_path / "rootfs", events),  # type: ignore[arg-type]
        _recipe(),
        emptytree=True,
        completed=completed,
        until=None,
        snapshot="S",
        pins=PINS,
        fork_points_dir=tmp_path,
        state_path=path,
        on_checkpoint=checkpoint,
        on_failure=on_failure,
        **extra,
    )
    return events, path


def test_the_hook_runs_after_the_emerge_before_snapshot_save_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    _run(
        tmp_path,
        monkeypatch,
        lambda name: events.append(("hook", name)),
        completed=("rebuild",),
        events=events,
    )
    assert [n for k, n in events if k == "hook"] == ["graphics", "apps"]
    kinds = [k for k, _ in events]
    for i, kind in enumerate(kinds):
        if kind == "emerge":
            assert kinds[i + 1] == "hook", events  # nothing between the emerge and the hook
    for name in ("graphics", "apps"):
        at = events.index(("hook", name))
        assert events.index(("snap", f"v3-minimal-systemd-S-{PINS}-{BK}-{name}.tar")) > at
        assert events.index(("checkpoint", name)) > at
        saves = [i for i, (k, v) in enumerate(events) if k == "save" and v.endswith(name)]
        assert saves and min(saves) > at


def test_without_a_hook_the_run_is_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6.10: omitting the hook, passing None and passing a no-op give one run."""
    runs = [
        _run(tmp_path / "a", monkeypatch, None, pass_hook=False)[0],
        _run(tmp_path / "b", monkeypatch, None)[0],
        _run(tmp_path / "c", monkeypatch, lambda _name: None)[0],
    ]
    renamed = [[(k, v.replace(str(tmp_path), "")) for k, v in run] for run in runs]
    assert renamed[0] == renamed[1] == renamed[2]


def test_a_refused_recheck_persists_only_the_earlier_phases_and_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = tmp_path / "pkgdir"
    check_or_record(pkgdir, _fp("16.2.0"))
    failures: list[str] = []

    def on_failure(name: str, _err: Exception) -> FailureDecision:
        failures.append(name)
        return FailureDecision.RETRY

    def hook(name: str) -> None:
        if name == "graphics":
            check_or_record(pkgdir, _fp("17.1.0"), after_phase=name)

    events: list[tuple[str, str]] = []
    with pytest.raises(GenerationMismatchError) as err:
        _run(tmp_path, monkeypatch, hook, on_failure=on_failure, events=events)
    assert err.value.after_phase == "graphics"
    assert failures == []  # R4.6: never offered to the retry prompt
    saved = state.load_state(tmp_path / "state.json")
    assert saved is not None and saved.completed_phases == ("rebuild",)  # R4.4
    emerges = [n for k, n in events if k == "emerge"]
    # rebuild once; graphics and apps carry no target, so they emerge nothing --
    # what R4.3 asks is that nothing of apps runs after the refusal
    assert len(emerges) == 1
    assert not any(v.endswith("apps") or v.endswith("apps.tar") for _k, v in events)
    assert ("checkpoint", "graphics") not in events
    assert ("snap", f"v3-minimal-systemd-S-{PINS}-{BK}-graphics.tar") not in events
