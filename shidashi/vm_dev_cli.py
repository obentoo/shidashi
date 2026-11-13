"""The CLI side of ``shidashi vm start --dev`` -- kept out of ``cli.py``, which predates the
500-line rule.

stdout carries exactly one line, the VM as JSON (bentoolkit parses it); every human
message goes to stderr. Exit codes: 0 ready, :data:`vm_dev.NO_ISO_EXIT` no ISO to boot,
1 any other failure, 2 a usage error.

Without ``--work-dir`` the session lives in the user's state directory and the cache stays
the shared one: ``--work-dir`` moves the cache too, and the trees a dev VM mounts are
hundreds of megabytes.
"""

import json
import os
from pathlib import Path

import typer

import shidashi
from shidashi import vm, vm_dev

#: Options ``--dev`` decides itself; given anyway, they are refused, never ignored.
_MANAGED = ("--uefi", "--cid", "--disk", "--share", "--forward", "--no-wait")


def start(
    *,
    iso: Path | None,
    name: str,
    init: str,
    arch: str,
    memory: str,
    cpus: int,
    display: str,
    disk_size: str | None,
    work_dir: Path | None,
    managed: dict[str, bool],
) -> None:
    """Start a dev VM, print it as JSON; raise :class:`typer.Exit` with the code above."""
    given = [opt for opt in _MANAGED if managed.get(opt)]
    if given:
        typer.echo(f"error: --dev decides {', '.join(given)} itself", err=True)
        raise typer.Exit(2)
    # cli._apply_work_dir has already pointed cache and scratch under --work-dir
    named_scratch = work_dir is not None or bool(os.environ.get("SHIDASHI_SCRATCH"))
    directory = vm.session_dir(name) if named_scratch else vm.user_session_dir(name)
    try:
        dev = vm_dev.start_dev(
            name=name,
            iso=iso,
            init=init,
            arch=arch,
            directory=directory,
            repo=Path(shidashi.__file__).resolve().parent.parent,
            memory=memory,
            cpus=cpus,
            display=display,
            disk_size=disk_size or vm_dev.DEFAULT_DISK_SIZE,
        )
    except vm_dev.NoDevIsoError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(vm_dev.NO_ISO_EXIT) from err
    except vm.VmError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    typer.echo(json.dumps(dev.as_json()))
