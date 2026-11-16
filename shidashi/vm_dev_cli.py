"""The CLI side of ``shidashi vm start --dev`` -- kept out of ``cli.py``, which predates the
500-line rule.

stdout carries exactly one line, the VM as JSON (bentoolkit parses it); every human
message goes to stderr. Exit codes: 0 ready, :data:`vm_dev.NO_ISO_EXIT` no ISO to boot,
1 any other failure, 2 a usage error -- an option ``--dev`` decides itself, a
``--max-life`` that is no positive number of hours, a ``--pins-json`` that is no pins
block, either of the two without ``--dev``. A usage error is told before anything boots.

Without ``--work-dir`` the session lives in the user's state directory and the cache stays
the shared one: ``--work-dir`` moves the cache too, and the trees a dev VM mounts are
hundreds of megabytes.

``--pins-json`` is hidden: it is how a relay on the host hands the ISO's pins to the
``vm start --dev`` it runs on a build worker, whose source tree has no git to read them
with (:func:`shidashi.iso_pins.handed_pins`).
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

import typer

import shidashi
from shidashi import iso_pins, vm, vm_dev, vm_dev_expiry

#: Options ``--dev`` decides itself; given anyway, they are refused, never ignored.
_MANAGED = ("--uefi", "--cid", "--disk", "--share", "--forward", "--no-wait")


def _usage_error(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(2)


def refuse_dev_only(given: Mapping[str, bool]) -> None:
    """Exit 2 when an option that only means something with ``--dev`` was given without
    it: a session expected to end itself, or to use the pins handed in, would not."""
    options = [option for option, was_given in given.items() if was_given]
    if options:
        _usage_error(f"{', '.join(options)} needs --dev")


def start(
    *,
    iso: Path | None,
    name: str,
    init: str,
    arch: str,
    memory: str | None,
    cpus: int,
    display: str,
    disk_size: str | None,
    work_dir: Path | None,
    managed: dict[str, bool],
    max_life: float | None = None,
    pins_json: str | None = None,
) -> None:
    """Start a dev VM, print it as JSON; raise :class:`typer.Exit` with the code above.

    ``memory`` None is "not given": :data:`vm.DEFAULT_MEMORY`.
    """
    given = [opt for opt in _MANAGED if managed.get(opt)]
    if given:
        _usage_error(f"--dev decides {', '.join(given)} itself")
    if max_life is not None:
        reason = vm_dev_expiry.max_life_error(max_life, now=vm_dev_expiry.utc_now())
        if reason is not None:
            _usage_error(f"--max-life: {reason}")
    pins = None
    if pins_json is not None:
        try:
            pins = iso_pins.handed_pins(json.loads(pins_json))
        except (ValueError, iso_pins.IsoPinsError) as err:  # JSONDecodeError is a ValueError
            _usage_error(f"--pins-json: {err}")
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
            memory=memory or vm.DEFAULT_MEMORY,
            cpus=cpus,
            display=display,
            disk_size=disk_size or vm_dev.DEFAULT_DISK_SIZE,
            warn=lambda message: typer.echo(f"warning: {message}", err=True),
            pins=pins,
            max_life=max_life,
        )
    except vm_dev.NoDevIsoError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(vm_dev.NO_ISO_EXIT) from err
    except vm.VmError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from err
    typer.echo(json.dumps(dev.as_json()))
