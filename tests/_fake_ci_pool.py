"""Fakes for the story 034 CI pool tests (``shidashi.ci_pool``).

- ``FakeGitHub``: the ``github`` a pool round is handed -- a callable taking a
  ``github_api.ApiRequest`` and returning GitHub's JSON answer, as
  ``functools.partial(github_api.send, token=...)`` does. It answers from repositories,
  runs and jobs a test declares, honouring ``?status=`` on the runs listing as GitHub
  does, and raises ``github_api.GitHubApiError`` (with ``status``) for a failure a test
  injects. Every JIT config it mints is new.
- ``FakePoolWorker``: the ``worker`` a round is handed -- ``busy()``,
  ``results()`` (the ``results/<job>.json`` objects ``run_job`` left, read once) and
  ``dispatch(job, repo, jitconfig)``; unreachable on demand, a dispatch error on demand. A
  successful dispatch makes it busy, as the ``shidashi-ci-<job>`` unit it starts does,
  until the test says the VM ended (``is_busy = False``).
- ``write_config``: a ``ci.toml`` and its 0600 token file, any field overridden.
- ``pool_events``: the JSON events a round or the CLI emitted, from stderr and log records.

Tokens and JIT configs are minted at run time (tests/_fake_github_api.py).
"""

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests._fake_github_api import mint_jit_config, mint_token

LABEL = "bentoo-worker"
REPO = "obentoo/private-app"
OTHER = "obentoo/private-lib"


@dataclass
class FakeJob:
    id: int
    labels: list[str] = field(default_factory=lambda: ["self-hosted", LABEL])
    status: str = "queued"
    created_at: str = "2026-11-03T10:00:00Z"


@dataclass
class FakeRun:
    id: int
    jobs: list[FakeJob]
    status: str = "queued"


@dataclass
class FakeRepo:
    name: str
    runs: list[FakeRun] = field(default_factory=list)
    private: bool | None = True


@dataclass
class SeenCall:
    method: str
    path: str
    query: dict[str, list[str]]
    body: Any


@dataclass
class FakeGitHub:
    repos: dict[str, FakeRepo] = field(default_factory=dict)
    #: ``(method, path suffix) -> status`` (``None``: no answer, a timeout)
    failures: dict[tuple[str, str], int | None] = field(default_factory=dict)
    calls: list[SeenCall] = field(default_factory=list)
    minted: list[str] = field(default_factory=list)

    def repo(
        self, name: str, *jobs: FakeJob, private: bool | None = True, run_status: str = "queued"
    ) -> FakeRepo:
        """Declare ``name`` with one run (id 100 + n) per job given."""
        repo = self.repos.setdefault(name, FakeRepo(name, private=private))
        repo.private = private
        for job in jobs:
            repo.runs.append(FakeRun(100 + len(repo.runs), [job], status=run_status))
        return repo

    def __call__(self, request: Any) -> Any:
        from shidashi.github_api import API_ROOT, GitHubApiError

        url = urllib.parse.urlsplit(str(request.url))
        path = url.path
        assert str(request.url).startswith(API_ROOT), request.url
        body = json.loads(request.body) if request.body else None
        self.calls.append(
            SeenCall(str(request.method), path, urllib.parse.parse_qs(url.query), body)
        )
        for (method, suffix), status in self.failures.items():
            if request.method == method and path.endswith(suffix):
                if status is None:
                    raise GitHubApiError("GitHub gave no answer within 30 s (timed out)")
                raise GitHubApiError(f"GitHub answered {status}: Bad credentials", status=status)
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "repos":
            raise GitHubApiError("GitHub answered 404: Not Found", status=404)
        name = f"{parts[1]}/{parts[2]}"
        repo = self.repos.get(name)
        if repo is None:
            raise GitHubApiError("GitHub answered 404: Not Found", status=404)
        rest = parts[3:]
        query = urllib.parse.parse_qs(url.query)
        if request.method == "GET" and rest == []:
            answer: dict[str, Any] = {"full_name": name, "name": parts[2]}
            if repo.private is not None:
                answer["private"] = repo.private
            return answer
        if request.method == "GET" and rest == ["actions", "runs"]:
            wanted = query.get("status", [None])[0]
            runs = [r for r in repo.runs if wanted is None or r.status == wanted]
            return {
                "total_count": len(runs),
                "workflow_runs": [{"id": r.id, "status": r.status} for r in runs],
            }
        if request.method == "GET" and rest[:2] == ["actions", "runs"] and rest[3:] == ["jobs"]:
            run = next((r for r in repo.runs if str(r.id) == rest[2]), None)
            jobs = run.jobs if run else []
            return {
                "total_count": len(jobs),
                "jobs": [
                    {
                        "id": j.id,
                        "run_id": int(rest[2]),
                        "status": j.status,
                        "labels": list(j.labels),
                        "created_at": j.created_at,
                        "name": f"job-{j.id}",
                    }
                    for j in jobs
                ],
            }
        if request.method == "POST" and rest == ["actions", "runners", "generate-jitconfig"]:
            encoded = mint_jit_config()
            self.minted.append(encoded)
            return {
                "runner": {"id": len(self.minted), "name": (body or {}).get("name")},
                "encoded_jit_config": encoded,
            }
        raise GitHubApiError("GitHub answered 404: Not Found", status=404)

    def mints(self) -> list[SeenCall]:
        return [
            c for c in self.calls if c.method == "POST" and c.path.endswith("/generate-jitconfig")
        ]


