"""``shidashi ci jit-config`` and ``shidashi ci dispatch`` (story 032, task 6.3; R5.1-R5.5,
Q4, Q15).

Contract (story.md R5, design.md "shidashi ci jit-config and shidashi ci dispatch"):

- ``ci jit-config --repo R --token-file F --runner-group-id N --label L --name NAME`` asks
  GitHub for a JIT runner config and prints ONLY its ``encoded_jit_config`` on stdout (R5.1);
- ``ci dispatch --repo R --token-file F --workflow W --ref REF`` requests a
  ``workflow_dispatch`` of W on REF and exits 0 (R5.2);
- a token file that is missing, not a regular file, not the caller's or not 0600 exits 1
  naming it, before any request (R5.3);
- an error status, or no answer within 30 s, exits 1 naming the status or the timeout (R5.4);
- the token is never in argv, stdout, stderr, a log line, an audit trail or an error (R5.5):
  every test greps every one of them.

Driven through the real CLI against ``FakeGitHubApi`` (tests/_fake_github_api.py).
"""

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli
from tests._fake_github_api import (
    DISPATCH_PATH,
    JIT_PATH,
    FakeGitHubApi,
    Reply,
    mint_jit_config,
    mint_token,
    serving_fake_github,
)

ci_runner = CliRunner()


@dataclass
class CiHost:
    api: FakeGitHubApi
    token: str
    token_file: Path
    caplog: pytest.LogCaptureFixture

    def jit_config(self, *extra: str, token_file: Path | None = None) -> Result:
        argv = [
            "ci",
            "jit-config",
            "--repo",
            "obentoo/shidashi",
            "--token-file",
            str(token_file or self.token_file),
            "--runner-group-id",
            "3",
            "--label",
            "bentoo-builder",
            "--name",
            "builder-20261101T010003Z",
            *extra,
        ]
        return self._invoke(argv)

    def dispatch(self, *, token_file: Path | None = None) -> Result:
        argv = [
            "ci",
            "dispatch",
            "--repo",
            "obentoo/shidashi",
            "--token-file",
            str(token_file or self.token_file),
            "--workflow",
            "weekly-release.yml",
            "--ref",
            "main",
        ]
        return self._invoke(argv)

    def _invoke(self, argv: list[str]) -> Result:
        assert self.token not in " ".join(argv)
        return ci_runner.invoke(cli.app, argv)

    def token_never_shown(self, result: Result) -> None:
        """R5.5 / Q4: the token is in no stream, log, audit trail or request but its header."""
        for stream, text in (
            ("stdout", result.stdout),
            ("stderr", result.stderr),
            ("output", result.output),
            ("logs", self.caplog.text),
            ("exception", repr(result.exception)),
        ):
            assert self.token not in text, f"the token leaked to {stream}"
        runs = Path(os.environ["SHIDASHI_RUNS"])
        for path in runs.rglob("*"):
            if path.is_file():
                assert self.token not in path.read_text(encoding="utf-8", errors="replace"), path
        for seen in self.api.requests:
            assert self.token not in seen.everything()


@pytest.fixture
def ci_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[CiHost]:
    caplog.set_level(logging.DEBUG)
    token = mint_token()
    path = tmp_path / "github-token"
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    with serving_fake_github(monkeypatch) as api:
        yield CiHost(api, token, path, caplog)


def test_ci_jit_config_prints_only_the_encoded_config(ci_host: CiHost) -> None:
    encoded = mint_jit_config()
    ci_host.api.routes[("POST", JIT_PATH)] = Reply(
        201, {"runner": {"id": 42, "name": "builder-x"}, "encoded_jit_config": encoded}
    )
    result = ci_host.jit_config()
    assert result.exit_code == 0, result.output
    assert result.stdout == encoded + "\n"
    hits = ci_host.api.at(JIT_PATH)
    assert len(hits) == 1, ci_host.api.requests
    sent = hits[0].json() or {}
    assert sent.get("name") == "builder-20261101T010003Z"
    assert sent.get("runner_group_id") == 3
    assert "bentoo-builder" in sent.get("labels", [])
    auth = {k.lower(): v for k, v in hits[0].headers.items()}.get("authorization", "")
    assert auth.endswith(ci_host.token), "the request did not carry the token"
    ci_host.token_never_shown(result)


