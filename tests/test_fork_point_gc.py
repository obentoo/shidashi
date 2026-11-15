"""``shidashi.fork_point_gc``: which restore points a gc deletes, and the deletion itself.

The names are the real shapes (OVERVIEW §6): stage fork points, the bootstrap checkpoint
and the update's images (``<arch>-<init>-<stage3>-<pin>-<build key>-<what>.tar``), and the
per-phase snapshots, which carry the flavor too (``<arch>-<flavor>-<init>-...``).
"""

import os
from pathlib import Path

import pytest

from shidashi.fork_point_gc import (
    ForkPointFile,
    delete_fork_points,
    fork_point_pin,
    plan_fork_point_gc,
    scan_fork_points,
)

STAGE3 = "20260823T153057Z"
BUILD_KEY = "b392fdb91"
CURRENT = "p20261106.c059ece9"
PREVIOUS = "p20261101.28ad071a"
OLDEST = "p20260928.60cfe9d6"


def restore_point_name(
    pin: str, what: str = "kde", *, arch: str = "v3", init: str = "systemd"
) -> str:
    return f"{arch}-{init}-{STAGE3}-{pin}-{BUILD_KEY}-{what}.tar"


def per_phase_name(pin: str, phase: str = "desktop") -> str:
    return f"v3-kde-systemd-{STAGE3}-{pin}-{BUILD_KEY}-{phase}.tar"


def pinned_files(*pins: str) -> list[ForkPointFile]:
    """Two restore points per pin, in the order given."""
    return [
        ForkPointFile(Path(restore_point_name(pin, what)), pin, pin[1:9], 10)
        for pin in pins
        for what in ("desktop", "kde")
    ]


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(restore_point_name(CURRENT), id="stage"),
        pytest.param(restore_point_name(CURRENT, "bootstrap"), id="bootstrap"),
        pytest.param(restore_point_name(CURRENT, init="openrc"), id="other-init"),
        pytest.param(per_phase_name(CURRENT), id="per-phase"),
    ],
)
def test_every_restore_point_shape_parses_to_its_pin(name: str) -> None:
    assert fork_point_pin(name, "v3") == (CURRENT, "20261106")


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(restore_point_name(CURRENT, arch="znver5"), id="another-arch"),
        pytest.param(restore_point_name(CURRENT, arch="v3x"), id="an-arch-it-prefixes"),
        pytest.param(f".{restore_point_name(CURRENT)}.tmp", id="tarball-being-written"),
        pytest.param(restore_point_name(CURRENT).removesuffix(".tar"), id="no-tar-suffix"),
        pytest.param(f"v3-systemd-{STAGE3}-{CURRENT}-kde.tar", id="no-build-key"),
        pytest.param(f"v3-systemd-{STAGE3}-p2026110.c059ece9-{BUILD_KEY}-kde.tar", id="short-date"),
        pytest.param("v3.owner.json", id="unrelated"),
    ],
)
def test_any_other_name_is_no_restore_point(name: str) -> None:
    """Hostile half: what the gc cannot parse it never deletes."""
    assert fork_point_pin(name, "v3") is None


def test_the_current_pin_and_the_newest_other_one_stay() -> None:
    plan = plan_fork_point_gc(pinned_files(OLDEST, PREVIOUS, CURRENT), current_pin=CURRENT, keep=1)
    assert plan.kept_pins == (CURRENT, PREVIOUS)
    assert {f.pin for f in plan.delete} == {OLDEST}
    assert {f.pin for f in plan.keep} == {CURRENT, PREVIOUS}
    assert len(plan.delete) + len(plan.keep) == 6


def test_keep_zero_deletes_every_other_pin_and_never_the_current_one() -> None:
    plan = plan_fork_point_gc(pinned_files(OLDEST, PREVIOUS, CURRENT), current_pin=CURRENT, keep=0)
    assert plan.kept_pins == (CURRENT,)
    assert {f.pin for f in plan.delete} == {OLDEST, PREVIOUS}
    assert {f.pin for f in plan.keep} == {CURRENT}


def test_a_keep_beyond_the_pins_on_disk_deletes_nothing() -> None:
    plan = plan_fork_point_gc(pinned_files(OLDEST, CURRENT), current_pin=CURRENT, keep=5)
    assert plan.delete == ()
    assert plan.kept_pins == (CURRENT, OLDEST)


