"""Tests of `shidashi build` -- a tree of images in one audited run."""

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from shidashi import cli
from shidashi.assembler import AssembleResult
from shidashi.cli import app, plan_tree

runner = CliRunner()


def test_plan_tree_builds_flavors_and_lets_their_chain_settle_minimal() -> None:
    assert plan_tree(["minimal"]) == (["minimal"], ["minimal"])
    # minimal ships inside kde's chain: no factory run of its own
    assert plan_tree(["kde", "minimal"]) == (["kde"], ["minimal", "kde"])
    # chain order, whatever the order asked
    assert plan_tree(["wm", "gnome", "kde"]) == (
        ["gnome", "kde", "wm"], ["gnome", "kde", "wm"])


def test_plan_tree_explains_that_desktop_is_not_an_image() -> None:
    with pytest.raises(typer.BadParameter, match="shared trunk"):
        plan_tree(["desktop"])


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Factory and Assembler as recorders; cache, scratch and runs under tmp."""
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    calls: list[str] = []

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            self.recipe = recipe

        def build(self, **_k: object) -> None:
            calls.append(f"factory:{self.recipe.flavor}")

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe

        def assemble(self, output_dir: Path, **kw: object) -> AssembleResult:
            calls.append(f"assemble:{self.recipe.flavor}:{kw['compressions']}")
            iso = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    monkeypatch.setattr(cli, "Factory", FakeFactory)
    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    return calls


def test_build_runs_the_factory_then_every_iso_in_one_audited_run(
    lab: list[str], tmp_path: Path
) -> None:
    result = runner.invoke(
        app, ["build", "v3", "systemd", "--images", "kde,minimal", "--compression", "both"]
    )
    assert result.exit_code == 0, result.output
    assert lab == [
        "factory:kde",
        "assemble:minimal:('zstd', 'xz')",
        "assemble:kde:('zstd', 'xz')",
    ]
    (run_dir,) = (tmp_path / "runs").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["command"] == "build" and manifest["status"] == "ok"
    assert [s["step"] for s in manifest["steps"]] == [
        "factory:kde", "assemble:minimal", "assemble:kde"]
    assert manifest["inputs"]["images"] == ["minimal", "kde"]
    bundle = tmp_path / f"{run_dir.name}.build.tar.zst"
    assert bundle.is_file() and bundle.name in (tmp_path / "SHA256SUMS").read_text()


def test_skip_factory_assembles_from_what_is_built(lab: list[str]) -> None:
    result = runner.invoke(
        app, ["build", "v3", "systemd", "--images", "minimal", "--skip-factory"]
    )
    assert result.exit_code == 0, result.output
    assert lab == ["assemble:minimal:('zstd',)"]


def test_build_refuses_desktop_with_a_reason(lab: list[str]) -> None:
    result = runner.invoke(app, ["build", "v3", "systemd", "--images", "desktop"])
    assert result.exit_code == 1 and "shared trunk" in result.output
    assert lab == []


def test_boot_test_gates_the_build(
    lab: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shidashi import vm

    booted: list[str] = []

    def boot_test(iso: Path, **_k: object) -> dict[str, object]:
        booted.append(iso.name)
        ok = "minimal" in iso.name
        return {"passed": ok, "firmwares": {"uefi": {"checks": [
            {"check": "os-release", "passed": ok}]}}}

    monkeypatch.setattr(vm, "boot_test", boot_test)
    result = runner.invoke(
        app, ["build", "v3", "systemd", "--images", "minimal,kde", "--boot-test"]
    )
    assert booted == ["bentoo-x-minimal.iso", "bentoo-x-kde.iso"]
    assert result.exit_code == 1 and "failed its boot test: uefi: os-release" in result.output
