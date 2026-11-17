"""Superseded runner images go when a new one is imported (worker disk policy).

Every toolbox rebuild imports a new ``localhost/shidashi-vm-runner:<tag>`` (5.8-7.5 GB)
and nothing removed the old ones: on 2026-11-16 bentoo-lab held eight, ~50 GB of a
466 GB work disk. :func:`prune_runner_images` keeps the image just imported and the
newest ``keep - 1`` others -- the previous one serves a re-pin reverted to it, as the
fork-point gc keeps the newest older pin -- and removes the rest with a plain
``podman rmi``: one a container still uses is refused by podman and stays.

Pruning never fails the import that triggered it: what was removed, kept and refused
is returned for the caller to report.
"""

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The runner images' repository (vmrunner names its tags under it).
RUNNER_REPOSITORY = "localhost/shidashi-vm-runner"
#: The image just imported and the one before it.
RUNNER_IMAGES_KEPT = 2

PodmanRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RunnerPrune:
    """What one prune did: tags removed, kept, and refused (with podman's reason)."""

    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    error: str = ""


def plan_runner_prune(
    images: Sequence[tuple[str, int]], current: str, keep: int = RUNNER_IMAGES_KEPT
) -> tuple[list[str], list[str]]:
    """``(kept, removed)`` tags of ``images`` (``(tag, created unix seconds)``): ``current``
    and the newest ``keep - 1`` others stay. Pure."""
    others = sorted((img for img in images if img[0] != current), key=lambda i: -i[1])
    kept = [current, *(tag for tag, _ in others[: max(0, keep - 1)])]
    removed = [tag for tag, _ in others[max(0, keep - 1) :]]
    return kept, removed


def _runner_images(listing: str) -> list[tuple[str, int]]:
    """``(tag, created)`` of the runner images in ``podman images --format json``. Pure."""
    found: list[tuple[str, int]] = []
    data: Any = json.loads(listing or "[]")
    for image in data if isinstance(data, list) else []:
        created = int(image.get("Created", 0) or 0)
        for name in image.get("Names") or []:
            if isinstance(name, str) and name.startswith(RUNNER_REPOSITORY + ":"):
                found.append((name, created))
    return found


def prune_runner_images(current: str, runner: PodmanRunner) -> RunnerPrune:
    """Remove superseded runner images, keeping ``current`` and the one before. I/O
    through ``runner`` (``subprocess.run``'s signature); never raises for podman."""
    try:
        listed = runner(
            ["podman", "images", "--format", "json", "--filter", f"reference={RUNNER_REPOSITORY}"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as err:
        return RunnerPrune(error=f"cannot list runner images: {err}")
    if listed.returncode != 0:
        return RunnerPrune(error=f"podman images failed: {listed.stderr.strip()}")
    try:
        images = _runner_images(listed.stdout)
    except (ValueError, TypeError) as err:
        return RunnerPrune(error=f"unreadable podman images listing: {err}")
    kept, doomed = plan_runner_prune(images, current)
    removed: list[str] = []
    refused: list[tuple[str, str]] = []
    for tag in doomed:
        try:
            done = runner(
                ["podman", "rmi", tag],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )
        except OSError as err:
            refused.append((tag, str(err)))
            continue
        if done.returncode == 0:
            removed.append(tag)
        else:
            refused.append((tag, done.stderr.strip()))
    return RunnerPrune(removed=tuple(removed), kept=tuple(kept), refused=tuple(refused))
