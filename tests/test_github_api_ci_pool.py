"""``github_api``: the four requests the CI pool makes (story 034, task 4.1; R5.4, R6.1).

Contract (design.md, "shidashi/ci_pool.py" -> Dependencies; GitHub REST API 2022-11-28):
pure builders that carry no token --

- ``queued_runs_request(repo)``: ``GET /repos/{repo}/actions/runs?status=queued``;
- ``run_jobs_request(repo, run_id)``: ``GET /repos/{repo}/actions/runs/{run_id}/jobs``;
- ``repo_request(repo)``: ``GET /repos/{repo}`` (its ``private`` field, R5.4);
- ``repo_jit_config_request(repo, label, name)``: ``POST
  /repos/{repo}/actions/runners/generate-jitconfig`` with ``runner_group_id: 1`` (a
  repository's Default group), ``labels`` holding ``self-hosted`` and ``label``, ``name``.

``repo`` is ``OWNER/NAME`` of ``[A-Za-z0-9_.-]``; anything else is refused (``ValueError``
or ``GitHubApiError``) -- it becomes a URL path. ``send`` (story 032) makes the GET calls
too, the token in the ``Authorization`` header only.

Lives beside tests/test_github_api.py (story 032), which this story does not change.
"""

import json
import urllib.parse
from collections.abc import Iterator
from typing import Any

import pytest

from tests._fake_github_api import FakeGitHubApi, Reply, mint_token, serving_fake_github
from tests._pending import try_import

queued_runs_request: Any = try_import("shidashi.github_api", "queued_runs_request")
run_jobs_request: Any = try_import("shidashi.github_api", "run_jobs_request")
repo_request: Any = try_import("shidashi.github_api", "repo_request")
repo_jit_config_request: Any = try_import("shidashi.github_api", "repo_jit_config_request")
send: Any = try_import("shidashi.github_api", "send")
GitHubApiError: Any = try_import("shidashi.github_api", "GitHubApiError")

API = "https://api.github.com"
REPO = "obentoo/private-app"


@pytest.fixture
def api_fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubApi]:
    with serving_fake_github(monkeypatch) as api:
        yield api


def split(request: Any) -> tuple[str, str, dict[str, list[str]], dict[str, str]]:
    """Method, path, query and lower-cased headers of a built request."""
    url = urllib.parse.urlsplit(str(request.url))
    assert f"{url.scheme}://{url.netloc}" == API, request.url
    headers = {k.lower(): v for k, v in dict(request.headers).items()}
    return str(request.method), url.path, urllib.parse.parse_qs(url.query), headers


def no_token_header(headers: dict[str, str]) -> None:
    assert "authorization" not in headers


def test_queued_runs_request_is_a_get_of_the_queued_runs() -> None:
    method, path, query, headers = split(queued_runs_request(REPO))
    assert (method, path) == ("GET", f"/repos/{REPO}/actions/runs")
    assert query.get("status") == ["queued"]
    assert headers.get("x-github-api-version") == "2022-11-28"
    no_token_header(headers)


def test_run_jobs_request_is_a_get_of_one_runs_jobs() -> None:
    method, path, _, headers = split(run_jobs_request(REPO, 123456789))
    assert (method, path) == ("GET", f"/repos/{REPO}/actions/runs/123456789/jobs")
    no_token_header(headers)


def test_repo_request_is_a_get_of_the_repository() -> None:
    method, path, _, headers = split(repo_request(REPO))
    assert (method, path) == ("GET", f"/repos/{REPO}")
    no_token_header(headers)


def test_repo_jit_config_request_is_a_post_in_the_default_group_with_both_labels() -> None:
    """R6.1: one runner named for the job, ``self-hosted`` beside the configured label."""
    request = repo_jit_config_request(REPO, "bentoo-worker", "bentoo-worker-9001")
    method, path, _, headers = split(request)
    assert (method, path) == ("POST", f"/repos/{REPO}/actions/runners/generate-jitconfig")
    body = json.loads(request.body)
    assert body["name"] == "bentoo-worker-9001"
    assert body["runner_group_id"] == 1
    assert {"self-hosted", "bentoo-worker"} <= set(body["labels"])
    no_token_header(headers)


@pytest.mark.parametrize(
    "repo",
    [
        # hostile: each would change the URL GitHub is asked
        pytest.param("obentoo", id="no-name"),
        pytest.param("obentoo/app/extra", id="extra-segment"),
        pytest.param("obentoo/app?status=completed", id="query"),
        pytest.param("obentoo/app#x", id="fragment"),
        pytest.param("obentoo/app ", id="space"),
        pytest.param("obentoo/%2e%2e", id="encoded"),
        pytest.param("", id="empty"),
    ],
)
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda r: queued_runs_request(r), id="runs"),
        pytest.param(lambda r: run_jobs_request(r, 1), id="jobs"),
        pytest.param(lambda r: repo_request(r), id="repo"),
        pytest.param(lambda r: repo_jit_config_request(r, "bentoo-worker", "n"), id="jit"),
    ],
)
def test_queued_run_jobs_repo_request_and_repo_jit_refuse_a_repo_that_is_not_owner_name(
    repo: str, build: Any
) -> None:
    with pytest.raises((ValueError, GitHubApiError)):
        build(repo)


def test_repo_request_keeps_every_character_github_allows() -> None:
    """Hostile split: dots, underscores and dashes are part of real names."""
    _, path, _, _ = split(repo_request("my.org_1/re_po-2.x"))
    assert path == "/repos/my.org_1/re_po-2.x"


def test_queued_runs_request_is_sent_as_a_get_with_the_token_in_its_header_only(
    api_fake: FakeGitHubApi,
) -> None:
    token = mint_token()
    path = f"/repos/{REPO}/actions/runs"
    api_fake.routes[("GET", path)] = Reply(200, {"total_count": 0, "workflow_runs": []})
    request = queued_runs_request(REPO)
    assert request.method == "GET", request
    answer = send(request, token)
    assert answer == {"total_count": 0, "workflow_runs": []}
    hits = api_fake.at(path)
    assert len(hits) == 1 and hits[0].method == "GET"
    auth = {k.lower(): v for k, v in hits[0].headers.items()}.get("authorization", "")
    assert auth.endswith(token)
    assert token not in hits[0].everything()
