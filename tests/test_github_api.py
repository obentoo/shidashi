"""``github_api`` -- the two GitHub requests and the one client (story 032, task 6.2; R5.1,
R5.2, R5.4, R5.5).

Contract (design.md "shidashi ci jit-config and shidashi ci dispatch"; GitHub REST API
2022-11-28): pure builders ``jit_config_request(repo, group_id, label, name)`` -> ``POST
/repos/{owner}/{repo}/actions/runners/generate-jitconfig`` (``name``, ``runner_group_id``,
``labels``) and ``dispatch_request(repo, workflow, ref)`` -> ``POST
/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches`` (``ref``); neither carries
the token. ``send(request, token, *, timeout=30)`` adds the token to the ``Authorization``
header only, follows no redirect, and raises ``GitHubApiError`` naming the status (and the
API's ``message``) or the timeout -- never the token, even when the API echoes it back.

The client is driven against ``FakeGitHubApi`` (tests/_fake_github_api.py), a local
``http.server`` every HTTPS connection is pointed at.
"""

import inspect
import json
import time
import urllib.request
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from tests._fake_github_api import (
    DISPATCH_PATH,
    JIT_PATH,
    FakeGitHubApi,
    Reply,
    mint_jit_config,
    mint_token,
    serving_fake_github,
)
from tests._pending import try_import

jit_config_request: Any = try_import("shidashi.github_api", "jit_config_request")
dispatch_request: Any = try_import("shidashi.github_api", "dispatch_request")
send: Any = try_import("shidashi.github_api", "send")
GitHubApiError: Any = try_import("shidashi.github_api", "GitHubApiError")

API = "https://api.github.com"
API_REPO = "obentoo/shidashi"


@pytest.fixture
def api_fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubApi]:
    with serving_fake_github(monkeypatch) as api:
        yield api


def parts_of(request: Any) -> tuple[str, str, dict[str, str], Any]:
    """Method, URL, headers (lower-case names) and JSON body of a built request."""
    if isinstance(request, urllib.request.Request):
        headers = {k.lower(): v for k, v in request.header_items()}
        data = request.data
        method, url = request.get_method(), request.full_url
    else:  # a request record of the module's own
        headers = {k.lower(): v for k, v in dict(getattr(request, "headers", {})).items()}
        data = getattr(request, "body", getattr(request, "data", None))
        method, url = str(request.method), str(request.url)
    body = json.loads(data) if isinstance(data, (bytes, str)) and data else None
    return method, url, headers, body


def body_of(answer: object) -> Mapping[str, Any]:
    if isinstance(answer, (bytes, str)):
        answer = json.loads(answer)
    assert isinstance(answer, Mapping), f"send returned {answer!r}"
    return answer


def jit_request() -> Any:
    return jit_config_request(API_REPO, 3, "bentoo-builder", "builder-20261101T010003Z")


# --- builders (pure) --------------------------------------------------------------------------


def test_jit_config_request_is_a_post_of_the_runner_name_group_and_label() -> None:
    method, url, headers, body = parts_of(jit_request())
    assert method == "POST"
    assert url == f"{API}{JIT_PATH}"
    assert isinstance(body, dict), body
    assert body.get("name") == "builder-20261101T010003Z"
    assert body.get("runner_group_id") == 3
    assert "bentoo-builder" in body.get("labels", [])
    assert headers.get("accept") == "application/vnd.github+json"
    assert headers.get("x-github-api-version") == "2022-11-28"
    assert "authorization" not in headers


def test_dispatch_request_is_a_post_of_the_ref_to_the_named_workflow() -> None:
    method, url, headers, body = parts_of(dispatch_request(API_REPO, "weekly-release.yml", "main"))
    assert method == "POST"
    assert url == f"{API}{DISPATCH_PATH}"
    assert body == {"ref": "main"}
    assert headers.get("x-github-api-version") == "2022-11-28"
    assert "authorization" not in headers


def test_requests_name_the_repository_they_were_built_for() -> None:
    """Hostile half: a near-identical repository is another repository."""
    _m, url, _h, _b = parts_of(jit_config_request("obentoo/shidashi-fork", 3, "x", "y"))
    assert url == f"{API}/repos/obentoo/shidashi-fork/actions/runners/generate-jitconfig"


# --- send -------------------------------------------------------------------------------------


