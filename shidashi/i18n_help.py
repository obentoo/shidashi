"""Translate the help of the built Click tree, just before it runs.

Typer evaluates ``help=`` and reads docstrings when the command modules are imported
and when :func:`typer.main.get_command` builds the Click tree -- before the catalog
could ever be loaded, and identically in tests. So nothing is translated at
declaration: the root group (:class:`LocalizedGroup`) walks its own tree in
:meth:`LocalizedGroup.main` and replaces every help text with the loaded catalog's
translation. With no catalog (tests, child processes started with ``app()``) every
lookup returns the English text and the tree is unchanged.

:mod:`shidashi.i18n_catalog` extracts the same texts from the same tree, so the
catalog and what is shown cannot drift apart.
"""

from collections.abc import Iterator
from typing import Any

import typer
from typer import rich_utils
from typer.core import TyperGroup

from shidashi import i18n

# Typer's own labels: module globals of ``typer.rich_utils``, read each time help or an
# error panel is drawn. Typer runs them through stdlib gettext's default domain,
# which no catalog of ours serves, so they are set here instead.
# ``test_i18n_help.py`` fails when a Typer upgrade renames or rewords one, instead of
# leaving it English in silence.
FRAMEWORK_LABELS: dict[str, str] = {
    "ARGUMENTS_PANEL_TITLE": i18n.mark("Arguments"),
    "OPTIONS_PANEL_TITLE": i18n.mark("Options"),
    "COMMANDS_PANEL_TITLE": i18n.mark("Commands"),
    "DEFAULT_STRING": i18n.mark("[default: {}]"),
    "ENVVAR_STRING": i18n.mark("[env var: {}]"),
    "REQUIRED_LONG_STRING": i18n.mark("[required]"),
    "DEPRECATED_STRING": i18n.mark("(deprecated) "),
    "RICH_HELP": i18n.mark("Try [blue]'{command_path} {help_option}'[/] for help."),
}

# Click's ``--help`` option, created lazily per command with this literal help.
HELP_OPTION_TEXT = i18n.mark("Show this message and exit.")


def walk_commands(root: Any) -> Iterator[Any]:
    """Yield ``root`` and every command under it, depth first, in declaration order."""
    yield root
    for sub in getattr(root, "commands", {}).values():
        yield from walk_commands(sub)


def _translated(text: str | None) -> str | None:
    return i18n.t(text) if text else text


def localize_command_tree(root: Any) -> None:
    """Replace the help of ``root`` and every command and parameter under it, in place."""
    for name, english in FRAMEWORK_LABELS.items():
        setattr(rich_utils, name, i18n.t(english))
    for command in walk_commands(root):
        command.help = _translated(command.help)
        command.short_help = _translated(command.short_help)
        command.epilog = _translated(command.epilog)
        help_option = command.get_help_option(typer.Context(command))
        params = [*command.params, *([help_option] if help_option is not None else [])]
        for param in params:
            if param is help_option:
                param.help = i18n.t(HELP_OPTION_TEXT)
                continue
            if getattr(param, "help", None):
                param.help = i18n.t(param.help)
            panel = getattr(param, "rich_help_panel", None)
            if panel:
                param.rich_help_panel = i18n.t(panel)


class LocalizedGroup(TyperGroup):
    """The root group: translates its whole tree before parsing the command line."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        localize_command_tree(self)
        return super().main(*args, **kwargs)
