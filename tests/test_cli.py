"""INTEGRATION tests of Shidashi's CLI (shidashi.cli) via Typer's ``CliRunner``.

Builds the minimal, VALID ``variants/`` tree of ``tests/_variants_tree.py`` in
``tmp_path`` and points the CLI at it with ``SHIDASHI_VARIANTS_DIR``. The
``minimal`` and ``kde`` chains resolve; ``flavor/broken`` declares another
stage's name and forces a :class:`RecipeChainError`, which the CLI must report
without a traceback.

Requirements exercised: R1.4, R4.1, R4.2, R4.3, R5.1, R5.2, R6.1, R6.2, R6.3.
"""

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from shidashi.cli import app
from tests._variants_tree import write_variants

runner = CliRunner()


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared stage-format tree, with kde, openrc and a broken flavor."""
    root = write_variants(tmp_path / "variants", kde=True, openrc=True, broken=True)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    return root


# --- recipe show -------------------------------------------------------------


def test_show_default_yaml_parses(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "show", "v3", "minimal", "systemd"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.stdout)
    assert isinstance(data, dict)
    assert data["arch"] == "v3"
    assert data["flavor"] == "minimal"
    assert data["init"] == "systemd"


def test_show_json_parses(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "show", "v3", "minimal", "systemd", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["arch"] == "v3"
    assert data["profile"] == "default/linux/amd64/23.0/no-multilib/systemd"


def test_show_pretty_runs(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "show", "v3", "kde", "systemd", "--format", "pretty"])
    assert result.exit_code == 0
    assert "v3" in result.stdout


def test_show_unknown_axis_exit1_no_traceback(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "show", "nope", "kde", "systemd"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "v3" in combined  # lists the available arches


# --- recipe validate ---------------------------------------------------------


def test_validate_clean_merge_exit0(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "validate", "v3", "kde", "systemd"])
    assert result.exit_code == 0
    assert "v3" in result.stdout


def test_validate_broken_chain_exit1_friendly(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "validate", "v3", "broken", "systemd"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "not-broken" in combined  # the offending stage name reaches the message


def test_validate_unknown_arch_lists_available(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "validate", "bogus", "kde", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "v3" in combined  # the available arch names are listed


# --- recipe list -------------------------------------------------------------


def test_list_shows_axis_names(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "list"])
    assert result.exit_code == 0
    out = result.stdout
    assert "v3" in out
    assert "kde" in out
    assert "minimal" in out
    assert "systemd" in out
    assert "openrc" in out


# --- stubs (R6.2) ------------------------------------------------------------


# NB: factory (story 003) and assemble (Phase 1) are no longer stubs; their
# coverage lives in tests/test_cli_factory.py and tests/test_cli_assemble.py.
# Only `release` is still a stub (Phase 4) — exit 2 with "Phase 0".


def test_stub_release_exit2(variants_tree: Path) -> None:
    result = runner.invoke(app, ["release", "--all"])
    assert result.exit_code == 2
    assert "Phase 0" in result.stdout


# --- no args shows help (R6.3) ----------------------------------------------


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code in (0, 2)
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Usage" in result.stdout or "Commands" in result.stdout
