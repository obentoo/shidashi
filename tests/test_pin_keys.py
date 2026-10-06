"""Unit tests of the restore-point keys of phases and toolbox under the pins
(story 016, task 3.2, R8.3, R8.4, R8.5, R6.11).

Every stage, toolbox and per-phase key carries the pin id after the stage3
snapshot; every probe sees only the current pin id's files.
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import phases, toolbox
from shidashi.recipe import Phase, ResolvedRecipe

pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

NEW, OLD = "p20260928.3fa9c2d1", "p20260901.0a1b2c3d"


def _recipe(flavor: str = "kde", chain: tuple[Phase, ...] | None = None) -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor=flavor,
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=chain
        if chain is not None
        else (
            Phase(name="base", stage="base", emptytree=True),
            Phase(name="minimal", stage="minimal"),
            Phase(name="desktop", stage="desktop"),
            Phase(name="flavor", stage=flavor, ships=False),
        ),
        portage_layers=(),
    )


def _touch(d: Path, name: str) -> Path:
    (d / name).write_bytes(b"")
    return d / name


# --- the keys ---------------------------------------------------------------------------


def test_the_keys_carry_the_pin_id_after_the_snapshot(tmp_path: Path) -> None:
    r = _recipe()
    stage = phases.stage_fork_point_path(
        r, "base", snapshot="S", pins=NEW, fork_points_dir=tmp_path
    )
    phase = phases.phase_snapshot_path(
        r, snapshot="S", pins=NEW, phase="base", fork_points_dir=tmp_path
    )
    box = toolbox.tarball_path(r, snapshot="S", pins=NEW, fork_points_dir=tmp_path)
    assert stage == tmp_path / f"v3-systemd-S-{NEW}-base.tar"
    assert phase == tmp_path / f"v3-kde-systemd-S-{NEW}-base.tar"
    assert box == tmp_path / f"v3-systemd-S-{NEW}-toolbox.tar"


def test_two_pin_ids_give_two_keys(tmp_path: Path) -> None:
    r = _recipe()
    for pins_a, pins_b in ((NEW, OLD), (NEW, "p20260928.3fa9c2d2")):
        assert phases.stage_fork_point_path(
            r, "base", snapshot="S", pins=pins_a, fork_points_dir=tmp_path
        ) != phases.stage_fork_point_path(
            r, "base", snapshot="S", pins=pins_b, fork_points_dir=tmp_path
        )
        assert phases.phase_snapshot_path(
            r, snapshot="S", pins=pins_a, phase="base", fork_points_dir=tmp_path
        ) != phases.phase_snapshot_path(
            r, snapshot="S", pins=pins_b, phase="base", fork_points_dir=tmp_path
        )
        assert toolbox.tarball_path(r, snapshot="S", pins=pins_a, fork_points_dir=tmp_path) != (
            toolbox.tarball_path(r, snapshot="S", pins=pins_b, fork_points_dir=tmp_path)
        )


# --- fork_point and latest_resumable see only the current pin (R8.5, R6.11) ---------------


def test_fork_point_ignores_older_pins_and_pre_fix_keys(tmp_path: Path) -> None:
    """Hostile: a DEEPER stage of an old pin (and of the pre-fix key) is on disk."""
    _touch(tmp_path, f"v3-systemd-S-{OLD}-desktop.tar")
    _touch(tmp_path, "v3-systemd-S-desktop.tar")
    assert phases.fork_point(_recipe(), snapshot="S", pins=NEW, fork_points_dir=tmp_path) is None
    _touch(tmp_path, f"v3-systemd-S-{NEW}-base.tar")
    found = phases.fork_point(_recipe(), snapshot="S", pins=NEW, fork_points_dir=tmp_path)
    assert found is not None
    assert (found[0].name, found[1].name) == ("base", f"v3-systemd-S-{NEW}-base.tar")


def test_latest_resumable_ignores_older_pins_and_pre_fix_keys(tmp_path: Path) -> None:
    done = ("base", "minimal")
    _touch(tmp_path, f"v3-kde-systemd-S-{OLD}-minimal.tar")
    _touch(tmp_path, "v3-kde-systemd-S-minimal.tar")
    r = _recipe()
    assert phases.latest_resumable(
        r, snapshot="S", pins=NEW, completed=done, fork_points_dir=tmp_path
    ) == (None, None)
    current = _touch(tmp_path, f"v3-kde-systemd-S-{NEW}-base.tar")
    assert phases.latest_resumable(
        r, snapshot="S", pins=NEW, completed=done, fork_points_dir=tmp_path
    ) == ("base", current)


# --- the runners write under the given pins -----------------------------------------------


class _Container:
    def __init__(self, rootfs: Path) -> None:
        self.rootfs = rootfs

    def run(self, argv: Any, **_k: Any) -> Any:
        from shidashi.container import CommandResult

        return CommandResult(0, "[ebuild  N    ] cat/pkg-1\n", "")

    def shell(self) -> None:
        pass


def _record_snapshots(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    snaps: list[str] = []

    def snapshot(_root: Path, dest: Path) -> Path:
        snaps.append(dest.name)
        return dest

    monkeypatch.setattr(phases, "snapshot_fork_point", snapshot)
    return snaps


def test_run_phases_writes_stage_fork_points_under_the_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snaps = _record_snapshots(monkeypatch)
    phases.run_phases(
        _Container(tmp_path / "rootfs"),  # type: ignore[arg-type]
        _recipe(),
        emptytree=True,
        snapshot="S",
        pins=NEW,
        fork_points_dir=tmp_path,
        stop_after="minimal",
    )
    assert snaps == [f"v3-systemd-S-{NEW}-base.tar", f"v3-systemd-S-{NEW}-minimal.tar"]


def test_run_phases_stepwise_writes_phase_snapshots_under_the_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snaps = _record_snapshots(monkeypatch)
    chain = (Phase(name="rebuild", emptytree=True), Phase(name="graphics"))
    phases.run_phases_stepwise(
        _Container(tmp_path / "rootfs"),  # type: ignore[arg-type]
        _recipe("minimal", chain),
        emptytree=True,
        completed=(),
        until=None,
        snapshot="S",
        pins=NEW,
        fork_points_dir=tmp_path,
        state_path=tmp_path / "state.json",
    )
    assert snaps == [
        f"v3-minimal-systemd-S-{NEW}-rebuild.tar",
        f"v3-minimal-systemd-S-{NEW}-graphics.tar",
    ]
