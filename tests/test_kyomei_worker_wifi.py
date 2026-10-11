"""Tests of the worker installing the Wi-Fi connection its provisioned identity carries
(story 021, task 4.2): ``restore(root, runner, *, source=...)`` -- ``--restore-medium``.

The contract (design.md, "Worker restore"): when the medium's identity holds
``wifi/shidashi-wifi.nmconnection``, the worker installs it as
``/etc/NetworkManager/system-connections/shidashi-wifi.nmconnection`` (root:root 0600 --
NetworkManager ignores a keyfile owned by another user or readable by others) and runs
``nmcli connection reload`` with the module's command timeout, BEFORE the RAM record. A
malformed file (not a ``type=wifi`` keyfile) is reported as ``kyomei: wifi connection
ignored`` and skipped: the identity still restores, a wired worker must not lose its
pairing to a bad Wi-Fi file. A failing NetworkManager reload is reported and the restore
still succeeds. The passphrase is never printed.

Everything privileged is faked as in tests/test_kyomei_worker_medium.py (its medium,
fresh root and chown recorder are reused, not copied); ``os.chown`` is recorded, never
run. The live root's ``/etc/NetworkManager/system-connections`` exists, as the image
ships it.

Hostile fixtures come first: keyfiles that merely LOOK like a Wi-Fi connection (``type=wifi``
in the wrong section, in a comment, without a section, ``wifi-p2p``) must be ignored,
and a real one written with the keyfile format's latitude (spaces around ``=``,
comments) must not be.

Requirements exercised: R5.4, R5.5, R5.7, R5.8.
"""

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_kyomei_worker import (
    _exit_code,
    _granted,
    _host_keys,
    _mount,
    _no_console,
    _ram_record,
    _reboot,
    _Runner,
    _worker_root,
    kw,
)
from tests.test_kyomei_worker_medium import _Chowns, _fresh_root, _medium, _names

#: The ``kw`` fixture comes from tests/test_kyomei_worker.py; naming it marks it used.
_FIXTURES = (kw,)

NM_DIR = Path("etc/NetworkManager/system-connections")
NM_FILE = NM_DIR / "shidashi-wifi.nmconnection"
WIFI_NAME = "shidashi-wifi.nmconnection"
#: A passphrase no message would contain by chance.
PSK = "Qv8 tKx3 m9Zr"

KEYFILE = (
    "[connection]\n"
    "id=bentoo-lab\n"
    "uuid=6f1c2a64-6c0e-5a8e-9d0b-3a4f5e6d7c8b\n"
    "type=wifi\n"
    "autoconnect=true\n"
    "\n"
    "[wifi]\n"
    "mode=infrastructure\n"
    "ssid=bentoo-lab\n"
    "\n"
    "[wifi-security]\n"
    "key-mgmt=wpa-psk\n"
    f"psk={PSK}\n"
    "\n"
    "[ipv4]\n"
    "method=auto\n"
    "\n"
    "[ipv6]\n"
    "method=auto\n"
).encode()


# --- helpers ----------------------------------------------------------------------------


def _nm_root(base: Path, seed: str = "boot-1") -> Path:
    """A fresh live root whose NetworkManager connections directory exists, as shipped."""
    root = _fresh_root(base, seed)
    (root / NM_DIR).mkdir(parents=True, mode=0o700)
    return root


def _with_wifi(source: Path, data: bytes, mode: int = 0o644) -> Path:
    """The identity's Wi-Fi connection on the medium, with the provisioning user's mode."""
    wifi = source / "wifi" / WIFI_NAME
    wifi.parent.mkdir(exist_ok=True)
    wifi.write_bytes(data)
    wifi.chmod(mode)
    return wifi


def _reloads(argv: list[str]) -> bool:
    """``nmcli connection reload`` (any path to nmcli, any global flags)."""
    if not argv or Path(argv[0]).name != "nmcli":
        return False
    return [a for a in argv[1:] if not a.startswith("-")] == ["connection", "reload"]


def _nmcli(argv: list[str]) -> bool:
    return bool(argv) and Path(argv[0]).name == "nmcli"


class _NmRunner(_Runner):
    """Also records each nmcli call's keyword arguments and what was on disk at that
    moment; ``nmcli`` can be made to fail (``"rc"``), time out or be missing."""

    def __init__(self, root: Path, *, nmcli: str = "ok", **kwargs: Any) -> None:
        super().__init__(root, **kwargs)
        self.nmcli = nmcli
        self.nm_kwargs: list[dict[str, Any]] = []
        self.at_reload: list[dict[str, Any]] = []

    def __call__(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(a) for a in argv]
        if _nmcli(argv):
            self.calls.append(argv)
            self.events.append(("run", tuple(argv)))
            self.nm_kwargs.append(dict(kwargs))
            installed = self.root / NM_FILE
            self.at_reload.append(
                {
                    "installed": installed.read_bytes() if installed.exists() else None,
                    "ram_record": _ram_record(self.root).exists(),
                }
            )
            if self.nmcli == "missing":
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            if self.nmcli == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 8)
            rc, err = (
                (8, "Error: NetworkManager is not running.\n") if self.nmcli == "rc" else (0, "")
            )
            if kwargs.get("check") and rc:
                raise subprocess.CalledProcessError(rc, argv, "", err)
            text = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
            if text:
                return subprocess.CompletedProcess(argv, rc, "", err)
            return subprocess.CompletedProcess(argv, rc, b"", err.encode())
        return super().__call__(argv, *args, **kwargs)

    def nmcli_calls(self) -> list[list[str]]:
        return [c for c in self.calls if _nmcli(c)]


