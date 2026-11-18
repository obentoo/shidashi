"""``shidashi fork-points``: the restore points under ``<cache>/fork-points`` (``gc``).

``cli.py`` only registers :data:`app`. The worker that owns an arch's cache, this host's
owner record, the size format and the worker job are looked up in ``cli`` at call time,
the way :mod:`shidashi.binhost_cli` does, so the tests stand in for them.
"""

from collections.abc import Sequence
from typing import Annotated, NoReturn

import typer

from shidashi import config, fork_point_gc, i18n, ownership
from shidashi.fork_point_gc import ForkPointFile, ForkPointGcPlan
from shidashi.tree import TreeError, load_pin_id

app = typer.Typer(
    no_args_is_help=True, help="The restore points: tarballs of finished build stages."
)

#: After every total: the sizes are ``st_size``, and on the v3 worker's zstd btrfs 79.8 GiB
#: of tarballs freed 33 GiB (2026-11-15) -- a total read as space to win misleads.
_APPARENT_SIZES_NOTE = i18n.mark("apparent sizes: a compressed filesystem frees less")


def _fork_points_exit(message: object) -> NoReturn:
    """Print ``message`` on stderr and exit 1: every failure of this sub-app."""
    typer.echo(str(message), err=True)
    raise typer.Exit(code=1)


def _fork_points_size(files: Sequence[ForkPointFile]) -> str:
    from shidashi import cli

    return cli._size(sum(item.size for item in files))


def _kept_pins_line(plan: ForkPointGcPlan) -> str:
    current, *others = plan.kept_pins
    current_pin = i18n.t("{pin} (current)").format(pin=current)
    return i18n.t("kept pins: {pins}").format(pins=", ".join([current_pin, *others]))


@app.command("gc")
def gc_fork_points(
    arch: Annotated[
        str, typer.Argument(help="Microarchitecture whose fork points to collect (e.g. v3).")
    ],
    keep: Annotated[
        int,
        typer.Option(
            "--keep",
            min=0,
            help="Other pins whose fork points stay besides the current pin's, newest first.",
        ),
    ] = 1,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan; delete nothing, take no lock.")
    ] = False,
) -> None:
    """Delete the fork points of every pin but the current one and the --keep newest others."""
    from shidashi import cli  # looked up at call time: the tests patch the owner and the job

    # a worker-owned arch keeps its fork points on that worker (story 028): the same gc
    # runs there, under the worker's own lock
    if (owner := cli._cache_owner(arch)) is not None:
        argv = ["fork-points", "gc", arch, "--keep", str(keep)]
        if dry_run:
            argv.append("--dry-run")
        cli.worker_job(
            owner,
            f"fork-points-gc-{arch}",
            argv,
            allow_dirty=False,
            no_follow=False,
            results=None,
            bwlimit=None,
            address=None,
        )
        return
    try:
        current = load_pin_id(config.seeds_dir())
    except TreeError as err:
        _fork_points_exit(err)
    directory = config.fork_points_dir()
    if dry_run:
        # no lock: it only lists, so the plan can be read while a build holds the arch;
        # the real run plans again under the lock
        plan = fork_point_gc.plan_fork_point_gc(
            fork_point_gc.scan_fork_points(directory, arch), current_pin=current, keep=keep
        )
        for item in plan.delete:
            typer.echo(
                i18n.t("would delete {name} ({size})").format(
                    name=item.path.name, size=cli._size(item.size)
                )
            )
        typer.echo(_kept_pins_line(plan))
        deleting = i18n.tn(
            "{n} fork point of {arch} would be deleted ({size})",
            "{n} fork points of {arch} would be deleted ({size})",
            len(plan.delete),
        ).format(n=len(plan.delete), arch=arch, size=_fork_points_size(plan.delete))
        typer.echo(
            i18n.t("{deleting}; {n} kept ({size}) -- {note}").format(
                deleting=deleting,
                n=len(plan.keep),
                size=_fork_points_size(plan.keep),
                note=i18n.t(_APPARENT_SIZES_NOTE),
            )
        )
        return
    try:
        # a pull copies fork points into this directory and takes no owner lock: the same
        # refusal as the binhost gc's (story 029, R3.8)
        if (puller := ownership.pulling_pid(arch)) is not None:
            raise ownership.PullRunning(arch, puller)
        with ownership.held(arch, cli._host_owner(arch, "fork-points-gc")):
            plan = fork_point_gc.plan_fork_point_gc(
                fork_point_gc.scan_fork_points(directory, arch), current_pin=current, keep=keep
            )
            deleted, failures = fork_point_gc.delete_fork_points(plan.delete)
    except (ownership.OwnedElsewhere, ownership.PullRunning, ownership.LockError) as err:
        _fork_points_exit(err)
    for item in deleted:
        typer.echo(
            i18n.t("deleted {name} ({size})").format(name=item.path.name, size=cli._size(item.size))
        )
    typer.echo(_kept_pins_line(plan))
    deleted_line = i18n.tn(
        "deleted {n} fork point of {arch} ({size})",
        "deleted {n} fork points of {arch} ({size})",
        len(deleted),
    ).format(n=len(deleted), arch=arch, size=_fork_points_size(deleted))
    typer.echo(
        i18n.t("{deleted}; kept {n} ({size}) -- {note}").format(
            deleted=deleted_line,
            n=len(plan.keep),
            size=_fork_points_size(plan.keep),
            note=i18n.t(_APPARENT_SIZES_NOTE),
        )
    )
    if failures:
        _fork_points_exit("\n".join(failures))
