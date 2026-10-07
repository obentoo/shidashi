"""The host's side of a kyomei pairing: find, pick, pair, pin and prove.

The worker announces itself over mDNS and shows a one-time code on its console;
:func:`choose` turns the announcements into the worker to pair, :func:`pair_with`
sends the hello (the key to grant, MACed under the code) and accepts only a welcome
that proves the worker knows the same code, and :func:`complete` pins the worker's
sshd host key under its name, records it in the registry and proves the pin with one
SSH connection. With ``shidashi.trust=`` on the worker's kernel command line
(:func:`trust_param`), the hello carries no code: the worker checks this host's
address and key instead, and the host trusts the welcome's key on first use.

The code and the key derived from it never reach a log line, an error message, a file
or a repr. The library raises; ``shidashi kyomei`` (cli) reports.
"""

import datetime as dt
import ipaddress
import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from shidashi import kyomei_protocol as P
from shidashi import remote, workers
from shidashi.mdns import Found

#: How long :func:`complete` keeps trying the first SSH connection while the worker's
#: sshd restarts. Read per call (tests shorten it).
PROOF_WINDOW = 15.0

Runner = Callable[..., subprocess.CompletedProcess[Any]]


class PairingError(Exception):
    """A pairing that did not happen (or did not finish)."""


class WorkerUnreachable(PairingError):
    """Nothing answered at the address."""

    def __init__(self, address: str, port: int, reason: str) -> None:
        self.address, self.port, self.reason = address, port, reason
        super().__init__(f"the worker at {address}:{port} did not answer: {reason}")


class PairingRefused(PairingError):
    """The worker answered and said no (400, 403) or not now (423)."""

    def __init__(self, status: int, retry_after: int | None) -> None:
        self.status, self.retry_after = status, retry_after
        if status == 423:
            wait = f"{retry_after} s" if retry_after is not None else "a while"
            text = f"the worker is locked after too many wrong codes; try again in {wait}"
        elif status == 400:
            text = "the worker refused the hello as malformed (HTTP 400)"
        else:
            text = f"the worker refused the pairing: wrong code or not trusted (HTTP {status})"
        super().__init__(text)


class PairingNotAuthenticated(PairingError):
    """The welcome does not prove the worker knows the code: nothing is pinned."""

    def __init__(self) -> None:
        super().__init__("the worker's answer is not authenticated by the code; nothing was pinned")


class PairingNotProven(PairingError):
    """The pin is recorded, but no SSH connection verified it."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"paired but not proven: {reason}")


@dataclass(frozen=True)
class Target:
    """Where to send the hello; ``name`` is the mDNS instance name, if any."""

    address: str
    port: int
    name: str | None


@dataclass(frozen=True)
class Paired:
    """An authenticated welcome, and under which name and address to record it."""

    welcome: P.Welcome
    name: str
    address: str
    trusted: bool


# --- the exchange (3.1) ------------------------------------------------------------


def pair_with(
    target: Target,
    worker_key: Path,
    *,
    code: str | None,
    name: str | None,
    opener: Callable[..., Any] | None = None,
    timeout: float = 10,
) -> Paired:
    """Send the hello to ``target``; return the pairing once the welcome checks out.

    Code mode when ``code`` is given (the welcome must carry the code's MAC and echo
    our nonce), trusted mode otherwise (the welcome must echo our nonce).
    """
    key = P.derive_key(P.normalize_code(code)) if code is not None else None
    payload: dict[str, Any] = {
        "v": 1,
        "mode": "code" if key is not None else "trusted",
        "nonce": P.new_nonce(),
        "authorized_key": _public_key(worker_key),
        "name": name,
    }
    tag = P.mac(key, "hello", payload) if key is not None else None
    request = urllib.request.Request(
        f"http://{target.address}:{target.port}/kyomei/v1",
        data=json.dumps({"payload": payload, "mac": tag}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_ = opener if opener is not None else _direct_opener().open
    try:
        with open_(request, timeout=timeout) as response:
            body = response.read(P.MAX_BODY + 1)
    except urllib.error.HTTPError as err:  # first: an HTTPError is also a URLError
        raise _refusal(err) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        reason = str(getattr(err, "reason", None) or err)
        if "timed out" in reason:
            reason += " (if the worker's console says it paired, it did: re-pair to pin it)"
        raise WorkerUnreachable(target.address, target.port, reason) from err
    try:
        welcome, welcome_tag = P.parse_welcome(body)
    except P.ProtocolError as err:
        raise PairingError(f"the worker's answer is not a welcome: {err}") from err
    if key is not None and (
        welcome_tag is None or not P.verify(key, "welcome", _payload(welcome), welcome_tag)
    ):
        raise PairingNotAuthenticated()
    if welcome.nonce != payload["nonce"]:
        raise PairingNotAuthenticated()
    return Paired(
        welcome=welcome,
        name=name or welcome.hostname,
        address=target.address,
        trusted=key is None,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A worker answers itself: a redirect is an error, never another server."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _direct_opener() -> urllib.request.OpenerDirector:
    """No proxy (http_proxy would carry the MACed hello elsewhere), no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _shown(text: str) -> str:
    """An mDNS string made safe for the terminal: no control or escape characters."""
    return "".join(c if c.isprintable() else "?" for c in text)


def _refusal(err: urllib.error.HTTPError) -> PairingError:
    if err.code in (400, 403):
        return PairingRefused(err.code, None)
    if err.code == 423:
        try:
            retry = int(err.headers.get("Retry-After", ""))
        except ValueError:
            retry = None
        return PairingRefused(423, retry)
    if err.code == 500:
        return PairingError("the worker could not install the key (HTTP 500); see its console")
    return PairingError(f"the worker answered HTTP {err.code}")


