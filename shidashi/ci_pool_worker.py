"""The pool's side of the worker: the dispatch command and :class:`SshWorker` (story 034).

One ssh carries the JIT config on STDIN to ``/run/shidashi/ci/<job>/jitconfig`` (tmpfs,
0600) and starts ``shidashi ci-guest run`` as unit ``shidashi-ci-<job>`` -- from the tree
and venv ``shidashi worker job`` ships (story 010), with ``CPUWeight=20`` and room above
the guest's RAM. The token never leaves the host; the JIT config is never in an argv.
"""

import json
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from shidashi import remote, worker

#: A Shidashi job runs at systemd's default weight (100); a CI VM at a fifth of it (R7.2).
CPU_WEIGHT = 20
#: QEMU's own memory above the guest's RAM (design.md gotcha), so MemoryMax never OOMs it.
QEMU_OVERHEAD_GIB = 1
RUN_DIR = "/run/shidashi/ci"
RESULTS = f"{worker.WORK}/ci/results"
UNIT_PREFIX = "shidashi-ci-"
_JOB = re.compile(r"[0-9]+")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMIT = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
_DISPATCH_TIMEOUT_S = 60
_PROBE_TIMEOUT_S = 20
_STDERR_TAIL = 500
#: A unit starting or stopping still holds the worker's one CI VM slot.
_LIST_CI = (
    f"systemctl list-units '{UNIT_PREFIX}*' --state=active,activating,deactivating,reloading "
    "--plain --no-legend --full"
)
#: Prints each ``results/*.json`` on one line and removes it, in the same ssh.
_TAKE_RESULTS = (
    f"cd {RESULTS} 2>/dev/null || exit 0; "
    'for f in *.json; do [ -f "$f" ] || continue; '
    'tr -d "\\n" < "$f"; echo; rm -f -- "$f"; done'
)


class WorkerUnreachable(Exception):
    """The worker could not be asked: the round is skipped, the job retried (R6.5)."""


class CiDispatchError(Exception):
    """The worker answered and refused, or the commit to ship is missing: names why."""


class VmLimits(Protocol):
    @property
    def memory_gib(self) -> int: ...
    @property
    def disk_gib(self) -> int: ...
    @property
    def timeout_min(self) -> int: ...
    @property
    def min_free_gib(self) -> int: ...


def _check(job: str, repo: str) -> None:
    if not _JOB.fullmatch(job):
        raise ValueError(f"job id {job!r} is not a GitHub job id (digits only)")
    if not _REPO.fullmatch(repo):
        raise ValueError(f"repository {repo!r} is not OWNER/NAME")


def unit_argv(job: str, repo: str, commit: str, limits: VmLimits) -> list[str]:
    """``systemd-run`` of ``shidashi ci-guest run`` as ``shidashi-ci-<job>``. Pure."""
    _check(job, repo)
    # the shipped tree's working directory and environment, exactly as ``worker job``'s
    shipped = [
        word
        for word in worker.job_unit_argv(job, commit, ["status"])
        if word.startswith(("--working-directory=", "--setenv="))
    ]
    return [
        "systemd-run",
        f"--unit={UNIT_PREFIX}{job}",
        "--collect",
        "--service-type=exec",
        "-p",
        f"CPUWeight={CPU_WEIGHT}",
        # podman --cgroups=split creates the container's sub-cgroup inside this unit's;
        # podman's own Quadlet pairs split with Delegate=yes (story 034 tech review)
        "-p",
        "Delegate=yes",
        "-p",
        f"MemoryMax={limits.memory_gib + QEMU_OVERHEAD_GIB}G",
        *shipped,
        f"{worker.WORKER_VENV}/bin/python",
        "-c",
        "from shidashi.cli import app; app()",
        "ci-guest",
        "run",
        "--job",
        job,
        "--repo",
        repo,
        "--memory-gib",
        str(limits.memory_gib),
        "--disk-gib",
        str(limits.disk_gib),
        "--timeout-min",
        str(limits.timeout_min),
        "--min-free-gib",
        str(limits.min_free_gib),
    ]


