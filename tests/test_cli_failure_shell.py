"""UNIT tests of the CLI failure callback (story 004, sub-task 8.1, v2).

R3.1: under --step, a failed phase prints the failing phase and its captured
output, THEN opens the shell in the live rootfs, and only THEN asks retry/abort.
R3.2: ``r`` -> RETRY. R3.3: ``a`` -> ABORT. R3.4: the prompt offers no skip.

Contract (design.md v2): ``cli._on_failure(phase, err, shell)`` prints, calls
``shell()``, then prompts via ``cli._prompt_choice``. The prompt is patched so
no TTY is needed; what reached the terminal is read through ``capsys`` at the
moment the shell opens and at the moment the prompt asks, so the order is
observed rather than assumed.
"""

from typing import Any

import pytest

from shidashi import cli
from shidashi.factory import FactoryError, FailureDecision

_SENTINEL = "sentinel-emerge-line: dev-libs/broken-9 failed"


def _err() -> FactoryError:
    return FactoryError("emerge failed", phase="graphics", output=f"noise\n{_SENTINEL}\n")


def _drive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], answer: str
) -> tuple[Any, list[tuple[str, str]], list[list[str]]]:
    events: list[tuple[str, str]] = []
    offered: list[list[str]] = []

    def _terminal() -> str:
        captured = capsys.readouterr()
        return captured.out + captured.err

    def shell() -> None:
        events.append(("shell", _terminal()))

    def prompt(text: str, choices: list[str], default: str) -> str:
        events.append(("prompt", _terminal()))
        offered.append(list(choices))
        return answer

    monkeypatch.setattr(cli, "_prompt_choice", prompt)
    decision = cli._on_failure("graphics", _err(), shell)
    return decision, events, offered


def test_on_failure_prints_output_then_opens_shell_then_prompts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _decision, events, _offered = _drive(monkeypatch, capsys, "a")

    # exactly one shell and one prompt, the shell FIRST (R3.1)
    assert [kind for kind, _ in events] == ["shell", "prompt"]
    printed_before_shell = events[0][1]
    # the failing phase and the captured output were on screen before the shell opened
    assert "graphics" in printed_before_shell
    assert _SENTINEL in printed_before_shell


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("r", FailureDecision.RETRY), ("a", FailureDecision.ABORT)],
)
def test_on_failure_maps_the_answer_to_the_decision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    expected: Any,
) -> None:
    decision, events, _offered = _drive(monkeypatch, capsys, answer)

    assert decision is expected
    # the shell is offered whatever the user then decides
    assert [kind for kind, _ in events] == ["shell", "prompt"]


def test_on_failure_prompt_offers_retry_and_abort_but_no_skip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _decision, _events, offered = _drive(monkeypatch, capsys, "a")

    assert len(offered) == 1
    choices = offered[0]
    assert "r" in choices
    assert "a" in choices
    # R3.4: a failed phase is never skipped
    assert not any(c.lower().startswith("s") for c in choices)