def _payload(welcome: P.Welcome) -> dict[str, Any]:
    """The welcome's payload as the worker MACed it (the parser kept every value)."""
    return {
        "v": 1,
        "nonce": welcome.nonce,
        "worker_nonce": welcome.worker_nonce,
        "host_key": welcome.host_key,
        "hostname": welcome.hostname,
        "addresses": list(welcome.addresses),
        "cpu_flags": list(welcome.cpu_flags),
        "image": welcome.image,
    }


def _public_key(worker_key: Path) -> str:
    return Path(f"{worker_key}.pub").read_text(encoding="utf-8").strip()


# --- discovery and the pick (3.2) ----------------------------------------------------


def choose(
    found: Sequence[Found],
    *,
    ask: Callable[..., Any] = typer.prompt,
    confirm: Callable[..., Any] = typer.confirm,
) -> Target:
    """The worker to pair among the announced ones, asking only what is needed."""
    if not found:
        raise PairingError(
            "no worker announced itself on this network; across networks (a VPN, another "
            "subnet) give its address with --address"
        )
    if len(found) == 1:
        (only,) = found
        if not confirm(f"Pair {_shown(only.name)} ({only.address})?"):
            raise PairingError("cancelled")
        return Target(only.address, only.port, only.name)
    for number, item in enumerate(found, start=1):
        txt = dict(item.txt)
        trusted = "  trusted" if txt.get("trusted") == "yes" else ""
        typer.echo(
            f"  {number}) {_shown(item.name)}  {item.address}:{item.port}  "
            f"image {_shown(txt.get('image', '?'))}{trusted}"
        )
    while True:
        answer = str(ask(f"Which worker (1-{len(found)})")).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            item = found[int(answer) - 1]
            return Target(item.address, item.port, item.name)


def parse_address(text: str) -> Target:
    """``A[:P]`` -> a target; ``ValueError`` for anything but a plain IPv4 and a port."""
    address, sep, port_text = text.partition(":")
    if sep and not port_text.isdigit():
        raise ValueError(f"not a port: {port_text!r}")
    port = int(port_text) if sep else P.PORT
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    if str(ipaddress.IPv4Address(address)) != address:
        raise ValueError(f"not a plain IPv4 address: {address!r}")
    return Target(address, port, None)


def default_address(dest: str = "1.1.1.1") -> str:
    """This host's source address on the route towards ``dest`` (``ip -4 route get``).

    The default route unless the worker's address is given: across a full-tunnel VPN
    or from a host on several networks, only the route towards the worker names the
    address the worker will see the hello come from.
    """
    done = subprocess.run(
        ["ip", "-4", "route", "get", dest], capture_output=True, text=True, check=False
    )
    fields = done.stdout.split() if done.returncode == 0 else []
    if "src" in fields[:-1]:
        return str(fields[fields.index("src") + 1])
    hint = (
        "give the worker's with --address"
        if dest == "1.1.1.1"
        else "check the route to the --address given"
    )
    raise PairingError(
        f"this host has no route towards {dest}: no address to put in shidashi.trust ({hint})"
    )


def trust_param(worker_key: Path, address: str) -> str:
    """The kernel parameter that lets a worker pair with this host without a code."""
    return f"shidashi.trust={address},{P.fingerprint(_public_key(worker_key))}"


# --- completing the pairing (3.3) ----------------------------------------------------


def complete(
    paired: Paired,
    registry_path: Path,
    *,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    proof_window: float | None = None,
) -> workers.WorkerEntry:
    """Pin the worker's host key, record it, then prove the pin over SSH.

    A worker whose sshd is still restarting is retried every second for the proof
    window; a changed key never is. A failed proof keeps the record and raises
    :class:`PairingNotProven`.
    """
    base = registry_path.parent
    known_hosts = base / "known_hosts"
    host_key = paired.welcome.host_key
    fingerprint = _keygen_fingerprint(host_key, runner)
    entry = workers.WorkerEntry(
        name=paired.name,
        address=paired.address,
        host_key=host_key,
        host_key_fingerprint=fingerprint,
        paired_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        cpu_flags=tuple(paired.welcome.cpu_flags),
        image=paired.welcome.image,
    )
    workers.pin(known_hosts, paired.name, host_key)
    registry = workers.load_registry(registry_path)
    registry[paired.name] = entry
    workers.save_registry(registry_path, registry)

    proof = remote.Remote(
        name=paired.name,
        address=paired.address,
        key=base / "id_ed25519",
        known_hosts=known_hosts,
        expected_fingerprint=fingerprint,
    )
    window = PROOF_WINDOW if proof_window is None else proof_window
    deadline = clock() + window
    while True:
        try:
            remote.check(proof, runner=runner)
            return entry
        except remote.HostKeyMismatch as err:
            raise PairingNotProven(str(err)) from err
        except remote.RemoteError as err:
            if isinstance(err, remote.RemoteUnreachable) and clock() < deadline:
                sleep(1.0)
                continue
            raise PairingNotProven(str(err)) from err


def _keygen_fingerprint(host_key: str, runner: Runner) -> str:
    """OpenSSH's own fingerprint of the welcome's host key (``ssh-keygen -lf -``)."""
    done = runner(["ssh-keygen", "-lf", "-"], input=host_key + "\n", capture_output=True, text=True)
    fields = (done.stdout or "").split()
    if done.returncode != 0 or len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise PairingError(
            f"ssh-keygen could not fingerprint the worker's host key: {(done.stderr or '').strip()}"
        )
    return fields[1]
