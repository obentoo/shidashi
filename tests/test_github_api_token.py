"""``github_api.read_token`` -- the GitHub token comes only from a safe file (story 032,
task 6.1; R5.3, R5.5).

Contract (story.md R5.3, R5.5; design.md "shidashi ci jit-config and shidashi ci
dispatch"): ``read_token(path) -> str`` returns the token of a regular file owned by the
caller with mode 0600 (its trailing newline dropped). A missing file, one that is not a
regular file, one owned by someone else or of any other mode raises ``GitHubApiError``
naming the file -- and never the token, in the message, its cause, or a log line.
"""

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from tests._fake_github_api import mint_token
from tests._pending import try_import

read_token: Any = try_import("shidashi.github_api", "read_token")
GitHubApiError: Any = try_import("shidashi.github_api", "GitHubApiError")


def token_file(tmp_path: Path, *, mode: int = 0o600, newline: str = "\n") -> tuple[Path, str]:
    token = mint_token()
    path = tmp_path / "github-token"
    path.write_text(token + newline, encoding="utf-8")
    path.chmod(mode)
    return path, token


def chain_of(err: BaseException) -> str:
    """The error, its cause and its context, as one text."""
    seen: list[str] = []
    current: BaseException | None = err
    while current is not None and len(seen) < 10:
        seen.append(f"{current!s} {current!r} {current.args!r}")
        current = current.__cause__ or current.__context__
    return "\n".join(seen)


def refused_token(path: Path, token: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(GitHubApiError) as caught:
        read_token(path)
    assert "".join(str(path).split()) in "".join(str(caught.value).split()), caught.value
    assert token not in chain_of(caught.value)
    assert token not in caplog.text


@pytest.mark.parametrize("newline", ["\n", "", "\r\n"], ids=["newline", "bare", "crlf"])
def test_read_token_returns_the_token_of_a_0600_file_owned_by_the_caller(
    tmp_path: Path, newline: str
) -> None:
    path, token = token_file(tmp_path, newline=newline)
    assert read_token(path) == token


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(0o644, id="0644"),
        pytest.param(0o640, id="0640"),
        pytest.param(0o604, id="0604"),
        pytest.param(0o660, id="0660"),
        # Hostile halves: modes that differ from 0600 by one bit the owner might not mind.
        pytest.param(0o700, id="0700"),
        pytest.param(0o400, id="0400"),
        pytest.param(0o4600, id="setuid-0600"),
    ],
)
def test_read_token_refuses_a_mode_other_than_0600_naming_the_file_never_the_token(
    tmp_path: Path, mode: int, caplog: pytest.LogCaptureFixture
) -> None:
    path, token = token_file(tmp_path, mode=mode)
    refused_token(path, token, caplog)


def test_read_token_refuses_a_missing_file_naming_it(tmp_path: Path) -> None:
    gone = tmp_path / "github-token"
    with pytest.raises(GitHubApiError) as caught:
        read_token(gone)
    assert str(gone) in str(caught.value)


def test_read_token_refuses_a_directory_naming_it(tmp_path: Path) -> None:
    directory = tmp_path / "github-token"
    directory.mkdir(mode=0o700)
    with pytest.raises(GitHubApiError) as caught:
        read_token(directory)
    assert str(directory) in str(caught.value)


def test_read_token_refuses_a_symlink_to_a_valid_token_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Hostile half: a symlink is not a regular file, even to a 0600 file of the caller."""
    target, token = token_file(tmp_path)
    link = tmp_path / "token-link"
    link.symlink_to(target)
    refused_token(link, token, caplog)


def test_read_token_refuses_a_file_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path, token = token_file(tmp_path)
    someone_else = path.stat().st_uid + 1
    monkeypatch.setattr(os, "getuid", lambda: someone_else)
    monkeypatch.setattr(os, "geteuid", lambda: someone_else)
    refused_token(path, token, caplog)
