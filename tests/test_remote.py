"""Tests of shidashi.remote -- SSH to a registered worker, only through its pinned key.

``ssh_argv`` is pure; ``run`` takes the runner as a parameter, so ssh never runs here.
The stderr fixtures are OpenSSH's own wording (StrictHostKeyChecking=yes).

Requirements exercised: R4.4 (one connection that verifies the pin), R4.5 (a different
host key is refused, naming the worker and both fingerprints).
"""

import base64
import hashlib
import shlex
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, remote, workers


def _ed25519_line(seed: str, comment: str = "") -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


PINNED = _ed25519_line("pinned")
IMPOSTOR = _ed25519_line("impostor")


def _options(argv: list[str]) -> dict[str, str]:
    """The ``-o Key=Value`` pairs of an ssh argv."""
    opts: dict[str, str] = {}
    for i, arg in enumerate(argv):
        if arg == "-o":
            key, _, value = argv[i + 1].partition("=")
            opts[key] = value
        elif arg.startswith("-o") and len(arg) > 2:
            key, _, value = arg[2:].partition("=")
            opts[key] = value
    return opts


def _remote(tmp_path: Path, name: str = "bentoo-lab", address: str = "192.168.15.7"):
    return remote.Remote(
        name=name, address=address, key=tmp_path / "id_ed25519", known_hosts=tmp_path / "kh"
    )


