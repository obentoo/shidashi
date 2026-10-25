"""The README sets up the weekly release cycle (story 032, task 9.2; R6.1, Q15).

Contract (story.md Q15; tasks.md 9.2 Objective "the weekly cycle can be set up from the
README alone"): the README names the weekly cycle (``builder.sh weekly`` and its
``timer-install``), the unsigned release it makes, and the GitHub token file
(``~/.config/shidashi/github-token``, mode 0600) the host reads; ``shidashi release --help``,
``shidashi release report --help`` and ``shidashi ci --help`` exit 0.
"""

import pytest
from typer.testing import CliRunner

from shidashi import cli
from tests._fake_builder_guest import CHECKOUT

WEEKLY_README = CHECKOUT / "README.md"
help_runner = CliRunner()


@pytest.mark.parametrize(
    "words",
    [
        pytest.param(("builder.sh weekly",), id="the-weekly-cycle"),
        pytest.param(("builder.sh timer-install",), id="the-timer"),
        pytest.param(("--unsigned",), id="the-unsigned-release"),
        pytest.param(("github-token", "0600"), id="the-token-file"),
        pytest.param(("release report",), id="the-report"),
        pytest.param(("release prune",), id="the-retention"),
    ],
)
def test_readme_names_what_the_weekly_release_needs(words: tuple[str, ...]) -> None:
    text = WEEKLY_README.read_text(encoding="utf-8")
    for word in words:
        assert word in text, f"README.md does not mention {word!r}"


@pytest.mark.parametrize(
    "argv",
    [["release", "--help"], ["release", "report", "--help"], ["ci", "--help"]],
    ids=["release", "release-report", "ci"],
)
def test_readme_commands_have_help_that_exits_0(argv: list[str]) -> None:
    result = help_runner.invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
