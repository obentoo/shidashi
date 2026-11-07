"""The host's CI pool: queued GitHub jobs become one JIT runner each on the worker (story 034).

:func:`load_config` reads ``ci.toml`` (never in this repository: it holds the allowlist);
:func:`round` is one pass -- the worker's state, then each allowlisted repository's queued
jobs, then at most one mint and one dispatch. ``github`` (``ApiRequest -> JSON``) and
``worker`` (``busy``/``results``/``dispatch``) arrive as parameters: the CLI hands the real
ones, the tests fakes. Every event is one JSON line on stderr with a ``level``; the token
and the JIT config are in none.
"""

import builtins
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from shidashi import github_api
from shidashi.ci_pool_worker import CiDispatchError, SshWorker, WorkerUnreachable, dispatch_argv
from shidashi.github_api import ApiRequest, GitHubApiError

__all__ = [
    "CiDispatchError",
    "PoolConfig",
    "PoolConfigError",
    "PoolState",
    "RoundResult",
    "SshWorker",
    "WorkerUnreachable",
    "dispatch_argv",
    "load_config",
    "round",
]

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_LABEL = re.compile(r"[A-Za-z0-9_.-]+")
_WORKER = re.compile(r"[A-Za-z0-9_.-]+")
_TOP_KEYS = frozenset({"worker", "token_file", "label", "repos", "source", "vm"})
_VM_DEFAULTS = {
    "memory_gib": 8,
    "disk_gib": 64,
    "timeout_min": 360,
    "min_free_gib": 20,
    "poll_s": 30,
}
_AUTH = (401, 403)
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}
#: what a failed job's rc means, for the WARN the pool logs (``ci_guest.run_job``)
_RC_REASONS = {
    1: "the job failed or its VM gave no result",
    3: "low disk on the worker",
    4: "timeout",
}


class PoolConfigError(Exception):
    """``ci.toml`` is missing or wrong (R6.9): names the file and the field."""

    def __init__(self, file: Path, field: str, reason: str) -> None:
        super().__init__(f"{file}: {field}: {reason}")


@dataclass(frozen=True)
class PoolVm:
    memory_gib: int = 8
    disk_gib: int = 64
    timeout_min: int = 360
    min_free_gib: int = 20
    poll_s: int = 30


@dataclass(frozen=True)
class PoolConfig:
    worker: str
    token_file: Path
    repos: tuple[str, ...]
    source: Path
    label: str = "bentoo-worker"
    vm: PoolVm = field(default_factory=PoolVm)


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "shidashi" / "ci.toml"


def default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "shidashi" / "ci-pool.json"


def _work_tree(path: Path) -> bool:
    """Whether ``path`` is the top of a git work tree (an empty ``.git`` is not)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree", "--show-cdup"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return False
    lines = done.stdout.splitlines()
    return done.returncode == 0 and lines[:1] == ["true"] and not "".join(lines[1:]).strip()


def _package_tree() -> Path:
    return Path(__file__).resolve().parent.parent


def _text(doc: Mapping[str, Any], key: str, file: Path, rule: re.Pattern[str]) -> str:
    value = doc.get(key)
    if value is None:
        raise PoolConfigError(file, key, "missing")
    if not isinstance(value, str) or not rule.fullmatch(value):
        raise PoolConfigError(file, key, f"{value!r} does not match {rule.pattern}")
    return value


def _vm(doc: Mapping[str, Any], file: Path) -> PoolVm:
    table = doc.get("vm", {})
    if not isinstance(table, dict):
        raise PoolConfigError(file, "vm", "must be a table")
    for key in table:
        if key not in _VM_DEFAULTS:
            raise PoolConfigError(file, f"vm.{key}", "unknown key (the worker derives the vCPUs)")
    values: dict[str, int] = {}
    for key, default in _VM_DEFAULTS.items():
        value = table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise PoolConfigError(file, f"vm.{key}", f"{value!r} is not a positive integer")
        values[key] = value
    return PoolVm(**values)


def load_config(path: Path | None = None) -> PoolConfig:
    """``ci.toml``, refused whole before any GitHub call when anything is wrong (R6.9)."""
    path = default_config_path() if path is None else path
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise PoolConfigError(path, "file", f"cannot read: {err.strerror or err}") from err
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        raise PoolConfigError(path, "file", f"not TOML: {err}") from err
    for key in doc:
        if key not in _TOP_KEYS:
            raise PoolConfigError(path, key, "unknown key")
    worker = _text(doc, "worker", path, _WORKER)
    raw_token = doc.get("token_file")
    if not isinstance(raw_token, str) or not raw_token:
        raise PoolConfigError(path, "token_file", "missing")
    token_file = Path(raw_token).expanduser()
    try:
        github_api.read_token(token_file)
    except GitHubApiError as err:
        raise PoolConfigError(path, "token_file", str(err)) from err
    label = _text(doc, "label", path, _LABEL) if "label" in doc else "bentoo-worker"
    repos = doc.get("repos")
    if not isinstance(repos, list) or not repos:
        raise PoolConfigError(path, "repos", "must be a non-empty list of OWNER/NAME")
    for repo in repos:
        if not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise PoolConfigError(path, "repos", f"{repo!r} is not OWNER/NAME")
    raw_source = doc.get("source")
    if raw_source is not None and not isinstance(raw_source, str):
        raise PoolConfigError(path, "source", f"{raw_source!r} is not a path")
    source = _package_tree() if raw_source is None else Path(raw_source).expanduser().absolute()
    if not _work_tree(source):
        raise PoolConfigError(path, "source", f"{source} is not the top of a git work tree")
    return PoolConfig(worker, token_file, tuple(repos), source, label, _vm(doc, path))


# --- state ------------------------------------------------------------------------------------


@dataclass
class PoolState:
    """Job ids served while their runner may live, and consecutive 401/403 rounds."""

    served: set[str] = field(default_factory=set)
    auth_failures: int = 0


def load_state(path: Path, *, now: Callable[[], float] = time.time) -> PoolState:
    """The saved state; a missing file is empty, a corrupt one set aside (Q11)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return PoolState()
    try:
        data = json.loads(raw.decode("utf-8"))  # not UTF-8 is corrupt too (UnicodeDecodeError)
        served = {str(j) for j in data["served"]}
        failures = int(data["auth_failures"])
    except ValueError, KeyError, TypeError:
        aside = path.with_name(f"{path.name}.corrupt-{int(now())}")
        os.replace(path, aside)
        event("WARN", "ci_pool_state", path=str(path), result="corrupt", moved_to=str(aside))
        return PoolState()
    return PoolState(served, failures)


