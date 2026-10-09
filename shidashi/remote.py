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
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shidashi import config
from shidashi.workers import WorkerEntry

#: ssh's own exit code for its failures (connection, authentication, host key).
_SSH_FAILED = 255
#: Where a pull keeps an interrupted file, inside the destination directory.
PULL_PARTIAL_DIR = ".rsync-partial"
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


class SyncError(RemoteError):
    """A transfer to or from a worker failed: which step, its exit code, its stderr tail."""

    def __init__(self, step: str, exit_code: int | None, stderr_tail: str = "") -> None:
        self.step, self.exit_code, self.stderr_tail = step, exit_code, stderr_tail
        detail = f": {stderr_tail.strip()}" if stderr_tail.strip() else ""
        super().__init__(f"{step} failed (exit {exit_code}){detail}")


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


def _ssh_options(remote: Remote, timeout: float) -> list[str]:
    """``ssh`` and the options that verify the pin, with no destination."""
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
    ]


def ssh_argv(remote: Remote, command: str, *, timeout: float = 10) -> list[str]:
    """``ssh root@<address> <command>``, verifying the pin. Pure.

    ``ConnectTimeout`` is an integer of at most 10 s; the whole command is bounded by
    :func:`run`'s ``timeout``.
    """
    return [*_ssh_options(remote, timeout), f"root@{remote.address}", command]


def ssh_command(remote: Remote, *, timeout: float = 10) -> list[str]:
    """The pinned ``ssh`` prefix for rsync's ``-e``: no destination, no command. Pure.

    rsync names the worker ``root@<name>``; ``HostName`` sends it to the registered
    address (a worker needs no DNS entry) while ``HostKeyAlias`` keeps the pin.
    """
    return [*_ssh_options(remote, timeout), "-o", f"HostName={remote.address}"]


def rsync_argv(
    remote: Remote,
    sources: Sequence[str],
    dest: str,
    *,
    push: bool,
    bwlimit: int | None = None,
    excludes: Sequence[str] = (),
    mkpath: bool = True,
    dry_run: bool = False,
) -> list[str]:
    """``rsync`` between the host and the worker over the pinned ssh. Pure; never deletes.

    ``push``: ``sources`` are host paths and ``dest`` a worker path; otherwise the
    reverse. A push keeps an interrupted file (``--partial``) to resume it. A pull
    writes into a host cache shared with root's builds: it keeps an interrupted file in
    ``.rsync-partial/`` (never a truncated file under its final name) and sets no
    times, permissions or group on host directories it does not own. ``dry_run``:
    the same transfer with ``--dry-run --stats`` -- nothing is written, and the stats
    report its ``Total transferred file size``.
    """
    argv = ["rsync", "-aH", "--numeric-ids"]
    if push:
        argv.append("--partial")
    else:
        argv += [
            f"--partial-dir={PULL_PARTIAL_DIR}",
            "--omit-dir-times",
            "--no-perms",
            "--no-group",
        ]
    if mkpath:
        argv.append("--mkpath")
    if dry_run:
        # in place of --info: a later --info=stats1 lowers --stats back to stats1
        argv += ["--dry-run", "--stats"]
    else:
        argv.append("--info=stats1,progress2")
    if bwlimit is not None:
        argv.append(f"--bwlimit={bwlimit}")
    argv += [f"--exclude={pattern}" for pattern in excludes]
    # -e takes ONE string: shlex.join keeps a key path with spaces whole
    argv += ["-e", shlex.join(ssh_command(remote))]
    side = f"root@{remote.name}:"
    if push:
        return [*argv, *sources, side + dest]
    return [*argv, *(side + src for src in sources), dest]


def _tail(text: str | bytes | None, lines: int = 20) -> str:
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return "\n".join((text or "").strip().splitlines()[-lines:])


def put_tree(
    remote: Remote,
    commit: str,
    dest: str,
    *,
    repo: Path,
    runner: Callable[..., Any] = subprocess.Popen,
) -> None:
    """Ship ``commit`` of ``repo`` (never the working tree) into ``dest`` on the worker.

    ``git archive`` is piped into ``tar -x`` over the pinned ssh; both exit codes are
    checked and a failure of either raises :class:`SyncError`.
    """
    target = shlex.quote(dest)
    archive = runner(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    unpack = runner(
        ssh_argv(remote, f"mkdir -p {target} && tar -x -C {target}"),
        stdin=archive.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if archive.stdout is not None:
        archive.stdout.close()  # tar's exit must reach git as SIGPIPE, not a hang
    _, unpack_err = unpack.communicate()
    _, archive_err = archive.communicate()
    # git killed by SIGPIPE is the echo of a tar that stopped reading: report the tar
    if archive.returncode not in (0, -signal.SIGPIPE):
        raise SyncError("ship: git archive", archive.returncode, _tail(archive_err))
    if unpack.returncode != 0:
        raise SyncError("ship: tar", unpack.returncode, _tail(unpack_err))
    if archive.returncode != 0:
        raise SyncError("ship: git archive", archive.returncode, _tail(archive_err))


def stream(
    remote: Remote,
    command: str,
    *,
    sink: Callable[[str], None],
    runner: Callable[..., Any] = subprocess.Popen,
) -> int:
    """Run ``command`` on the worker, each output line to ``sink`` as it comes.

    stderr is merged into stdout; undecodable bytes are replaced, never fatal. On
    ``KeyboardInterrupt`` (or any error) the local ssh is stopped and the exception
    re-raised; whatever runs detached on the worker keeps running.
    """
    proc = runner(
        ssh_argv(remote, command),
        stdin=subprocess.DEVNULL,  # never forward the caller's stdin to the worker
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",  # a log line in Latin-1 must not end the follow
        bufsize=1,
    )
    try:
        for line in iter(proc.stdout.readline, ""):
            sink(line)
        return int(proc.wait())
    except BaseException:
        # Ctrl+C or a failing sink: stop the local ssh before propagating
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise


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
