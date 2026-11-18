"""The CLI side of a dev VM on a build worker -- kept out of ``cli.py``, like
:mod:`shidashi.vm_dev_cli`, whose contract with bentoolkit it keeps to the letter.

Where a ``vm start --dev`` runs its VM (:func:`chosen_worker`): ``--worker NAME``, else
the worker ``SHIDASHI_DEV_WORKER`` names (:func:`shidashi.config.dev_worker`), else this
host; ``--local`` is this host whatever the variable says. Both options together are a
usage error. ``vm run`` and ``vm stop`` take no option: a session recorded on a worker
(:mod:`shidashi.vm_dev_record`) is relayed there, any other is a VM on this host
(:func:`run_if_on_worker`, :func:`stop_if_on_worker`). The Shidashi a relay runs on
the worker never relays again (:func:`shidashi.config.dev_relayed`).

The start prints the worker's one JSON line on stdout with ``"worker"`` added; every
human message goes to stderr, one line each. Exit codes, the local ones: 0 ready,
3 no ISO to boot, 2 a usage error (a name that cannot cross to a worker included),
1 any other failure -- no paired worker of that name, one that does not answer, a job
running there, a start that failed there. ``vm run`` exits with the guest command's
code, 124 past its timeout, ssh's own 255 when the worker could not be reached, and 1
when the worker is not paired any more. ``vm stop`` exits 0 for a session the worker no
longer has -- said on stderr, the record forgotten -- and 1, the record kept, when the
worker does not answer or its ``vm stop`` fails.

A worker's name becomes a :class:`shidashi.remote.Remote` the way ``shidashi worker run``
makes one -- the registry, the recorded address, mDNS when that one does not answer
(``cli._paired``, ``cli._on_worker``, looked up at call time) -- so the errors are the
same. A start is tried at a second address only when the first never answered; a
``vm run`` never is (a command that may have run is not run twice); a stop is.

A start and a stop on a worker are audited runs on this host (``vm-dev-worker``,
``vm-dev-worker-stop``), as a ``worker job`` is; a ``vm run`` opens none -- bentoolkit
runs hundreds -- and records in whatever run is in force.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

import typer

from shidashi import (
    config,
    i18n,
    stop_signals,
    vm,
    vm_dev,
    vm_dev_expiry,
    vm_dev_record,
    vm_dev_remote,
    vm_dev_remote_run,
    workers,
)
from shidashi import remote as remote_mod
from shidashi.remote import Remote

#: ssh's own exit code: a relayed command that ends with it reached nobody.
_RELAY_SSH_EXIT = 255
#: What a relay reports as one line and exit 1.
_RELAY_ERRORS = (vm.VmError, workers.RegistryError, remote_mod.RemoteError, OSError)


def _relay_error(message: object, code: int = 1) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def chosen_worker(*, worker: str | None, local: bool) -> str | None:
    """The worker a ``vm start --dev`` runs its VM on; None: this host. Exit 2 when
    ``--worker`` and ``--local`` were both given."""
    if worker is not None and local:
        _relay_error("--worker and --local exclude each other", 2)
    if local or config.dev_relayed():
        return None
    return worker if worker is not None else config.dev_worker()


def _on_named_worker[T](
    name: str,
    attempt: Callable[[Remote], T],
    *,
    reached: Callable[[T], bool] = lambda _result: True,
    retry_on: Callable[[Exception], bool] | None = None,
) -> T:
    """``attempt`` against paired worker ``name``, reached as ``worker run`` reaches it.
    ``retry_on`` None: a second address is tried whenever the first did not answer."""
    from shidashi import cli  # call time: cli imports the modules that import this one

    retry = cli._unanswered if retry_on is None else retry_on
    result, _entry = cli._on_worker(
        cli._paired(name), None, attempt, reached=reached, retry_on=retry
    )
    return result


def _recorded(name: str) -> vm_dev_record.WorkerSession | None:
    """The host's record of session ``name``; None when it has none -- and always on
    the worker's side of a relay, which is where the chain ends."""
    if config.dev_relayed():
        return None
    try:
        return vm_dev_record.read_record(name)
    except vm.VmError as err:
        _relay_error(err)


