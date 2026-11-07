"""``shidashi ci-guest``: the worker side of the CI runner (story 034), root only.

``fetch``, ``prepare``, ``run`` and ``sweep`` run on the worker, ``run`` inside the
``shidashi-ci-<job>`` unit the host's pool starts. ``exec`` and ``firmware`` are hidden:
they run INSIDE the toolbox container, started by :mod:`shidashi.ci_guest_vm`, because
QEMU, ``qemu-img``, ``xorriso`` and OVMF exist only there.

The ``ci_guest``/``ci_guest_seed`` functions are looked up on their modules at call time,
so a test (or a future caller) replacing one is honoured.
"""

import os
import subprocess
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from shidashi import ci_guest, ci_guest_seed, ci_guest_vm, config, vm

app = typer.Typer(no_args_is_help=True, help="The worker as a CI runner: one job per VM (root).")


def _die(message: object) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def _require_root() -> None:
    if os.geteuid() != 0:
        _die("ci-guest runs as root: it drives rootful podman and the work disk")


def _pins() -> ci_guest_seed.GuestPins:
    runner_env = Path(ci_guest.__file__).resolve().parent.parent / "lab" / "vm" / "runner.env"
    try:
        return ci_guest_seed.load_pins(config.seeds_dir() / "ci-guest.toml", runner_env)
    except ci_guest_seed.CiGuestSeedError as err:
        _die(err)


@app.command("fetch")
def fetch() -> None:
    """Download the pinned Ubuntu cloud image to the work disk, verified."""
    _require_root()
    pins = _pins()
    try:
        typer.echo(ci_guest.fetch(pins))
    except ci_guest.CiGuestError as err:
        _die(err)


@app.command("prepare")
def prepare() -> None:
    """Prepare the guest image once for the current pins (Docker, runner, units)."""
    _require_root()
    pins = _pins()
    try:
        typer.echo(ci_guest.prepare(pins))
    except ci_guest.CiGuestError as err:
        _die(err)


@app.command("run")
def run(
    job: Annotated[str, typer.Option("--job", help="GitHub job id (digits).")],
    repo: Annotated[str, typer.Option("--repo", help="OWNER/NAME the runner was minted for.")],
    memory_gib: Annotated[int, typer.Option("--memory-gib", min=1)] = 8,
    disk_gib: Annotated[int, typer.Option("--disk-gib", min=1)] = 64,
    timeout_min: Annotated[int, typer.Option("--timeout-min", min=1)] = 360,
    min_free_gib: Annotated[int, typer.Option("--min-free-gib", min=0)] = 20,
) -> None:
    """Run one job in one ephemeral VM: exit 0 pass, 1 fail, 3 low disk, 4 timeout."""
    _require_root()
    pins = _pins()
    limits = ci_guest.Limits(
        memory_gib=memory_gib, disk_gib=disk_gib, timeout_min=timeout_min, min_free_gib=min_free_gib
    )
    try:
        rc = ci_guest.run_job(job, repo, limits=limits, pins=pins)
    except (ci_guest.CiGuestError, ci_guest_seed.CiGuestSeedError) as err:
        _die(err)
    raise typer.Exit(rc)


@app.command("sweep")
def sweep() -> None:
    """Delete job directories and JIT configs whose VM no longer runs."""
    _require_root()
    try:
        removed = ci_guest.sweep(ci_guest.ROOT, alive=ci_guest.container_alive(subprocess.run))
    except ci_guest.CiGuestError as err:
        _die(err)
    for name in removed:
        typer.echo(name)


_PASS_THROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


@app.command("exec", hidden=True, context_settings=_PASS_THROUGH)
def exec_tool(ctx: typer.Context) -> None:
    """Inside the toolbox container: exec one allowlisted tool with its arguments."""
    _require_root()
    args = list(ctx.args)
    if not args or args[0] not in ci_guest_vm.TOOLS:
        _die(f"exec runs only {', '.join(ci_guest_vm.TOOLS)}, not {args[:1]}")
    try:
        os.execvp(args[0], args)
    except OSError as err:
        _die(f"cannot run {args[0]}: {err}")


@app.command("firmware", hidden=True)
def firmware(dest: Annotated[Path, typer.Argument(help="Directory to copy OVMF into.")]) -> None:
    """Inside the toolbox container: copy its OVMF code and variables template to DEST."""
    _require_root()
    try:
        code, vars_, fmt = vm.ovmf()
        ci_guest_vm.write_firmware(dest, Path(code), Path(vars_), fmt)
    except (vm.VmError, OSError) as err:
        _die(f"cannot copy the toolbox's OVMF to {dest}: {err}")
