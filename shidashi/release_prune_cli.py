"""``shidashi release prune``: keep the newest releases, set the rest aside (story 032, R4).

Registered by ``release_cli``. Nothing is deleted unless ``--purge-pruned`` is given; a
release is *moved* to ``<root>/.pruned/`` with one ``os.replace``, so its ISOs keep their
inodes. A release being written holds ``.release.lock`` (``release_sign.claim_release_dir``):
a directory whose lock is held is left where it is.
"""

import os
import shutil
from pathlib import Path
from typing import Annotated

import typer

from shidashi import config, locks, release_prune, release_sign


def _entries(directory: Path) -> tuple[list[str], list[str]]:
    """The real directories under ``directory`` and the other entries (files, symlinks)."""
    dirs: list[str] = []
    others: list[str] = []
    with os.scandir(directory) as scan:
        for entry in scan:
            (dirs if entry.is_dir(follow_symlinks=False) else others).append(entry.name)
    return dirs, others


def _fail(message: str) -> None:
    typer.echo(f"error: {message}", err=True)


def _move_aside(root: Path, name: str) -> str | None:
    """Move ``root/name`` into ``.pruned`` under its lock. ``None`` when moved; otherwise
    why it stayed: ``busy`` (a release holds it) or the ``OSError`` text."""
    source = root / name
    try:
        with locks.hold(source / release_sign.LOCK_FILE, what=f"release {name}", wait=False):
            os.replace(source, root / release_prune.PRUNED_DIR / name)
    except locks.LockBusy:
        return "busy"
    except (locks.LockError, OSError) as err:
        return str(err)
    return None


def prune(
    keep: Annotated[int, typer.Option("--keep", min=1, help="Releases that stay in the root.")] = 4,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Parent of the dated releases; default <cache>/releases."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan; move and delete nothing.")
    ] = False,
    purge_pruned: Annotated[
        bool,
        typer.Option("--purge-pruned", help="Also delete the .pruned entries beyond --keep."),
    ] = False,
) -> None:
    """Move every release but the newest --keep into <root>/.pruned; delete only on request."""
    root = output_root or config.cache_dir() / "releases"
    pruned_root = root / release_prune.PRUNED_DIR
    try:
        names, stray = _entries(root)
        pruned_names = _entries(pruned_root)[0] if pruned_root.is_dir() else []
    except OSError as err:
        _fail(f"cannot read {root}: {err}")
        raise typer.Exit(1) from err
    done = release_prune.plan(names, keep, pruned_names if purge_pruned else [])
    for name in [*done.skipped, *stray]:
        typer.echo(f"skipped {name}: not a release directory")
    if dry_run:
        for name in done.move:
            typer.echo(f"would move {name} to {release_prune.PRUNED_DIR}/")
        for name in done.purge:
            typer.echo(f"would delete {release_prune.PRUNED_DIR}/{name}")
        return
    failed = False
    if done.move:
        try:
            release_sign.ensure_release_dir(pruned_root)
        except OSError as err:
            _fail(f"cannot create {pruned_root}: {err}")
            raise typer.Exit(1) from err
    for name in done.move:
        why = _move_aside(root, name)
        if why == "busy":
            typer.echo(f"left {name}: a release holds its lock")
        elif why is not None:
            _fail(f"cannot move {root / name}: {why}")
            failed = True
        else:
            typer.echo(f"moved {name} to {release_prune.PRUNED_DIR}/")
    for name in done.purge:
        try:
            shutil.rmtree(pruned_root / name)
        except OSError as err:
            _fail(f"cannot delete {pruned_root / name}: {err}")
            failed = True
        else:
            typer.echo(f"deleted {release_prune.PRUNED_DIR}/{name}")
    if failed:
        raise typer.Exit(1)
