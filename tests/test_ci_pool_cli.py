"""``shidashi ci pool [--config PATH] [--once]`` (story 034, tasks 4.5 and 4.4's
``SshWorker``; R5.1, R5.2, R6.3-R6.10, R9.1, Q4, Q11).

Contract (design.md, "shidashi/ci_pool.py + shidashi ci pool"; story.md Q11):

- the config defaults to ``$XDG_CONFIG_HOME/shidashi/ci.toml``; a missing or bad one exits 1
  naming the file and the field before any GitHub call (R6.9);
- ``--once`` does one round and exits 0 (R6.8); without it, a round every ``poll_s``;
  401/403 on three consecutive rounds stops with exit 1 and an ERROR event (R6.7);
- a worker running a ``shidashi-ci-*`` unit gets no new runner (R6.4); an unreachable one
  is a WARN and no runner (R6.5);
- an idle worker's ``/mnt/work/ci/results/*.json`` are read and removed (``SshWorker.results``)
  and a failed job is a WARN ``ci_job_failed``; then a job still queued is minted again,
  whatever the persisted state says (R6.10);
- the served state (``{"served": [...], "auth_failures": n}``) is written through a
  temporary file renamed over it, and a corrupt one is set aside and the round starts
  empty (Q11);
- the commit shipped is ``source``'s HEAD; a ``source`` without one is an ERROR
  ``ci_dispatch`` event naming it, and nothing is started on the worker;
- the token and the JIT config are in no ssh argument, no output, no log line, no file
  the pool writes (R5.1, R5.2).

GitHub is ``FakeGitHubApi`` (a local HTTP server every HTTPS connection goes to); the
worker is ``FakeWorker`` (story 010: ssh, systemctl and systemd-run shims). The dispatch's
remote ``systemd-run`` is answered 0 without running (a rule): /run is the real host's.
"""

import json
import logging
import os
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli
from tests._fake_ci_pool import level_of, pool_events, write_config
from tests._fake_github_api import (
    FakeGitHubApi,
    Reply,
    mint_jit_config,
    serving_fake_github,
)
from tests._fake_worker import FakeWorker

pool_runner = CliRunner()
REPO = "obentoo/private-app"
RUNS = f"/repos/{REPO}/actions/runs"
JOBS = f"/repos/{REPO}/actions/runs/101/jobs"
JIT = f"/repos/{REPO}/actions/runners/generate-jitconfig"


@dataclass
class PoolHost:
    api: FakeGitHubApi
    fw: FakeWorker
    config: Path
    token: str
    jitconfig: str
    state_home: Path
    caplog: pytest.LogCaptureFixture

    def queue(self, job: int = 9001) -> None:
        self.api.routes[("GET", RUNS)] = Reply(
            200, {"total_count": 1, "workflow_runs": [{"id": 101, "status": "queued"}]}
        )
        self.api.routes[("GET", JOBS)] = Reply(
            200,
            {
                "total_count": 1,
                "jobs": [
                    {
                        "id": job,
                        "run_id": 101,
                        "status": "queued",
                        "labels": ["self-hosted", "bentoo-worker"],
                        "created_at": "2026-11-03T10:00:00Z",
                        "name": "proof",
                    }
                ],
            },
        )

    def pool(self, *extra: str, config: Path | None = None, default: bool = False) -> Result:
        """``shidashi ci pool EXTRA``; ``--config`` this host's file unless ``default``."""
        argv = ["ci", "pool", *extra]
        if not default:
            argv += ["--config", str(config or self.config)]
        return pool_runner.invoke(cli.app, argv)

    def secrets_never_shown(self, result: Result) -> None:
        """R5.1, R5.2, Q4: in no ssh argv or remote command, stream, log, or written file."""
        ssh = "\n".join(str(c) for c in self.fw.calls("ssh"))
        for secret, what in ((self.token, "token"), (self.jitconfig, "JIT config")):
            for where, text in (
                ("ssh", ssh),
                ("stdout", result.stdout),
                ("stderr", result.stderr),
                ("logs", self.caplog.text),
                ("exception", repr(result.exception)),
            ):
                assert secret not in text, f"the {what} leaked to {where}"
            for base in (self.state_home, Path(os.environ["SHIDASHI_RUNS"])):
                for path in base.rglob("*"):
                    if path.is_file():
                        assert secret not in path.read_text(errors="replace"), (what, path)

    def state_files(self) -> list[Path]:
        return sorted(p for p in self.state_home.rglob("*") if p.is_file())


