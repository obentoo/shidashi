"""Container — wrapper around ``systemd-nspawn`` for isolated builds (OVERVIEW §12).

Isolation is done by ``systemd-nspawn`` (OVERVIEW §12): ``emerge``/``eselect``
run *inside* the container, never via ``import portage``. Building the command
line (:func:`_nspawn_argv`) is **pure and inspectable** — testable without root;
execution (:meth:`Container.run`) and the lifecycle (context manager) require
root + ``systemd-nspawn`` and are exercised by the host-gated integration tests.
"""

import datetime
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

from shidashi import audit


class CommandResult:
    """Result of a command run inside the container (OVERVIEW §12).

    Value object: exit code and the captured ``stdout``/``stderr`` streams.
    """

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


#: The lab's validated nspawn line (start.sh). --resolv-conf=copy-host: fetches
#: must not depend on whatever resolv.conf the stage3 ships. --register=no: not a
#: --boot container, so there is nothing for systemd-machined to manage.
#: --timezone=off: nspawn's default (auto) writes the BUILD HOST's zone into the
#: image -- the first ISO shipped America/Sao_Paulo that way (F79); the image's
#: zone is system.yaml's, set by shidashi.system.
_HOST_OPTIONS = ("--register=no", "--resolv-conf=copy-host", "--timezone=off")

# Commands (not the interactive shell) add --console=pipe and get stdin from
# /dev/null. nspawn's default console is INTERACTIVE when it is started from a
# terminal -- a factory run inside tmux -- which would hand the build the
# user's keyboard and raw tty for hours; otherwise it is read-only, still
# through a pty that merges stderr into stdout. Pipe mode passes our pipes
# straight through, which nspawn(1) documents as safe for pipe descriptors.
#
# They also run with --as-pid2: a stub init is PID 1 and reaps orphans, and the
# command is PID 2 like any process. Run as PID 1 itself, locale-gen aborted
# with "not all of the selected locales were compiled" (its wait() accounting
# broke); as a child it succeeds -- reproduced in a user+pid namespace on
# 2026-09-26. The lab never saw it: its commands ran under a shell.


def _emit_binds(
    binds: Sequence[tuple[Path, Path]],
    binds_rw: Sequence[tuple[Path, Path]],
) -> list[str]:
    """``systemd-nspawn`` bind block: ``--bind-ro=`` (RO) and then ``--bind=`` (RW).

    Emits ``--bind-ro=src:dst`` per RO bind in the declared order, followed by
    ``--bind=src:dst`` per RW bind in the declared order (every RW *after* the RO ones).
    Single source of truth for the block — shared by :func:`_nspawn_argv` and
    :func:`_nspawn_shell_argv` so that parity is structural (R7.2).
    """
    block: list[str] = []
    for src, dst in binds:
        block.append(f"--bind-ro={src}:{dst}")
    for src, dst in binds_rw:
        block.append(f"--bind={src}:{dst}")
    return block


def _nspawn_argv(
    rootfs: Path,
    argv: Sequence[str],
    *,
    binds: Sequence[tuple[Path, Path]],
    binds_rw: Sequence[tuple[Path, Path]] = (),
    ephemeral: bool,
) -> list[str]:
    """Build the ``systemd-nspawn`` command line (R4.4/R7.1/R7.2). **Pure**, no side effects.

    Shape: ``["systemd-nspawn", "--directory", <rootfs>, ("--ephemeral")?,
    ("--bind-ro=src:dst" per RO bind, in order), ("--bind=src:dst" per RW bind,
    in order, *after* every RO one), "--", *argv]``. The binds come before the
    ``--`` separator; the command's ``argv`` comes after. Without ``binds_rw`` the argv
    is identical to story 002's (R7.3 back-compat).
    """
    cmd: list[str] = [
        "systemd-nspawn",
        "--directory",
        str(rootfs),
        *_HOST_OPTIONS,
        "--console=pipe",
        "--as-pid2",
    ]
    if ephemeral:
        cmd.append("--ephemeral")
    cmd.extend(_emit_binds(binds, binds_rw))
    cmd.append("--")
    cmd.extend(argv)
    return cmd


def _nspawn_shell_argv(
    rootfs: Path,
    *,
    binds: Sequence[tuple[Path, Path]] = (),
    binds_rw: Sequence[tuple[Path, Path]] = (),
) -> list[str]:
    """Build the line for an interactive ``systemd-nspawn`` shell (R7.1/R7.2). **Pure**.

    Shape: ``["systemd-nspawn", "--directory", <rootfs>, ("--bind-ro=src:dst"),
    ("--bind=src:dst" after the RO ones)]`` — the SAME bind block as
    :func:`_nspawn_argv` (via :func:`_emit_binds`), but **WITHOUT** a trailing command
    (no ``--`` separator and no argv), so that nspawn drops into the container's
    login shell. Does not change the argv of :func:`_nspawn_argv` (R8.4).
    """
    cmd: list[str] = ["systemd-nspawn", "--directory", str(rootfs), *_HOST_OPTIONS]
    cmd.extend(_emit_binds(binds, binds_rw))
    return cmd


