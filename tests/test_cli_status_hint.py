"""The status listing tells how to reach a provisioned worker it never reached (story 022,
task 1.2).

``shidashi worker status`` with no name probes only recorded addresses. A provisioned
worker that was never reached has none, and the listing printed ``refused: N has no
recorded address`` and exited 1. It now prints how to reach it, probes nothing for it,
and that line alone does not fail the listing.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli as cli_mod
from shidashi import mdns, remote, workers
from shidashi.cli import app
from tests._fake_worker import FakeWorker

runner = CliRunner()


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "200")
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    yield w
    w.close()


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every mDNS lookup the listing makes (it must make none)."""
    asked: list[str] = []

    def _find(name: str, **_kw: Any) -> str | None:
        asked.append(name)
        return None

    for module in (mdns, cli_mod, remote):
        monkeypatch.setattr(module, "find", _find, raising=False)
    return asked


def _entry(fw: FakeWorker, name: str, *, provisioned: bool, address: bool) -> workers.WorkerEntry:
    raw = fw.entry().model_dump(mode="json")
    raw["name"] = name
    if not address:
        for field in ("address", "cpu_flags", "image"):
            raw.pop(field)
    raw["provisioned"] = provisioned
    return workers.WorkerEntry.model_validate(raw)


def _listing(capfd: pytest.CaptureFixture[str]) -> tuple[int, str]:
    result = runner.invoke(app, ["worker", "status"])
    captured = capfd.readouterr()
    return result.exit_code, result.output + captured.out + captured.err


def test_an_unreached_provisioned_worker_reads_how_to_reach_it(
    fw: FakeWorker, lookups: list[str], capfd: pytest.CaptureFixture[str]
) -> None:
    fw.register(_entry(fw, "vmworker", provisioned=True, address=False))
    code, out = _listing(capfd)
    line = next(ln for ln in out.splitlines() if ln.startswith("vmworker"))
    assert "shidashi worker status vmworker" in line
    assert "refused" not in line
    assert "has no recorded address" not in line
    assert code == 0, out
    assert fw.calls("ssh") == []  # nothing probed
    assert lookups == []  # and no mDNS lookup either


def test_it_is_listed_next_to_a_reachable_worker(
    fw: FakeWorker, lookups: list[str], capfd: pytest.CaptureFixture[str]
) -> None:
    fw.register(fw.entry(), _entry(fw, "vmworker", provisioned=True, address=False))
    code, out = _listing(capfd)
    lines = out.splitlines()
    assert any(ln.startswith(fw.entry().name) and "reachable" in ln for ln in lines), out
    assert any(ln.startswith("vmworker") and "worker status vmworker" in ln for ln in lines)
    assert code == 0, out
    assert lookups == []


def test_a_non_provisioned_entry_with_no_address_keeps_todays_refusal(
    fw: FakeWorker, lookups: list[str], capfd: pytest.CaptureFixture[str]
) -> None:
    """Hostile: the hint is for a provisioned worker; any other entry without an address
    is a broken registry, still reported as refused and still failing the listing."""
    fw.register(_entry(fw, "odd", provisioned=False, address=False))
    code, out = _listing(capfd)
    line = next(ln for ln in out.splitlines() if ln.startswith("odd"))
    assert "refused" in line
    assert code == 1


def test_a_provisioned_worker_with_an_address_is_probed_as_today(
    fw: FakeWorker, lookups: list[str], capfd: pytest.CaptureFixture[str]
) -> None:
    reached = _entry(fw, fw.entry().name, provisioned=True, address=True)
    fw.register(reached)
    code, out = _listing(capfd)
    assert fw.calls("ssh"), "a provisioned worker with an address was not probed"
    assert "worker status" not in out.splitlines()[0]
    assert code == 0, out