@pytest.fixture
def host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[PoolHost]:
    caplog.set_level(logging.DEBUG)
    monkeypatch.chdir(tmp_path)
    fw = FakeWorker.install(tmp_path / "fw", monkeypatch)
    fw.register()
    # the remote dispatch is accepted without running: /run on this host is not the worker's
    fw.fail(pattern=r"systemd-run", code=0)
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    config = write_config(tmp_path / "cfg", repos=[REPO], worker=fw.name, vm={"poll_s": 1})
    token = (tmp_path / "cfg" / "github-ci.token").read_text(encoding="utf-8").strip()
    jitconfig = mint_jit_config()
    with serving_fake_github(monkeypatch) as api:
        api.routes[("GET", RUNS)] = Reply(200, {"total_count": 0, "workflow_runs": []})
        api.routes[("GET", f"/repos/{REPO}")] = Reply(200, {"full_name": REPO, "private": True})
        api.routes[("POST", JIT)] = Reply(
            201, {"runner": {"id": 1}, "encoded_jit_config": jitconfig}
        )
        yield PoolHost(api, fw, config, token, jitconfig, state_home, caplog)
    fw.close()


def test_cli_once_with_nothing_queued_does_one_round_and_exits_0(host: PoolHost) -> None:
    """R6.8."""
    result = host.pool("--once")
    assert result.exit_code == 0, result.output
    # one round = the two filtered listings, queued and in_progress (tech review 4.x)
    assert len(host.api.at(RUNS)) == 2, host.api.requests
    assert host.api.at(JIT) == []
    host.secrets_never_shown(result)


def test_cli_once_hands_a_queued_job_to_the_worker_and_never_shows_a_secret(host: PoolHost) -> None:
    """R6.1, R5.1, R5.2: one runner minted, one ``shidashi-ci-<job>`` unit asked for."""
    host.queue(9001)
    result = host.pool("--once")
    assert result.exit_code == 0, result.output
    assert len(host.api.at(JIT)) == 1
    assert any("shidashi-ci-9001" in c for c in host.fw.ssh_commands()), host.fw.ssh_commands()
    host.secrets_never_shown(result)


def test_cli_once_mints_nothing_while_the_worker_runs_a_ci_vm(host: PoolHost) -> None:
    """R6.4: the worker's own ``shidashi-ci-*`` unit says it is busy."""
    host.fw.activate_unit("shidashi-ci-7000")
    host.queue(9002)
    result = host.pool("--once")
    assert result.exit_code == 0, result.output
    assert host.fw.ssh_commands(), "the worker was never asked"
    assert host.api.at(JIT) == []


def test_cli_a_shidashi_job_on_the_worker_does_not_hold_back_a_ci_job(host: PoolHost) -> None:
    """Hostile collapse of R6.4: only a CI VM makes the worker busy -- a Shidashi job
    (``shidashi-job-*``) runs beside a CI VM by design (R7.3)."""
    host.fw.activate_unit("shidashi-job-fac-v3")
    host.queue(9003)
    result = host.pool("--once")
    assert result.exit_code == 0, result.output
    assert len(host.api.at(JIT)) == 1


@pytest.mark.parametrize(
    ("spoil", "field"),
    [
        pytest.param("missing", None, id="missing"),
        pytest.param("token-0644", "token_file", id="token-0644"),
        pytest.param("no-repos", "repos", id="no-repos"),
    ],
)
def test_cli_a_bad_config_exits_1_naming_it_before_any_request(
    host: PoolHost, tmp_path: Path, spoil: str, field: str | None
) -> None:
    """R6.9."""
    bad = tmp_path / "bad"
    if spoil == "missing":
        path = bad / "ci.toml"
    elif spoil == "token-0644":
        path = write_config(bad, token_mode=0o644, repos=[REPO])
    else:
        path = write_config(bad, repos=[])
    result = host.pool("--once", config=path)
    assert result.exit_code == 1, result.output
    shown = "".join((result.output + result.stderr).split())
    assert "".join(str(path).split()) in shown
    if field:
        assert field in shown
    assert host.api.requests == []


def test_cli_the_default_config_is_read_from_xdg_config_home(
    host: PoolHost, tmp_path: Path
) -> None:
    default = tmp_path / "xdg-config" / "shidashi" / "ci.toml"
    result = host.pool("--once", default=True)
    assert result.exit_code == 1, result.output
    assert "".join(str(default).split()) in "".join((result.output + result.stderr).split())
    default.parent.mkdir(parents=True)
    default.write_text(host.config.read_text(encoding="utf-8"), encoding="utf-8")
    result = host.pool("--once", default=True)
    assert result.exit_code == 0, result.output
    assert host.api.at(RUNS)


def test_cli_three_rounds_of_401_stop_the_pool_with_exit_1(host: PoolHost) -> None:
    """R6.7: a revoked token must not spin. ``poll_s`` is 1 here."""
    host.api.routes[("GET", RUNS)] = Reply(401, {"message": "Bad credentials"})
    outcome: dict[str, Result] = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("r", host.pool()), daemon=True)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "the pool kept polling after three 401 rounds"
    result = outcome["r"]
    assert result.exit_code == 1, result.output
    assert len(host.api.at(RUNS)) == 3, len(host.api.at(RUNS))
    seen = pool_events(result.stderr, host.caplog.records)
    assert any(level_of(e) == "ERROR" for e in seen), seen
    host.secrets_never_shown(result)