def _restored(root: Path, source: Path, runner: _Runner) -> None:
    """The identity itself was restored: keys, record, name, sshd."""
    ssh = root / "etc" / "ssh"
    key = "ssh_host_ed25519_key"
    assert (ssh / key).read_bytes() == (source / "ssh" / key).read_bytes()
    assert json.loads(_ram_record(root).read_text())["name"] == "bentoo-lab"
    assert runner.hostnames_set() == [["hostnamectl", "hostname", "bentoo-lab"]]
    assert runner.at_sshd_start is not None


# --- R5.5 hostile first: what only looks like a Wi-Fi connection is ignored -------------


MALFORMED: dict[str, bytes] = {
    # type=wifi, but in the wrong section: the connection itself is ethernet
    "type-wifi-outside-connection": (
        b"[connection]\nid=bentoo-lab\ntype=ethernet\n\n[wifi]\ntype=wifi\nssid=bentoo-lab\n"
        b"\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=" + PSK.encode() + b"\n"
    ),
    # a prefix of the right value is not the right value
    "type-wifi-p2p": (
        b"[connection]\nid=bentoo-lab\ntype=wifi-p2p\n\n[wifi]\nssid=bentoo-lab\n"
        b"\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=" + PSK.encode() + b"\n"
    ),
    # the text is there, only commented out
    "type-wifi-only-in-a-comment": (
        b"[connection]\nid=bentoo-lab\n# type=wifi\n\n[wifi]\nssid=bentoo-lab\n"
        b"\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=" + PSK.encode() + b"\n"
    ),
    # no [connection] header at all
    "no-section-header": b"id=bentoo-lab\ntype=wifi\nssid=bentoo-lab\npsk=" + PSK.encode() + b"\n",
    "binary": bytes(range(256)) * 4,
    "empty": b"",
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_wifi_a_malformed_connection_is_ignored_and_the_identity_still_restores(
    tmp_path: Path,
    kw: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    root = _nm_root(tmp_path)
    source = _medium(root)
    _with_wifi(source, MALFORMED[case])
    _no_console(monkeypatch)
    runner = _NmRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    out, err = capsys.readouterr()
    assert "kyomei: wifi connection ignored" in err
    assert PSK not in out and PSK not in err
    assert list((root / NM_DIR).iterdir()) == []  # nothing installed
    assert runner.nmcli_calls() == []
    _restored(root, source, runner)


# --- R5.4 the converse: a real connection in the keyfile format's latitude installs ----


def test_wifi_a_connection_with_spaces_around_type_and_comments_is_installed(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hostile (split): a textual ``type=wifi`` match would refuse a valid keyfile."""
    data = (
        b"# written by hand\n"
        b"[connection]\n"
        b"id = bentoo-lab\n"
        b"; the type NetworkManager reads\n"
        b"type = wifi\n"
        b"\n"
        b"[wifi]\n"
        b"ssid = bentoo-lab\n"
        b"\n"
        b"[wifi-security]\n"
        b"key-mgmt = wpa-psk\n"
        b"psk = " + PSK.encode() + b"\n"
    )
    root = _nm_root(tmp_path)
    source = _medium(root)
    _with_wifi(source, data)
    runner = _NmRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    assert (root / NM_FILE).read_bytes() == data
    assert [c for c in runner.nmcli_calls() if _reloads(c)]
    _out, err = capsys.readouterr()
    assert "ignored" not in err


# --- R5.4: installed, root-only, then NetworkManager reloads -----------------------------


def test_wifi_the_connection_is_installed_byte_identical_root_root_0600(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _nm_root(tmp_path)
    source = _medium(root)
    medium_wifi = _with_wifi(source, KEYFILE, mode=0o644)  # Rock Ridge keeps the user's mode
    _no_console(monkeypatch)
    runner = _NmRunner(root)
    chowns = _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    installed = root / NM_FILE
    assert installed.read_bytes() == KEYFILE
    # hostile: the medium's 0644 must not be copied -- NetworkManager would ignore it
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    owners = chowns.owners_of(installed)
    assert owners and all(owner == (0, 0) for owner in owners)
    # the medium itself untouched (it is read-only on a real stick)
    assert medium_wifi.read_bytes() == KEYFILE
    assert stat.S_IMODE(medium_wifi.stat().st_mode) == 0o644
    assert not [c for c in chowns.calls if Path(c[0]).is_relative_to(source)]
    _restored(root, source, runner)
    out, err = capsys.readouterr()
    assert "ignored" not in err
    assert PSK not in out and PSK not in err


def test_wifi_networkmanager_reloads_with_the_file_in_place_before_the_ram_record(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _nm_root(tmp_path)
    source = _medium(root)
    _with_wifi(source, KEYFILE)
    runner = _NmRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    reloads = [i for i, c in enumerate(runner.nmcli_calls()) if _reloads(c)]
    assert len(reloads) == 1, runner.nmcli_calls()
    at = runner.at_reload[reloads[0]]
    assert at["installed"] == KEYFILE
    assert at["ram_record"] is False  # the record still says nothing: it comes last
    # re-owned before NetworkManager read it
    reload_event = next(
        i for i, e in enumerate(runner.events) if e[0] == "run" and _reloads(list(e[1]))
    )
    chowned = [
        i for i, e in enumerate(runner.events) if e[0] == "chown" and _names(e[1], root / NM_FILE)
    ]
    assert chowned and max(chowned) < reload_event
    assert _ram_record(root).exists()


def test_wifi_nmcli_runs_with_the_modules_command_timeout(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NetworkManager that never answers must not hang the boot unit."""
    root = _nm_root(tmp_path)
    source = _medium(root)
    _with_wifi(source, KEYFILE)
    runner = _NmRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    assert runner.nm_kwargs, "nmcli was never run"
    assert all(k.get("timeout") == kw._COMMAND_TIMEOUT for k in runner.nm_kwargs)


def test_wifi_a_stale_connection_is_replaced_and_an_unrelated_one_left_alone(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _nm_root(tmp_path)
    stale = root / NM_FILE
    stale.write_bytes(b"[connection]\nid=old-net\ntype=wifi\n\n[wifi]\nssid=old-net\n")
    stale.chmod(0o600)
    other = root / NM_DIR / "Wired connection 1.nmconnection"
    other_bytes = b"[connection]\nid=Wired connection 1\ntype=ethernet\n"
    other.write_bytes(other_bytes)
    other.chmod(0o600)
    source = _medium(root)
    _with_wifi(source, KEYFILE)
    runner = _NmRunner(root)
    chowns = _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    assert stale.read_bytes() == KEYFILE
    assert other.read_bytes() == other_bytes
    assert stat.S_IMODE(other.stat().st_mode) == 0o600
    assert chowns.owners_of(other) == []
    assert sorted(p.name for p in (root / NM_DIR).iterdir()) == sorted(
        [WIFI_NAME, "Wired connection 1.nmconnection"]
    )


# --- a failing NetworkManager costs the Wi-Fi, never the identity ------------------------


@pytest.mark.parametrize("failure", ["rc", "timeout", "missing"])
def test_wifi_a_failing_nmcli_is_reported_and_the_restore_still_succeeds(
    tmp_path: Path,
    kw: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    root = _nm_root(tmp_path)
    source = _medium(root)
    _with_wifi(source, KEYFILE)
    runner = _NmRunner(root, nmcli=failure)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    assert runner.nmcli_calls(), "nmcli was never run"
    out, err = capsys.readouterr()
    assert "kyomei:" in err
    assert PSK not in out and PSK not in err
    assert (root / NM_FILE).read_bytes() == KEYFILE  # NetworkManager reads it on its start
    _restored(root, source, runner)


# --- R5.8: no connection on the medium, no NetworkManager call ---------------------------


def test_wifi_a_medium_without_a_connection_installs_none_and_runs_no_nmcli(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _nm_root(tmp_path)
    source = _medium(root)
    runner = _NmRunner(root)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=root, runner=runner, source=source) == 0

    assert list((root / NM_DIR).iterdir()) == []
    assert runner.nmcli_calls() == []
    _out, err = capsys.readouterr()
    assert "wifi" not in err.lower()
    _restored(root, source, runner)


def test_wifi_the_work_disk_restore_never_installs_a_connection(
    tmp_path: Path, kw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a provisioned medium carries Wi-Fi: a file at the same relative path on the
    work disk (which any job running as root could have written) is not installed."""
    first = _worker_root(tmp_path, "boot-1")
    _mount(monkeypatch, first)
    _granted(first, name="bentoo-lab")
    kw.persist(first)
    second = _reboot(tmp_path, first)
    (second / NM_DIR).mkdir(parents=True, mode=0o700)
    _with_wifi(second / "mnt" / "work" / ".shidashi", KEYFILE)
    _mount(monkeypatch, first, second)
    pinned = _host_keys(first)
    runner = _NmRunner(second, mounted=True)
    _Chowns(monkeypatch, runner)

    assert _exit_code(kw, kw.restore, root=second, runner=runner) == 0

    assert _host_keys(second) == pinned  # the disk restore itself still worked
    assert list((second / NM_DIR).iterdir()) == []
    assert runner.nmcli_calls() == []
