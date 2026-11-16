"""``vm ssh-config`` and ``vm env`` from the command line, with no VM and no worker.

What crosses to a worker is replaced at the functions ``vm run`` already uses
(:func:`shidashi.vm_dev_remote_cli._on_named_worker`,
:func:`shidashi.vm_dev_remote_run.run_on_worker`), so the test sees the script, the
stdin and the ``ProxyCommand`` exactly as they would leave this host.
"""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import vm, vm_access, vm_dev_record, vm_dev_remote_cli, vm_dev_remote_run
from shidashi import vm_dev_remote_shell as shell
from shidashi.cli import app
from shidashi.remote import Remote

_COMMIT = "5" * 40


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SHIDASHI_DEV_RELAYED", raising=False)
    return tmp_path / "state" / "shidashi" / "vm"


@dataclass
class _Spec:
    cid: int


@dataclass
class _LocalSession:
    key: Path
    spec: _Spec


def _record() -> vm_dev_record.WorkerSession:
    return vm_dev_record.WorkerSession(
        name="dev", worker="bentoo-lab", commit=_COMMIT, started="2026-11-16T00:00:00Z"
    )


@pytest.fixture
def worker(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Session ``dev`` recorded on bentoo-lab; every guest command lands in the list."""
    vm_dev_record.write_record(_record())
    target = Remote("bentoo-lab", "10.0.0.3", tmp_path / "wkey", tmp_path / "known_hosts")
    calls: list[dict[str, Any]] = []

    def on_named(name: str, attempt: Any, **_kw: Any) -> Any:
        assert name == "bentoo-lab"
        return attempt(target)

    def run_on_worker(remote: Remote, session: Any, command: str, **kw: Any) -> int:
        stdin = kw.get("stdin")
        calls.append(
            {
                "remote": remote,
                "session": session,
                "command": command,
                "stdin": stdin.read() if stdin is not None else None,
            }
        )
        return 0

    monkeypatch.setattr(vm_dev_remote_cli, "_on_named_worker", on_named)
    monkeypatch.setattr(vm_dev_remote_run, "run_on_worker", run_on_worker)
    return calls


def test_a_local_session_gets_its_host_written_beside_it(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "id_ed25519"
    monkeypatch.setattr(vm, "load_session", lambda name: _LocalSession(key, _Spec(9)))
    result = CliRunner().invoke(app, ["vm", "ssh-config", "--name", "dev"])
    assert result.exit_code == 0, result.output
    block = vm_access.local_host_block("dev", key=key, cid=9)
    assert result.stdout == block
    written = state / "dev" / "ssh_config"
    assert written.read_text() == block
    assert oct(written.stat().st_mode & 0o777) == "0o600"
    assert f"Include {state}/*/ssh_config" in " ".join(result.stderr.split())


def test_a_worker_session_authorizes_a_key_made_here_and_relays_through_the_worker(
    state: Path, worker: list[dict[str, Any]]
) -> None:
    result = CliRunner().invoke(app, ["vm", "ssh-config", "--name", "dev"])
    assert result.exit_code == 0, result.output
    key = state / "dev" / "client_ed25519"
    pub = key.with_suffix(".pub").read_bytes()
    assert pub.startswith(b"ssh-ed25519 ")
    [authorize] = worker
    assert authorize["command"] == vm_access.authorize_script()
    assert authorize["stdin"] == pub  # the key, never the session's own
    proxy = next(line.strip() for line in result.stdout.splitlines() if "ProxyCommand" in line)
    words = shlex.split(proxy.removeprefix("ProxyCommand ").replace("%%", "%"))
    assert words[0] == "ssh" and words[-2] == "root@10.0.0.3"
    assert words[-1] == shell.relayed_ssh_relay_command(_COMMIT, "dev")
    assert f"IdentityFile {key}" in result.stdout
    # a second run reuses the key: the editor's Host keeps working
    again = CliRunner().invoke(app, ["vm", "ssh-config", "--name", "dev"])
    assert again.exit_code == 0 and key.with_suffix(".pub").read_bytes() == pub


def test_a_guest_that_refuses_the_key_is_an_error(
    state: Path, worker: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm_dev_remote_run, "run_on_worker", lambda *_a, **_k: 2)
    result = CliRunner().invoke(app, ["vm", "ssh-config", "--name", "dev"])
    assert result.exit_code == 1
    assert "refused the key" in result.stderr
    assert not (state / "dev" / "ssh_config").exists()


def test_env_sends_the_value_as_stdin_and_never_as_a_word(
    state: Path, worker: list[dict[str, Any]]
) -> None:
    result = CliRunner().invoke(
        app, ["vm", "env", "--name", "dev", "CLAUDE_CODE_OAUTH_TOKEN"], input="s3cr3t\n"
    )
    assert result.exit_code == 0, result.output
    [sent] = worker
    assert sent["command"] == vm_access.env_script("CLAUDE_CODE_OAUTH_TOKEN")
    assert "s3cr3t" not in sent["command"] and "s3cr3t" not in result.output


@pytest.mark.parametrize("variable", ["1A", "A-B", "A=B"])
def test_env_refuses_a_name_a_shell_would_not_export(
    state: Path, worker: list[dict[str, Any]], variable: str
) -> None:
    result = CliRunner().invoke(app, ["vm", "env", "--name", "dev", variable], input="x")
    assert result.exit_code == 2
    assert worker == []


def test_the_relay_is_hidden_from_help() -> None:
    help_text = CliRunner().invoke(app, ["vm", "--help"]).output
    assert "ssh-config" in help_text and "env" in help_text
    assert "ssh-relay" not in help_text