def test_cli_once_keeps_the_served_state_through_replace_and_an_idle_worker_mints_again(
    host: PoolHost,
) -> None:
    """R6.3 across runs while the job's unit runs; R6.10 once the worker is idle again --
    a persisted ``served`` must not hold a still-queued job back. Q11: the state is a
    renamed temporary, nothing half-written left."""
    host.queue(9004)
    first = host.pool("--once")
    assert first.exit_code == 0, first.output
    assert len(host.api.at(JIT)) == 1
    files = host.state_files()
    assert len(files) == 1, files
    assert "9004" in {str(j) for j in json.loads(files[0].read_text(encoding="utf-8"))["served"]}
    host.fw.activate_unit("shidashi-ci-9004")  # the dispatched unit runs the job's VM
    second = host.pool("--once")
    assert second.exit_code == 0, second.output
    assert len(host.api.at(JIT)) == 1, "a second runner while the first may still be alive"
    host.fw.deactivate_unit("shidashi-ci-9004")  # the VM ended; GitHub still lists the job
    third = host.pool("--once")
    assert third.exit_code == 0, third.output
    assert len(host.api.at(JIT)) == 2, "a still-queued job was not minted again"
    files = host.state_files()
    assert not [p for p in files if p.name.endswith((".tmp", ".part")) or p.name.startswith(".")]
    host.secrets_never_shown(third)


@pytest.mark.parametrize(("rc", "reason"), [(4, "time"), (3, "disk")])
def test_cli_once_reports_a_failed_job_the_worker_left_and_takes_its_result_away(
    host: PoolHost, rc: int, reason: str
) -> None:
    """``SshWorker.results()``: one ssh reads and removes ``results/*.json``."""
    left = {"job": "9001", "repo": REPO, "rc": rc, "ended": "2026-11-03T10:30:00Z"}
    result_file = host.fw.put("ci/results/9001.json", json.dumps(left))
    result = host.pool("--once")
    assert result.exit_code == 0, result.output
    seen = pool_events(result.stderr, host.caplog.records)
    failed = [e for e in seen if e.get("event") == "ci_job_failed"]
    assert len(failed) == 1 and level_of(failed[0]) == "WARNING", seen
    assert str(failed[0].get("job")) == "9001" and reason in json.dumps(failed[0]).lower()
    assert not result_file.exists(), "the result was left on the worker"


def test_cli_once_an_unreachable_worker_is_a_warn_and_no_runner_is_minted(
    host: PoolHost,
) -> None:
    """R6.5: ``busy()`` over ssh fails -- a WARN, not a crash and not a runner."""
    host.fw.unreachable()
    host.queue(9006)
    result = host.pool("--once")
    assert result.exit_code == 0, result.output
    assert host.fw.calls("ssh"), "the worker was never asked"
    assert host.api.at(JIT) == []
    seen = pool_events(result.stderr, host.caplog.records)
    assert any(level_of(e) == "WARNING" for e in seen), seen


def test_cli_once_a_source_without_a_commit_is_an_error_naming_it_and_starts_nothing(
    host: PoolHost, tmp_path: Path
) -> None:
    """``source`` is a work tree (the config accepts it) with no commit to ship: one
    ERROR ``ci_dispatch`` event naming it; no unit on the worker; the job unserved."""
    source = tmp_path / "fresh-checkout"
    subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
    config = write_config(
        tmp_path / "cfg-fresh", repos=[REPO], worker=host.fw.name, source=str(source)
    )
    host.queue(9007)
    result = host.pool("--once", config=config)
    assert result.exit_code == 0, result.output
    seen = pool_events(result.stderr, host.caplog.records)
    errors = [e for e in seen if e.get("event") == "ci_dispatch" and level_of(e) == "ERROR"]
    assert len(errors) == 1, seen
    assert str(source) in json.dumps(errors[0]), errors[0]
    assert not [c for c in host.fw.ssh_commands() if "systemd-run" in c]
    host.secrets_never_shown(result)


def test_cli_once_a_corrupt_state_file_is_set_aside_and_the_round_starts_empty(
    host: PoolHost,
) -> None:
    """Q11: unreadable state is moved aside (kept for a look), never a crash."""
    host.queue(9005)
    assert host.pool("--once").exit_code == 0
    files = host.state_files()
    assert len(files) == 1, files
    files[0].write_text("{ not json", encoding="utf-8")
    result = host.pool("--once")
    assert result.exit_code == 0, result.output
    assert len(host.api.at(JIT)) == 2, "the round did not go on past the corrupt state"
    aside = [p for p in host.state_files() if "corrupt" in p.name]
    assert len(aside) == 1 and aside[0].parent == files[0].parent
    assert aside[0].read_text(encoding="utf-8") == "{ not json"
    assert files[0].read_text(encoding="utf-8") != "{ not json"
