"""Unit tests of the update's source across pins (story 016, task 3.5, D10).

``update_source(recipe, target, *, snapshot, pins, fork_points_dir)``: the
current pin's image, else the newest older pin of the same snapshot (newest
date, then newest mtime; a later date is ignored) of the same build key, else
``None`` (R8.9, R8.10); a pre-fix key (no pin id, no build key) is never taken.
``Factory.update`` restores it and writes the result under the current pin,
never over the source (R8.11, R8.8).
"""

import os
from pathlib import Path

import pytest

from shidashi import factory
from shidashi.generation import check_or_record, fingerprint
from shidashi.phases import FactoryError, PhaseResult, build_key
from shidashi.recipe import Phase
from tests.test_factory_pins import SNAP, Wired, current_pins, recipe, tarball, wire

NEW = "p20260928.3fa9c2d1"
BK = build_key(recipe())


def _file(d: Path, name: str, mtime: int = 1_700_000_000) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"")
    os.utime(d / name, (mtime, mtime))
    return d / name


def _source(d: Path) -> Path | None:
    return factory.update_source(recipe(), "kde", snapshot=SNAP, pins=NEW, fork_points_dir=d)


# --- hostile: files the update must never take ----------------------------------------------


def test_the_current_key_wins_over_a_newer_mtime_older_pin(tmp_path: Path) -> None:
    current = _file(tmp_path, f"v3-systemd-{SNAP}-{NEW}-{BK}-kde.tar", 1_000)
    _file(tmp_path, f"v3-systemd-{SNAP}-p20260915.0a1b2c3d-{BK}-kde.tar", 9_000)
    assert _source(tmp_path) == current


def test_a_pin_dated_after_the_current_one_is_ignored(tmp_path: Path) -> None:
    older = _file(tmp_path, f"v3-systemd-{SNAP}-p20260915.0a1b2c3d-{BK}-kde.tar")
    _file(tmp_path, f"v3-systemd-{SNAP}-p20261010.0a1b2c3d-{BK}-kde.tar")
    assert _source(tmp_path) == older


@pytest.mark.parametrize(
    "name",
    [
        f"v3-systemd-S2-p20260915.0a1b2c3d-{BK}-kde.tar",  # another snapshot
        f"v3-systemd-{SNAP}-p20260915.0a1b2c3d-{BK}-desktop.tar",  # another stage
        f"v3-systemd-{SNAP}-p20260915.0a1b2c3d-{BK}-plasma-kde.tar",  # a stage ending in -kde
        f"v3-openrc-{SNAP}-p20260915.0a1b2c3d-{BK}-kde.tar",  # another init
        f"znver5-systemd-{SNAP}-p20260915.0a1b2c3d-{BK}-kde.tar",  # another arch
        f".v3-systemd-{SNAP}-p20260915.0a1b2c3d-{BK}-kde.tar.tmp",  # a half-written snapshot
        f"v3-systemd-{SNAP}-p2026091.0a1b2c3d-{BK}-kde.tar",  # not a pin id
        f"v3-systemd-{SNAP}-p20260915.0A1B2C3D-{BK}-kde.tar",
        f"v3-systemd-{SNAP}-kde.tar.tmp",
    ],
)
def test_a_file_of_another_image_or_not_a_restore_point_is_ignored(
    tmp_path: Path, name: str
) -> None:
    _file(tmp_path, name)
    assert _source(tmp_path) is None


# --- the order D10 prescribes ---------------------------------------------------------------


def test_without_the_current_key_the_newest_older_pin_date_wins(tmp_path: Path) -> None:
    _file(tmp_path, f"v3-systemd-{SNAP}-p20260901.0a1b2c3d-{BK}-kde.tar", 9_000)
    newest = _file(tmp_path, f"v3-systemd-{SNAP}-p20260915.0a1b2c3d-{BK}-kde.tar", 1_000)
    _file(tmp_path, f"v3-systemd-{SNAP}-kde.tar", 9_999)
    assert _source(tmp_path) == newest


def test_two_pins_of_one_date_are_ordered_by_mtime(tmp_path: Path) -> None:
    _file(tmp_path, f"v3-systemd-{SNAP}-p20260928.00000000-{BK}-kde.tar", 1_000)
    newer = _file(tmp_path, f"v3-systemd-{SNAP}-p20260928.ffffffff-{BK}-kde.tar", 2_000)
    assert _source(tmp_path) == newer


def test_the_pre_fix_key_is_never_taken(tmp_path: Path) -> None:
    """Nothing proves what an image keyed by neither pin id nor build key was built from."""
    _file(tmp_path, f"v3-systemd-{SNAP}-kde.tar")
    _file(tmp_path, f"v3-systemd-{SNAP}-{BK}-kde.tar")
    _file(tmp_path, f"v3-systemd-{SNAP}-p20260915.0a1b2c3d-kde.tar")
    assert _source(tmp_path) is None


def test_an_older_pins_image_of_another_build_key_is_not_taken(tmp_path: Path) -> None:
    """An image built with other flags is never brought under these ones."""
    other = build_key(recipe().model_copy(update={"common_flags": "-O3"}))
    assert other != BK
    _file(tmp_path, f"v3-systemd-{SNAP}-p20260915.0a1b2c3d-{other}-kde.tar", 9_000)
    assert _source(tmp_path) is None
    same = _file(tmp_path, f"v3-systemd-{SNAP}-p20260901.0a1b2c3d-{BK}-kde.tar", 1_000)
    assert _source(tmp_path) == same


def test_nothing_to_update_is_none(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    assert _source(tmp_path) is None


# --- Factory.update writes under the current pin (R8.11, R8.8) --------------------------------


def _update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Wired, list[str]]:
    w = wire(monkeypatch, tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    check_or_record(w.pkgdir, fingerprint(empty, recipe()))
    restored: list[str] = []
    real_restore = factory._restore_into

    def restore(src: Path, rootfs: Path) -> None:
        restored.append(src.name)
        real_restore(src, rootfs)

    monkeypatch.setattr(factory, "_restore_into", restore)
    monkeypatch.setattr(factory, "attach_packages", lambda *_a, **_k: None)
    monkeypatch.setattr(
        factory,
        "run_update",
        lambda *_a, **_k: PhaseResult(
            phase=Phase(name="update", stage="kde"), built_atoms=(), snapshot=None
        ),
    )
    return w, restored


def test_update_restores_an_older_pins_image_and_writes_the_current_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    w, restored = _update(monkeypatch, tmp_path)
    fps = w.fork_points
    source = tarball(tmp_path, fps / f"v3-systemd-{SNAP}-p20260901.0a1b2c3d-{BK}-kde.tar", "old")
    before = source.read_bytes()
    pins = current_pins(w.seeds)
    result = factory.Factory(recipe(), w.pkgdir).update(download=False)
    assert restored == [source.name]
    assert w.snapshots == [fps / f"v3-systemd-{SNAP}-{pins}-{BK}-kde.tar"]
    assert result.fork_point == fps / f"v3-systemd-{SNAP}-{pins}-{BK}-kde.tar"
    assert source.read_bytes() == before


def test_update_with_no_image_of_any_pin_has_nothing_to_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    w, _restored = _update(monkeypatch, tmp_path)
    w.fork_points.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FactoryError, match="nothing to update"):
        factory.Factory(recipe(), w.pkgdir).update(download=False)
