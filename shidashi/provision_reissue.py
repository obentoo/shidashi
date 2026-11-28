"""Re-issue a provisioned worker's medium with the identity it already has.

``shidashi worker provision N --iso PATH --keep-identity`` personalizes a NEW generic
worker ISO with N's EXISTING identity (``<workers dir>/N/identity/``): the same host key
and pairing record, nothing re-pinned, the registry untouched. The worker boots the new
image and the host reaches it as before. ``--replace`` is for a lost medium: a new key.

Why (2026-11-28): bentoo-lab's image was rebuilt to ship its scratch disk, and
``provision`` only offered ``--replace``; a generic ISO would have restored the older
pairing left on its work disk, whose host key the host no longer pinned. The copy was made
by hand with provision's own xorriso step -- this module is that step, refused and
verified the way provision is.
"""

import json
import os
import re
import secrets
from pathlib import Path

from shidashi import kyomei_protocol, workers
from shidashi.provision import (
    _MEDIUM_OWNERSHIP,
    _XORRISO,
    MEDIUM_IDENTITY,
    WORKER_VOLUME_ID,
    Provisioned,
    ProvisionError,
    Runner,
    _fingerprint,
    _StepFailed,
    _tool,
    _verify_copy,
    inside_git_work_tree,
    iso_volume_id,
)

#: The identity files a medium cannot boot paired without (the worker's restore refuses
#: a record without its key, or a key pair missing a half).
_REQUIRED = ("pairing.json", "ssh/ssh_host_ed25519_key", "ssh/ssh_host_ed25519_key.pub")


def reissue(name: str, iso: Path, *, workers_dir: Path, runner: Runner) -> Provisioned:
    """Write ``<workers dir>/name/name.iso``: ``iso`` carrying ``name``'s identity.

    Every refusal comes first and writes nothing. The copy is built beside the old one,
    verified (it boots as ``iso`` does and carries the identity byte for byte), then moved
    over it; any failure removes the partial copy and leaves the old one in place.
    """
    home, identity, fingerprint = _refuse_reissue(name, iso, workers_dir, runner)
    target = home / f"{name}.iso"
    copy = home / f".{name}.iso.tmp-{secrets.token_hex(4)}"
    step = "xorriso"
    try:
        _tool(
            runner,
            step,
            _XORRISO
            + ["-indev", str(iso.absolute()), "-outdev", str(copy)]
            + ["-boot_image", "any", "replay", "-hfsplus", "off"]
            + ["-map", str(identity), MEDIUM_IDENTITY]
            + _MEDIUM_OWNERSHIP,
        )
        step = "verify"
        _verify_copy(runner, iso.absolute(), copy, identity)
        copy.chmod(0o600)  # it holds the worker's private key
        os.replace(copy, target)
    except (ProvisionError, _StepFailed, OSError) as err:
        copy.unlink(missing_ok=True)
        failed_at = err.step if isinstance(err, _StepFailed) else step
        raise ProvisionError(
            f"re-issue failed at {failed_at}: {err}; {target} was not changed"
        ) from err
    return Provisioned(name=name, fingerprint=fingerprint, iso_path=target)


def _refuse_reissue(
    name: str, iso: Path, workers_dir: Path, runner: Runner
) -> tuple[Path, Path, str]:
    """Every refusal; the worker's home, its identity directory and its key's fingerprint."""
    if not re.fullmatch(kyomei_protocol.HOSTNAME_RE, name):
        raise ProvisionError(f"{name!r} is not a valid worker name (one lower-case RFC 1123 label)")
    volume = iso_volume_id(iso)
    if volume != WORKER_VOLUME_ID:
        raise ProvisionError(f"{iso} is not a worker ISO: its volume is {volume}")
    if inside_git_work_tree(workers_dir):
        raise ProvisionError(f"{workers_dir} is inside a git work tree")
    try:
        entry = workers.load_registry(workers_dir / "workers.json").get(name)
    except workers.RegistryError as err:
        raise ProvisionError(str(err)) from err
    if entry is None:
        raise ProvisionError(
            f"{name} is not paired: there is no identity to keep (provision it without "
            "--keep-identity)"
        )
    home = workers_dir / name
    identity = home / "identity"
    for relative in _REQUIRED:
        if not (identity / relative).is_file():
            raise ProvisionError(
                f"{name} has no identity to keep: {identity / relative} is missing "
                "(a worker paired over kyomei has none on the host; --replace makes one)"
            )
    try:
        record = json.loads((identity / "pairing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ProvisionError(f"{identity / 'pairing.json'} is not a pairing record: {err}") from err
    if not isinstance(record, dict) or record.get("name") != name:
        raise ProvisionError(f"{identity / 'pairing.json'} names another worker, not {name}")
    try:
        fingerprint = _fingerprint(runner, identity / "ssh" / "ssh_host_ed25519_key.pub")
    except _StepFailed as err:
        raise ProvisionError(f"cannot read the identity's host key: {err}") from err
    if fingerprint != entry.host_key_fingerprint:
        raise ProvisionError(
            f"{name}'s identity holds the host key {fingerprint}, but the host pins "
            f"{entry.host_key_fingerprint}: that medium would boot a worker the host "
            "refuses (--replace makes a new identity)"
        )
    return home, identity, fingerprint