def save_state(path: Path, state: PoolState) -> None:
    """Through a temporary file renamed over the target: never half-written (Q11)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.part")
    data = {"served": sorted(state.served), "auth_failures": state.auth_failures}
    with part.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(data))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, path)


# --- events -----------------------------------------------------------------------------------


def _threshold() -> int:
    return _LEVELS.get(os.environ.get("SHIDASHI_LOG_LEVEL", "INFO").upper(), 20)


def event(level: str, name: str, **fields: Any) -> None:
    """One JSON line on stderr; below ``SHIDASHI_LOG_LEVEL`` (default INFO), nothing."""
    if _LEVELS[level] >= _threshold():
        sys.stderr.write(json.dumps({"level": level, "event": name, **fields}) + "\n")
        sys.stderr.flush()


def _ms(began: float) -> int:
    # builtins.round: this module defines its own ``round`` (the pool pass)
    return max(0, builtins.round((time.monotonic() - began) * 1000))


# --- one round --------------------------------------------------------------------------------


class Worker(Protocol):
    def busy(self) -> bool: ...
    def results(self) -> list[dict[str, Any]]: ...
    def dispatch(self, job: str, repo: str, jitconfig: str) -> None: ...


GitHub = Callable[[ApiRequest], Mapping[str, Any]]


@dataclass(frozen=True)
class RoundResult:
    """What the round did: ``dispatched`` a job id, or why it did not."""

    outcome: str
    job: str | None = None


class _AuthFailed(Exception):
    """GitHub refused the token: the round ends, the count goes on (R6.7)."""


def _call(github: GitHub, request: ApiRequest, seen: list[int]) -> Any:
    began = time.monotonic()
    endpoint = request.url.removeprefix(github_api.API_ROOT)
    try:
        answer = github(request)
    except GitHubApiError as err:
        status: int | str = err.status if err.status is not None else "failed"
        event(
            "ERROR",
            "github_api_call",
            method=request.method,
            endpoint=endpoint,
            status=status,
            duration_ms=_ms(began),
            error=str(err),
        )
        seen.append(err.status or 0)
        if err.status in _AUTH:
            raise _AuthFailed from err
        raise
    seen.append(200)
    event(
        "DEBUG",
        "github_api_call",
        method=request.method,
        endpoint=endpoint,
        status="ok",
        duration_ms=_ms(began),
    )
    return answer


def _labelled(job: Mapping[str, Any], label: str) -> bool:
    """``self-hosted`` and ``label`` among the job's labels, and none the minted runner
    lacks: GitHub gives a job only to a runner holding all of them, in any case -- a
    ``gpu`` job would leave the one CI VM idle until its timeout (tech review 4.x)."""
    labels = {str(x).lower() for x in job.get("labels") or []}
    minted = {x.lower() for x in github_api.runner_labels(label)}
    return {"self-hosted", label.lower()} <= labels <= minted


def _queued(cfg: PoolConfig, repo: str, github: GitHub, seen: list[int]) -> list[Mapping[str, Any]]:
    """The repository's queued jobs carrying both labels, oldest first (R6.1, R6.2).

    Two filtered listings, as design.md has it: the unfiltered newest 100 outgrew
    ``send``'s 1 MiB cap at ~85 runs and hid a queued job in any older run (review 4.x)."""
    found: dict[str, Mapping[str, Any]] = {}
    run_ids: list[int] = []
    for status in ("queued", "in_progress"):
        runs = _call(github, github_api.queued_runs_request(repo, status), seen)
        for run in runs.get("workflow_runs") or []:
            if run.get("status") == status and int(run["id"]) not in run_ids:
                run_ids.append(int(run["id"]))
    for run_id in run_ids:
        jobs = _call(github, github_api.run_jobs_request(repo, run_id), seen)
        for job in jobs.get("jobs") or []:
            if job.get("status") == "queued" and _labelled(job, cfg.label):
                found[str(job["id"])] = job
    return sorted(found.values(), key=lambda j: (str(j.get("created_at", "")), int(j["id"])))


