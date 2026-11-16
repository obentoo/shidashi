"""``vm ssh-config``, ``vm ssh-relay`` and ``vm env``: what they write and run (vm_access).

The guest scripts are run for real by ``sh`` against a temporary directory standing in for
the guest's ``/root`` and ``/run``: they are the part a test can only trust by running.
"""

import os
import shlex
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from shidashi import vm_access


def test_the_host_alias_names_the_session() -> None:
    assert vm_access.host_alias("dev") == "shidashi-dev"


def test_a_local_session_is_reached_through_systemd_ssh_proxy(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    block = vm_access.local_host_block("dev", key=key, cid=7)
    lines = [line.strip() for line in block.splitlines()]
    assert lines[0] == "Host shidashi-dev"
    assert "HostName vsock/7" in lines
    assert "User root" in lines
    assert f"IdentityFile {key}" in lines
    assert "IdentitiesOnly yes" in lines
    assert "ProxyCommand /usr/lib/systemd/systemd-ssh-proxy %h %p" in lines
    # the guest's host key changes with every boot of a live medium (vm.ssh_argv)
    assert "StrictHostKeyChecking no" in lines
    assert "UserKnownHostsFile /dev/null" in lines


def test_a_worker_session_is_reached_through_the_pinned_worker(tmp_path: Path) -> None:
    key = tmp_path / "client_ed25519"
    proxy = ["ssh", "-o", "UserKnownHostsFile=/home/me/kh", "root@10.0.0.3", "cd /w && x 50%"]
    block = vm_access.worker_host_block("dev", key=key, proxy=proxy)
    lines = [line.strip() for line in block.splitlines()]
    assert lines[0] == "Host shidashi-dev"
    assert f"IdentityFile {key}" in lines
    command = next(line for line in lines if line.startswith("ProxyCommand "))
    # ssh expands % tokens in ProxyCommand: a literal one is doubled
    assert command == "ProxyCommand " + shlex.join(proxy).replace("%", "%%")
    assert not any(line.startswith("HostName") for line in lines)


@pytest.mark.parametrize("name", ["CLAUDE_CODE_OAUTH_TOKEN", "_X", "a1"])
def test_an_environment_name_is_accepted(name: str) -> None:
    assert vm_access.env_name_error(name) is None


@pytest.mark.parametrize("name", ["", "1A", "A-B", "A B", "A=B", "$(id)", "A" * 129])
def test_an_environment_name_that_a_shell_would_read_otherwise_is_refused(name: str) -> None:
    assert vm_access.env_name_error(name) is not None


def _sh(script: str, *, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", "-c", script], input=stdin, capture_output=True, text=True)


def test_authorizing_a_key_mends_an_unended_line_and_adds_the_key_once(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    keys = ssh / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAA session")  # as systemd wrote it before the fix
    script = vm_access.authorize_script(home=str(tmp_path))
    for _ in range(2):
        done = _sh(script, stdin="ssh-ed25519 BBBB client\n")
        assert done.returncode == 0, done.stderr
    assert keys.read_text() == "ssh-ed25519 AAAA session\nssh-ed25519 BBBB client\n"
    assert oct(keys.stat().st_mode & 0o777) == "0o600"


def test_authorizing_a_key_creates_the_file_when_there_is_none(tmp_path: Path) -> None:
    done = _sh(vm_access.authorize_script(home=str(tmp_path)), stdin="ssh-ed25519 BBBB c\n")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / ".ssh" / "authorized_keys").read_text() == "ssh-ed25519 BBBB c\n"


def test_authorizing_refuses_what_is_not_one_public_key(tmp_path: Path) -> None:
    done = _sh(vm_access.authorize_script(home=str(tmp_path)), stdin='command="x" ssh-rsa A\n')
    assert done.returncode != 0
    assert not (tmp_path / ".ssh" / "authorized_keys").exists()


def test_a_variable_set_in_the_guest_is_kept_private_and_exported_by_a_login_shell(
    tmp_path: Path,
) -> None:
    env_dir, profile = tmp_path / "run-env", tmp_path / "profile.d" / "shidashi-env.sh"
    script = vm_access.env_script("TOKEN", env_dir=str(env_dir), profile=str(profile))
    done = _sh(script, stdin="s3cr3t\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "" and "s3cr3t" not in done.stderr
    stored = env_dir / "TOKEN"
    assert oct(stored.stat().st_mode & 0o777) == "0o600"
    assert oct(env_dir.stat().st_mode & 0o777) == "0o700"
    login = subprocess.run(
        ["sh", "-c", f'. {shlex.quote(str(profile))}; printf "%s" "$TOKEN"'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )
    assert login.stdout == "s3cr3t"


def test_the_login_profile_is_harmless_when_nothing_was_set(tmp_path: Path) -> None:
    env_dir, profile = tmp_path / "run-env", tmp_path / "shidashi-env.sh"
    profile.write_text(vm_access.profile_script(env_dir=str(env_dir)))
    login = subprocess.run(["sh", "-c", f". {profile}; echo ok"], capture_output=True, text=True)
    assert login.returncode == 0 and login.stdout == "ok\n"


def test_the_relay_carries_both_directions_until_each_side_ends() -> None:
    guest, ours = socket.socketpair()
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    relay = threading.Thread(target=vm_access.pump, args=(ours, in_r, out_w))
    relay.start()
    os.write(in_w, b"SSH-2.0-client\r\n")
    os.close(in_w)
    assert guest.recv(64) == b"SSH-2.0-client\r\n"
    assert guest.recv(64) == b""  # our stdin ended: the guest sees end of stream
    guest.sendall(b"SSH-2.0-guest\r\n")
    guest.close()
    relay.join(timeout=5)
    assert not relay.is_alive()
    assert os.read(out_r, 64) == b"SSH-2.0-guest\r\n"