def test_send_puts_the_token_in_the_authorization_header_only_and_returns_the_body(
    api_fake: FakeGitHubApi,
) -> None:
    token, encoded = mint_token(), mint_jit_config()
    api_fake.routes[("POST", JIT_PATH)] = Reply(
        201, {"runner": {"id": 42, "name": "builder-x"}, "encoded_jit_config": encoded}
    )
    answer = send(jit_request(), token)
    assert body_of(answer).get("encoded_jit_config") == encoded
    hits = api_fake.at(JIT_PATH)
    assert len(hits) == 1, api_fake.requests
    seen = hits[0]
    assert seen.method == "POST"
    auth = {k.lower(): v for k, v in seen.headers.items()}.get("authorization", "")
    assert auth in (f"Bearer {token}", f"token {token}"), "the token is not the auth header"
    assert token not in seen.everything()
    assert (seen.json() or {}).get("runner_group_id") == 3


def test_send_of_a_dispatch_answered_204_returns_without_error(api_fake: FakeGitHubApi) -> None:
    api_fake.routes[("POST", DISPATCH_PATH)] = Reply(204)
    send(dispatch_request(API_REPO, "weekly-release.yml", "main"), mint_token())
    hits = api_fake.at(DISPATCH_PATH)
    assert len(hits) == 1, api_fake.requests
    assert hits[0].json() == {"ref": "main"}


@pytest.mark.parametrize(
    ("status", "message"),
    [
        pytest.param(401, "Bad credentials", id="401"),
        pytest.param(403, "Resource not accessible by personal access token", id="403"),
        pytest.param(404, "Not Found", id="404"),
        pytest.param(503, "Service Unavailable", id="503"),
    ],
)
def test_send_of_an_error_status_raises_naming_the_status_and_message_never_the_token(
    api_fake: FakeGitHubApi, status: int, message: str
) -> None:
    token = mint_token()
    api_fake.routes[("POST", JIT_PATH)] = Reply(status, {"message": message})
    with pytest.raises(GitHubApiError) as caught:
        send(jit_request(), token)
    assert str(status) in str(caught.value)
    assert message in str(caught.value)
    assert token not in f"{caught.value!s} {caught.value!r}"


def test_send_never_repeats_a_token_the_api_echoes_back(api_fake: FakeGitHubApi) -> None:
    """Hostile half: the API's ``message`` is quoted -- unless it holds the token."""
    token = mint_token()
    api_fake.routes[("POST", JIT_PATH)] = Reply(401, {"message": f"Bad credentials: {token}"})
    with pytest.raises(GitHubApiError) as caught:
        send(jit_request(), token)
    assert "401" in str(caught.value)
    assert token not in f"{caught.value!s} {caught.value!r}"


def test_send_follows_no_redirect(api_fake: FakeGitHubApi) -> None:
    """The token must never reach a second address."""
    token = mint_token()
    api_fake.routes[("POST", JIT_PATH)] = Reply(
        302, {"message": "Moved"}, headers=(("Location", f"{API}/elsewhere"),)
    )
    with pytest.raises(GitHubApiError) as caught:
        send(jit_request(), token)
    assert "302" in str(caught.value)
    assert api_fake.at("/elsewhere") == []
    assert token not in str(caught.value)


def test_send_waits_30_seconds_by_default() -> None:
    assert inspect.signature(send).parameters["timeout"].default == 30


def test_send_asks_its_connection_for_the_timeout(api_fake: FakeGitHubApi) -> None:
    api_fake.routes[("POST", DISPATCH_PATH)] = Reply(204)
    send(dispatch_request(API_REPO, "weekly-release.yml", "main"), mint_token())
    assert api_fake.timeouts and api_fake.timeouts[-1] == 30


def test_send_that_gets_no_answer_in_time_raises_naming_the_timeout(
    api_fake: FakeGitHubApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint_token()
    api_fake.install(monkeypatch, max_timeout=0.5)
    api_fake.routes[("POST", JIT_PATH)] = Reply(201, {"encoded_jit_config": "x"}, delay=5)
    began = time.monotonic()
    with pytest.raises(GitHubApiError) as caught:
        send(jit_request(), token, timeout=0.5)
    assert time.monotonic() - began < 4
    assert "time" in str(caught.value).lower()  # "timed out" / "timeout"
    assert token not in str(caught.value)
