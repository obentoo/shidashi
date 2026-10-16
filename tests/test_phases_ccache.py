"""The stage emerge records what it did to the shared ccache (hit rate, audit).

Measured 2026-10-13: 10.85% hits over 466k compiles. The per-stage difference
is what tells whether CCACHE_COMPILERCHECK/CCACHE_BASEDIR changed that.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, phases
from shidashi.container import CommandResult
from tests.test_phases import _chain_recipe, _RecordingContainer

#: Trimmed from a real ``ccache --print-stats`` (ccache 4.14).
_STATS = (
    "stats_updated_timestamp\t1791480514\n"
    "autoconf_test\t39845\n"
    "cache_miss\t{miss}\n"
    "direct_cache_hit\t{direct}\n"
    "preprocessed_cache_hit\t{pre}\n"
)


def test_parse_reads_only_the_hit_and_miss_counters() -> None:
    text = _STATS.format(miss=415481, direct=25585, pre=25000)
    assert phases.parse_ccache_stats(text) == {
        "cache_miss": 415481,
        "direct_cache_hit": 25585,
        "preprocessed_cache_hit": 25000,
    }


def test_delta_is_the_difference_with_its_hit_rate() -> None:
    before = {"direct_cache_hit": 10, "preprocessed_cache_hit": 5, "cache_miss": 100}
    after = {"direct_cache_hit": 40, "preprocessed_cache_hit": 15, "cache_miss": 110}
    assert phases.ccache_delta(before, after) == {"hits": 40, "misses": 10, "hit_rate": 0.8}


def test_delta_without_compiles_has_no_rate() -> None:
    same = {"direct_cache_hit": 1, "preprocessed_cache_hit": 1, "cache_miss": 1}
    assert phases.ccache_delta(same, same) == {"hits": 0, "misses": 0, "hit_rate": None}


def test_delta_is_dropped_when_a_reading_is_missing_or_was_zeroed() -> None:
    some = {"direct_cache_hit": 9, "preprocessed_cache_hit": 9, "cache_miss": 9}
    zero = {"direct_cache_hit": 0, "preprocessed_cache_hit": 0, "cache_miss": 0}
    assert phases.ccache_delta(None, some) is None
    assert phases.ccache_delta(some, None) is None
    assert phases.ccache_delta(some, zero) is None


class _CcacheContainer(_RecordingContainer):
    """Has ccache installed; each stats reading shows the compiles of one emerge."""

    def __init__(self, rootfs: Path, *, stats_exit: int = 0) -> None:
        super().__init__(rootfs)
        (rootfs / "usr" / "bin").mkdir(parents=True)
        (rootfs / "usr" / "bin" / "ccache").touch()
        self.stats_calls = 0
        self._stats_exit = stats_exit

    def run(self, argv: Any, **kw: Any) -> Any:
        if list(argv)[:3] == ["env", "CCACHE_DIR=/var/cache/ccache", "ccache"]:
            n = self.stats_calls
            self.stats_calls += 1
            text = _STATS.format(miss=100 + 10 * n, direct=30 * n, pre=0)
            return CommandResult(self._stats_exit, text, "")
        return super().run(argv, **kw)


class _Steps(audit.Recorder):
    """Keeps every step handle, to read what each one recorded."""

    def __init__(self) -> None:
        self.steps: list[audit.Step] = []

    @contextlib.contextmanager
    def step(self, name: str, **fields: Any) -> Generator[audit.Step]:
        del fields
        handle = audit.Step(name)
        self.steps.append(handle)
        yield handle


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: _CcacheContainer) -> _Steps:
    monkeypatch.setattr(phases, "snapshot_fork_point", lambda _root, dest: dest)
    (container.rootfs / "var/db/pkg/dev-lang/python-3.14.7").mkdir(parents=True)
    recorder = _Steps()
    with audit.recording(recorder):
        phases.run_phases(
            container,  # type: ignore[arg-type]
            _chain_recipe(),
            emptytree=True,
            snapshot="S",
            pins="P",
            fork_points_dir=tmp_path,
            stop_after="base",
        )
    return recorder


def test_the_stage_emerge_records_its_ccache_hit_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _CcacheContainer(tmp_path / "rootfs")
    recorder = _run(tmp_path, monkeypatch, container)
    (emerge,) = [s for s in recorder.steps if s.name == "emerge-stage"]
    # one reading before the emerge, one after: 30 hits, 10 misses
    assert container.stats_calls == 2
    assert emerge.fields["ccache"] == {"hits": 30, "misses": 10, "hit_rate": 0.75}


def test_a_failed_stats_reading_records_nothing_and_does_not_stop_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _CcacheContainer(tmp_path / "rootfs", stats_exit=1)
    recorder = _run(tmp_path, monkeypatch, container)
    (emerge,) = [s for s in recorder.steps if s.name == "emerge-stage"]
    assert "ccache" not in emerge.fields
    assert len(container.emerge_calls) >= 1
