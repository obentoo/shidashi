"""Under a translated locale, what programs read and what users paste stays English.

These tests load the repository's own compiled catalogs, so they also prove the
po/ -> .mo -> shown text chain end to end.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shidashi import i18n
from shidashi.cli import app
from tests._i18n_catalog import compile_repo_catalogs, loaded_catalog
from tests._variants_tree import write_variants

runner = CliRunner()

LOCALES = ["es_ES.UTF-8", "pt_BR.UTF-8", "ru_RU.UTF-8", "zh_CN.UTF-8"]


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = write_variants(tmp_path / "variants", kde=True, openrc=True, broken=True)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    return root


@pytest.fixture(scope="module")
def localedir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return compile_repo_catalogs(tmp_path_factory.mktemp("locale"))


def _run(args: list[str]) -> tuple[int, str, str]:
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    "args",
    [
        ["recipe", "show", "v3", "minimal", "systemd", "--format", "json"],
        ["recipe", "show", "v3", "kde", "systemd"],  # YAML, the default
        ["recipe", "show", "nope", "kde", "systemd"],  # an error message
        ["recipe", "validate", "v3", "broken", "systemd"],  # an error message
    ],
)
def test_machine_output_and_errors_stay_english(
    variants_tree: Path,
    localedir: Path,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    args: list[str],
) -> None:
    english = _run(args)
    with loaded_catalog(monkeypatch, localedir, locale) as lang:
        assert lang
        translated = _run(args)
    assert translated == english


@pytest.mark.parametrize("locale", LOCALES)
def test_a_human_report_is_translated(
    variants_tree: Path, localedir: Path, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    english = _run(["recipe", "validate", "v3", "minimal", "systemd"])
    with loaded_catalog(monkeypatch, localedir, locale):
        translated = _run(["recipe", "validate", "v3", "minimal", "systemd"])
        expected = i18n.t("valid: {arch} × {flavor} × {init}")
    assert english[1] == "valid: v3 × minimal × systemd\n"
    assert expected != "valid: {arch} × {flavor} × {init}"
    assert translated[1] == expected.format(arch="v3", flavor="minimal", init="systemd") + "\n"


def test_only_the_console_entry_point_loads_a_catalog(variants_tree: Path, localedir: Path) -> None:
    """``main()`` (the ``shidashi`` script) translates; ``app()`` -- children, workers -- never."""
    env = {
        **os.environ,
        "LANG": "pt_BR.UTF-8",
        "LC_ALL": "pt_BR.UTF-8",
        i18n.LOCALEDIR_ENV: str(localedir),
        "SHIDASHI_VARIANTS_DIR": str(variants_tree),
    }
    env.pop("LANGUAGE", None)
    args = ["recipe", "validate", "v3", "minimal", "systemd"]

    def run(code: str) -> str:
        done = subprocess.run(
            [sys.executable, "-c", code, *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return done.stdout

    assert run("from shidashi.cli import app; app()") == "valid: v3 × minimal × systemd\n"
    assert run("from shidashi.cli import main; main()") != "valid: v3 × minimal × systemd\n"