class _Runner:
    """A subprocess.run stand-in: records argv, answers with one canned result."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def __call__(self, argv: list[str], *_a: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        out, err = self.stdout, self.stderr
        if not (kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")):
            return subprocess.CompletedProcess(argv, self.returncode, out.encode(), err.encode())
        return subprocess.CompletedProcess(argv, self.returncode, out, err)


def _mismatch_stderr(known_hosts: Path, presented: str) -> str:
    return (
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!\n"
        "Someone could be eavesdropping on you right now (man-in-the-middle attack)!\n"
        "It is also possible that a host key has just been changed.\n"
        "The fingerprint for the ED25519 key sent by the remote host is\n"
        f"{presented}.\n"
        "Please contact your system administrator.\n"
        f"Add correct host key in {known_hosts} to get rid of this message.\n"
        f"Offending ED25519 key in {known_hosts}:1\n"
        "  remove with:\n"
        f"  ssh-keygen -f '{known_hosts}' -R 'bentoo-lab'\n"
        "Host key for bentoo-lab has changed and you have requested strict checking.\n"
        "Host key verification failed.\n"
    )


# --- ssh_argv (pure) ----------------------------------------------------------------


def test_ssh_argv_verifies_the_pinned_key_and_never_asks_or_accepts(tmp_path: Path) -> None:
    rem = _remote(tmp_path)
    argv = remote.ssh_argv(rem, "uname -r", timeout=7)
    assert argv[0] == "ssh"
    assert argv[argv.index("-i") + 1] == str(tmp_path / "id_ed25519")
    opts = _options(argv)
    assert opts["IdentitiesOnly"] == "yes"
    assert opts["BatchMode"] == "yes"
    assert opts["StrictHostKeyChecking"] == "yes"
    assert opts["UserKnownHostsFile"] == str(tmp_path / "kh")
    assert opts["HostKeyAlias"] == "bentoo-lab"
    assert opts["ConnectTimeout"] == "7"
    assert opts["ServerAliveInterval"] == "30"
    assert argv[-2:] == ["root@192.168.15.7", "uname -r"]


def test_ssh_argv_never_carries_an_option_that_skips_the_pin(tmp_path: Path) -> None:
    argv = remote.ssh_argv(_remote(tmp_path), "true")
    joined = " ".join(argv)
    for forbidden in ("StrictHostKeyChecking=no", "accept-new", "/dev/null"):
        assert forbidden not in joined, forbidden
    for flag in ("-A", "-t", "-tt"):  # no agent forwarding, no terminal
        assert flag not in argv, flag


def test_ssh_argv_default_timeout_is_ten_seconds(tmp_path: Path) -> None:
    assert _options(remote.ssh_argv(_remote(tmp_path), "true"))["ConnectTimeout"] == "10"


# Hostile halves of "the pin follows the name": two workers at one address keep two
# pins; one worker at two addresses keeps one pin. Then the plain case above.


def test_ssh_argv_two_workers_at_one_address_keep_their_own_pins(tmp_path: Path) -> None:
    a = remote.ssh_argv(_remote(tmp_path, "lab", "192.168.15.7"), "true")
    b = remote.ssh_argv(_remote(tmp_path, "lab2", "192.168.15.7"), "true")
    assert _options(a)["HostKeyAlias"] == "lab"
    assert _options(b)["HostKeyAlias"] == "lab2"


def test_ssh_argv_one_worker_at_a_new_address_keeps_its_pin(tmp_path: Path) -> None:
    before = remote.ssh_argv(_remote(tmp_path, "lab", "192.168.15.7"), "true")
    after = remote.ssh_argv(_remote(tmp_path, "lab", "192.168.15.42"), "true")
    assert _options(before)["HostKeyAlias"] == _options(after)["HostKeyAlias"] == "lab"
    assert _options(before)["UserKnownHostsFile"] == _options(after)["UserKnownHostsFile"]
    assert after[-2] == "root@192.168.15.42"


def test_ssh_argv_keeps_the_remote_command_as_one_argument(tmp_path: Path) -> None:
    command = "shidashi disk-init /dev/sda " + shlex.quote("SN 1; touch /tmp/x")
    argv = remote.ssh_argv(_remote(tmp_path), command)
    assert argv[-1] == command
    assert argv.count(command) == 1


def test_remote_for_worker_takes_key_and_known_hosts_from_workers_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    entry = workers.WorkerEntry(
        name="bentoo-lab",
        address="192.168.15.7",
        host_key=PINNED,
        host_key_fingerprint=_fingerprint(PINNED),
        paired_at="2026-10-05T12:00:00+00:00",
        cpu_flags=("avx2",),
        image="20261005T1200",
    )
    rem = remote.Remote.for_worker(entry)
    assert rem.name == "bentoo-lab"
    assert rem.address == "192.168.15.7"
    assert rem.key == config.workers_dir() / "id_ed25519"
    assert rem.known_hosts == config.workers_dir() / "known_hosts"


# --- run: results and the exit-255 classification (R4.5) -----------------------------


def test_run_returns_the_commands_result(tmp_path: Path) -> None:
    runner = _Runner(0, stdout="6.17.1-gentoo\n")
    rem = _remote(tmp_path)
    result = remote.run(rem, "uname -r", timeout=10, runner=runner)
    assert runner.calls == [remote.ssh_argv(rem, "uname -r", timeout=10)]
    assert result.command == "uname -r"
    assert result.exit_code == 0
    assert result.stdout == "6.17.1-gentoo\n"
    assert result.duration_s >= 0


def test_run_returns_a_failing_remote_command_without_raising(tmp_path: Path) -> None:
    runner = _Runner(3, stderr="mount: /mnt/work: wrong fs type\n")
    result = remote.run(_remote(tmp_path), "mount /mnt/work", timeout=10, runner=runner)
    assert result.exit_code == 3
    assert "wrong fs type" in result.stderr


def test_run_raises_host_key_mismatch_naming_the_worker_and_both_fingerprints(
    tmp_path: Path,
) -> None:
    import dataclasses

    rem = dataclasses.replace(_remote(tmp_path), expected_fingerprint=_fingerprint(PINNED))
    rem.known_hosts.write_text(f"bentoo-lab {PINNED}\n")
    runner = _Runner(255, stderr=_mismatch_stderr(rem.known_hosts, _fingerprint(IMPOSTOR)))
    with pytest.raises(remote.HostKeyMismatch) as err:
        remote.run(rem, "true", timeout=10, runner=runner)
    message = str(err.value)
    assert "bentoo-lab" in message
    assert _fingerprint(PINNED) in message
    assert _fingerprint(IMPOSTOR) in message


def test_run_refuses_a_worker_whose_name_has_no_pinned_key(tmp_path: Path) -> None:
    rem = _remote(tmp_path)
    rem.known_hosts.write_text("")
    stderr = (
        "No ED25519 host key is known for bentoo-lab and you have requested strict checking.\n"
        "Host key verification failed.\n"
    )
    with pytest.raises((remote.HostKeyMismatch, remote.RemoteUnreachable)):
        remote.run(rem, "true", timeout=10, runner=_Runner(255, stderr=stderr))


@pytest.mark.parametrize(
    "stderr",
    [
        "ssh: connect to host 192.168.15.7 port 22: Connection refused\n",
        "ssh: connect to host 192.168.15.7 port 22: No route to host\n",
        "ssh: connect to host 192.168.15.7 port 22: Connection timed out\n",
        "",
    ],
)
def test_run_raises_unreachable_on_any_other_ssh_failure(tmp_path: Path, stderr: str) -> None:
    rem = _remote(tmp_path)
    with pytest.raises(remote.RemoteUnreachable) as err:
        remote.run(rem, "true", timeout=10, runner=_Runner(255, stderr=stderr))
    assert "bentoo-lab" in str(err.value)
    assert "192.168.15.7" in str(err.value)


def test_run_does_not_treat_a_command_quoting_ssh_as_a_host_key_mismatch(
    tmp_path: Path,
) -> None:
    """Hostile: the remote command's own output may quote ssh (a log, a grep); only
    ssh's exit 255 makes it a refusal."""
    rem = _remote(tmp_path)
    runner = _Runner(1, stderr="journal: Host key verification failed.\n")
    result = remote.run(rem, "journalctl -u sshd", timeout=10, runner=runner)
    assert result.exit_code == 1