@dataclass
class Dispatched:
    job: str
    repo: str
    jitconfig: str


#: What ``systemd-run`` says when the dispatch fails on the worker (the stderr tail).
DISPATCH_ERROR = "Failed to start transient service unit: Unit shidashi-ci-x already exists."


@dataclass
class FakePoolWorker:
    is_busy: bool = False
    unreachable: bool = False
    #: ``CiDispatchError`` from every dispatch while set
    dispatch_fails: bool = False
    asked_busy: int = 0
    asked_results: int = 0
    dispatched: list[Dispatched] = field(default_factory=list)
    #: ``results/<job>.json`` objects waiting on the worker; ``results()`` takes them
    waiting_results: list[dict[str, Any]] = field(default_factory=list)

    def _reachable(self) -> None:
        if self.unreachable:
            from shidashi.ci_pool import WorkerUnreachable

            raise WorkerUnreachable("bentoo-lab (192.0.2.10) is unreachable: connection timed out")

    def busy(self) -> bool:
        self.asked_busy += 1
        self._reachable()
        return self.is_busy

    def results(self) -> list[dict[str, Any]]:
        self.asked_results += 1
        self._reachable()
        taken, self.waiting_results = self.waiting_results, []
        return taken

    def dispatch(self, job: Any, repo: str, jitconfig: str) -> None:
        self._reachable()
        if self.dispatch_fails:
            from shidashi.ci_pool import CiDispatchError

            raise CiDispatchError(f"dispatch of job {job} failed: {DISPATCH_ERROR}")
        self.dispatched.append(Dispatched(str(job), repo, jitconfig))
        self.is_busy = True


def pool_events(stderr: str, records: list[logging.LogRecord]) -> list[dict[str, Any]]:
    """Every JSON event object, from stderr lines and log messages; a log record's level
    is kept as ``_level`` when the object has no ``level`` of its own."""
    found: list[dict[str, Any]] = []
    sources: list[tuple[str, str | None]] = [(line, None) for line in stderr.splitlines()]
    sources += [(r.getMessage(), r.levelname) for r in records]
    for text, level in sources:
        start = text.find("{")
        if start == -1:
            continue
        try:
            obj = json.loads(text[start:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "event" in obj:
            if level is not None:
                obj.setdefault("_level", level)
            found.append(obj)
    return found


def level_of(event: dict[str, Any]) -> str:
    raw = str(event.get("level") or event.get("_level") or "").upper()
    return "WARNING" if raw == "WARN" else raw


def write_config(
    directory: Path, *, token_mode: int = 0o600, raw: str | None = None, **fields: Any
) -> Path:
    """A ``ci.toml`` and its token file in ``directory``; ``fields`` override the top level,
    ``vm={...}`` the ``[vm]`` table; a ``None`` field is left out."""
    directory.mkdir(parents=True, exist_ok=True)
    token = directory / "github-ci.token"
    token.write_text(mint_token() + "\n", encoding="utf-8")
    token.chmod(token_mode)
    top: dict[str, Any] = {"worker": "bentoo-lab", "token_file": str(token), "repos": [REPO, OTHER]}
    vm = fields.pop("vm", {})
    top.update(fields)
    lines = [f"{k} = {json.dumps(v)}" for k, v in top.items() if v is not None]
    if vm:
        lines.append("[vm]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in vm.items()]
    path = directory / "ci.toml"
    path.write_text(raw if raw is not None else "\n".join(lines) + "\n", encoding="utf-8")
    return path
