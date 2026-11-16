"""``vm start --memory``: "not given" is told from "8G", and still means 8G.

A relay that starts the VM on a build worker gives it more memory unless the user chose
one, so the CLI must not turn an absent ``--memory`` into the default before anyone can
tell. The default itself is :data:`vm.DEFAULT_MEMORY`, applied where the VM is made.
"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import vm, vm_dev, vm_dev_cli
from shidashi.cli import app


class FakeVmSession:
    """Stands in for ``vm.Session``: keeps the spec it would have booted."""

    booted: list[vm.VmSpec] = []

    def __init__(self, spec: vm.VmSpec, directory: Path) -> None:
        self.spec = spec
        self.directory = directory

    def start(self, *, cid_lock: Path | None = None) -> None:
        FakeVmSession.booted.append(self.spec)


@pytest.fixture
def plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host where a plain ``vm start`` boots a ``FakeVmSession``; an ISO to give it."""
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CID_LOCKS", str(tmp_path / "cids"))
    (tmp_path / "cids").mkdir(mode=0o700)
    monkeypatch.setattr(vm, "Session", FakeVmSession)
    FakeVmSession.booted.clear()
    iso = tmp_path / "b.iso"
    iso.write_bytes(b"iso")
    return iso


@pytest.fixture
def dev_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``vm start --dev`` with ``start_dev`` stood in for; what it was called with."""
    captured: dict[str, Any] = {}

    def fake_start(**kw: Any) -> vm_dev.DevVm:
        captured.update(kw)
        return vm_dev.DevVm("dev", 43, Path("/i.iso"), "p1", [], Path("/s"))

    monkeypatch.setattr(vm_dev, "start_dev", fake_start)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return captured


def test_the_default_memory_is_8g_everywhere_a_vm_is_made() -> None:
    assert vm.DEFAULT_MEMORY == "8G"
    assert vm.VmSpec(Path("/b.iso")).memory == "8G"


def test_a_plain_start_without_memory_boots_with_the_default(plain: Path) -> None:
    result = CliRunner().invoke(app, ["vm", "start", str(plain), "--no-wait"])
    assert result.exit_code == 0, result.output
    assert [spec.memory for spec in FakeVmSession.booted] == ["8G"]


def test_a_plain_start_boots_with_the_memory_given(plain: Path) -> None:
    result = CliRunner().invoke(app, ["vm", "start", str(plain), "--no-wait", "--memory", "4G"])
    assert result.exit_code == 0, result.output
    assert [spec.memory for spec in FakeVmSession.booted] == ["4G"]


def test_a_dev_start_without_memory_boots_with_the_default(dev_started: dict[str, Any]) -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--dev"])
    assert result.exit_code == 0, result.output
    assert dev_started["memory"] == "8G"


def test_a_dev_start_boots_with_the_memory_given(dev_started: dict[str, Any]) -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--dev", "--memory", "24G"])
    assert result.exit_code == 0, result.output
    assert dev_started["memory"] == "24G"


def test_the_cli_hands_on_not_given_as_none(
    dev_started: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # what the relay will branch on: None, not the default already filled in
    seen: list[str | None] = []
    real = vm_dev_cli.start

    def spy(**kw: Any) -> None:
        seen.append(kw["memory"])
        real(**kw)

    monkeypatch.setattr(vm_dev_cli, "start", spy)
    CliRunner().invoke(app, ["vm", "start", "--dev"])
    CliRunner().invoke(app, ["vm", "start", "--dev", "--memory", "8G"])
    assert seen == [None, "8G"]


def test_the_help_still_shows_the_default() -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--help"])
    flat = " ".join(result.output.replace("│", " ").split())
    assert f"(default {vm.DEFAULT_MEMORY})" in flat
