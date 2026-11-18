"""``vm ssh-config``, ``vm ssh-relay`` and ``vm env`` -- the CLI side of :mod:`vm_access`.

Kept out of ``cli.py`` (past its size limit); :func:`register` adds the commands to the
``vm`` group. A session recorded on a build worker is reached there, as ``vm run``
reaches it (:mod:`shidashi.vm_dev_remote_cli`); any other is a VM on this host.

Exit codes: 0 done, 2 a usage error (a variable name, a value typed at a terminal), 1
anything else -- no such session, a worker that did not answer, a guest that refused.
"""

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from shidashi import i18n, vm, vm_access, vm_dev_record

_KEYGEN_TIMEOUT = 30
#: Seconds the guest gets for a key or a variable: a few file writes.
_GUEST_TIMEOUT = 60
#: ssh's own exit code: it reached nobody.
_NOBODY_ANSWERED = 255


def register(vm_app: typer.Typer) -> None:
    """Add this module's commands to ``shidashi vm``."""
    vm_app.command("ssh-config")(vm_ssh_config)
    vm_app.command("env")(vm_env)
    # the ProxyCommand `vm ssh-config` writes for a session on a worker: not for a person
    vm_app.command("ssh-relay", hidden=True)(vm_ssh_relay)


def _fail(message: object, code: int = 1) -> typer.Exit:
    print(f"error: {message}", file=sys.stderr, flush=True)
    return typer.Exit(code)


def _client_key(directory: Path, name: str) -> Path:
    """The client key of session ``name`` on this host, made on first use. I/O."""
    key = directory / vm_dev_record.CLIENT_KEY
    if key.exists() and key.with_suffix(".pub").exists():
        return key
    directory.mkdir(parents=True, exist_ok=True)
    for stale in (key, key.with_suffix(".pub")):
        stale.unlink(missing_ok=True)
    made = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"shidashi-{name}", "-f", str(key)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_KEYGEN_TIMEOUT,
        check=False,
    )
    if made.returncode != 0:
        raise _fail(f"ssh-keygen could not make {key}: {made.stderr.strip()}")
    return key


def _worker_block(record: vm_dev_record.WorkerSession) -> str:
    """Authorize this host's client key in the guest on the worker; its ``Host``. I/O."""
    from shidashi import remote as remote_mod
    from shidashi import vm_dev_remote_cli, vm_dev_remote_run, vm_dev_remote_shell

    key = _client_key(vm_dev_record.record_path(record.name).parent, record.name)
    script = vm_access.authorize_script()

    def authorize(target: remote_mod.Remote) -> tuple[int, remote_mod.Remote]:
        with key.with_suffix(".pub").open("rb") as pubkey:
            code = vm_dev_remote_run.run_on_worker(
                target,
                record,
                script,
                timeout=_GUEST_TIMEOUT,
                stream=False,
                stdin=pubkey,
                stdout=subprocess.DEVNULL,
            )
        return code, target

    code, target = vm_dev_remote_cli._on_named_worker(
        record.worker, authorize, reached=lambda done: done[0] != _NOBODY_ANSWERED
    )
    if code != 0:
        raise _fail(f"the guest of {record.name} on {record.worker} refused the key (exit {code})")
    relay = vm_dev_remote_shell.relayed_ssh_relay_command(record.commit, record.name)
    return vm_access.worker_host_block(
        record.name, key=key, proxy=remote_mod.ssh_argv(target, relay)
    )


def vm_ssh_config(
    name: Annotated[str, typer.Option("--name")] = "bentoo",
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Print an ssh ``Host`` (``shidashi-NAME``) that lands as root in the VM, and keep it
    beside the session as ``ssh_config``. One ``Include`` of
    ``~/.local/state/shidashi/vm/*/ssh_config`` in ``~/.ssh/config`` makes every session
    that ran this reachable -- by an editor's remote project (Zeo, Zed, VS Code) or by
    ``ssh shidashi-NAME``. A session on a build worker is reached through the worker with
    a key made here; it goes when the session is stopped."""
    from shidashi import cli, vm_dev_remote_cli

    cli._apply_work_dir(work_dir)
    record = vm_dev_remote_cli._recorded(name)
    try:
        if record is not None:
            block = _worker_block(record)
        else:
            session = vm.load_session(name)
            block = vm_access.local_host_block(name, key=session.key, cid=session.spec.cid)
    except vm_dev_remote_cli._RELAY_ERRORS as err:  # the words `vm run` gives for them
        raise _fail(err) from err
    path = vm.user_session_dir(name) / vm_dev_record.SSH_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block, encoding="utf-8")
    path.chmod(0o600)
    sys.stdout.write(block)
    print(
        i18n.t(
            "written to {path}; reach it with `ssh {alias}` once ~/.ssh/config has: {include}"
        ).format(
            path=path,
            alias=vm_access.host_alias(name),
            include=f"Include {path.parent.parent}/*/{vm_dev_record.SSH_CONFIG}",
        ),
        file=sys.stderr,
    )


def vm_ssh_relay(
    name: Annotated[str, typer.Option("--name")] = "bentoo",
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Join stdin and stdout to the guest's sshd on vsock (a ``ProxyCommand``)."""
    from shidashi import cli

    cli._apply_work_dir(work_dir)
    try:
        cid = vm.load_session(name).spec.cid
        vm_access.relay(cid)
    except (vm.VmError, OSError) as err:
        raise _fail(err) from err


def vm_env(
    variable: Annotated[
        str, typer.Argument(help="The variable's name, e.g. CLAUDE_CODE_OAUTH_TOKEN.")
    ],
    name: Annotated[str, typer.Option("--name")] = "bentoo",
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Set a variable in the VM from stdin, for every login shell there -- the
    environment an editor's remote server and its agents start from. The value never
    is a command-line word and never reaches a disk: it is kept in RAM in the guest
    (``/run``, mode 0600) and goes with the VM. Pipe it in, e.g.
    ``secret-tool lookup service anthropic purpose X | shidashi vm env --name dev
    CLAUDE_CODE_OAUTH_TOKEN``."""
    from shidashi import cli, vm_dev_remote_cli, vm_dev_remote_run, vm_exec

    cli._apply_work_dir(work_dir)
    problem = vm_access.env_name_error(variable)
    if problem is not None:
        raise _fail(problem, 2)
    if sys.stdin.isatty():
        raise _fail("pipe the value in: typed at a terminal it would be echoed", 2)
    script = vm_access.env_script(variable)
    record = vm_dev_remote_cli._recorded(name)
    try:
        if record is not None:
            code = vm_dev_remote_cli._on_named_worker(
                record.worker,
                lambda target: vm_dev_remote_run.run_on_worker(
                    target, record, script, timeout=_GUEST_TIMEOUT, stream=False
                ),
                reached=lambda exit_code: exit_code != _NOBODY_ANSWERED,
                retry_on=lambda _err: False,  # stdin is read once: never sent twice
            )
        else:
            result = vm_exec.run_captured(vm.load_session(name), script, timeout=_GUEST_TIMEOUT)
            sys.stderr.write(result.stderr)
            code = result.exit_code
    except vm_dev_remote_cli._RELAY_ERRORS as err:
        raise _fail(err) from err
    if code != 0:
        raise _fail(f"the guest of {name} did not take {variable} (exit {code})")
    print(
        i18n.t("{variable} set in {name} for login shells (kept in RAM)").format(
            variable=variable, name=name
        ),
        file=sys.stderr,
    )
