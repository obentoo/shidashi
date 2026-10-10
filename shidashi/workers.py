"""The paired workers: the registry and the name-keyed ``known_hosts`` pin.

``workers.json`` maps a worker's name to its :class:`WorkerEntry` -- the contract story
010 reads to reach a worker and to guard its ISA. ``known_hosts`` holds one line per
worker, ``<name> <host key>``, read by ssh through ``HostKeyAlias=<name>``: ssh
accepts a host whose key matches ANY line naming it, so :func:`pin` never leaves a
second key for a name.

Both files are rewritten atomically (a temp sibling and ``os.replace``): a crash or a
full disk leaves the old file or the new one, never half of either. The library
raises; the command reports.
"""

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pydantic


class RegistryError(Exception):
    """``workers.json`` exists but is not a registry."""


class WorkerEntry(pydantic.BaseModel):
    """One worker the host trusts: paired over kyomei, or provisioned into its ISO.

    A provisioned entry is recorded before the worker ever booted (R1.3): no address,
    no CPU flags, no image yet -- the first contact fills them in (R3.5). A registry
    written before ``provisioned`` existed still loads: the field defaults to ``False``.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    name: str
    address: str | None = None
    host_key: str
    host_key_fingerprint: str
    paired_at: str
    cpu_flags: tuple[str, ...] = ()
    image: str = ""
    provisioned: bool = False


def load_registry(path: Path) -> dict[str, WorkerEntry]:
    """The registry at ``path``; ``{}`` when absent, ``RegistryError`` when malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        doc = json.loads(text)
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
        entries = {name: WorkerEntry.model_validate(raw) for name, raw in doc.items()}
    except (ValueError, pydantic.ValidationError) as err:
        raise RegistryError(f"{path}: not a worker registry ({err})") from err
    for name, entry in entries.items():
        if entry.name != name:
            raise RegistryError(f"{path}: the entry under {name!r} is named {entry.name!r}")
    return entries


def save_registry(path: Path, entries: Mapping[str, WorkerEntry]) -> None:
    """Write the registry atomically, owner-only (0600). ``OSError`` propagates."""
    doc = {name: entry.model_dump(mode="json") for name, entry in sorted(entries.items())}
    _replace(path, json.dumps(doc, indent=1) + "\n")


def pin(known_hosts: Path, name: str, host_key: str) -> None:
    """Make ``name <host_key>`` the only line of ``known_hosts`` naming ``name``.

    Every line whose host list names it (alone or among others) goes; other workers'
    lines and comments stay. An absent file is created.
    """
    try:
        lines = known_hosts.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept = [line for line in lines if name not in _hosts(line)]
    key = " ".join(host_key.split()[:2])
    _replace(known_hosts, "\n".join([*kept, f"{name} {key}"]) + "\n")


def _hosts(line: str) -> list[str]:
    """The host names a ``known_hosts`` line applies to (none for a comment)."""
    fields = line.split()
    if not fields or fields[0].startswith("#"):
        return []
    hosts = fields[1] if fields[0].startswith("@") and len(fields) > 1 else fields[0]
    return hosts.split(",")


def _replace(path: Path, text: str) -> None:
    """Write ``text`` to a 0600 temp sibling and rename it over ``path``."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