def test_check_runs_true_once_on_the_entrys_pinned_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    entry = workers.WorkerEntry(
        name="bentoo-lab",
        address="192.168.15.7",
        host_key=PINNED,
        host_key_fingerprint=_fingerprint(PINNED),
        paired_at="2026-10-05T12:00:00+00:00",
        cpu_flags=("avx2",),
        image="20261005T1200",
    )
    calls: list[tuple[Any, str]] = []

    def _run(rem: Any, command: str, **_kw: Any) -> Any:
        calls.append((rem, command))
        return remote.RemoteResult(command, 0, "", "", 0.01)

    monkeypatch.setattr(remote, "run", _run)
    remote.check(entry)
    assert len(calls) == 1
    rem, command = calls[0]
    assert command == "true"
    assert (rem.name, rem.address) == ("bentoo-lab", "192.168.15.7")
    assert rem.known_hosts == config.workers_dir() / "known_hosts"


def test_check_propagates_a_host_key_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    entry = workers.WorkerEntry(
        name="bentoo-lab",
        address="192.168.15.7",
        host_key=PINNED,
        host_key_fingerprint=_fingerprint(PINNED),
        paired_at="2026-10-05T12:00:00+00:00",
        cpu_flags=("avx2",),
        image="20261005T1200",
    )

    def _run(*_a: Any, **_kw: Any) -> Any:
        raise remote.HostKeyMismatch("bentoo-lab", _fingerprint(PINNED), _fingerprint(IMPOSTOR))

    monkeypatch.setattr(remote, "run", _run)
    with pytest.raises(remote.HostKeyMismatch):
        remote.check(entry)


# --- v2 (review + contract C3): the expected key travels with the transport ----------


def _entry(**over: Any) -> Any:
    fields: dict[str, Any] = {
        "name": "bentoo-lab",
        "address": "192.168.15.7",
        "host_key": PINNED,
        "host_key_fingerprint": _fingerprint(PINNED),
        "paired_at": "2026-10-05T12:00:00+00:00",
        "cpu_flags": ("avx2",),
        "image": "20261005T1200",
    }
    fields.update(over)
    return workers.WorkerEntry(**fields)


def test_remote_for_worker_carries_the_expected_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rem = remote.Remote.for_worker(_entry())
    assert rem.expected_fingerprint == _fingerprint(PINNED)