class Container:
    """Wrapper of a ``systemd-nspawn`` rooted at ``rootfs`` (OVERVIEW §12).

    Context manager: ``__enter__`` validates the environment (root + ``systemd-nspawn``
    present) and ``__exit__`` tears down the ephemeral scratch. When
    ``ephemeral=True``, nspawn's ``--ephemeral`` already keeps the seeded rootfs
    immutable (disposable overlay); the teardown removes the scratch directory
    only when it is *not* a non-preserved ephemeral one.
    """

    def __init__(
        self,
        rootfs: Path,
        *,
        ephemeral: bool = False,
        binds: Sequence[tuple[Path, Path]] = (),
        binds_rw: Sequence[tuple[Path, Path]] = (),
        log: Path | None = None,
    ) -> None:
        self.rootfs = rootfs
        self.ephemeral = ephemeral
        self.binds: tuple[tuple[Path, Path], ...] = tuple(binds)
        self.binds_rw: tuple[tuple[Path, Path], ...] = tuple(binds_rw)
        #: When set, every command's output is appended here AS IT RUNS. A base
        #: build is hours of emerge; without this nothing is visible until the
        #: end, and a killed run leaves no trace at all.
        self.log = log

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Run ``argv`` inside the container and return the result (R4.1/R4.3).

        Builds the line via :func:`_nspawn_argv` and runs it with
        ``subprocess.run`` capturing the streams. When ``check`` is ``True`` and the
        command exits with a non-zero code, raises ``CalledProcessError`` (with the
        ``stderr`` attached), instead of returning silently.
        """
        cmd = _nspawn_argv(
            self.rootfs,
            argv,
            binds=self.binds,
            binds_rw=self.binds_rw,
            ephemeral=self.ephemeral,
        )
        if self.log is not None:
            return self._run_logged(cmd, argv, env=env, check=check)
        start = time.monotonic()
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(env) if env is not None else None,
            check=False,
        )
        self._audit(argv, proc.returncode, start, (proc.stdout + proc.stderr).count("\n"))
        result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
            )
        return result

    def _run_logged(
        self,
        cmd: list[str],
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None,
        check: bool,
    ) -> CommandResult:
        """Run ``cmd`` streaming its merged stdout+stderr into :attr:`log`.

        Returns the same :class:`CommandResult` as the captured path, with the
        merged stream as ``stdout`` (every caller reads ``stdout + stderr``).
        """
        assert self.log is not None
        self.log.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        start = time.monotonic()
        with self.log.open("a", encoding="utf-8") as out:
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            out.write(f"### {stamp} $ {' '.join(argv)}\n")
            out.flush()
            with subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                env=dict(env) if env is not None else None,
            ) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    lines.append(line)
                    out.write(line)
                    out.flush()
                returncode = proc.wait()
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            out.write(f"### {stamp} exit {returncode}\n")
        self._audit(argv, returncode, start, len(lines))
        output = "".join(lines)
        if check and returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd, output=output, stderr="")
        return CommandResult(returncode, output, "")

    def _audit(self, argv: Sequence[str], exit_code: int, start: float, lines: int) -> None:
        """One ``command`` event in the run's audit trail (a no-op outside a run)."""
        audit.current().command(
            argv,
            exit_code=exit_code,
            duration_s=round(time.monotonic() - start, 3),
            output_lines=lines,
            rootfs=str(self.rootfs),
            log=str(self.log) if self.log is not None else None,
        )

    def shell(self) -> None:
        """Open an interactive shell in the *live* rootfs and return when it exits (R7.1/R7.3).

        Builds the line via :func:`_nspawn_shell_argv` (same RO/RW binds as the
        build) and runs it with ``subprocess.run`` with **inherited** stdio — no
        ``capture_output``/``text``/``check`` — so that the user's terminal
        attaches to the container. Reuses the persistent rootfs
        (``ephemeral=False``), so changes made in the shell persist into the
        next phase; returns on exit **without** tearing down the rootfs.
        """
        subprocess.run(_nspawn_shell_argv(self.rootfs, binds=self.binds, binds_rw=self.binds_rw))

    def __enter__(self) -> Container:
        if shutil.which("systemd-nspawn") is None:
            raise RuntimeError("systemd-nspawn missing on the host")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Scratch teardown: we only remove it when the container is ephemeral (disposable
        # rootfs). ignore_errors=True ensures a failed teardown does not
        # mask an in-flight exception.
        if self.ephemeral and self.rootfs.exists():
            shutil.rmtree(self.rootfs, ignore_errors=True)
