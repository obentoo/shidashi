"""``shidashi ci``: the GitHub calls the weekly cycle makes from the host (story 032, R5).

``jit-config`` mints a just-in-time runner configuration and prints it; ``dispatch`` starts
the weekly workflow. The token is read from a 0600 file and never leaves ``github_api.send``.
One JSON event per call goes to stderr -- the endpoint, the status, the duration -- and never
the token or the configuration.
"""

import json
import time
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from shidashi import github_api
from shidashi.github_api import ApiRequest, GitHubApiError

app = typer.Typer(no_args_is_help=True, help="GitHub calls from the host: runner config, dispatch.")

_REPO = Annotated[str, typer.Option("--repo", help="OWNER/NAME of the repository.")]
_TOKEN_FILE = Annotated[
    Path, typer.Option("--token-file", help="File holding the token: yours, mode 0600.")
]


def _die(message: object) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def _event(request: ApiRequest, began: float, *, status: int | str) -> None:
    typer.echo(
        json.dumps(
            {
                "event": "github_api_call",
                "method": request.method,
                "endpoint": request.url.removeprefix(github_api.API_ROOT),
                "status": status,
                "duration_ms": round((time.monotonic() - began) * 1000),
            }
        ),
        err=True,
    )


def _call(request: ApiRequest, token_file: Path) -> Any:
    """The answer to ``request``; exits 1 on any ``GitHubApiError``."""
    began = time.monotonic()
    try:
        token = github_api.read_token(token_file)
        answer = github_api.send(request, token)
    except GitHubApiError as err:
        _event(request, began, status=err.status if err.status is not None else "failed")
        _die(err)
    _event(request, began, status="ok")
    return answer


@app.command("jit-config")
def jit_config(
    repo: _REPO,
    token_file: _TOKEN_FILE,
    runner_group_id: Annotated[int, typer.Option("--runner-group-id", min=1)],
    label: Annotated[str, typer.Option("--label", help="Label the runner carries.")],
    name: Annotated[str, typer.Option("--name", help="Name the runner registers as.")],
) -> None:
    """Print the encoded JIT configuration of a new one-job runner (stdout, nothing else)."""
    try:
        request = github_api.jit_config_request(repo, runner_group_id, label, name)
    except GitHubApiError as err:
        _die(err)
    encoded = _call(request, token_file).get("encoded_jit_config")
    if not isinstance(encoded, str) or not encoded:
        _die("GitHub's answer has no encoded_jit_config")
    typer.echo(encoded)


@app.command("dispatch")
def dispatch(
    repo: _REPO,
    token_file: _TOKEN_FILE,
    workflow: Annotated[str, typer.Option("--workflow", help="Workflow file name.")],
    ref: Annotated[str, typer.Option("--ref", help="Branch or tag to run it on.")],
) -> None:
    """Request a workflow_dispatch of WORKFLOW on REF."""
    try:
        request = github_api.dispatch_request(repo, workflow, ref)
    except GitHubApiError as err:
        _die(err)
    _call(request, token_file)
