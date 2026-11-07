"""``shidashi ci pool``: the host's poller, a round every ``poll_s`` (story 034, R6).

Registered on ``shidashi ci`` by :mod:`shidashi.ci_cli`; it lives here so ``ci_cli.py``
stays small. The token is read once, from its 0600 file, and only ``github_api.send``
sees it; the JIT configs GitHub mints go to the worker on ssh stdin and nowhere else.
"""

import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Annotated, NoReturn

import typer

from shidashi import ci_pool, config, github_api, remote, workers
from shidashi.ci_pool import PoolConfig, PoolConfigError
from shidashi.github_api import ApiRequest, GitHubApiError

#: R6.7: a revoked token must not spin.
AUTH_FAILURES_STOP = 3


def _die(message: object) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def _worker(cfg: PoolConfig) -> ci_pool.SshWorker:
    registry = workers.load_registry(config.workers_dir() / "workers.json")
    entry = registry.get(cfg.worker)
    if entry is None:
        known = ", ".join(sorted(registry)) or "none"
        _die(f"no paired worker named {cfg.worker!r} (paired: {known})")
    try:
        target = remote.Remote.for_worker(entry)
    except remote.RemoteUnreachable as err:
        _die(err)
    return ci_pool.SshWorker(target, cfg.source, cfg.vm)


class _Stop:
    """SIGTERM ends the loop after the current round (a user unit's stop)."""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        self.requested = True


def pool(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="ci.toml (default: $XDG_CONFIG_HOME/shidashi/ci.toml)."),
    ] = None,
    once: Annotated[bool, typer.Option("--once", help="One round, then exit 0.")] = False,
) -> None:
    """Hand queued jobs of the allowlisted private repositories to the worker."""
    run_pool(config_path, once=once)


def run_pool(
    config_path: Path | None, *, once: bool, sleep: Callable[[float], None] = time.sleep
) -> None:
    """The loop behind ``ci pool``; ``sleep`` injectable for a caller that must not wait."""
    try:
        cfg = ci_pool.load_config(config_path)
        token = github_api.read_token(cfg.token_file)
    except (PoolConfigError, GitHubApiError) as err:
        _die(err)

    def github(request: ApiRequest) -> object:
        return github_api.send(request, token)

    worker = _worker(cfg)
    state_path = ci_pool.default_state_path()
    stop = _Stop()
    # signal handlers belong to the main thread; a caller running the loop elsewhere
    # (a test) stops it by other means
    main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, stop) if main and not once else None
    try:
        while True:
            state = ci_pool.load_state(state_path)
            ci_pool.round(cfg, state, github=github, worker=worker)  # type: ignore[arg-type]
            ci_pool.save_state(state_path, state)
            if once:
                return
            if state.auth_failures >= AUTH_FAILURES_STOP:
                ci_pool.event(
                    "ERROR",
                    "ci_pool_stopped",
                    reason=f"GitHub refused the token {state.auth_failures} rounds in a row",
                )
                raise typer.Exit(1)
            if stop.requested:
                return
            sleep(cfg.vm.poll_s)
            if stop.requested:
                return
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
