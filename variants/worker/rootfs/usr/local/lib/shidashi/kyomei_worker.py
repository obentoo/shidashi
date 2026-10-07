"""The worker's side of a kyomei pairing (variants/worker), stdlib only.

``--listen`` opens a pairing window: the worker announces itself over mDNS
(``_shidashi-kyomei._tcp`` through systemd-resolved), shows a one-time code on its
console, and serves ``POST /kyomei/v1`` on port 8765. A host that proves the code (or,
with ``shidashi.trust=`` on the kernel command line, comes from the trusted address
with the trusted key) gets its key installed for root, the name it offered, and sshd
-- all BEFORE the answer leaves, so a host holding its 200 can log in at once. One
pairing closes the window: the announcement is withdrawn and the console cleared.

``--restore`` runs at boot when ``/mnt/work/.shidashi/pairing.json`` exists: it puts
back the host keys the host pinned, the granted key and the name, then starts sshd --
a reboot comes back paired, with nobody at the console.

:class:`WorkerSession` decides every request without touching a socket or the
system; :func:`listen`, :func:`persist` and :func:`restore` take the root path and the
command runner as parameters, so all of it is tested without root. The code and its
key live in the session only -- never in a file but the tmpfs console block, never in
a log line.
"""

import datetime as dt
import ipaddress
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import kyomei_protocol as P

Runner = Callable[..., subprocess.CompletedProcess[Any]]
Headers = dict[str, str]

PATH = "/kyomei/v1"
SSHD = "/usr/sbin/sshd"
RESOLVED = "systemd-resolved.service"

_RAM_RECORD = Path("run/shidashi/pairing.json")
_DISK_DIR = Path("mnt/work/.shidashi")
_DNSSD = Path("run/systemd/dnssd/shidashi-kyomei.dnssd")
_ISSUE = Path("run/issue.d/50-shidashi-kyomei.issue")
_AUTHORIZED_KEYS = Path("root/.ssh/authorized_keys")
_HOST_KEY_PUB = Path("etc/ssh/ssh_host_ed25519_key.pub")
_HOST_KEY_GLOBS = ("ssh_host_*_key", "ssh_host_*_key.pub")
#: Each install command must finish well inside the host's 10 s answer timeout.
_COMMAND_TIMEOUT = 8


class PersistError(Exception):
    """The pairing could not be written to the work disk."""


class InstallError(Exception):
    """A granted hello could not be installed (key, name, sshd, record, disk)."""


# ===================================================================================
# the session -- every decision, no I/O
# ===================================================================================


@dataclass(frozen=True)
class Granted:
    """The outcome of a pairing: the key to install, the name offered, by whom."""

    key: str
    name: str | None
    trusted: bool
    peer: str


