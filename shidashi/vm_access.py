"""Reaching a dev VM from outside Shidashi: an ssh ``Host`` for an editor, and variables.

``vm run`` is enough for bentoolkit; an editor that opens a remote project (Zeo, Zed, VS
Code) needs a plain ``ssh <host>`` that lands in the guest. Proven by hand on bentoo-lab
on 2026-11-16, Zeo's agent running in the guest:

- **A session on this host** is reached as ``vm run`` reaches it: ``root@vsock/<cid>``
  through ``systemd-ssh-proxy`` (:func:`shidashi.vm.ssh_argv`), with the session's own key.
- **A session on a build worker** cannot be: ``systemd-ssh-proxy`` hands ssh the vsock as
  a file descriptor (``Failed to send socket via STDOUT`` once its stdout is an ssh
  channel), and a descriptor does not cross a hop. So the ``ProxyCommand`` is the pinned
  ssh to the worker running ``vm ssh-relay`` there, which joins its stdin and stdout to
  the guest's sshd on vsock (:func:`pump`). The client key is made on this host
  (:data:`shidashi.vm_dev_record.CLIENT_KEY`) and added to the guest's authorized_keys
  (:func:`authorize_script`); the session's own key never leaves the worker.

The guest's host key changes with every boot of a live medium, so it is never checked --
the transport is vsock, and the hop to a worker is pinned (:mod:`shidashi.remote`).

``vm env`` puts a variable in the guest without it ever being a command-line word: its
value is the command's stdin, kept in RAM under :data:`ENV_DIR` (``/run`` is a tmpfs; mode
0600 in a 0700 directory), and :data:`PROFILE` exports every such file in a login shell --
which is how an editor's remote server reads a project's environment. Nothing of it
reaches the work disk, and it goes with the VM.
"""

import os
import re
import shlex
import socket
import threading
from pathlib import Path

#: The prefix of every ``Host`` this module writes.
HOST_PREFIX = "shidashi-"
#: The guest's sshd on vsock (systemd-ssh-generator binds port 22).
GUEST_SSH_PORT = 22
#: Where ``vm env`` keeps a variable in the guest: RAM.
ENV_DIR = "/run/shidashi-env"
#: The login-shell snippet that exports what :data:`ENV_DIR` holds.
PROFILE = "/etc/profile.d/shidashi-env.sh"
_SSH_PROXY = "/usr/lib/systemd/systemd-ssh-proxy"
#: A name a POSIX shell exports as it is, of a sane length.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
#: One public key with no options in front (``command=``, ``from=`` would ride along).
_PUBKEY_ERE = (
    r"^(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+) [A-Za-z0-9+/=]+( [^[:cntrl:]]*)?$"
)
_CHUNK = 65536


def host_alias(name: str) -> str:
    """The ``Host`` an editor opens for session ``name``. Pure."""
    return f"{HOST_PREFIX}{name}"


def _config_word(value: str) -> str:
    """``value`` as one ssh_config argument: double-quoted when it holds a blank."""
    return f'"{value}"' if any(c.isspace() for c in value) else value


def _common(name: str, key: Path) -> list[str]:
    return [
        f"Host {host_alias(name)}",
        "    User root",
        f"    IdentityFile {_config_word(str(key))}",
        "    IdentitiesOnly yes",
        "    StrictHostKeyChecking no",
        "    UserKnownHostsFile /dev/null",
        "    LogLevel ERROR",
        "    ServerAliveInterval 30",
    ]


def local_host_block(name: str, *, key: Path, cid: int) -> str:
    """The ``Host`` of a session on this host: vsock through systemd-ssh-proxy. Pure."""
    lines = _common(name, key)
    lines[1:1] = [f"    HostName vsock/{cid}"]
    lines.append(f"    ProxyCommand {_SSH_PROXY} %h %p")
    return "\n".join(lines) + "\n"


def worker_host_block(name: str, *, key: Path, proxy: list[str]) -> str:
    """The ``Host`` of a session on a worker: ``proxy`` (the pinned ssh running
    ``vm ssh-relay`` there) is the ``ProxyCommand``. Pure.

    ssh expands ``%`` tokens in a ``ProxyCommand``, so a literal one is doubled.
    """
    command = shlex.join(proxy).replace("%", "%%")
    return "\n".join([*_common(name, key), f"    ProxyCommand {command}"]) + "\n"


