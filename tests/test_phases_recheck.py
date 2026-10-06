"""Unit tests of the re-check hook in the one-shot runner (story 016, task 4.1).

``run_phases(..., on_phase_emerged=hook)`` calls ``hook(phase.name)`` right after
each phase's emerge, before that phase's after-steps (module rebuild, settle,
fork point, binpkg check); an exception from it propagates and nothing else runs
(R4.1, R4.3). Without a hook the run is as before (R6.10).

The refusing hook is the real thing: ``check_or_record`` over a PKGDIR recorded
at gcc 16, fed a gcc 17 fingerprint.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shidashi import phases
from shidashi.generation import GenerationFingerprint, GenerationMismatchError, check_or_record
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak

pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

PINS = "p20260928.3fa9c2d1"


def _recipe() -> ResolvedRecipe:
    cut = UseBreak(atom="dev-lang/python", flag="bluetooth", enable=False)
    return ResolvedRecipe(
        arch="v3",
        flavor="kde",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=("base", "extra-system", "gpu", "kde"),
        phases=(
            Phase(name="base", stage="base", sets=("base",), emptytree=True, use_break=(cut,)),
            Phase(name="minimal", stage="minimal", sets=("extra-system",), ships=True),
            Phase(name="desktop", stage="desktop", sets=("gpu",)),
            Phase(name="flavor", stage="kde", sets=("kde",), ships=True),
        ),
        portage_layers=(),
    )


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


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []


class _Container:
    def __init__(self, rootfs: Path, log: _Log) -> None:
        self.rootfs, self.log = rootfs, log

    def run(self, argv: Any, **_k: Any) -> Any:
        from shidashi.container import CommandResult

        argv = list(argv)
        if "@world" in argv and "--pretend" not in argv:
            kind = "emerge"
        elif "--pretend" in argv:
            kind = "check"
        else:
            kind = "other"
        self.log.events.append((kind, " ".join(argv)))
        return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook: Callable[[str], None] | None = None,
    *,
    pass_hook: bool = True,
    log: _Log | None = None,
    **kw: Any,
) -> _Log:
    log = log if log is not None else _Log()
    events = log.events
    monkeypatch.setattr(
        phases, "snapshot_fork_point", lambda _r, dest: events.append(("snap", dest.name))
    )
    rootfs = tmp_path / "rootfs"
    (rootfs / "var/db/pkg/dev-lang/python-3.14.7").mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {"on_phase_emerged": hook} if pass_hook else {}
    phases.run_phases(
        _Container(rootfs, log),  # type: ignore[arg-type]
        _recipe(),
        emptytree=True,
        snapshot="S",
        pins=PINS,
        fork_points_dir=tmp_path,
        **extra,
        **kw,
    )
    return log


def _run_with_recording_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> _Log:
    log = _Log()
    return _run(
        tmp_path, monkeypatch, lambda name: log.events.append(("hook", name)), log=log, **kw
    )


def test_the_hook_runs_right_after_each_emerge_before_any_after_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _run_with_recording_hook(tmp_path, monkeypatch)
    hooks = [name for kind, name in log.events if kind == "hook"]
    assert hooks == ["base", "minimal", "desktop", "flavor"]
    kinds = [kind for kind, _ in log.events]
    for i, kind in enumerate(kinds):
        if kind == "emerge":
            assert kinds[i + 1] == "hook", log.events  # nothing between emerge and hook


@pytest.mark.parametrize(
    ("kw", "expected"),
    [({"resume_at": "desktop"}, ["flavor"]), ({"stop_after": "minimal"}, ["base", "minimal"])],
)
def test_the_hook_runs_only_for_the_phases_that_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kw: dict[str, str], expected: list[str]
) -> None:
    log = _run_with_recording_hook(tmp_path, monkeypatch, **kw)
    assert [name for kind, name in log.events if kind == "hook"] == expected


def test_a_refused_recheck_stops_the_run_before_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """minimal ships: its settle, fork point and binpkg check must not run, nor desktop."""
    pkgdir = tmp_path / "pkgdir"
    check_or_record(pkgdir, _fp("16.2.0"))

    def hook(name: str) -> None:
        if name == "minimal":
            check_or_record(pkgdir, _fp("17.1.0"), after_phase=name)

    log = _Log()
    monkeypatch.setattr(
        phases, "snapshot_fork_point", lambda _r, dest: log.events.append(("snap", dest.name))
    )
    rootfs = tmp_path / "rootfs"
    (rootfs / "var/db/pkg/dev-lang/python-3.14.7").mkdir(parents=True)
    with pytest.raises(GenerationMismatchError) as err:
        phases.run_phases(
            _Container(rootfs, log),  # type: ignore[arg-type]
            _recipe(),
            emptytree=True,
            snapshot="S",
            pins=PINS,
            fork_points_dir=tmp_path,
            on_phase_emerged=hook,
        )
    assert err.value.after_phase == "minimal"
    kinds = [kind for kind, _ in log.events]
    assert kinds.count("emerge") == 2  # base and minimal, never desktop
    last_emerge = max(i for i, k in enumerate(kinds) if k == "emerge")
    assert log.events[last_emerge + 1 :] == []  # no settle, snapshot or check after it
    assert ("snap", f"v3-systemd-S-{PINS}-minimal.tar") not in log.events


def test_without_a_hook_the_run_is_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6.10: omitting the hook, passing None and passing a no-op give one run."""
    runs = [
        _run(tmp_path / "a", monkeypatch, pass_hook=False).events,
        _run(tmp_path / "b", monkeypatch, None).events,
        _run(tmp_path / "c", monkeypatch, lambda _name: None).events,
    ]
    assert runs[0] == runs[1] == runs[2]
    assert [k for k, _ in runs[0]].count("emerge") == 4
