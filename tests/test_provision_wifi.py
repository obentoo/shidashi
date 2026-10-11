"""Tests of the Wi-Fi connection a provisioned worker carries (story 021, task 4.1):
``provision.wifi_connection(ssid, psk) -> bytes`` (a NetworkManager keyfile),
``provision.provision(..., wifi=(ssid, psk))`` and ``shidashi worker provision N --iso ISO
--wifi SSID``.

The contract (design.md, "worker provision N --wifi SSID"): the keyfile holds
``[connection]`` (id, a uuid5 stable per SSID, ``type=wifi``, ``autoconnect=true``),
``[wifi]`` (infrastructure, the SSID), ``[wifi-security]`` (``wpa-psk``, the passphrase),
``[ipv4]``/``[ipv6]`` ``method=auto``; provision writes it as
``identity/wifi/shidashi-wifi.nmconnection`` (0600, root-only on the medium); the SSID is
1-32 bytes, the passphrase 8-63 printable ASCII or 64 hex digits, and an invalid one is
refused before anything is written; the passphrase is read without echo and confirmed on
a terminal, or as one line from stdin; it is never printed, logged or recorded.

What the keyfile MEANS is NetworkManager's call, not configparser's: where ``nmcli
--offline`` exists, NetworkManager reads the rendered keyfile and its reading is compared
with its own rendering of the same SSID and passphrase. The successful runs let
``ssh-keygen`` and ``xorriso`` really run (skipped where absent), and the terminal is a
real pseudo-terminal. Hostile fixtures come first: near-identical SSIDs that must stay
two connections, one SSID that must stay one connection, an SSID that tries to add a
keyfile line, and lengths counted in characters instead of bytes.

Requirements exercised: R5.1, R5.2, R5.3, R5.6, R5.7.
"""

import configparser
import contextlib
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import termios
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import config, provision
from shidashi.cli import app

NAME = "bentoo-lab"
SSID = "bentoo-lab"
#: 13 printable ASCII characters, a space inside; no word a message would use.
PSK = "Zq7 mVx2 r4Lk"
#: Where provision puts the connection, under the worker's identity (design.md).
WIFI_FILE = Path("identity/wifi/shidashi-wifi.nmconnection")
MEDIUM_WIFI_DIR = "/shidashi/identity/wifi"
MEDIUM_WIFI = f"{MEDIUM_WIFI_DIR}/shidashi-wifi.nmconnection"

needs_tools = pytest.mark.skipif(
    shutil.which("xorriso") is None or shutil.which("ssh-keygen") is None,
    reason="needs xorriso and ssh-keygen",
)

cli = CliRunner()


# --- helpers ----------------------------------------------------------------------------


def _keyfile(data: bytes) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=True, interpolation=None, delimiters=("=",))
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(data.decode("utf-8"))
    return parser


def _uuid(data: bytes) -> uuid.UUID:
    found = re.findall(rb"^uuid=(.+)$", data, flags=re.MULTILINE)
    assert len(found) == 1, data
    return uuid.UUID(found[0].decode().strip())


def _messages(err: BaseException) -> str:
    """The error's text and every cause and context chained to it."""
    seen: list[str] = []
    current: BaseException | None = err
    while current is not None and len(seen) < 10:
        seen.append(f"{current!s} {current!r}")
        current = current.__cause__ or current.__context__
    return "\n".join(seen)


def _spellings(secret: str) -> list[str]:
    """The ways a message could carry ``secret``: as is, or escaped by ``repr``."""
    return sorted({secret, secret.encode("unicode_escape").decode("ascii"), repr(secret)[1:-1]})


def _pvd_image(path: Path, label: bytes = b"BENTOO_WORKER") -> Path:
    """An ISO 9660 volume descriptor only: enough for the label check, no tool needed."""
    pvd = bytearray(2048)
    pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
    pvd[8:40] = b" " * 32
    pvd[40:72] = label.ljust(32, b" ")
    end = bytearray(2048)
    end[0], end[1:6], end[6] = 255, b"CD001", 1
    path.write_bytes(bytes(16 * 2048) + bytes(pvd) + bytes(end))
    return path