class WorkerSession:
    """One pairing window: the current code, the failures, the lock, the result.

    ``handle(body, peer)`` answers ``(status, body, headers)``: 200 with the welcome,
    400 for a malformed hello, 403 for one that does not verify (empty body), 423
    while locked. ``max_failures`` refusals in a row discard the code and lock for
    ``lockout`` seconds; :meth:`rotate` (the listener calls it once the lock passed)
    draws a new one. One pairing closes the session.
    """

    def __init__(
        self,
        host_key: str,
        hostname: str,
        addresses: tuple[str, ...],
        cpu_flags: tuple[str, ...],
        image: str,
        trust: P.Trust | None,
        *,
        code_factory: Callable[[], str] = P.new_code,
        clock: Callable[[], float] = time.monotonic,
        lockout: float = 30.0,
        max_failures: int = 3,
    ) -> None:
        self.host_key = host_key
        self.hostname = hostname
        self.addresses = tuple(addresses)
        self.cpu_flags = tuple(cpu_flags)
        self.image = image
        self.trust = trust
        self._code_factory = code_factory
        self._clock = clock
        self._lockout = lockout
        self._max_failures = max_failures
        self._code: str | None = None
        self._key: bytes | None = None
        self.seen_nonces: set[str] = set()
        self.failures = 0
        self.locked_until: float | None = None
        self.closed = False
        self.result: Granted | None = None
        self.rotate()

    def __repr__(self) -> str:
        return (
            f"WorkerSession(hostname={self.hostname!r}, failures={self.failures}, "
            f"locked={self.locked_until is not None}, closed={self.closed})"
        )

    @property
    def code(self) -> str | None:
        """The code to show on the console; ``None`` while locked."""
        return self._code

    def rotate(self) -> None:
        """A new code: the lock is over and the failures are forgotten."""
        self._code = self._code_factory()
        self._key = P.derive_key(self._code)
        self.failures = 0
        self.locked_until = None

    def lock_passed(self) -> bool:
        """Whether the session is locked and the lockout has run out."""
        return self.locked_until is not None and self._clock() >= self.locked_until

    def reopen(self) -> None:
        """Forget a grant that could not be installed: the window stays open."""
        self.closed = False
        self.result = None

    def handle(self, body: bytes, peer: str) -> tuple[int, bytes, Headers]:
        """Answer one request body from ``peer`` (its IPv4 address)."""
        if self.closed:
            return 403, b"", {}
        if self.locked_until is not None:
            wait = max(1, math.ceil(self.locked_until - self._clock()))
            return 423, b"", {"Retry-After": str(wait)}
        try:
            hello, tag = P.parse_hello(body)
        except P.ProtocolError:
            return self._refuse(400)
        if hello.mode == "code":
            key = self._key
            if key is None or tag is None or not P.verify(key, "hello", _payload(hello), tag):
                return self._refuse(403)
        elif not self._trusted(hello, peer):
            return self._refuse(403)
        if hello.nonce in self.seen_nonces:
            return self._refuse(403)
        self.seen_nonces.add(hello.nonce)
        welcome: dict[str, Any] = {
            "v": 1,
            "nonce": hello.nonce,
            "worker_nonce": P.new_nonce(),
            "host_key": self.host_key,
            "hostname": hello.name or self.hostname,
            "addresses": list(self.addresses),
            "cpu_flags": list(self.cpu_flags),
            "image": self.image,
        }
        tag_out = (
            P.mac(self._key, "welcome", welcome) if hello.mode == "code" and self._key else None
        )
        self.result = Granted(hello.authorized_key, hello.name, hello.mode == "trusted", peer)
        self.closed = True
        answer = json.dumps({"payload": welcome, "mac": tag_out}).encode()
        return 200, answer, {"Content-Type": "application/json"}

    def _trusted(self, hello: P.Hello, peer: str) -> bool:
        trust = self.trust
        if trust is None or peer != trust.address:
            return False
        try:
            presented = P.fingerprint(hello.authorized_key)
        except ValueError, IndexError:
            return False
        expected = trust.fingerprint.replace("-", "+").replace("_", "/")  # base64url too
        return presented == expected

    def _refuse(self, status: int) -> tuple[int, bytes, Headers]:
        self.failures += 1
        if self.failures >= self._max_failures:
            self._code = None
            self._key = None
            self.seen_nonces.clear()
            self.locked_until = self._clock() + self._lockout
        return status, b"", {}


def _payload(hello: P.Hello) -> dict[str, Any]:
    """The hello's payload as the host MACed it (the parser kept every value)."""
    return {
        "v": 1,
        "mode": hello.mode,
        "nonce": hello.nonce,
        "authorized_key": hello.authorized_key,
        "name": hello.name,
    }


# ===================================================================================
# persist / restore -- the pairing on the work disk
# ===================================================================================


def persist(root: Path, *, require_mount: bool = False, dest: Path | None = None) -> None:
    """Copy the RAM pairing to ``/mnt/work/.shidashi`` when the work disk is mounted.

    Written: ``pairing.json`` (the record), ``authorized_keys`` holding ONLY the
    granted key -- never the rest of root's file, which may hold a VM session
    credential -- and the sshd host keys, so the pin survives a reboot. Without the
    mount the pairing stays in RAM (said, not an error) unless ``require_mount``.
    """
    record_path = root / _RAM_RECORD
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        fingerprint = record["granting_key_fingerprint"]
    except FileNotFoundError as err:
        raise PersistError(f"not paired: {record_path} is absent") from err
    except (ValueError, KeyError, TypeError) as err:
        raise PersistError(f"{record_path} is not a pairing record") from err
    work = root / "mnt" / "work"
    if not os.path.ismount(work):
        if require_mount:
            raise PersistError(f"{work} is not mounted: the pairing cannot be kept")
        print(
            "kyomei: /mnt/work is not mounted -- the pairing stays in RAM only and a "
            "reboot forgets it (shidashi worker disk-init makes a work disk)",
            file=sys.stderr,
        )
        return
    target = dest if dest is not None else root / _DISK_DIR
    granted = _key_line(root / _AUTHORIZED_KEYS, fingerprint)
    if granted is None:
        raise PersistError(f"{root / _AUTHORIZED_KEYS} does not hold the granted key")
    try:
        _mkdir(target, 0o700)
        _write(target / "authorized_keys", (granted + "\n").encode(), 0o600)
        _copy_host_keys(root / "etc" / "ssh", target / "ssh", dir_mode=0o700)
        # last: pairing.json is what the restore unit's condition reads as "complete"
        _write(target / "pairing.json", json.dumps(record, indent=1).encode() + b"\n", 0o600)
    except OSError as err:
        raise PersistError(f"cannot write {target}: {err.strerror or err}") from err


