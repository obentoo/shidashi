"""Story 028: host commands that need an owned arch's binhost refuse, naming the worker."""

from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner, Result

from shidashi import cli as cli_mod
from shidashi import config, workers
from shidashi.cli import app
from shidashi.workers import WorkerEntry

runner = CliRunner()


class _PastTheGuard(Exception):
    """Raised by the first step after the guard: the command went on."""


@pytest.fixture
def owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = config.workers_dir() / "workers.json"
    path.parent.mkdir(parents=True)
    entry = WorkerEntry(
        name="bentoo-lab",
        host_key="ssh-ed25519 AAAA",
        host_key_fingerprint="SHA256:x",
        paired_at="2026-10-17T00:00:00Z",
        cache_archs=("v3",),
    )
    workers.save_registry(path, {"bentoo-lab": entry})

    def past(*_a: object, **_k: object) -> None:
        raise _PastTheGuard

    monkeypatch.setattr(cli_mod, "_require_build_host", past)
    yield


def _run(*args: str) -> Result:
    return runner.invoke(app, list(args))


@pytest.mark.parametrize(
    "argv",
    [
        ("factory", "v3", "minimal", "systemd"),
        ("assemble", "v3", "minimal", "systemd"),
        ("build", "v3", "systemd", "--local"),
    ],
)
def test_a_host_command_on_an_owned_arch_is_refused_naming_the_worker(
    owned: None, argv: tuple[str, ...]
) -> None:
    result = _run(*argv)
    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, _PastTheGuard)
    assert "bentoo-lab" in result.output
    assert "shidashi worker job bentoo-lab" in result.output


@pytest.mark.parametrize(
    "argv",
    [("factory", "znver5", "minimal", "systemd"), ("assemble", "znver5", "minimal", "systemd")],
)
def test_an_arch_nobody_owns_goes_on(owned: None, argv: tuple[str, ...]) -> None:
    result = _run(*argv)
    assert isinstance(result.exception, _PastTheGuard), result.output


def test_the_guard_reads_the_registry(owned: None) -> None:
    with pytest.raises(typer.Exit) as exc:
        cli_mod._refuse_owned_arch("v3", "factory")
    assert exc.value.exit_code == 1
    cli_mod._refuse_owned_arch("znver5", "factory")  # nobody's: returns


def test_under_sudo_the_guard_reads_the_invoking_users_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host builds run as root through sudo: root's own registry is empty, and the
    owner is recorded in the registry of the user who typed the command."""
    import os
    import pwd

    user_home = tmp_path / "user"
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "root"))
    monkeypatch.setenv("SUDO_USER", "someone")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    real = pwd.getpwnam(pwd.getpwuid(os.getuid()).pw_name)
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda _name: pwd.struct_passwd(
            ("someone", "x", real.pw_uid, real.pw_gid, "", str(user_home), "/bin/sh")
        ),
    )
    path = user_home / ".local" / "share" / "shidashi" / "worker" / "workers.json"
    path.parent.mkdir(parents=True)
    entry = WorkerEntry(
        name="bentoo-lab",
        host_key="ssh-ed25519 AAAA",
        host_key_fingerprint="SHA256:x",
        paired_at="2026-10-17T00:00:00Z",
        cache_archs=("v3",),
    )
    workers.save_registry(path, {"bentoo-lab": entry})
    with pytest.raises(typer.Exit) as exc:
        cli_mod._refuse_owned_arch("v3", "factory")
    assert exc.value.exit_code == 1
