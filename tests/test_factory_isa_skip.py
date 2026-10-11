"""The factory's isa-check step says when it could not run (story 023, task 1.2).

A host whose gcc cannot name its CPU -- the slim worker prunes cc1 -- makes
``isacheck.host_arch()`` return None. The scan then has nothing to compare
against; the step must record that it was skipped instead of reading like a
clean scan, and the run must still succeed.
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, factory, isacheck
from tests.test_factory_pins import recipe, wire


def _isa_step(trail: Any) -> dict[str, Any]:
    events = audit.read_events(trail.path / "events.jsonl")
    return next(e for e in events if e["kind"] == "step.end" and e["step"] == "isa-check")


def test_an_unknown_host_cpu_is_recorded_as_a_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    w = wire(monkeypatch, tmp_path)
    monkeypatch.setattr(isacheck, "host_arch", lambda: None)
    scanned: list[Path] = []

    def check_rootfs(rootfs: Path, _target: str, host: str | None = None) -> list[object]:
        scanned.append(rootfs)
        return []

    monkeypatch.setattr(isacheck, "check_rootfs", check_rootfs)
    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        result = factory.Factory(recipe(), w.pkgdir).build(download=False)
    step = _isa_step(trail)
    assert step["skipped"] == "host CPU unknown"
    assert step["unrunnable"] == 0
    assert scanned == []
    assert result.unrunnable_here == ()


def test_a_known_host_cpu_is_scanned_against_that_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    w = wire(monkeypatch, tmp_path)
    monkeypatch.setattr(isacheck, "host_arch", lambda: "znver5")
    hosts: list[object] = []

    def check_rootfs(_rootfs: Path, _target: str, host: str | None = None) -> list[object]:
        hosts.append(host)
        return []

    monkeypatch.setattr(isacheck, "check_rootfs", check_rootfs)
    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        factory.Factory(recipe(), w.pkgdir).build(download=False)
    step = _isa_step(trail)
    assert hosts == ["znver5"]
    assert "skipped" not in step
    assert step["unrunnable"] == 0