def dispatch_argv(job: str, repo: str, commit: str, limits: VmLimits) -> str:
    """The remote command of one dispatch (R5.1-R5.3). Pure.

    stdin -> the JIT config on tmpfs, 0600 in a 0700 directory, then the unit; a
    ``systemd-run`` that fails removes the directory and the command exits 1, so nothing
    is left that no unit would ever read or delete.
    """
    _check(job, repo)
    directory = shlex.quote(f"{RUN_DIR}/{job}")
    target = shlex.quote(f"{RUN_DIR}/{job}/jitconfig")
    start = shlex.join(unit_argv(job, repo, commit, limits))
    return (
        f"install -d -m 0700 {directory} && install -m 0600 /dev/stdin {target} && "
        f"{{ {start} || {{ rm -rf {directory}; exit 1; }}; }}"
    )


def _tail(result: remote.RemoteResult) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[-_STDERR_TAIL:] or f"exit {result.exit_code}"


def source_commit(source: Path, *, runner: Callable[..., Any] = subprocess.run) -> str:
    """HEAD of ``source``, the checkout shipped to the worker; :class:`CiDispatchError`
    naming ``source`` when it has none."""
    try:
        done = runner(
            ["git", "-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise CiDispatchError(f"cannot read the commit of source {source}: {err}") from err
    commit = (done.stdout or "").strip()
    if done.returncode != 0 or not _COMMIT.fullmatch(commit):
        raise CiDispatchError(f"source {source} has no commit to ship (git rev-parse HEAD failed)")
    return commit


class SshWorker:
    """The paired worker, over its pinned ssh (story 009)."""

    def __init__(self, target: remote.Remote, source: Path, limits: VmLimits) -> None:
        self.remote = target
        self.source = source
        self.limits = limits

    def _run(self, command: str, *, timeout: float, input: str | None = None) -> Any:
        try:
            return remote.run(self.remote, command, timeout=timeout, input=input)
        except remote.RemoteError as err:
            raise WorkerUnreachable(str(err)) from err

    def busy(self) -> bool:
        """An active ``shidashi-ci-*`` unit holds the worker's one CI VM (R6.4)."""
        result = self._run(_LIST_CI, timeout=_PROBE_TIMEOUT_S)
        if result.exit_code != 0:
            raise WorkerUnreachable(f"systemctl list-units on the worker failed: {_tail(result)}")
        return any(
            line.split()[0].startswith(UNIT_PREFIX)
            for line in result.stdout.replace("●", " ").splitlines()
            if line.split()
        )

    def results(self) -> list[dict[str, Any]]:
        """The ``results/<job>.json`` the worker's runs left, read and removed in one ssh."""
        result = self._run(_TAKE_RESULTS, timeout=_PROBE_TIMEOUT_S)
        taken = []
        for line in result.stdout.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                taken.append(value)
        return taken

    def _ship(self, commit: str) -> None:
        """The venv and ``commit``'s tree on the worker, as ``shidashi worker job`` ships them."""
        try:
            worker._push_runtime(self.remote, bwlimit=None)
            # every time, as ``shidashi worker job`` does: put_tree unpacks in place, so a
            # ship cut short leaves a src/<commit> a presence test takes for whole (review 4.x)
            remote.put_tree(self.remote, commit, f"{worker.WORK}/src/{commit}", repo=self.source)
        except remote.RemoteUnreachable as err:
            raise WorkerUnreachable(str(err)) from err
        except remote.RemoteError as err:
            raise CiDispatchError(f"shipping {commit[:12]} to the worker failed: {err}") from err

    def dispatch(self, job: str, repo: str, jitconfig: str) -> None:
        commit = source_commit(self.source)
        self._ship(commit)
        command = dispatch_argv(job, repo, commit, self.limits)
        result = self._run(command, timeout=_DISPATCH_TIMEOUT_S, input=jitconfig)
        if result.exit_code != 0:
            raise CiDispatchError(f"dispatch of job {job} failed: {_tail(result)}")