def refuse_recorded(name: str) -> None:
    """Exit 1 when session ``name`` is recorded on a worker: a VM started on this host
    under that name would be one ``vm run`` and ``vm stop`` never reach."""
    session = _recorded(name)
    if session is not None:
        _relay_error(
            f"VM session {name!r} is recorded on worker {session.worker} (started "
            f"{session.started}): stop it with `shidashi vm stop --name {name}` first, or "
            "choose another name"
        )


def start(*, worker_name: str, request: vm_dev_remote.StartRequest, repo: Path) -> None:
    """Start a dev VM on worker ``worker_name`` and print it as JSON; raise
    :class:`typer.Exit` with the codes above. SIGTERM and SIGHUP unwind, so a start
    that is stopped undoes what it began on the worker."""
    from shidashi import cli  # call time, as above

    reason = vm_dev_record.session_name_error(request.name)
    if reason is not None:
        _relay_error(f"--name: {reason}", 2)
    try:
        with (
            stop_signals.unwinding_on_stop(),
            cli._worker_audit("vm-dev-worker", worker=worker_name, session=request.name),
        ):
            line = _on_named_worker(
                worker_name,
                lambda target: vm_dev_remote.start_on_worker(
                    target,
                    request,
                    repo=repo,
                    warn=lambda message: typer.echo(f"warning: {message}", err=True),
                ),
                # only the contact that opens the start: nothing was begun there yet
                retry_on=lambda err: isinstance(err, vm_dev_remote.NoFirstAnswer),
            )
    except vm_dev.NoDevIsoError as err:
        _relay_error(err, vm_dev.NO_ISO_EXIT)
    except _RELAY_ERRORS as err:
        _relay_error(err)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    typer.echo(json.dumps(line))


def run_if_on_worker(name: str, command: Sequence[str], *, timeout: int, stream: bool) -> None:
    """``vm run`` for a session recorded on a worker: relay it and raise
    :class:`typer.Exit` with its code. Returns, having done nothing, for any other."""
    session = _recorded(name)
    if session is None:
        return
    try:
        code = _on_named_worker(
            session.worker,
            lambda target: vm_dev_remote_run.run_on_worker(
                target, session, " ".join(command), timeout=timeout, stream=stream
            ),
            reached=lambda exit_code: exit_code != _RELAY_SSH_EXIT,
            retry_on=lambda _err: False,
        )
    except _RELAY_ERRORS as err:
        _relay_error(err)
    raise typer.Exit(code)


def _why_gone(session: vm_dev_record.WorkerSession) -> str:
    """What most likely ended a session the worker no longer has. Pure but for the clock."""
    now = vm_dev_expiry.utc_stamp(vm_dev_expiry.utc_now())
    if session.expires is not None and session.expires <= now:  # one format, UTC: sortable
        return i18n.t("its maximum life ended at {stamp}").format(stamp=session.expires)
    return i18n.t("the worker restarted, or a job took its place")


def stop_if_on_worker(name: str) -> None:
    """``vm stop`` for a session recorded on a worker: stop it there, forget it, and
    raise :class:`typer.Exit`. Returns, having done nothing, for any other."""
    session = _recorded(name)
    if session is None:
        return
    from shidashi import cli  # call time, as above

    try:
        with cli._worker_audit("vm-dev-worker-stop", worker=session.worker, session=name):
            there = _on_named_worker(
                session.worker, lambda target: vm_dev_remote.stop_on_worker(target, session)
            )
    except _RELAY_ERRORS as err:
        _relay_error(f"{err} -- session {name} is still recorded on {session.worker}")
    if not there:
        gone = i18n.t("VM session {name} was no longer on {worker} ({reason}): forgotten")
        typer.echo(
            gone.format(name=name, worker=session.worker, reason=_why_gone(session)), err=True
        )
    raise typer.Exit(0)