def restore(root: Path = Path("/"), runner: Runner = subprocess.run) -> int:
    """Bring a persisted pairing back at boot; 0 when every step worked.

    Refuses (1, nothing changed) a missing or malformed record and a persisted key
    that is not the one the record names. Otherwise: the pinned host keys go back
    BEFORE sshd starts (``ssh-keygen -A`` is never run here), the granted key is
    added beside whatever root holds, the name is set (a failure is reported and the
    restore goes on) and sshd is ALWAYS started.
    """
    source = root / _DISK_DIR
    record_path = source / "pairing.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("v") != 1:
            raise ValueError("unknown record version")
        fingerprint = record["granting_key_fingerprint"]
        name = record.get("name")
        if not isinstance(fingerprint, str) or (
            name is not None and not (isinstance(name, str) and re.fullmatch(P.HOSTNAME_RE, name))
        ):
            raise ValueError("unexpected field values")
    except (OSError, ValueError, KeyError) as err:
        print(f"kyomei: cannot restore: {record_path} is not a pairing ({err})", file=sys.stderr)
        return 1
    keys_path = source / "authorized_keys"
    granted = _key_line(keys_path, fingerprint)
    if granted is None:
        print(
            f"kyomei: cannot restore: {keys_path} does not hold the key {record_path} names",
            file=sys.stderr,
        )
        return 1

    failed = False
    steps: list[tuple[str, Callable[[], None]]] = [
        (
            "host keys",
            lambda: _copy_host_keys(source / "ssh", root / "etc" / "ssh", dir_mode=0o755),
        ),
        ("authorized_keys", lambda: _add_key(root / _AUTHORIZED_KEYS, granted)),
        ("RAM record", lambda: _write_record(root, record)),
    ]
    for what, step in steps:
        try:
            step()
        except OSError as err:
            print(f"kyomei: restoring the {what} failed: {err}", file=sys.stderr)
            failed = True
    commands = [["systemctl", "--no-block", "start", "sshd.service"]]
    if name:
        commands.insert(0, ["hostnamectl", "hostname", name])
    for argv in commands:  # each one runs whatever happened to the one before
        try:
            _must(runner, argv)
        except InstallError as err:
            print(f"kyomei: {err}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


# ===================================================================================
# listen -- the announcing pairing window
# ===================================================================================


def console_show(text: str, *, root: Path = Path("/"), runner: Runner | None = None) -> None:
    """Show ``text`` above the login prompt (a tmpfs issue block) and on tty1."""
    run = runner or subprocess.run
    _mkdir((root / _ISSUE).parent, 0o755)
    _write(root / _ISSUE, (text.rstrip("\n") + "\n").encode(), 0o644)
    run(["agetty", "--reload"], capture_output=True, text=True)
    try:
        fd = os.open(root / "dev" / "tty1", os.O_WRONLY | os.O_APPEND | os.O_NOCTTY)
        with os.fdopen(fd, "w", encoding="utf-8") as tty:
            tty.write("\n" + text.rstrip("\n") + "\n")
    except OSError as err:
        print(f"kyomei: cannot write to tty1: {err}", file=sys.stderr)


def console_clear(*, root: Path = Path("/"), runner: Runner | None = None) -> None:
    """Remove the issue block."""
    run = runner or subprocess.run
    (root / _ISSUE).unlink(missing_ok=True)
    run(["agetty", "--reload"], capture_output=True, text=True)


def listen(
    *,
    root: Path = Path("/"),
    runner: Runner = subprocess.run,
    port: int = P.PORT,
    show: Callable[..., None] = console_show,
    clear: Callable[..., None] = console_clear,
    server_factory: Callable[..., HTTPServer] = HTTPServer,
) -> int:
    """Open the pairing window and serve it until one pairing succeeds; returns 0."""
    if not (root / _HOST_KEY_PUB).exists():
        made = runner(["ssh-keygen", "-A", "-f", str(root)], capture_output=True, text=True)
        if made.returncode != 0 or not (root / _HOST_KEY_PUB).exists():
            raise OSError(f"ssh-keygen -A made no ed25519 host key: {(made.stderr or '').strip()}")
    host_key = (root / _HOST_KEY_PUB).read_text(encoding="utf-8").strip()
    notes: list[str] = []
    try:
        trust = P.parse_trust(_read(root / "proc" / "cmdline"))
    except P.ProtocolError as err:
        trust = None
        notes.append(f"{err} -- shidashi.trust ignored, the code is required")
    for note in notes:
        print(f"kyomei: {note}", file=sys.stderr)
    session = WorkerSession(
        host_key=host_key,
        hostname=_hostname(),
        addresses=_addresses(runner),
        cpu_flags=_cpu_flags(root),
        image=_build_id(root),
        trust=trust,
    )

    def block(extra: str = "") -> str:
        lines = [
            "shidashi kyomei -- pairing window open",
            f"  code:      {P.format_code(session.code) if session.code else '(locked)'}",
            f"  name:      {session.hostname}",
            f"  addresses: {' '.join(session.addresses) or '(none)'}",
            f"  host key:  {P.fingerprint(host_key)}",
        ]
        if trust is not None:
            lines.append(f"  trusted:   {trust.address} {trust.fingerprint}")
        lines += [f"  note:      {n}" for n in notes]
        if extra:
            lines.append(f"  {extra}")
        lines.append("On the host: shidashi kyomei (or --address <this address>)")
        return "\n".join(lines)

    state: dict[str, bool] = {"paired": False}

    class Handler(BaseHTTPRequestHandler):
        """``POST /kyomei/v1``; the session decides, the install precedes a 200."""

        timeout = 5  # a silent or short-bodied connection must not hold the window

        def do_POST(self) -> None:  # noqa: N802 -- http.server's naming
            if self.path != PATH:
                self._answer(404, b"", {})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            body = self.rfile.read(min(max(length, 0), P.MAX_BODY + 1))
            status, answer, headers = session.handle(body, self.client_address[0])
            if status == 200 and session.result is not None:
                try:
                    _install(root, runner, session.result, session.hostname)
                except Exception as err:  # any failure keeps the window open
                    session.reopen()
                    print(f"kyomei: pairing failed: {err}", file=sys.stderr)
                    show(block(f"the last pairing failed: {err}"), root=root)
                    self._answer(500, b"", {})
                    return
                # installed: the window closes even if the answer cannot be delivered
                state["paired"] = True
            self._answer(status, answer, headers)

        def _answer(self, status: int, body: bytes, headers: Mapping[str, str]) -> None:
            self.close_connection = True
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            print(f"kyomei: {self.client_address[0]} {code}", file=sys.stderr)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Silenced: http.server would print request lines; status is enough."""

    server = server_factory(("0.0.0.0", port), Handler)
    previous = _exit_on_signals()
    try:
        server.timeout = 0.5
        _announce(root, runner, port, session.image, trust is not None)
        show(block(), root=root)
        while not state["paired"]:
            server.handle_request()
            if session.lock_passed():
                session.rotate()
                show(block(), root=root)
    finally:
        server.server_close()
        _withdraw(root, runner)
        clear(root=root)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(f"kyomei: paired; {session.hostname} is reachable over ssh", file=sys.stderr)
    return 0


def _exit_on_signals() -> dict[int, Any]:
    """SIGTERM (``systemctl stop``) and SIGHUP raise SystemExit, so cleanup runs.

    Only the main thread may set handlers; elsewhere (tests) nothing changes.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}

    def _exit(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    return {s: signal.signal(s, _exit) for s in (signal.SIGTERM, signal.SIGHUP)}


def _install(root: Path, runner: Runner, granted: Granted, hostname: str) -> None:
    """Everything a host holding its 200 relies on: key, name, sshd, record, disk."""
    try:
        _add_key(root / _AUTHORIZED_KEYS, granted.key)
    except OSError as err:
        raise InstallError(f"cannot write authorized_keys: {err}") from err
    if granted.name:
        _must(runner, ["hostnamectl", "hostname", granted.name])
    _must(runner, [SSHD, "-t"])
    _must(runner, ["systemctl", "restart", "sshd.service"])
    record = {
        "v": 1,
        "name": granted.name or hostname,
        "granting_key_fingerprint": P.fingerprint(granted.key),
        "paired_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    try:
        _write_record(root, record)
        persist(root)
    except (OSError, PersistError) as err:
        raise InstallError(str(err)) from err


def _must(runner: Runner, argv: list[str]) -> None:
    try:
        done = runner(argv, capture_output=True, text=True, timeout=_COMMAND_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        raise InstallError(f"{' '.join(argv)} failed: {err}") from err
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        raise InstallError(f"{' '.join(argv)} failed" + (f": {detail[-1]}" if detail else ""))


def _announce(root: Path, runner: Runner, port: int, image: str, trusted: bool) -> None:
    """Publish the DNS-SD service through systemd-resolved (a tmpfs .dnssd file)."""
    text = (
        "[Service]\n"
        "Name=%H\n"
        f"Type={P.SERVICE}\n"
        f"Port={port}\n"
        f"TxtText=v=1 image={image} trusted={'yes' if trusted else 'no'}\n"
    )
    _mkdir((root / _DNSSD).parent, 0o755)
    _write(root / _DNSSD, text.encode(), 0o644)
    _reload_resolved(runner)


def _withdraw(root: Path, runner: Runner) -> None:
    (root / _DNSSD).unlink(missing_ok=True)
    _reload_resolved(runner)


def _reload_resolved(runner: Runner) -> None:
    done = runner(["systemctl", "reload", RESOLVED], capture_output=True, text=True)
    if done.returncode != 0:
        print(
            "kyomei: reloading systemd-resolved failed; discovery may be stale "
            "(shidashi kyomei --address still works)",
            file=sys.stderr,
        )


# --- what the worker is -----------------------------------------------------------


def _hostname() -> str:
    name = socket.gethostname().split(".")[0].lower()
    return name if re.fullmatch(P.HOSTNAME_RE, name) else "shidashi-worker"


def _addresses(runner: Runner) -> tuple[str, ...]:
    """Every IPv4 address but loopback, without its prefix (``ip -4 -br addr``)."""
    done = runner(["ip", "-4", "-br", "addr"], capture_output=True, text=True)
    found: list[str] = []
    for line in (done.stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0] == "lo":
            continue
        for cidr in fields[2:]:
            address = cidr.split("/")[0]
            try:
                parsed = ipaddress.IPv4Address(address)
            except ValueError:
                continue
            if not parsed.is_loopback and str(parsed) == address and address not in found:
                found.append(address)
    return tuple(found)


def _cpu_flags(root: Path) -> tuple[str, ...]:
    for line in _read(root / "proc" / "cpuinfo").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "flags":
            return tuple(value.split())
    return ()


def _build_id(root: Path) -> str:
    for line in _read(root / "etc" / "os-release").splitlines():
        key, sep, value = line.partition("=")
        if sep and key == "BUILD_ID":
            return value.strip().strip("\"'")
    return "unknown"


# --- files ------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _mkdir(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def _write(path: Path, data: bytes, mode: int) -> None:
    """Write through a temp sibling at ``mode`` and rename it into place."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)  # the rename itself survives a power cut
        finally:
            os.close(directory)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_record(root: Path, record: Mapping[str, Any]) -> None:
    _mkdir((root / _RAM_RECORD).parent, 0o700)
    _write(root / _RAM_RECORD, json.dumps(record, indent=1).encode() + b"\n", 0o600)


def _key_line(path: Path, fingerprint: str) -> str | None:
    """The line of an authorized_keys file whose key has ``fingerprint``."""
    for line in _read(path).splitlines():
        try:
            if P.fingerprint(line) == fingerprint:
                return line.strip()
        except ValueError, IndexError:
            continue  # another key type, options, a comment: not the granted key
    return None


def _add_key(path: Path, line: str) -> None:
    """Add ``line`` once (by key, whatever its comment), keeping every other line."""
    _mkdir(path.parent, 0o700)
    existing = _read(path)
    wanted = line.split()[:2]
    if not any(have.split()[:2] == wanted for have in existing.splitlines()):
        # a credential-provided file may lack its final newline (seen 2026-10-04)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        _write(path, (existing + line.strip() + "\n").encode(), 0o600)
    os.chmod(path, 0o600)


def _copy_host_keys(source: Path, dest: Path, *, dir_mode: int) -> None:
    """Copy sshd's host keys: private 0600, public 0644."""
    _mkdir(dest, dir_mode)
    keys = sorted({p for pattern in _HOST_KEY_GLOBS for p in source.glob(pattern)})
    if not keys:
        raise FileNotFoundError(2, "no host keys", str(source))
    for key in keys:
        _write(dest / key.name, key.read_bytes(), 0o644 if key.suffix == ".pub" else 0o600)


# ===================================================================================
# the command
# ===================================================================================


def main(argv: list[str] | None = None) -> int:
    """``--listen`` (the console and its unit) or ``--restore`` (the boot unit)."""
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["--listen"]:
            return listen()
        if args == ["--restore"]:
            return restore()
    except KeyboardInterrupt:
        return 130
    except OSError as err:
        print(f"kyomei: {err}", file=sys.stderr)
        return 1
    print("usage: kyomei_worker.py --listen | --restore", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
