"""The two GitHub REST calls the weekly cycle makes from the host (story 032, R5).

``jit_config_request`` and ``dispatch_request`` build a request and carry no secret;
:func:`send` is the one place the token touches the wire, and :func:`read_token` the one
place it is read -- from a file only, never argv or the environment. GitHub REST API
version ``2022-11-28``. Stdlib ``urllib`` only: no proxy and no redirect, so the token goes
to ``api.github.com`` and nowhere else (the same posture as ``kyomei``).
"""

import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
#: The seconds :func:`send` waits for GitHub before it gives up (R5.4).
DEFAULT_TIMEOUT = 30
_TOKEN_MODE = 0o600
#: A token is a few hundred bytes; a file past this is not one.
_TOKEN_LIMIT = 4096
#: An answer past this is not GitHub's; the calls here return a few hundred bytes.
_ANSWER_LIMIT = 1 << 20
#: How much of GitHub's ``message`` an error quotes.
_MESSAGE_LIMIT = 200
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_WORKFLOW = re.compile(r"[A-Za-z0-9_.-]+")


class GitHubApiError(Exception):
    """A GitHub call or the token file failed. The message never holds the token.

    ``status`` is the HTTP status of an answer GitHub gave, ``None`` for anything else.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ApiRequest:
    """One call, without its token: :func:`send` adds the ``Authorization`` header."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes


def read_token(path: Path) -> str:
    """The token in ``path``: a regular file of the caller's, mode 0600, opened without
    following a symlink. Raises :class:`GitHubApiError` naming ``path`` -- never the content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as err:
        raise GitHubApiError(f"cannot open the token file {path}: {err.strerror}") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != _TOKEN_MODE
        ):
            raise GitHubApiError(
                f"the token file {path} must be a regular file of yours with mode 0600 "
                f"(found mode {stat.S_IMODE(info.st_mode):04o}, owner uid {info.st_uid})"
            )
        raw = os.read(fd, _TOKEN_LIMIT + 1)
    except OSError as err:
        raise GitHubApiError(f"cannot read the token file {path}: {err.strerror}") from None
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii").removesuffix("\n").removesuffix("\r")
    except UnicodeDecodeError:
        token = ""
    if not token or len(raw) > _TOKEN_LIMIT or not token.isprintable() or " " in token:
        raise GitHubApiError(f"the token file {path} does not hold exactly one token")
    return token


def _request(repo: str, path: str, body: Mapping[str, object]) -> ApiRequest:
    if not _REPO.fullmatch(repo):
        raise GitHubApiError(f"--repo {repo!r} is not OWNER/NAME")
    return ApiRequest(
        method="POST",
        url=f"{API_ROOT}/repos/{repo}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "shidashi",
            "X-GitHub-Api-Version": API_VERSION,
        },
        body=json.dumps(body).encode("utf-8"),
    )


def jit_config_request(repo: str, group_id: int, label: str, name: str) -> ApiRequest:
    """``POST .../actions/runners/generate-jitconfig``: one runner, named ``name``, in runner
    group ``group_id``. ``self-hosted`` is sent beside ``label`` so a job's
    ``runs-on: [self-hosted, <label>]`` matches whether or not GitHub adds it itself."""
    return _request(
        repo,
        "actions/runners/generate-jitconfig",
        {
            "name": name,
            "runner_group_id": group_id,
            "labels": ["self-hosted", label],
            "work_folder": "_work",
        },
    )


def dispatch_request(repo: str, workflow: str, ref: str) -> ApiRequest:
    """``POST .../actions/workflows/<workflow>/dispatches``: run ``workflow`` on ``ref``."""
    if not _WORKFLOW.fullmatch(workflow):
        raise GitHubApiError(f"--workflow {workflow!r} is not a workflow file name")
    return _request(
        repo, f"actions/workflows/{urllib.parse.quote(workflow)}/dispatches", {"ref": ref}
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the token must never reach a second address."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _reason(err: urllib.error.HTTPError, token: str) -> str:
    """GitHub's ``message`` for an error answer, without the token if it echoed one."""
    try:
        said = json.loads(err.read(_ANSWER_LIMIT)).get("message", "")
    except OSError, ValueError, AttributeError:
        said = ""
    text = " ".join(str(said).split()).replace(token, "***")
    return text[:_MESSAGE_LIMIT] or str(err.reason)


def send(request: ApiRequest, token: str, *, timeout: float = DEFAULT_TIMEOUT) -> Mapping[str, Any]:
    """Make ``request`` with ``token`` and return GitHub's JSON answer (``{}`` for a 204).

    Raises :class:`GitHubApiError` naming the HTTP status and GitHub's message, or the
    timeout, or the network error. The token appears in the ``Authorization`` header only.
    """
    call = urllib.request.Request(
        request.url,
        data=request.body,
        method=request.method,
        headers={**request.headers, "Authorization": f"Bearer {token}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(call, timeout=timeout) as answer:
            raw = answer.read(_ANSWER_LIMIT)
    except urllib.error.HTTPError as err:
        # from None: the answer's body is the one place the token could be echoed back
        raise GitHubApiError(
            f"GitHub answered {err.code}: {_reason(err, token)}", status=err.code
        ) from None
    except TimeoutError:
        raise GitHubApiError(f"GitHub gave no answer within {timeout:g} s (timed out)") from None
    except urllib.error.URLError as err:
        if isinstance(err.reason, TimeoutError):
            raise GitHubApiError(
                f"GitHub gave no answer within {timeout:g} s (timed out)"
            ) from None
        raise GitHubApiError(f"cannot reach GitHub: {err.reason}") from None
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise GitHubApiError("GitHub's answer is not JSON") from None
    if not isinstance(body, dict):
        raise GitHubApiError("GitHub's answer is not a JSON object")
    return body