def test_ci_dispatch_requests_the_named_workflow_on_the_named_ref_and_exits_0(
    ci_host: CiHost,
) -> None:
    ci_host.api.routes[("POST", DISPATCH_PATH)] = Reply(204)
    result = ci_host.dispatch()
    assert result.exit_code == 0, result.output
    hits = ci_host.api.at(DISPATCH_PATH)
    assert len(hits) == 1, ci_host.api.requests
    assert hits[0].json() == {"ref": "main"}
    ci_host.token_never_shown(result)


@pytest.mark.parametrize(
    ("command", "path", "status", "message"),
    [
        pytest.param("jit-config", JIT_PATH, 401, "Bad credentials", id="jit-401"),
        pytest.param("jit-config", JIT_PATH, 404, "Not Found", id="jit-404"),
        pytest.param("dispatch", DISPATCH_PATH, 403, "Must have admin rights", id="dispatch-403"),
        pytest.param("dispatch", DISPATCH_PATH, 404, "Not Found", id="dispatch-404"),
    ],
)
def test_ci_an_error_status_exits_1_naming_it(
    ci_host: CiHost, command: str, path: str, status: int, message: str
) -> None:
    ci_host.api.routes[("POST", path)] = Reply(status, {"message": message})
    result = ci_host.jit_config() if command == "jit-config" else ci_host.dispatch()
    assert result.exit_code == 1, result.output
    assert str(status) in result.output
    assert result.stdout.strip() == "" or str(status) in result.stdout
    ci_host.token_never_shown(result)


def test_ci_an_api_that_echoes_the_token_back_never_shows_it(ci_host: CiHost) -> None:
    """Hostile half of R5.5: the API's message is quoted, never a token inside it."""
    ci_host.api.routes[("POST", JIT_PATH)] = Reply(
        401, {"message": f"Bad credentials for {ci_host.token}"}
    )
    result = ci_host.jit_config()
    assert result.exit_code == 1, result.output
    assert "401" in result.output
    ci_host.token_never_shown(result)


def test_ci_an_api_that_does_not_answer_in_30_seconds_exits_1_naming_the_timeout(
    ci_host: CiHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the fake cuts the connection's 30 s to 0.5 s and records the 30 s it was asked for
    ci_host.api.install(monkeypatch, max_timeout=0.5)
    ci_host.api.routes[("POST", JIT_PATH)] = Reply(201, {"encoded_jit_config": "x"}, delay=5)
    result = ci_host.jit_config()
    assert result.exit_code == 1, result.output
    assert "time" in result.output.lower()  # "timed out" / "timeout"
    assert ci_host.api.timeouts and ci_host.api.timeouts[-1] == 30
    ci_host.token_never_shown(result)


@pytest.mark.parametrize("command", ["jit-config", "dispatch"])
@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param("mode-0644", id="mode-0644"),
        pytest.param("missing", id="missing"),
        pytest.param("directory", id="directory"),
    ],
)
def test_ci_an_unsafe_token_file_exits_1_naming_it_before_any_request(
    ci_host: CiHost, tmp_path: Path, command: str, spoil: str
) -> None:
    bad = tmp_path / "unsafe-token"
    if spoil == "mode-0644":
        bad.write_text(ci_host.token + "\n", encoding="utf-8")
        bad.chmod(0o644)
    elif spoil == "directory":
        bad.mkdir()
    ci_host.api.routes[("POST", JIT_PATH)] = Reply(201, {"encoded_jit_config": "x"})
    ci_host.api.routes[("POST", DISPATCH_PATH)] = Reply(204)
    if command == "jit-config":
        result = ci_host.jit_config(token_file=bad)
    else:
        result = ci_host.dispatch(token_file=bad)
    assert result.exit_code == 1, result.output
    assert "".join(str(bad).split()) in "".join(result.output.split())
    assert ci_host.api.requests == [], "a request was made with an unsafe token file"
    ci_host.token_never_shown(result)


@pytest.mark.parametrize("argv", [["ci"], ["ci", "jit-config"], ["ci", "dispatch"]])
def test_ci_help_exits_0(argv: list[str]) -> None:
    result = ci_runner.invoke(cli.app, [*argv, "--help"])
    assert result.exit_code == 0, result.output
    assert "--token-file" in result.output or argv == ["ci"]