def _report_results(worker: Worker) -> None:
    for done in worker.results():
        rc = done.get("rc")
        if rc in _RC_REASONS:
            event(
                "WARN",
                "ci_job_failed",
                job=str(done.get("job")),
                repo=done.get("repo"),
                rc=rc,
                reason=_RC_REASONS[rc],
                ended=done.get("ended"),
            )


def _serve(
    cfg: PoolConfig,
    repo: str,
    job: str,
    *,
    github: GitHub,
    worker: Worker,
    state: PoolState,
    seen: list[int],
) -> RoundResult:
    """Mint one runner for ``job`` and hand it to the worker; the round ends here."""
    began = time.monotonic()
    request = github_api.repo_jit_config_request(repo, cfg.label, f"bentoo-worker-{job}")
    try:
        answer = _call(github, request, seen)
    except GitHubApiError:
        event("ERROR", "ci_mint", job=job, repo=repo, duration_ms=_ms(began), result="failed")
        return RoundResult("mint-failed", job)
    jitconfig = answer.get("encoded_jit_config")
    if not isinstance(jitconfig, str) or not jitconfig:
        event(
            "ERROR",
            "ci_mint",
            job=job,
            repo=repo,
            duration_ms=_ms(began),
            result="no encoded_jit_config in GitHub's answer",
        )
        return RoundResult("mint-failed", job)
    event("INFO", "ci_mint", job=job, repo=repo, duration_ms=_ms(began), result="ok")
    began = time.monotonic()
    try:
        worker.dispatch(job, repo, jitconfig)
    except WorkerUnreachable as err:
        event(
            "WARN",
            "ci_dispatch",
            job=job,
            repo=repo,
            duration_ms=_ms(began),
            result="unreachable",
            error=str(err),
        )
        return RoundResult("unreachable", job)
    except CiDispatchError as err:
        event(
            "ERROR",
            "ci_dispatch",
            job=job,
            repo=repo,
            duration_ms=_ms(began),
            result="failed",
            error=str(err),
        )
        return RoundResult("dispatch-failed", job)
    state.served.add(job)
    event("INFO", "ci_dispatch", job=job, repo=repo, duration_ms=_ms(began), result="ok")
    return RoundResult("dispatched", job)


def round(cfg: PoolConfig, state: PoolState, *, github: GitHub, worker: Worker) -> RoundResult:
    """One pass of the pool (R5.4, R6.1-R6.7, R6.10); ``state`` is updated in place."""
    try:
        if worker.busy():
            return RoundResult("busy")
        _report_results(worker)
    except WorkerUnreachable as err:
        event("WARN", "ci_worker_unreachable", worker=cfg.worker, error=str(err))
        return RoundResult("unreachable")
    # one VM per worker: an idle worker means every runner minted before is gone (R6.10)
    state.served.clear()
    seen: list[int] = []
    try:
        return _round_repos(cfg, state, github=github, worker=worker, seen=seen)
    except _AuthFailed:
        return RoundResult("auth-failed")
    finally:
        if seen:  # GitHub answered this round: count 401/403 rounds, reset on any other
            auth = any(status in _AUTH for status in seen)
            state.auth_failures = state.auth_failures + 1 if auth else 0


def _round_repos(
    cfg: PoolConfig, state: PoolState, *, github: GitHub, worker: Worker, seen: list[int]
) -> RoundResult:
    queued_ids: set[str] = set()
    for repo in cfg.repos:
        try:
            jobs = _queued(cfg, repo, github, seen)
        except GitHubApiError:
            continue  # its ERROR is out; the other repositories still get their turn
        candidates = [j for j in jobs if str(j["id"]) not in state.served]
        queued_ids |= {str(j["id"]) for j in jobs}
        if not candidates:
            continue
        try:
            private = _call(github, github_api.repo_request(repo), seen).get("private")
        except GitHubApiError:
            continue
        if private is not True:
            event("WARN", "ci_repo_refused", repo=repo, reason="not a private repository")
            continue
        return _serve(
            cfg,
            repo,
            str(candidates[0]["id"]),
            github=github,
            worker=worker,
            state=state,
            seen=seen,
        )
    state.served &= queued_ids
    return RoundResult("idle")