def _snapshot(root: Path) -> dict[str, tuple[str, int, bytes]]:
    """Every path under ``root``: its kind, mode and bytes."""
    out: dict[str, tuple[str, int, bytes]] = {}
    if not root.exists():
        return out
    for path in sorted([root, *root.rglob("*")]):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = ("link", 0, os.readlink(path).encode())
        elif path.is_dir():
            out[rel] = ("dir", stat.S_IMODE(path.stat().st_mode), b"")
        else:
            out[rel] = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    return out


def _holding(roots: list[Path], secret: bytes) -> list[Path]:
    """Every file under ``roots`` whose bytes contain ``secret``."""
    return sorted(
        path
        for root in roots
        if root.exists()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and secret in path.read_bytes()
    )


def _listing(iso: Path) -> list[str]:
    done = subprocess.run(
        ["xorriso", "-no_rc", "-indev", str(iso), "-find", "/"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [p.strip("'") for p in done.stdout.split()]


def _owners(iso: Path, inside: str = "/shidashi") -> dict[str, tuple[str, str, str]]:
    """Each path under ``inside`` on ``iso``: its permission string, uid and gid."""
    done = subprocess.run(
        ["xorriso", "-no_rc", "-indev", str(iso), "-find", inside, "-exec", "lsdl", "--"],
        check=True,
        capture_output=True,
        text=True,
    )
    out: dict[str, tuple[str, str, str]] = {}
    for line in done.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 9 and line.rstrip().endswith("'"):
            out[line[line.index("'") :].strip().strip("'")] = (fields[0], fields[2], fields[3])
    return out


def _extract(iso: Path, inside: str, dest: Path) -> Path:
    subprocess.run(
        ["xorriso", "-no_rc", "-osirrox", "on", "-indev", str(iso), "-extract", inside, str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


def _assert_root_only(owners: dict[str, tuple[str, str, str]], path: str) -> None:
    assert path in owners, sorted(owners)
    perms, uid, gid = owners[path]
    assert (uid, gid) == ("0", "0"), (path, perms, uid, gid)
    assert perms[4:] == "------", (path, perms)  # no group, no other bits


# --- the source ISO and the host ------------------------------------------------------


@pytest.fixture(scope="module")
def worker_iso(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A generic worker ISO, built with xorriso (El Torito, like GRUB's BIOS boot)."""
    if shutil.which("xorriso") is None:
        pytest.skip("needs xorriso")
    base = tmp_path_factory.mktemp("wifi-iso")
    tree = base / "tree"
    (tree / "LiveOS").mkdir(parents=True)
    (tree / "LiveOS" / "squashfs.img").write_bytes(b"squash" * 512)
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "boot" / "grub" / "eltorito.img").write_bytes(bytes(2048))
    iso = base / "worker.iso"
    subprocess.run(
        ["xorriso", "-no_rc", "-as", "mkisofs", "-quiet", "-R", "-V", "BENTOO_WORKER"]
        + ["-b", "boot/grub/eltorito.img", "-no-emul-boot", "-boot-load-size", "4"]
        + ["-boot-info-table", "--protective-msdos-label", "-o", str(iso), str(tree)],
        check=True,
        capture_output=True,
    )
    return iso


@dataclass
class _Host:
    workers_dir: Path
    iso: Path


@pytest.fixture
def host(tmp_path: Path, worker_iso: Path) -> _Host:
    """The host's workers directory with its real worker key, and the worker ISO."""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("needs ssh-keygen")
    wd = tmp_path / "xdg" / "shidashi" / "worker"
    wd.mkdir(parents=True, mode=0o700)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "shidashi-worker-key"]
        + ["-f", str(wd / "id_ed25519")],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    iso = dist / "bentoo-2026.10.11-worker-systemd-v3.iso"
    shutil.copyfile(worker_iso, iso)
    return _Host(wd, iso)


@pytest.fixture
def bare(tmp_path: Path) -> _Host:
    """A workers directory with a host key file and a label-only worker ISO: enough to
    reach the refusals, and nothing a tool could use."""
    wd = tmp_path / "xdg" / "shidashi" / "worker"
    wd.mkdir(parents=True, mode=0o700)
    (wd / "id_ed25519.pub").write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl x\n"
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    return _Host(wd, _pvd_image(dist / "worker.iso"))


class _Sentinel:
    """Records every command and fails it: a provisioning that reached a tool had
    accepted its Wi-Fi arguments."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, *_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 1, "", "sentinel: stopped at the first tool")


# ===================================================================================
# the keyfile
# ===================================================================================


def test_wifi_connection_renders_a_networkmanager_wpa_psk_keyfile() -> None:
    data = provision.wifi_connection(SSID, PSK)

    assert isinstance(data, bytes)
    keyfile = _keyfile(data)
    assert keyfile["connection"]["type"] == "wifi"
    assert keyfile["connection"]["id"] == SSID
    assert keyfile.getboolean("connection", "autoconnect") is True
    assert keyfile["wifi"]["mode"] == "infrastructure"
    assert keyfile["wifi"]["ssid"] == SSID
    assert keyfile["wifi-security"]["key-mgmt"] == "wpa-psk"
    assert keyfile["wifi-security"]["psk"] == PSK
    assert keyfile["ipv4"]["method"] == "auto"
    assert keyfile["ipv6"]["method"] == "auto"


# --- the uuid: one connection per SSID ------------------------------------------------


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("Bentoo-Lab", "bentoo-lab"),  # case: two networks
        ("lab", "lab "),  # a trailing space: two networks
        ("café", "café"),  # NFC and NFD: different bytes on the air
        ("a" * 31 + "1", "a" * 31 + "2"),  # differ only in the 32nd byte
    ],
)
def test_near_identical_ssids_get_different_connection_uuids(one: str, other: str) -> None:
    """Hostile, wrongly collapsed: a uuid derived from a normalized or truncated SSID
    would make two networks one connection, the second replacing the first."""
    assert _uuid(provision.wifi_connection(one, PSK)) != _uuid(
        provision.wifi_connection(other, PSK)
    )


def test_one_ssid_keeps_its_connection_uuid_whatever_the_passphrase() -> None:
    """Hostile, wrongly split: a re-provision after a passphrase change must replace the
    connection, not add a second one (NetworkManager keys a connection by its uuid)."""
    before = provision.wifi_connection(SSID, PSK)
    after = provision.wifi_connection(SSID, "an0ther passphrase")

    assert _uuid(before) == _uuid(after)
    assert provision.wifi_connection(SSID, PSK) == before  # pure: same input, same bytes


def test_the_connection_uuid_is_a_name_based_uuid5() -> None:
    assert _uuid(provision.wifi_connection(SSID, PSK)).version == 5


# --- a derived value: the rendered lines ------------------------------------------------


@pytest.mark.parametrize(
    "ssid",
    [
        "lab\npsk=attacker12",
        "lab\n[ipv4]\nmethod=disabled",
        "x\r\nkey-mgmt=none",
    ],
)
def test_an_ssid_cannot_add_or_override_a_keyfile_line(ssid: str) -> None:
    """An SSID is any 1-32 bytes; rendered into a line-based keyfile it must not become
    a line of its own. Refusing it is as good as escaping it."""
    try:
        data = provision.wifi_connection(ssid, PSK)
    except provision.ProvisionError as err:
        for spelling in _spellings(PSK):
            assert spelling not in _messages(err)
        return
    lines = data.decode("utf-8").splitlines()
    assert [ln for ln in lines if ln.startswith("psk=")] == [f"psk={PSK}"]
    assert sum(ln.startswith("ssid=") for ln in lines) == 1
    assert sum(ln.startswith("key-mgmt=") for ln in lines) == 1
    headers = [ln.strip() for ln in lines if ln.strip().startswith("[")]
    assert len(headers) == len(set(headers)), headers
    assert "method=disabled" not in lines
    assert "key-mgmt=none" not in [ln.strip() for ln in lines]


# --- NetworkManager's own reading ---------------------------------------------------------


def _nmcli(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["nmcli", "--offline", *args],
        input=stdin,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
        timeout=30,
    )


@pytest.fixture(scope="module")
def nm_offline() -> None:
    """NetworkManager's keyfile reader, without a daemon (``nmcli --offline``, 1.46+)."""
    if shutil.which("nmcli") is None:
        pytest.skip("needs nmcli")
    probe = _nmcli(["connection", "add", "type", "wifi", "con-name", "p", "ssid", "p"])
    if probe.returncode != 0:
        pytest.skip(f"nmcli has no --offline mode: {probe.stderr.decode(errors='replace')}")


def _line(keyfile: bytes, section: str, key: str) -> bytes | None:
    """The raw ``key=`` line of ``[section]`` as NetworkManager wrote it."""
    current = None
    for line in keyfile.splitlines():
        if line.startswith(b"[") and line.rstrip().endswith(b"]"):
            current = line.strip()[1:-1].decode()
        elif current == section and line.split(b"=", 1)[0] == key.encode():
            return line
    return None


@pytest.mark.usefixtures("nm_offline")
@pytest.mark.parametrize(
    ("ssid", "psk"),
    [
        (" leading", "  spaced passphrase  "),
        ("trailing ", "semi;colon#hash"),
        ("semi;colon", "back\\slash=equals"),
        ("back\\slash", "#starts-with-hash"),
        ("café", "0123456789abcdefABCDEF0123456789abcdef0123456789abcdef0123456789"),
        ("é" * 16, "x" * 63),
        ("tab\there", "12345678"),
        (SSID, PSK),
    ],
)
def test_networkmanager_reads_the_ssid_and_passphrase_it_was_given(ssid: str, psk: str) -> None:
    """Fidelity: NetworkManager reads the rendered keyfile (``nmcli --offline connection
    modify`` takes a keyfile on stdin) and writes back the SSID and passphrase exactly as
    it writes the same values given on its own command line -- leading and trailing
    spaces, ``;``, ``#``, ``\\``, ``=``, non-ASCII and control bytes included."""
    ours = _nmcli(
        ["connection", "modify", "connection.autoconnect-priority", "0"],
        stdin=provision.wifi_connection(ssid, psk),
    )
    assert ours.returncode == 0, ours.stderr.decode(errors="replace")
    reference = _nmcli(
        ["connection", "add", "type", "wifi", "con-name", "reference", "ifname", "*"]
        + ["ssid", ssid, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
    )
    assert reference.returncode == 0, reference.stderr.decode(errors="replace")

    assert _line(ours.stdout, "connection", "type") == b"type=wifi"
    assert _line(ours.stdout, "connection", "autoconnect") in (None, b"autoconnect=true")
    assert _line(ours.stdout, "wifi-security", "key-mgmt") == b"key-mgmt=wpa-psk"
    assert _line(ours.stdout, "ipv4", "method") == b"method=auto"
    assert _line(ours.stdout, "ipv6", "method") == b"method=auto"
    assert _line(ours.stdout, "wifi", "ssid") == _line(reference.stdout, "wifi", "ssid")
    assert _line(ours.stdout, "wifi-security", "psk") == _line(
        reference.stdout, "wifi-security", "psk"
    )


# ===================================================================================
# R5.2: what is refused, before anything is written
# ===================================================================================

REFUSED = [
    # hostile: lengths that only pass when counted in characters, not bytes
    pytest.param("é" * 17, PSK, "ssid", id="ssid-17-chars-34-bytes"),
    pytest.param("\U0001f6f0" * 8 + "x", PSK, "ssid", id="ssid-9-chars-33-bytes"),
    pytest.param("x" * 33, PSK, "ssid", id="ssid-33-bytes"),
    pytest.param("", PSK, "ssid", id="ssid-empty"),
    # hostile: 64 characters that are not 64 hex digits, and one hex digit too many
    pytest.param(SSID, "0" * 63 + "g", "passphrase", id="psk-64-one-non-hex"),
    pytest.param(SSID, "Zq7#mVx2" + "a" * 56, "passphrase", id="psk-64-printable"),
    pytest.param(SSID, "0123456789abcdef" * 4 + "0", "passphrase", id="psk-65-hex"),
    pytest.param(SSID, "Zq7#mVx", "passphrase", id="psk-7"),
    pytest.param(SSID, "", "passphrase", id="psk-empty"),
    pytest.param(SSID, "Zq7\tmVx2r4", "passphrase", id="psk-tab"),
    pytest.param(SSID, "Zq7\nmVx2r4", "passphrase", id="psk-newline"),
    pytest.param(SSID, "Zq7\x7fmVx2r4", "passphrase", id="psk-del"),
    pytest.param(SSID, "Zq7ömVx2r4", "passphrase", id="psk-non-ascii"),
]

ACCEPTED = [
    # the converse halves: the byte limits reached by multibyte SSIDs
    pytest.param("é" * 16, PSK, id="ssid-16-chars-32-bytes"),
    pytest.param("\U0001f6f0" * 8, PSK, id="ssid-8-chars-32-bytes"),
    pytest.param("x" * 32, PSK, id="ssid-32-bytes"),
    pytest.param("a", PSK, id="ssid-1-byte"),
    pytest.param(SSID, "0" * 63 + "f", id="psk-64-hex-lower"),
    pytest.param(SSID, "0123456789ABCDEF" * 4, id="psk-64-hex-upper"),
    pytest.param(SSID, "Zq7#mVx2" + "a" * 55, id="psk-63-printable"),
    pytest.param(SSID, "Zq7#mVx2", id="psk-8"),
    pytest.param(SSID, " ~ edges ~ ", id="psk-space-and-tilde"),
]


@pytest.mark.parametrize(("ssid", "psk", "which"), REFUSED)
def test_an_invalid_ssid_or_passphrase_is_refused_writing_nothing_and_running_nothing(
    tmp_path: Path, bare: _Host, ssid: str, psk: str, which: str
) -> None:
    before = _snapshot(tmp_path)
    runner = _Sentinel()

    with pytest.raises(provision.ProvisionError) as caught:
        provision.provision(
            NAME,
            bare.iso,
            workers_dir=bare.workers_dir,
            replace=False,
            runner=runner,
            wifi=(ssid, psk),
        )

    assert runner.calls == [], "refused only after a tool ran"
    assert _snapshot(tmp_path) == before
    message = _messages(caught.value)
    assert which in str(caught.value).lower(), str(caught.value)
    if len(psk) >= 4:
        for spelling in _spellings(psk):
            assert spelling not in message, message


@pytest.mark.parametrize(("ssid", "psk"), ACCEPTED)
def test_a_valid_ssid_and_passphrase_pass_the_refusals(
    tmp_path: Path, bare: _Host, ssid: str, psk: str
) -> None:
    """Accepted means the provisioning went on to its first tool (here a sentinel
    that stops it), and the keyfile renders."""
    runner = _Sentinel()

    with pytest.raises(provision.ProvisionError) as caught:
        provision.provision(
            NAME,
            bare.iso,
            workers_dir=bare.workers_dir,
            replace=False,
            runner=runner,
            wifi=(ssid, psk),
        )

    assert runner.calls, f"refused: {caught.value}"
    assert "sentinel" in str(caught.value)
    assert _uuid(provision.wifi_connection(ssid, psk)).version == 5


# ===================================================================================
# R5.1, R5.6: the connection on the identity and on the copy (real tools)
# ===================================================================================


@needs_tools
def test_provision_with_wifi_puts_the_connection_on_the_copy_root_only(
    tmp_path: Path, host: _Host
) -> None:
    result = provision.provision(
        NAME,
        host.iso,
        workers_dir=host.workers_dir,
        replace=False,
        runner=subprocess.run,
        wifi=(SSID, PSK),
    )

    expected = provision.wifi_connection(SSID, PSK)
    kept = host.workers_dir / NAME / WIFI_FILE
    assert kept.read_bytes() == expected
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert stat.S_IMODE(kept.parent.stat().st_mode) == 0o700

    copy = Path(result.iso_path)
    owners = _owners(copy)
    _assert_root_only(owners, MEDIUM_WIFI_DIR)
    _assert_root_only(owners, MEDIUM_WIFI)
    assert _extract(copy, MEDIUM_WIFI, tmp_path / "read-back").read_bytes() == expected


@needs_tools
def test_a_later_provision_without_wifi_carries_no_connection(host: _Host) -> None:
    """Hostile: a connection written for one worker must not reach the next one
    provisioned without ``--wifi``, nor survive a ``--replace`` without it (the worker
    moved to a cable). Neither on the identity nor on the copy."""
    provision.provision(
        NAME,
        host.iso,
        workers_dir=host.workers_dir,
        replace=False,
        runner=subprocess.run,
        wifi=(SSID, PSK),
    )
    assert (host.workers_dir / NAME / WIFI_FILE).is_file()

    other = provision.provision(
        "spare", host.iso, workers_dir=host.workers_dir, replace=False, runner=subprocess.run
    )
    replaced = provision.provision(
        NAME, host.iso, workers_dir=host.workers_dir, replace=True, runner=subprocess.run
    )

    for name, done in (("spare", other), (NAME, replaced)):
        assert not (host.workers_dir / name / "identity" / "wifi").exists(), name
        listing = _listing(Path(done.iso_path))
        assert "/shidashi/identity/pairing.json" in listing
        assert not [p for p in listing if p.startswith(MEDIUM_WIFI_DIR)], (name, listing)
    assert _holding([host.workers_dir], PSK.encode()) == []


# ===================================================================================
# the command: stdin, the terminal, and where the passphrase goes
# ===================================================================================


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "200")
    (tmp_path / "home").mkdir()
    return tmp_path / "xdg"


def _out(result: Any) -> str:
    out: str = result.stdout + (getattr(result, "stderr", "") or "")
    return out


def _invoke(*args: str, stdin: str) -> Any:
    return cli.invoke(app, ["worker", "provision", *args], input=stdin)


def _assert_kept_only_on_the_medium(tmp_path: Path, secret: str) -> None:
    """R5.7: the passphrase is in the identity's connection and the copy, nowhere else
    -- not in workers.json, known_hosts, the audit trail or any other file."""
    wd = config.workers_dir()
    allowed = {wd / NAME / WIFI_FILE, wd / NAME / f"{NAME}.iso"}
    holding = _holding([tmp_path / "xdg", tmp_path / "runs", tmp_path / "home"], secret.encode())
    assert set(holding) <= allowed, holding
    for name in ("workers.json", "known_hosts"):
        assert secret.encode() not in (wd / name).read_bytes(), name


@needs_tools
def test_the_command_reads_the_passphrase_from_stdin_and_never_prints_it(
    tmp_path: Path, xdg: Path, worker_iso: Path
) -> None:
    result = _invoke(NAME, "--iso", str(worker_iso), "--wifi", SSID, stdin=PSK + "\n")

    out = _out(result)
    assert result.exit_code == 0, out
    assert PSK not in out
    wd = config.workers_dir()
    assert (wd / NAME / WIFI_FILE).read_bytes() == provision.wifi_connection(SSID, PSK)
    owners = _owners(wd / NAME / f"{NAME}.iso")
    _assert_root_only(owners, MEDIUM_WIFI)
    _assert_kept_only_on_the_medium(tmp_path, PSK)


@needs_tools
def test_the_stdin_line_keeps_the_spaces_of_the_passphrase(
    tmp_path: Path, xdg: Path, worker_iso: Path
) -> None:
    """Hostile: spaces are printable ASCII, part of the passphrase -- only the line's
    end is not. Stripping them would join a different network key."""
    spaced = "  spaced passphrase  "
    result = _invoke(NAME, "--iso", str(worker_iso), "--wifi", SSID, stdin=spaced + "\n")

    assert result.exit_code == 0, _out(result)
    kept = (config.workers_dir() / NAME / WIFI_FILE).read_bytes()
    assert kept == provision.wifi_connection(SSID, spaced)


@pytest.mark.parametrize(
    ("ssid", "stdin", "secret"),
    [
        pytest.param("x" * 33, PSK + "\n", PSK, id="ssid-33-bytes"),
        pytest.param(SSID, "Zq7#mVx\n", "Zq7#mVx", id="psk-7"),
        pytest.param(SSID, "\n", "", id="empty-line"),
        pytest.param(SSID, "", "", id="no-line"),
    ],
)
def test_the_command_refuses_an_invalid_wifi_writing_nothing(
    tmp_path: Path, xdg: Path, ssid: str, stdin: str, secret: str
) -> None:
    iso = _pvd_image(tmp_path / "worker.iso")
    result = _invoke(NAME, "--iso", str(iso), "--wifi", ssid, stdin=stdin)

    out = _out(result)
    assert result.exit_code == 1, out
    assert "error:" in out
    assert "Traceback" not in out
    if secret:
        assert secret not in out
    wd = config.workers_dir()
    assert not (wd / NAME).exists()
    assert not (wd / "id_ed25519").exists()
    assert not (wd / "workers.json").exists()
    assert sorted(tmp_path.rglob("*.iso")) == [iso]


# --- R5.3: a terminal -----------------------------------------------------------------

_EOF = "\x04"


@dataclass
class _Tty:
    exit_code: int
    output: bytes
    unanswered: list[str]


def _echo_on(fd: int) -> bool:
    return bool(termios.tcgetattr(fd)[3] & termios.ECHO)


def _run_on_a_tty(argv: list[str], env: dict[str, str], answers: list[str]) -> _Tty:
    """Run ``argv`` on a fresh pseudo-terminal. Each answer is typed only once the
    program has written a prompt AND turned the terminal's echo off -- a hidden read.
    An answer never typed is returned in ``unanswered``."""
    pid, fd = pty.fork()
    if pid == 0:  # the child: the terminal is its stdin, stdout, stderr and /dev/tty
        try:
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    pending = list(answers)
    output = bytearray()
    mark = 0
    deadline = time.monotonic() + 120
    status: int | None = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                with contextlib.suppress(OSError):  # EIO: the child closed the terminal
                    output += os.read(fd, 4096)
            done, raw = os.waitpid(pid, os.WNOHANG)
            if done:
                status = os.waitstatus_to_exitcode(raw)
                break
            if pending and bytes(output[mark:]).strip() and not _echo_on(fd):
                answer = pending.pop(0)
                os.write(fd, answer.encode() if answer == _EOF else answer.encode() + b"\n")
                mark = len(output)
        while True:  # what the child wrote before it exited
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                break
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
    if status is None:
        raise AssertionError(
            "the command did not finish: it never asked for the passphrase without echo, "
            f"or never stopped; it wrote {bytes(output)!r}"
        )
    return _Tty(status, bytes(output), pending)


def _tty_env(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "SHIDASHI_RUNS": str(tmp_path / "runs"),
        "LC_ALL": "C.UTF-8",
        "COLUMNS": "200",
        "TERM": "dumb",
    }


def _cli_argv(*args: str) -> list[str]:
    return [sys.executable, "-c", "from shidashi.cli import app; app(prog_name='shidashi')", *args]


@needs_tools
def test_on_a_terminal_the_passphrase_is_asked_twice_without_echo(
    tmp_path: Path, xdg: Path, worker_iso: Path
) -> None:
    tty = _run_on_a_tty(
        _cli_argv("worker", "provision", NAME, "--iso", str(worker_iso), "--wifi", SSID),
        _tty_env(tmp_path),
        [PSK, PSK],
    )

    assert tty.exit_code == 0, tty.output
    assert tty.unanswered == [], f"asked once, not twice: {tty.output!r}"
    assert PSK.encode() not in tty.output  # never echoed, never printed
    kept = (config.workers_dir() / NAME / WIFI_FILE).read_bytes()
    assert kept == provision.wifi_connection(SSID, PSK)
    _assert_kept_only_on_the_medium(tmp_path, PSK)


@needs_tools
def test_on_a_terminal_a_mismatched_confirmation_provisions_nothing(
    tmp_path: Path, xdg: Path, worker_iso: Path
) -> None:
    """Neither value is used: the command refuses (or asks again, and gets end of
    input), exits non-zero, and writes nothing -- with a real ISO, so a command that
    went on with either value would succeed and be caught."""
    second = "Zq7 mVx2 r4LK"  # one letter's case away from PSK
    tty = _run_on_a_tty(
        _cli_argv("worker", "provision", NAME, "--iso", str(worker_iso), "--wifi", SSID),
        _tty_env(tmp_path),
        [PSK, second, _EOF, _EOF],
    )

    assert tty.exit_code != 0, tty.output
    assert len(tty.unanswered) < 3, f"never asked for the passphrase: {tty.output!r}"
    assert PSK.encode() not in tty.output
    assert second.encode() not in tty.output
    wd = config.workers_dir()
    assert not (wd / NAME).exists()
    assert sorted((tmp_path / "xdg").rglob("*.iso")) == []
    registry = wd / "workers.json"
    assert not registry.exists() or NAME not in registry.read_text()
