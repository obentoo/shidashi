"""SSH to a paired worker, only through the host key pinned at pairing.

A worker is reached as ``root@<address>``, but its key is looked up by NAME
(``HostKeyAlias=<name>`` in the shidashi ``known_hosts``), so a worker that moved to
another address keeps its pin and two workers behind one address keep theirs.
``StrictHostKeyChecking=yes`` with ``BatchMode=yes``: an unknown or changed key is a
refusal, never a question and never an acceptance.

:func:`ssh_argv` is pure; :func:`run` takes the runner as a parameter, so tests never
start ssh. Exit 255 is ssh's own failure: a host-key refusal becomes
:class:`HostKeyMismatch`, anything else :class:`RemoteUnreachable`. Any other exit
code is the remote command's, returned as it is.
"""

import math
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shidashi import config
from shidashi.workers import WorkerEntry

#: ssh's own exit code for its failures (connection, authentication, host key).
_SSH_FAILED = 255
_MISMATCH = "Host key verification failed"
_PRESENTED_RE = re.compile(
    r"key sent by the remote host is\s*\n\s*(SHA256:[A-Za-z0-9+/=]+?)\.?\s*$", re.M
)

Runner = Callable[..., subprocess.CompletedProcess[Any]]


class RemoteError(Exception):
    """A worker could not be reached through its pin."""


class HostKeyMismatch(RemoteError):
    """The worker presented a key other than the pinned one (or none is pinned)."""

    def __init__(self, name: str, expected: str | None, presented: str | None) -> None:
        self.name, self.expected, self.presented = name, expected, presented
        super().__init__(
            f"{name}: the host key does not match the pin "
            f"(expected {expected or 'unknown'}, presented {presented or 'unknown'}); "
            "re-pair with shidashi kyomei if the worker was reinstalled"
        )


class RemoteUnreachable(RemoteError):
    """ssh failed for any reason other than the host key."""

    def __init__(self, name: str, address: str, reason: str | None = None) -> None:
        self.name, self.address, self.reason = name, address, reason
        super().__init__(f"{name} ({address}) is unreachable" + (f": {reason}" if reason else ""))


@dataclass(frozen=True)
class Remote:
    """How to reach one worker: its name (the pin), address, our key, the pin file."""

    name: str
    address: str
    key: Path
    known_hosts: Path
    expected_fingerprint: str | None = None

    @classmethod
    def for_worker(cls, entry: WorkerEntry) -> Remote:
        """The registered worker, with shidashi's key and known_hosts (contract C3)."""
        base = config.workers_dir()
        return cls(
            name=entry.name,
            address=entry.address,
            key=base / "id_ed25519",
            known_hosts=base / "known_hosts",
            expected_fingerprint=entry.host_key_fingerprint,
        )


@dataclass
class RemoteResult:
    """A command run on the worker."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


def ssh_argv(remote: Remote, command: str, *, timeout: float = 10) -> list[str]:
    """``ssh root@<address> <command>``, verifying the pin. Pure.

    ``ConnectTimeout`` is an integer of at most 10 s; the whole command is bounded by
    :func:`run`'s ``timeout``.
    """
    connect = max(1, math.ceil(min(timeout, 10)))
    return [
        "ssh",
        "-i",
        str(remote.key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={remote.known_hosts}",
        "-o",
        f"HostKeyAlias={remote.name}",
        "-o",
        f"ConnectTimeout={connect}",
        "-o",
        "ServerAliveInterval=30",
        f"root@{remote.address}",
        command,
    ]


def run(
    remote: Remote, command: str, *, timeout: float = 10, runner: Runner = subprocess.run
) -> RemoteResult:
    """Run ``command`` on the worker; raise :class:`RemoteError` when ssh itself fails."""
    argv = ssh_argv(remote, command, timeout=timeout)
    started = time.monotonic()
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as err:
        raise RemoteError("ssh not found") from err
    except subprocess.TimeoutExpired as err:
        raise RemoteUnreachable(
            remote.name, remote.address, f"timed out after {timeout:g} s"
        ) from err
    duration = time.monotonic() - started
    stderr = proc.stderr or ""
    if proc.returncode == _SSH_FAILED:
        if _MISMATCH in stderr:
            match = _PRESENTED_RE.search(stderr)
            presented = match.group(1) if match else None
            raise HostKeyMismatch(remote.name, remote.expected_fingerprint, presented)
        last = stderr.strip().splitlines()[-1] if stderr.strip() else None
        raise RemoteUnreachable(remote.name, remote.address, last)
    return RemoteResult(command, proc.returncode, proc.stdout or "", stderr, duration)


def check(target: WorkerEntry | Remote, *, runner: Runner = subprocess.run) -> None:
    """Prove the pin: run ``true`` once. Raises what :func:`run` raises."""
    rem = target if isinstance(target, Remote) else Remote.for_worker(target)
    run(rem, "true", runner=runner)
