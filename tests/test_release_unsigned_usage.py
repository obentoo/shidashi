"""``shidashi release --unsigned`` and ``--key``: exactly one of them (story 032, task 2.1; R1.2).

Contract (story.md R1.2, design.md "shidashi release --unsigned and --images"): ``--unsigned``
is a flag, mutually exclusive with ``--key``, and one of the two is required. Both, or
neither, is a usage error -- exit 2 -- before any build, any key check and any release
directory. Either one alone is accepted.

Driven through the real CLI with story 030's release lab (``release_lab`` of
tests/test_release_cli.py: the child builds, the publish and the maintainer's gpg faked).
"""

from pathlib import Path

import pytest

from tests.test_release_cli import Lab, release_lab
from tests.test_release_core import FPR


@pytest.fixture
def usage_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


def _flat_usage(output: str) -> str:
    """The output with every run of whitespace made one space: a wrapped usage box
    must not hide an option's name."""
    return " ".join(output.split())


def _nothing_started(lab: Lab) -> None:
    assert lab.builds.calls == [], "a build started before the usage error"
    assert lab.maintainer.checked == [], "a key was checked before the usage error"
    assert not lab.releases.exists() or not any(lab.releases.iterdir())


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(FPR, id="a-valid-fingerprint"),
        # Hostile half: a malformed key must not turn the usage error into the key-form
        # error (exit 1) -- the usage is refused first, whatever the key is.
        pytest.param("maintainer@example.org", id="a-malformed-key"),
    ],
)
def test_unsigned_and_key_together_are_a_usage_error_before_any_build(
    usage_lab: Lab, key: str
) -> None:
    result = usage_lab.release("v3", "systemd", "--unsigned", "--key", key)
    assert result.exit_code == 2, result.output
    flat = _flat_usage(result.output)
    assert "--unsigned" in flat and "--key" in flat, result.output
    _nothing_started(usage_lab)


def test_neither_unsigned_nor_key_is_a_usage_error_naming_both(usage_lab: Lab) -> None:
    result = usage_lab.release("v3", "systemd")
    assert result.exit_code == 2, result.output
    flat = _flat_usage(result.output)
    assert "--unsigned" in flat and "--key" in flat, result.output
    _nothing_started(usage_lab)


def test_unsigned_alone_is_accepted_and_builds_without_checking_any_key(usage_lab: Lab) -> None:
    """The converse half: the refusal is the combination's, not ``--unsigned``'s."""
    result = usage_lab.release("v3", "systemd", "--unsigned")
    assert result.exit_code != 2, result.output
    assert [(c.arch, c.init) for c in usage_lab.builds.calls] == [("v3", "systemd")], result.output
    assert usage_lab.maintainer.checked == []


def test_key_alone_still_checks_that_key_and_builds(usage_lab: Lab) -> None:
    """Story 030's signed path is unchanged by the new option."""
    result = usage_lab.release("v3", "systemd", "--key", FPR)
    assert usage_lab.maintainer.checked == [FPR], result.output
    assert [(c.arch, c.init) for c in usage_lab.builds.calls] == [("v3", "systemd")]