def env_name_error(name: str) -> str | None:
    """Why ``name`` cannot be a guest variable, or None. Pure."""
    if _ENV_NAME_RE.fullmatch(name):
        return None
    return f"{name!r} is not a variable name: 1-128 of A-Z a-z 0-9 _, not starting with a digit"


def authorize_script(*, home: str = "/root") -> str:
    """Guest shell: add the public key on stdin to ``home``'s authorized_keys. Pure.

    One key, no options in front, or exit 2 having written nothing. An existing last line
    with no newline -- what systemd wrote from a credential before Shidashi ended it -- is
    ended first, so the key never joins it. A key already there is not added twice.
    """
    ssh = shlex.quote(f"{home}/.ssh")
    keys = shlex.quote(f"{home}/.ssh/authorized_keys")
    return (
        "set -eu; umask 077; "
        'IFS= read -r k || [ -n "$k" ]; '
        f"printf '%s\\n' \"$k\" | grep -Eq {shlex.quote(_PUBKEY_ERE)} "
        "|| { echo 'not one public key' >&2; exit 2; }; "
        f"install -d -m 700 {ssh}; touch {keys}; chmod 600 {keys}; "
        f'if [ -n "$(tail -c 1 {keys})" ]; then printf "\\n" >> {keys}; fi; '
        f'grep -qxF "$k" {keys} || printf \'%s\\n\' "$k" >> {keys}'
    )


def profile_script(*, env_dir: str = ENV_DIR) -> str:
    """The login-shell snippet exporting each file of ``env_dir`` as a variable. Pure.

    A file whose name is no variable name (a ``.tmp`` left by a cut write) is skipped:
    ``export`` would fail on it, and with it the login shell an editor depends on.
    """
    return (
        "# shidashi vm env: variables set with `shidashi vm env`, kept in RAM\n"
        f"for _shidashi_f in {shlex.quote(env_dir)}/*; do\n"
        '    [ -f "$_shidashi_f" ] || continue\n'
        '    case "${_shidashi_f##*/}" in *[!A-Za-z0-9_]*|[0-9]*) continue ;; esac\n'
        '    export "${_shidashi_f##*/}=$(cat "$_shidashi_f")"\n'
        "done\n"
        "unset _shidashi_f\n"
    )


def env_script(name: str, *, env_dir: str = ENV_DIR, profile: str = PROFILE) -> str:
    """Guest shell: store stdin as variable ``name`` and make login shells export it. Pure.

    ``name`` must have passed :func:`env_name_error`. The value is never echoed; it is
    written whole or not at all (a temporary file, then a rename).
    """
    if env_name_error(name) is not None:
        raise ValueError(f"not a variable name: {name!r}")
    directory = shlex.quote(env_dir)
    target = shlex.quote(f"{env_dir}/{name}")
    temp = shlex.quote(f"{env_dir}/{name}.tmp")
    snippet = shlex.quote(profile)
    snippet_temp = shlex.quote(f"{profile}.tmp")
    return (
        "set -eu; umask 077; "
        f"install -d -m 700 {directory}; cat > {temp}; mv -f {temp} {target}; "
        f"install -d -m 755 {shlex.quote(os.path.dirname(profile) or '.')}; "
        f"printf '%s' {shlex.quote(profile_script(env_dir=env_dir))} > {snippet_temp}; "
        f"chmod 644 {snippet_temp}; mv -f {snippet_temp} {snippet}"
    )


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def pump(sock: socket.socket, in_fd: int, out_fd: int) -> None:
    """Join ``in_fd`` -> ``sock`` and ``sock`` -> ``out_fd`` until the guest ends. I/O.

    The end of ``in_fd`` is passed on as a half close, so the guest sees end of stream
    and still answers; the relay ends when the guest closes its side.
    """

    def upstream() -> None:
        try:
            while chunk := os.read(in_fd, _CHUNK):
                sock.sendall(chunk)
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            return  # the guest is gone: the downstream loop ends on its own

    threading.Thread(target=upstream, daemon=True).start()
    try:
        while chunk := sock.recv(_CHUNK):
            _write_all(out_fd, chunk)
    except BrokenPipeError:
        return  # ssh closed our stdout: nobody is listening any more
    finally:
        sock.close()


def relay(cid: int, *, port: int = GUEST_SSH_PORT) -> None:
    """``vm ssh-relay``: stdin and stdout to the guest's sshd on vsock. I/O."""
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.connect((cid, port))
    pump(sock, 0, 1)
