"""The build outcome contract -- what one ``shidashi build`` ended with (story 030).

The child build writes it, the release reads it. A child killed before or while
writing leaves no file or a broken one; ``load`` refuses both, and a file of the
wrong shape, as a ``ReleaseError`` naming the path (R3.7), so a release never
reads a partial success. ``write`` goes through a sibling temporary file renamed
over the target (R5.3): a reader sees the whole old file or the whole new one.
"""

import contextlib
import json
import os
import signal
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Literal, Self

import pydantic
import typer

from shidashi.generation import GenerationMismatchError


class ReleaseError(Exception):
    """A release step failed; the message names the file or the step."""


class ToolchainRefusal(pydantic.BaseModel):
    """Why a build refused its PKGDIR: each ABI difference, that PKGDIR, its successor."""

    model_config = pydantic.ConfigDict(extra="forbid")
    differences: list[str]
    pkgdir: str
    successor: str

    @classmethod
    def from_error(cls, err: GenerationMismatchError) -> Self:
        """The refusal ``err`` carries, from its attributes -- never parsed from its text."""
        return cls(
            differences=[
                f"{field} {was} -> {now}" for field, (was, now) in err.differences.items()
            ],
            pkgdir=str(err.pkgdir),
            successor=str(err.successor),
        )


class BuildOutcome(pydantic.BaseModel):
    """One build's result; ``isos`` are file names in its output directory."""

    model_config = pydantic.ConfigDict(extra="forbid")
    arch: str
    init: str
    status: Literal["ok", "images-failed", "toolchain-refused", "error"]
    exit_code: int
    isos: list[str]
    failed_images: list[str]
    message: str
    toolchain: ToolchainRefusal | None


def write(path: Path, outcome: BuildOutcome) -> None:
    """Write ``outcome`` to ``path`` atomically (R5.3). I/O.

    Raises :class:`ReleaseError` naming ``path`` when it cannot; the previous
    file, if any, is then left whole and no temporary file is left behind.
    """
    data = (outcome.model_dump_json(indent=1) + "\n").encode("utf-8")
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            tmp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as err:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise ReleaseError(f"{path}: cannot write the build outcome: {err}") from err


def load(path: Path) -> BuildOutcome:
    """The outcome in ``path``. I/O.

    Raises :class:`ReleaseError` naming ``path`` when the file is missing,
    unreadable, not JSON (empty, truncated, not UTF-8) or not a build outcome
    (an unknown status, a wrong type, a missing or extra key) -- R3.7.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError as err:
        raise ReleaseError(f"{path}: no build outcome file") from err
    except OSError as err:
        raise ReleaseError(f"{path}: cannot read the build outcome: {err}") from err
    try:
        # Validating the bytes, not decoded text: a non-UTF-8 file is then a
        # ValidationError rather than a UnicodeDecodeError.
        return BuildOutcome.model_validate_json(data)
    except (json.JSONDecodeError, pydantic.ValidationError) as err:
        raise ReleaseError(f"{path}: not a build outcome: {err}") from err


@contextlib.contextmanager
def recording(path: Path | None, *, arch: str, init: str, output_dir: Path) -> Generator[None]:
    """Run the build body and write to ``path`` how it ended (R3.1-R3.4). I/O.

    Every failure reaches here as the ``typer.Exit`` that ``cli._build_errors``
    raised, so the outcome is read from its ``__cause__``. The exception is
    re-raised unchanged; ``path is None`` records nothing (R3.5).
    """
    if path is None:
        yield
        return
    try:
        yield
    except typer.Exit as err:
        _record_failure(path, _from_exit(err, arch=arch, init=init))
        raise
    except BaseException as err:
        # Ctrl-C, or SIGTERM/SIGHUP that cli._stopped turned into SystemExit(128 + sig)
        _record_failure(path, _outcome(arch, init, "error", _exit_code_of(err), str(err)))
        raise
    isos = _variant_isos(output_dir, arch=arch, init=init)
    try:
        write(path, _outcome(arch, init, "ok", 0, "", isos=isos))
    except ReleaseError as err:
        # asked to record and could not: the release would find no outcome anyway
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err


def _variant_isos(output_dir: Path, *, arch: str, init: str) -> list[str]:
    """This variant's ISOs in ``output_dir``. A release directory holds every variant's
    ISOs; the name ends ``-<init>-<arch>.iso`` (``-<init>-<arch>-xz.iso`` for the second
    ISO of ``--compression both``), so ``v3`` never matches ``v3x``."""
    tag = f"-{init}-{arch}"
    return sorted(
        iso.name
        for iso in output_dir.glob("*.iso")
        if iso.name.endswith(f"{tag}.iso") or f"{tag}-" in iso.name
    )


def _from_exit(err: typer.Exit, *, arch: str, init: str) -> BuildOutcome:
    """The outcome a ``typer.Exit`` stands for, from the error it was raised from."""
    from shidashi.cli import _ImagesFailed  # lazy: cli imports this module

    cause = err.__cause__
    message = "" if cause is None else str(cause)
    if isinstance(cause, GenerationMismatchError):
        refusal = ToolchainRefusal.from_error(cause)
        return _outcome(arch, init, "toolchain-refused", err.exit_code, message, toolchain=refusal)
    if isinstance(cause, _ImagesFailed):
        failed = list(cause.failed)
        return _outcome(arch, init, "images-failed", err.exit_code, message, failed=failed)
    return _outcome(arch, init, "error", err.exit_code, message)


def _outcome(
    arch: str,
    init: str,
    status: Literal["ok", "images-failed", "toolchain-refused", "error"],
    exit_code: int,
    message: str,
    *,
    isos: list[str] | None = None,
    failed: list[str] | None = None,
    toolchain: ToolchainRefusal | None = None,
) -> BuildOutcome:
    return BuildOutcome(
        arch=arch,
        init=init,
        status=status,
        exit_code=exit_code,
        isos=isos or [],
        failed_images=failed or [],
        message=message,
        toolchain=toolchain,
    )


def _exit_code_of(err: BaseException) -> int:
    """The code the process exits with for ``err``: 128 + SIGINT for Ctrl-C, a
    ``SystemExit``'s own integer code, and 1 (Python's) for anything else."""
    if isinstance(err, KeyboardInterrupt):
        return 128 + signal.SIGINT
    if isinstance(err, SystemExit):
        return err.code if isinstance(err.code, int) else 1
    return 1


def _record_failure(path: Path, outcome: BuildOutcome) -> None:
    """Write a failed build's outcome; an outcome that cannot be written is said, never
    raised in place of the build's own exception."""
    try:
        write(path, outcome)
    except ReleaseError as err:
        typer.echo(f"error: {err}", err=True)