def test_a_current_pin_without_restore_points_still_keeps_the_newest_other() -> None:
    """Right after a re-pin, before its first build: the previous pin's images are what the
    update and a revert restore."""
    plan = plan_fork_point_gc(pinned_files(OLDEST, PREVIOUS), current_pin=CURRENT, keep=1)
    assert plan.kept_pins == (CURRENT, PREVIOUS)
    assert {f.pin for f in plan.delete} == {OLDEST}


def test_newest_is_the_pin_date_not_the_order_on_disk() -> None:
    plan = plan_fork_point_gc(
        pinned_files(PREVIOUS, OLDEST, CURRENT)[::-1], current_pin=CURRENT, keep=1
    )
    assert plan.kept_pins == (CURRENT, PREVIOUS)


def test_two_pins_of_one_date_keep_the_same_one_every_time() -> None:
    low, high = "p20261101.00000000", "p20261101.ffffffff"
    first = plan_fork_point_gc(pinned_files(low, high, CURRENT), current_pin=CURRENT, keep=1)
    second = plan_fork_point_gc(pinned_files(high, low, CURRENT), current_pin=CURRENT, keep=1)
    assert first.kept_pins == second.kept_pins == (CURRENT, high)


def test_a_newer_pin_left_by_a_revert_is_the_newest_other() -> None:
    newer = "p20261113.0badc0de"
    plan = plan_fork_point_gc(pinned_files(OLDEST, PREVIOUS, newer), current_pin=PREVIOUS, keep=1)
    assert plan.kept_pins == (PREVIOUS, newer)
    assert {f.pin for f in plan.delete} == {OLDEST}


def test_a_negative_keep_is_refused() -> None:
    with pytest.raises(ValueError, match="keep"):
        plan_fork_point_gc(pinned_files(CURRENT), current_pin=CURRENT, keep=-1)


def test_the_scan_reads_only_the_arch_s_regular_restore_points(tmp_path: Path) -> None:
    mine = [restore_point_name(OLDEST), per_phase_name(CURRENT)]
    others = [
        restore_point_name(OLDEST, arch="znver5"),
        f".{restore_point_name(CURRENT)}.tmp",
        "v3.owner.json",
    ]
    for name in [*mine, *others]:
        (tmp_path / name).write_bytes(b"abc")
    (tmp_path / restore_point_name(PREVIOUS)).mkdir()  # a directory never passes for one
    (tmp_path / restore_point_name(CURRENT, "gnome")).symlink_to(tmp_path / mine[0])  # nor a link
    found = scan_fork_points(tmp_path, "v3")
    assert sorted(f.path.name for f in found) == sorted(mine)
    assert all(f.size == 3 for f in found)


def test_the_scan_of_a_missing_directory_finds_nothing(tmp_path: Path) -> None:
    assert scan_fork_points(tmp_path / "absent", "v3") == []


def test_delete_removes_exactly_the_planned_files(tmp_path: Path) -> None:
    for name in (restore_point_name(OLDEST), restore_point_name(CURRENT)):
        (tmp_path / name).write_bytes(b"x")
    plan = plan_fork_point_gc(scan_fork_points(tmp_path, "v3"), current_pin=CURRENT, keep=0)
    deleted, failures = delete_fork_points(plan.delete)
    assert failures == []
    assert [f.path.name for f in deleted] == [restore_point_name(OLDEST)]
    assert sorted(p.name for p in tmp_path.iterdir()) == [restore_point_name(CURRENT)]


def test_a_file_already_gone_counts_as_deleted(tmp_path: Path) -> None:
    gone = ForkPointFile(tmp_path / restore_point_name(OLDEST), OLDEST, "20260928", 1)
    assert delete_fork_points([gone]) == ([gone], [])


@pytest.mark.skipif(os.geteuid() == 0, reason="root deletes from a read-only directory")
def test_a_failed_deletion_is_named_and_the_others_still_go(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    stuck = locked / restore_point_name(OLDEST)
    stuck.write_bytes(b"x")
    loose = tmp_path / restore_point_name(OLDEST, "desktop")
    loose.write_bytes(b"x")
    locked.chmod(0o555)
    try:
        deleted, failures = delete_fork_points(
            [
                ForkPointFile(stuck, OLDEST, "20260928", 1),
                ForkPointFile(loose, OLDEST, "20260928", 1),
            ]
        )
    finally:
        locked.chmod(0o755)
    assert [f.path for f in deleted] == [loose]
    assert len(failures) == 1 and str(stuck) in failures[0]
    assert stuck.exists() and not loose.exists()
