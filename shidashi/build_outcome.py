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
import tempfile
from pathlib import Path
from typing import Literal, Self

import pydantic

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
