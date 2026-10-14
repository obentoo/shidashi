"""UNIT tests of the failure shell in shidashi.phases (story 004, sub-task 8.1, v2).

R3.1: IF a phase's emerge returns non-zero under --step THEN the failure hook is
handed a way to open an interactive shell in the LIVE rootfs (the container the
build is running in), before it decides retry/abort. R3.2: RETRY re-runs the same
phase, and a second failure offers the shell again. R3.3: ABORT raises. R3.4: no
skip -- a failed phase never lets a later phase run.

Contract (design.md v2): ``FailureHook = Callable[[str, Exception,
Callable[[], None]], FailureDecision]``; ``run_phases_stepwise`` calls the hook
with ``(phase_name, err, shell)`` on every failure, where ``shell()`` opens the
shell of the SAME container passed to ``run_phases_stepwise``.

Container, snapshot and state I/O are faked (same idioms as
tests/test_phases_stepwise.py); no root, no nspawn.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from shidashi import phases
from shidashi.phases import CheckpointDecision, FailureDecision, run_phases_stepwise
from shidashi.recipe import Phase, ResolvedRecipe

# the binpkg check of a shipped stage extracts the stage3's vdb: stubbed here
pytestmark = pytest.mark.usefixtures("no_stage3_vdb")


def _recipe(*phases_: Phase) -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=phases_ or (Phase(name="rebuild", emptytree=True),),
        portage_layers=("base", "arch/v3", "flavor/minimal", "init/systemd"),
    )


class _LiveContainer:
    """The live build container: records every emerge and every shell opened.

    ``shell()`` notes how many emerges had run when it opened, so a test can
    tell whether the shell came after the failure and before the retry.
    """

    def __init__(self, *, fail_first: int = 0) -> None:
        self.rootfs = Path("/live-rootfs")
        self.binds: tuple[Any, ...] = ()
        self.binds_rw: tuple[Any, ...] = ()
        self.emerge_calls: list[list[str]] = []
        self.shells_opened_after: list[int] = []
        self._fail_first = fail_first

    def run(self, argv: Any, **_k: Any) -> Any:
        from shidashi.container import CommandResult

        self.emerge_calls.append(list(argv))
        if len(self.emerge_calls) <= self._fail_first:
            raise subprocess.CalledProcessError(
                1, list(argv), output="!!! sentinel emerge failure", stderr="boom"
            )
        return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")

    def shell(self) -> None:
        self.shells_opened_after.append(len(self.emerge_calls))


@pytest.fixture
def no_fresh_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell in a FRESH container would land in an empty rootfs, not the live one."""

    def _refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the failure shell must use the live container, not a new one")

    monkeypatch.setattr(phases, "Container", _refuse)


def _stepwise(container: Any, recipe: Any, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
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
        pins="P",
        fork_points_dir=Path("/fp"),
        state_path=Path("/state.json"),
        on_checkpoint=lambda *_a: CheckpointDecision.CONTINUE,
        **kw,
    )


@pytest.mark.usefixtures("no_fresh_container")
def test_failure_hook_third_argument_opens_the_live_container_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _LiveContainer(fail_first=1)  # 1st emerge fails, the retry passes
    calls: list[tuple[str, Exception]] = []

    def on_failure(phase: str, err: Exception, shell: Any) -> Any:
        calls.append((phase, err))
        assert callable(shell)
        shell()
        return FailureDecision.RETRY

    _stepwise(container, _recipe(), monkeypatch, on_failure=on_failure)

    assert [p for p, _ in calls] == ["rebuild"]
    assert isinstance(calls[0][1], Exception)
    # the shell opened in the SAME container the build ran in, once, after the
    # failed emerge and before the retry emerge (R3.1)
    assert container.shells_opened_after == [1]
    assert len(container.emerge_calls) == 2


@pytest.mark.usefixtures("no_fresh_container")
def test_retry_then_second_failure_offers_the_shell_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _LiveContainer(fail_first=2)  # two failures, then success
    seen: list[str] = []

    def on_failure(phase: str, _err: Exception, shell: Any) -> Any:
        seen.append(phase)
        shell()
        return FailureDecision.RETRY

    results = _stepwise(container, _recipe(), monkeypatch, on_failure=on_failure)

    # every failure reaches the hook, and every time it can open the live shell (R3.2)
    assert seen == ["rebuild", "rebuild"]
    assert container.shells_opened_after == [1, 2]
    # RETRY re-ran the SAME phase with the same argv
    assert len(container.emerge_calls) == 3
    assert container.emerge_calls[0] == container.emerge_calls[1] == container.emerge_calls[2]
    assert [r.phase.name for r in results] == ["rebuild"]


@pytest.mark.usefixtures("no_fresh_container")
def test_the_build_layer_never_opens_the_shell_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the hook decides whether the shell opens: a hook that does not call it
    # gets no shell (otherwise the user would get it twice under the CLI hook)
    container = _LiveContainer(fail_first=1)

    def on_failure(_phase: str, _err: Exception, _shell: Any) -> Any:
        return FailureDecision.RETRY

    _stepwise(container, _recipe(), monkeypatch, on_failure=on_failure)

    assert container.shells_opened_after == []
    assert len(container.emerge_calls) == 2


@pytest.mark.usefixtures("no_fresh_container")
def test_abort_after_the_shell_raises_and_never_skips_to_the_next_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _LiveContainer(fail_first=99)  # always fails

    def on_failure(_phase: str, _err: Exception, shell: Any) -> Any:
        shell()
        return FailureDecision.ABORT

    recipe = _recipe(Phase(name="rebuild", emptytree=True), Phase(name="graphics"))
    with pytest.raises(phases.FactoryError):
        _stepwise(container, recipe, monkeypatch, on_failure=on_failure)

    # one failed emerge, one shell, no retry and no later phase (R3.3/R3.4)
    assert container.shells_opened_after == [1]
    assert len(container.emerge_calls) == 1