def test_a_mismatch_names_the_expected_fingerprint_even_with_an_empty_known_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: the known_hosts file was truncated; the registry still knows the pin."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    rem = remote.Remote.for_worker(_entry())
    rem.known_hosts.parent.mkdir(parents=True, exist_ok=True)
    rem.known_hosts.write_text("")
    runner = _Runner(255, stderr=_mismatch_stderr(rem.known_hosts, _fingerprint(IMPOSTOR)))
    with pytest.raises(remote.HostKeyMismatch) as err:
        remote.run(rem, "true", timeout=10, runner=runner)
    assert err.value.expected == _fingerprint(PINNED)
    assert err.value.presented == _fingerprint(IMPOSTOR)


def test_no_key_known_for_the_name_is_a_mismatch_with_no_expected_key(tmp_path: Path) -> None:
    rem = _remote(tmp_path)
    rem.known_hosts.write_text("")
    stderr = (
        "No ED25519 host key is known for bentoo-lab and you have requested strict checking.\n"
        "Host key verification failed.\n"
    )
    with pytest.raises(remote.HostKeyMismatch) as err:
        remote.run(rem, "true", timeout=10, runner=_Runner(255, stderr=stderr))
    assert err.value.expected is None
    assert err.value.presented is None
    assert "bentoo-lab" in str(err.value)


def test_run_bounds_the_whole_command_with_its_timeout(tmp_path: Path) -> None:
    runner = _Runner(0)
    remote.run(_remote(tmp_path), "sleep 1", timeout=600, runner=runner)
    assert runner.kwargs[0].get("timeout") == 600
    assert _options(runner.calls[0])["ConnectTimeout"] == "10"  # min(600, 10)


def test_a_command_over_its_timeout_is_an_unreachable_worker(tmp_path: Path) -> None:
    def _slow(argv: list[str], *_a: Any, **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    with pytest.raises(remote.RemoteUnreachable) as err:
        remote.run(_remote(tmp_path), "true", timeout=5, runner=_slow)
    assert "timed out" in str(err.value)
    assert "bentoo-lab" in str(err.value)


def test_a_missing_ssh_binary_is_a_remote_error_not_a_traceback(tmp_path: Path) -> None:
    def _no_ssh(*_a: Any, **_kw: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", "ssh")

    with pytest.raises(remote.RemoteError) as err:
        remote.run(_remote(tmp_path), "true", timeout=5, runner=_no_ssh)
    assert "ssh" in str(err.value)
    assert issubclass(remote.HostKeyMismatch, remote.RemoteError)
    assert issubclass(remote.RemoteUnreachable, remote.RemoteError)


def test_check_passes_its_runner_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runner = _Runner(0)
    remote.check(_entry(), runner=runner)
    assert len(runner.calls) == 1
    assert runner.calls[0][-1] == "true"


def test_ssh_argv_writes_an_integer_connect_timeout_for_a_float(tmp_path: Path) -> None:
    """v2: story 010 passes 10.0 and 5.0; ssh wants an integer."""
    runner = _Runner(0)
    remote.run(_remote(tmp_path), "true", timeout=5.0, runner=runner)
    assert _options(runner.calls[0])["ConnectTimeout"] == "5"
    assert (
        _options(remote.ssh_argv(_remote(tmp_path), "true", timeout=0.2))["ConnectTimeout"] == "1"
    )
    assert (
        _options(remote.ssh_argv(_remote(tmp_path), "true", timeout=2.5))["ConnectTimeout"] == "3"
    )


def test_a_mismatch_without_an_expected_fingerprint_reports_none_even_with_a_pinned_line(
    tmp_path: Path,
) -> None:
    """v2: no known_hosts parsing -- the transport's expected_fingerprint or nothing."""
    rem = _remote(tmp_path)
    rem.known_hosts.write_text(f"bentoo-lab {PINNED}\n")
    runner = _Runner(255, stderr=_mismatch_stderr(rem.known_hosts, _fingerprint(IMPOSTOR)))
    with pytest.raises(remote.HostKeyMismatch) as err:
        remote.run(rem, "true", timeout=10, runner=runner)
    assert err.value.expected is None
    assert err.value.presented == _fingerprint(IMPOSTOR)
