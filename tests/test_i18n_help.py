"""The help of the CLI, translated in place before it runs (shidashi.i18n_help)."""

from pathlib import Path

import pytest
import typer
import typer.main
from typer import rich_utils
from typer.testing import CliRunner

from shidashi import i18n_help
from shidashi.cli import app
from tests._i18n_catalog import english_again, loaded_catalog, write_test_catalog

runner = CliRunner()


def _help_texts(root: object) -> list[object]:
    out: list[object] = []
    for command in i18n_help.walk_commands(root):
        out += [command.help, command.short_help, command.epilog]
        out += [
            (getattr(p, "help", None), getattr(p, "rich_help_panel", None)) for p in command.params
        ]
    return out


def test_typer_still_has_every_label_we_translate() -> None:
    """A Typer upgrade that renames or rewords a label must fail here, not go English silently."""
    english_again()
    for name, english in i18n_help.FRAMEWORK_LABELS.items():
        assert getattr(rich_utils, name) == english, name


def test_click_still_words_the_help_option_as_we_translate_it() -> None:
    command = typer.main.get_command(app)
    option = command.get_help_option(typer.Context(command))
    assert option is not None
    assert option.help == i18n_help.HELP_OPTION_TEXT


def test_with_no_catalog_localizing_changes_nothing() -> None:
    english_again()
    root = typer.main.get_command(app)
    before = _help_texts(root)
    i18n_help.localize_command_tree(root)
    assert _help_texts(root) == before


def test_help_is_shown_in_the_loaded_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    localedir = write_test_catalog(
        tmp_path,
        "pt_BR",
        {
            "Inspect and validate resolved recipes.": "Inspecionar e validar receitas resolvidas.",
            "Validate load+merge: success → exit 0; conflict/unknown axis → exit 1 (R5.1/R5.2).": (
                "Validar a carga e a fusão (R5.1/R5.2)."
            ),
            "Options": "Opções",
            "Commands": "Comandos",
            "Show this message and exit.": "Mostrar esta mensagem e sair.",
            "Output format.": "Formato de saída.",
        },
    )
    with loaded_catalog(monkeypatch, localedir, "pt_BR.UTF-8"):
        group = runner.invoke(app, ["recipe", "--help"])
        show = runner.invoke(app, ["recipe", "show", "--help"])
    assert group.exit_code == 0, group.output
    for text in (
        "Inspecionar e validar receitas resolvidas.",
        "Validar a carga e a fusão (R5.1/R5.2).",
        "Opções",
        "Comandos",
        "Mostrar esta mensagem e sair.",
    ):
        assert text in group.output
    assert "Formato de saída." in show.output
    # a message the catalog lacks stays English
    assert "Resolve, merge and render the recipe" in group.output


def test_after_the_catalog_is_unloaded_help_is_english_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    localedir = write_test_catalog(tmp_path, "pt_BR", {"Options": "Opções"})
    with loaded_catalog(monkeypatch, localedir, "pt_BR.UTF-8"):
        assert "Opções" in runner.invoke(app, ["recipe", "--help"]).output
    output = runner.invoke(app, ["recipe", "--help"]).output
    assert "Opções" not in output
    assert "Options" in output
