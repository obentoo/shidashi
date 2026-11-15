"""The ``perl-rebuild`` step AFTER a stage's emerge: the fork point is saved clean.

The base upgrades perl inside its own emerge (``--emptytree @world``), so the
step before the emerge finds nothing there; the stage3's build-only modules
turn stale during it. Until 2026-11-15 the base's fork point was saved in that
state and every stage restored from it rebuilt ~20 dev-perl from source on
every run (2m34 per toolbox run on the build host).
"""

import shutil
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, phases
from shidashi.container import CommandResult
from shidashi.recipe import Phase, ResolvedRecipe
from tests.test_phases import _perl_rootfs, _PerlRebuildContainer, _recipe

_GETTEXT = "dev-perl/Locale-gettext-1.70.0_p20181130"
_OLD_DIR = "/usr/lib64/perl5/vendor_perl/5.42"
_REPAIR = ["emerge", "--oneshot", "--verbose", "--usepkg=n", f"={_GETTEXT}"]


def _stage3_perl_rootfs(root: Path) -> Path:
    """A stage3 as the base finds it: perl 5.42 installed, Locale-gettext in its
    directory -- nothing is stale until an emerge upgrades perl."""
    _perl_rootfs(root)
    vdb = root / "var/db/pkg/dev-lang"
    (vdb / "perl-5.44.0").rename(vdb / "perl-5.42.0")
    shutil.rmtree(root / "usr/lib64/perl5/5.44")
    shutil.rmtree(root / "usr/lib64/perl5/vendor_perl/5.44")
    return root


class _PerlUpgradingStage(_PerlRebuildContainer):
    """The first stage emerge (``@world``) upgrades perl 5.42 → 5.44 and leaves
    Locale-gettext, which the world does not hold, where 5.42 kept it."""

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> Any:
        if argv[0] == "emerge" and "@world" in argv:
            self.calls.append(list(argv))
            vdb = self.rootfs / "var/db/pkg/dev-lang"
            if (vdb / "perl-5.42.0").is_dir():
                (vdb / "perl-5.42.0").rename(vdb / "perl-5.44.0")
                (self.rootfs / "usr/lib64/perl5/5.44").mkdir()
                (self.rootfs / "usr/lib64/perl5/vendor_perl/5.44").mkdir()
            return CommandResult(0, "[ebuild     U ] dev-lang/perl-5.44.0\n", "")
        return super().run(argv, env=env, check=check)


def _base_then_toolbox() -> ResolvedRecipe:
    recipe: ResolvedRecipe = _recipe(
        flavor="toolbox",
        phases=(
            Phase(name="base", stage="base", sets=("base",), emptytree=True),
            Phase(name="toolbox", stage="toolbox", sets=("toolbox",)),
        ),
    )
    return recipe


def _stale_at_each_snapshot(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """What :func:`phases.stale_perl_dirs` says of the rootfs each fork point is written from."""
    seen: list[tuple[str, ...]] = []

    def snapshot(root: Path, dest: Path) -> Path:
        seen.append(phases.stale_perl_dirs(root))
        return dest

    monkeypatch.setattr(phases, "snapshot_fork_point", snapshot)
    return seen


def _stage_steps(trail: audit.Run, stage: str) -> list[dict[str, Any]]:
    manifest = audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))
    prefix = f"stage:{stage}/"
    return [s for s in manifest["steps"] if str(s["step"]).startswith(prefix)]


def _emerges(container: _PerlRebuildContainer) -> list[list[str]]:
    return [call for call in container.calls if call[0] == "emerge"]


def test_perl_upgraded_by_the_stage_emerge_is_repaired_before_the_fork_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _stage3_perl_rootfs(tmp_path / "rootfs")
    assert phases.stale_perl_dirs(root) == ()  # the step before the emerge has nothing to do
    container = _PerlUpgradingStage(root)
    at_snapshot = _stale_at_each_snapshot(monkeypatch)
    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        phases.run_phases(
            container,  # type: ignore[arg-type]
            _base_then_toolbox(),
            emptytree=True,
            snapshot="S",
            pins="P",
            fork_points_dir=tmp_path,
        )
    # the base: its emerge, then the repair; the toolbox: its emerge only
    base, repair, toolbox = _emerges(container)
    assert "--emptytree" in base and "@world" in base
    assert repair == _REPAIR
    assert "@toolbox" in toolbox
    assert container.calls.index(repair) == container.calls.index(base) + 1
    assert at_snapshot == [(), ()]  # neither fork point keeps the old perl's directory
    assert not (root / _OLD_DIR.lstrip("/")).exists()

    steps = _stage_steps(trail, "base")
    names = [s["step"].removeprefix("stage:base/") for s in steps]
    before, after = (i for i, name in enumerate(names) if name == "perl-rebuild")
    assert before < names.index("emerge-stage") == after - 1 < names.index("fork-point")
    assert steps[before]["stale"] == [] and "rebuilt" not in steps[before]
    assert steps[after]["stale"] == [_OLD_DIR]
    assert steps[after]["rebuilt"] == [_GETTEXT]
    # the stage restored from that fork point: both steps recorded, neither rebuilds
    derived = [s for s in _stage_steps(trail, "toolbox") if s["step"].endswith("/perl-rebuild")]
    assert [(s["status"], s["stale"], "rebuilt" in s) for s in derived] == [("ok", [], False)] * 2


def test_the_stepwise_run_repairs_perl_before_the_phase_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _stage3_perl_rootfs(tmp_path / "rootfs")
    container = _PerlUpgradingStage(root)
    at_snapshot = _stale_at_each_snapshot(monkeypatch)
    phases.run_phases_stepwise(
        container,  # type: ignore[arg-type]
        _base_then_toolbox(),
        emptytree=True,
        completed=(),
        until="base",
        snapshot="S",
        pins="P",
        fork_points_dir=tmp_path,
        state_path=tmp_path / "state.json",
    )
    base, repair = _emerges(container)
    assert "@world" in base
    assert repair == _REPAIR
    assert at_snapshot == [()]


def test_with_nothing_stale_the_step_after_the_emerge_is_a_recorded_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _perl_rootfs(tmp_path / "rootfs")
    shutil.rmtree(root / _OLD_DIR.lstrip("/"))  # perl 5.44 and only its own directories
    container = _PerlUpgradingStage(root)
    at_snapshot = _stale_at_each_snapshot(monkeypatch)
    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        phases.run_phases(
            container,  # type: ignore[arg-type]
            _base_then_toolbox(),
            emptytree=True,
            snapshot="S",
            pins="P",
            fork_points_dir=tmp_path,
            stop_after="base",
        )
    assert [call[0] for call in container.calls] == ["emerge"]  # the stage's, nothing else
    assert at_snapshot == [()]
    steps = _stage_steps(trail, "base")
    names = [s["step"].removeprefix("stage:base/") for s in steps]
    assert names[names.index("emerge-stage") + 1] == "perl-rebuild"
    recorded = [s for s in steps if s["step"].endswith("/perl-rebuild")]
    assert [(s["status"], s["stale"], "rebuilt" in s) for s in recorded] == [("ok", [], False)] * 2
