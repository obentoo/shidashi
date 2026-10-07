"""Tests of ``shidashi worker disk-init`` (host side) through Typer's CliRunner.

The command runs the worker's own ``shidashi disk-init DISK SERIAL`` over the pinned
transport; ``remote.run`` is faked, so ssh never runs. The registry lives under a
temporary ``XDG_DATA_HOME``.

Requirements exercised: R6.1 (issued from the host to a paired worker), and the relay
of the worker's refusals (R6.2-R6.4, R6.6 are decided on the worker).
"""

import base64
import hashlib
import shlex
import struct
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, config, remote, workers
from shidashi.cli import app

runner = CliRunner()

SERIAL = "AA000000000000000412"


def _ed25519_line(seed: str) -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()}"


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _out(result: Any) -> str:
    return result.stdout + (getattr(result, "stderr", "") or "")


def _entry(name: str, address: str) -> workers.WorkerEntry:
    key = _ed25519_line(name)
    return workers.WorkerEntry(
        name=name,
        address=address,
        host_key=key,
        host_key_fingerprint=_fingerprint(key),
        paired_at="2026-10-05T12:00:00+00:00",
        cpu_flags=("avx2",),
        image="20261005T1200",
    )


class _Remote:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: tuple[int, str, str] = (0, "/dev/sda1 SHIDASHI-WORK 238.5G /mnt/work\n", "")
        self.error: BaseException | None = None

    def __call__(self, rem: Any, command: str, **kw: Any) -> Any:
        self.calls.append({"remote": rem, "command": command, **kw})
        if self.error is not None:
            raise self.error
        code, out, err = self.result
        return remote.RemoteResult(command, code, out, err, 1.5)


@pytest.fixture
def fake_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Remote:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("COLUMNS", "200")
    wdir = config.workers_dir()
    wdir.mkdir(parents=True)
    workers.save_registry(
        wdir / "workers.json",
        {
            "bentoo-lab": _entry("bentoo-lab", "192.168.15.7"),
            "spare": _entry("spare", "192.168.15.9"),
        },
    )
    fake = _Remote()
    monkeypatch.setattr(remote, "run", fake)
    monkeypatch.setattr(cli, "run", fake, raising=False)
    return fake


def _words_after_disk_init(command: str) -> list[str]:
    words = shlex.split(command)
    assert "disk-init" in words, command
    return words[words.index("disk-init") + 1 :]


def test_disk_init_runs_the_workers_disk_init_on_the_named_worker(fake_remote: _Remote) -> None:
    result = runner.invoke(
        app, ["worker", "disk-init", "bentoo-lab", "/dev/sda", "--confirm", SERIAL]
    )
    assert result.exit_code == 0, _out(result)
    assert len(fake_remote.calls) == 1
    call = fake_remote.calls[0]
    assert (call["remote"].name, call["remote"].address) == ("bentoo-lab", "192.168.15.7")
    assert _words_after_disk_init(call["command"]) == ["/dev/sda", SERIAL]
    assert call["timeout"] == 600
    assert "/dev/sda1 SHIDASHI-WORK 238.5G /mnt/work" in _out(result)


@pytest.mark.parametrize(
    ("disk", "serial"),
    [
        ("/dev/sda", "AA00 0412; touch /tmp/pwned"),
        ("/dev/sda$(reboot)", SERIAL),
        ("/dev/sda", '\'"`id`"'),
    ],
)
def test_disk_init_passes_disk_and_serial_as_exactly_two_words(
    fake_remote: _Remote, disk: str, serial: str
) -> None:
    """The remote command is a shell string on the worker: input stays two words."""
    runner.invoke(app, ["worker", "disk-init", "bentoo-lab", disk, "--confirm", serial])
    assert len(fake_remote.calls) == 1
    assert _words_after_disk_init(fake_remote.calls[0]["command"]) == [disk, serial]


def test_disk_init_relays_the_workers_refusal_and_exit_code(fake_remote: _Remote) -> None:
    fake_remote.result = (1, "", "disk-init: serial ZZZ does not match /dev/sda\n")
    result = runner.invoke(
        app, ["worker", "disk-init", "bentoo-lab", "/dev/sda", "--confirm", "ZZZ"]
    )
    assert result.exit_code == 1
    assert "serial ZZZ does not match /dev/sda" in _out(result)


def test_disk_init_of_an_unknown_worker_exits_1_listing_the_registered_ones(
    fake_remote: _Remote,
) -> None:
    result = runner.invoke(app, ["worker", "disk-init", "nope", "/dev/sda", "--confirm", SERIAL])
    assert result.exit_code == 1
    out = _out(result)
    assert "bentoo-lab" in out and "spare" in out
    assert fake_remote.calls == []


def test_disk_init_needs_the_serial_confirmation(fake_remote: _Remote) -> None:
    result = runner.invoke(app, ["worker", "disk-init", "bentoo-lab", "/dev/sda"])
    assert result.exit_code == 2
    assert fake_remote.calls == []


def test_disk_init_refuses_a_worker_presenting_another_host_key(fake_remote: _Remote) -> None:
    pinned = _fingerprint(_ed25519_line("bentoo-lab"))
    presented = _fingerprint(_ed25519_line("impostor"))
    fake_remote.error = remote.HostKeyMismatch("bentoo-lab", pinned, presented)
    result = runner.invoke(
        app, ["worker", "disk-init", "bentoo-lab", "/dev/sda", "--confirm", SERIAL]
    )
    out = _out(result)
    assert result.exit_code == 1
    assert pinned in out and presented in out
    assert "Traceback" not in out


def test_disk_init_reports_an_unreachable_worker(fake_remote: _Remote) -> None:
    fake_remote.error = remote.RemoteUnreachable("bentoo-lab", "192.168.15.7")
    result = runner.invoke(
        app, ["worker", "disk-init", "bentoo-lab", "/dev/sda", "--confirm", SERIAL]
    )
    assert result.exit_code == 1
    assert "192.168.15.7" in _out(result)
    assert "Traceback" not in _out(result)


# --- v2 (review): every failure the command can meet ends in exit 1, never a traceback


def test_disk_init_a_malformed_registry_exits_1_naming_it(fake_remote: _Remote) -> None:
    (config.workers_dir() / "workers.json").write_text("{not json")
    result = runner.invoke(
        app, ["worker", "disk-init", "bentoo-lab", "/dev/sda", "--confirm", SERIAL]
    )
    assert result.exit_code == 1, _out(result)
    assert "workers.json" in _out(result)
    assert "Traceback" not in _out(result)
    assert fake_remote.calls == []


def test_disk_init_without_an_ssh_client_exits_1_without_a_traceback(
    fake_remote: _Remote,
) -> None:
    fake_remote.error = remote.RemoteError("ssh not found")
    result = runner.invoke(
        app, ["worker", "disk-init", "bentoo-lab", "/dev/sda", "--confirm", SERIAL]
    )
    assert result.exit_code == 1, _out(result)
    assert "ssh not found" in _out(result)
    assert "Traceback" not in _out(result)


def test_disk_init_passes_the_carried_expected_fingerprint_to_the_transport(
    fake_remote: _Remote,
) -> None:
    runner.invoke(app, ["worker", "disk-init", "bentoo-lab", "/dev/sda", "--confirm", SERIAL])
    rem = fake_remote.calls[0]["remote"]
    assert rem.expected_fingerprint == _fingerprint(_ed25519_line("bentoo-lab"))
