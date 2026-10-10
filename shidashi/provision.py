"""A worker's identity, provisioned on the host (story 020).

``shidashi worker provision N --iso PATH`` gives worker ``N`` its identity before it
ever boots: an ed25519 host key, the pairing record and the host's authorized key,
written to ``/shidashi/identity/`` of a personalized copy of the generic worker ISO --
outside the squashfs, so the generic image stays generic. The worker restores that
directory at boot exactly as it restores a pairing persisted to its work disk.

The library raises :class:`ProvisionError`; the command reports.
"""

import dataclasses
import datetime as dt
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from shidashi import kyomei_protocol, workers


class ProvisionError(Exception):
    """A provisioning refused or failed; nothing was changed."""


#: Where the Primary Volume Descriptor starts (sector 16) and where its fields sit.
_PVD_OFFSET = 16 * 2048
_PVD_MAGIC = slice(1, 6)
_PVD_VOLUME_ID = slice(40, 72)


def iso_volume_id(iso: Path) -> str:
    """The volume label ``iso`` declares in its Primary Volume Descriptor. Pure stdlib.

    The label is space-padded to 32 bytes; only the padding is stripped, so a lookalike
    (``BENTOO_WORKERS``) comes back whole.
    """
    try:
        with iso.open("rb") as handle:
            handle.seek(_PVD_OFFSET)
            pvd = handle.read(_PVD_VOLUME_ID.stop)
    except OSError as err:
        raise ProvisionError(f"{iso}: cannot read it ({err})") from err
    if len(pvd) < _PVD_VOLUME_ID.stop:
        raise ProvisionError(f"{iso} is not an ISO 9660 image: shorter than its volume descriptor")
    if pvd[_PVD_MAGIC] != b"CD001":
        raise ProvisionError(f"{iso} is not an ISO 9660 image: no CD001 volume descriptor")
    return pvd[_PVD_VOLUME_ID].decode("ascii", errors="replace").rstrip(" \x00")


def inside_git_work_tree(path: Path) -> bool:
    """Whether ``path`` -- existing or not -- sits in a git checkout (a ``.git`` entry,
    file or directory, in it or an ancestor). Read-only."""
    path = path.absolute()
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def identity_files(name: str, host_pub: str, host_fp: str, now: dt.datetime) -> dict[str, bytes]:
    """The pairing record and the authorized key of worker ``name``, as bytes. Pure.

    The shape is the work disk's (story 009), plus ``source: provisioned``.
    """
    if not re.fullmatch(kyomei_protocol.HOSTNAME_RE, name):
        raise ProvisionError(f"{name!r} is not a valid worker name (one lower-case RFC 1123 label)")
    record = {
        "v": 1,
        "name": name,
        "granting_key_fingerprint": host_fp,
        "paired_at": now.astimezone(dt.UTC).isoformat(timespec="seconds"),
        "source": "provisioned",
    }
    return {
        "pairing.json": json.dumps(record, indent=1).encode() + b"\n",
        "authorized_keys": host_pub.strip().encode() + b"\n",
    }


# --- the transaction (1.3) ------------------------------------------------------------

#: The volume label of the generic worker ISO; the live root is found by it.
WORKER_VOLUME_ID = "BENTOO_WORKER"

#: Where the identity sits on the medium (outside the squashfs), and the tree it is in.
MEDIUM_IDENTITY = "/shidashi/identity"
_MEDIUM_TREE = "/shidashi"

#: The identity's owner and modes on the medium: root's, nothing for group or others.
#: Rock Ridge would keep the provisioning user's uid -- usually 1000, the uid of the
#: worker's live user too, who could then read the private key under
#: /run/initramfs/live without root. The identity is named before its tree so that a
#: copy without it fails naming it ("Cannot find path '/shidashi/identity'").
_MEDIUM_OWNERSHIP = [
    arg
    for command in (["-chown_r", "0"], ["-chgrp_r", "0"], ["-chmod_r", "go-rwx"])
    for arg in (*command, MEDIUM_IDENTITY, _MEDIUM_TREE, "--")
]

#: xorriso, ignoring its startup files: ``-no_rc`` only works as the FIRST argument, and
#: a ``~/.xorrisorc`` with ``-abort_on NEVER`` / ``-return_with`` can make a failed
#: ``-map`` exit 0.
_XORRISO = ["xorriso", "-no_rc"]

Runner = Callable[..., subprocess.CompletedProcess[Any]]


@dataclasses.dataclass(frozen=True)
class Provisioned:
    """A provisioned worker: its name, its host key's fingerprint, its personalized ISO.

    ``leftover`` is the previous identity's directory when a replace could not remove it
    -- it still holds the old private key, so the caller must say so -- else ``None``.
    """

    name: str
    fingerprint: str
    iso_path: Path
    leftover: Path | None = None


class _StepFailed(Exception):
    """A step of the transaction failed; ``str()`` is the reason, ``step`` names it."""

    def __init__(self, step: str, reason: str) -> None:
        super().__init__(reason)
        self.step = step


def provision(
    name: str,
    iso: Path,
    *,
    workers_dir: Path,
    replace: bool,
    runner: Runner,
    ensure_host_key: Callable[[], object] | None = None,
) -> Provisioned:
    """Give worker ``name`` its identity and a personalized copy of the worker ``iso``.

    Refusals come first and write nothing (R1.5, R4.3, R1.6). Everything is then built
    in a 0700 temp directory under ``workers_dir`` and verified before it is moved onto
    ``workers_dir/name/``; ``name`` is pinned in ``known_hosts`` and recorded in
    ``workers.json``, the last write. Any failure undoes what was done (R1.8). Nothing
    is written outside ``workers_dir`` (R4.2).
    """
    registry_path = workers_dir / "workers.json"
    known_hosts = workers_dir / "known_hosts"
    registry = _refuse(name, iso, workers_dir, registry_path, known_hosts, replace=replace)
    if ensure_host_key is not None:
        try:
            ensure_host_key()
        except OSError as err:
            raise ProvisionError(f"cannot create the host's worker key: {err}") from err
    host_pub_path = workers_dir / "id_ed25519.pub"
    try:
        host_pub = host_pub_path.read_text(encoding="utf-8").strip()
    except OSError as err:
        raise ProvisionError(f"{host_pub_path}: cannot read the host's worker key ({err})") from err

    # Absolute, so xorriso never reads a path like "-" as a stream (iso_volume_id read
    # the file of that name).
    source = iso.absolute()
    home = workers_dir / name
    tmp: Path | None = None
    installed = False
    old: Path | None = None
    known_hosts_before: tuple[str, int] | None = None
    pinned = False
    step = "prepare"
    try:
        tmp = Path(tempfile.mkdtemp(dir=workers_dir, prefix=f".{name}.tmp-"))
        step = "ssh-keygen"
        identity = tmp / "identity"
        (identity / "ssh").mkdir(parents=True, mode=0o700)
        host_key = identity / "ssh" / "ssh_host_ed25519_key"
        _tool(
            runner,
            step,
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(host_key)],
        )
        worker_pub = Path(f"{host_key}.pub").read_text(encoding="utf-8").strip()
        fingerprint = _fingerprint(runner, Path(f"{host_key}.pub"))
        now = dt.datetime.now(dt.UTC)
        step = "identity"
        files = identity_files(name, host_pub, _fingerprint(runner, host_pub_path), now)
        for filename, content in files.items():
            _write_private(identity / filename, content)
        step = "xorriso"
        copy = tmp / f"{name}.iso"
        try:
            _tool(
                runner,
                step,
                _XORRISO
                + ["-indev", str(source), "-outdev", str(copy)]
                + ["-boot_image", "any", "replay", "-hfsplus", "off"]
                + ["-map", str(identity), MEDIUM_IDENTITY]
                + _MEDIUM_OWNERSHIP,
            )
        except BaseException:
            copy.unlink(missing_ok=True)  # a partial image is no copy
            raise
        step = "verify"
        _verify_copy(runner, source, copy, identity)
        _chmod_private(tmp)

        step = "install"
        if replace and home.exists():
            aside = workers_dir / f".{name}.old-{secrets.token_hex(4)}"
            os.replace(home, aside)
            old = aside  # only once it moved: a failed move has nothing to restore
        os.replace(tmp, home)
        installed = True
        step = "pin"
        known_hosts_before = _read_known_hosts(known_hosts)
        pinned = True
        workers.pin(known_hosts, name, worker_pub)
        step = "registry"
        # Re-read: another command may have recorded a worker during the slow copy.
        registry = workers.load_registry(registry_path)
        entry = workers.WorkerEntry(
            name=name,
            address=None,
            host_key=worker_pub,
            host_key_fingerprint=fingerprint,
            paired_at=now.isoformat(timespec="seconds"),
            provisioned=True,
        )
        workers.save_registry(registry_path, {**registry, name: entry})
    except BaseException as err:
        undone = _rollback(
            tmp=tmp,
            home=home,
            installed=installed,
            old=old,
            known_hosts=known_hosts if pinned else None,
            known_hosts_before=known_hosts_before,
        )
        if not isinstance(err, Exception):
            raise
        failed_at = err.step if isinstance(err, _StepFailed) else step
        reason = str(err).strip() or type(err).__name__
        outcome = "nothing was changed" if not undone else "the rollback left " + "; ".join(undone)
        raise ProvisionError(f"provision failed at {failed_at}: {reason}; {outcome}") from err

    leftover: Path | None = None
    if old is not None:  # the previous identity goes last, once the new one is recorded
        shutil.rmtree(old, ignore_errors=True)
        if old.exists():
            leftover = old  # still holds the old private key; the caller reports it
    return Provisioned(
        name=name, fingerprint=fingerprint, iso_path=home / f"{name}.iso", leftover=leftover
    )


def _refuse(
    name: str,
    iso: Path,
    workers_dir: Path,
    registry_path: Path,
    known_hosts: Path,
    *,
    replace: bool,
) -> dict[str, workers.WorkerEntry]:
    """Every refusal, before anything is written; the registry as it stands."""
    if not re.fullmatch(kyomei_protocol.HOSTNAME_RE, name):
        raise ProvisionError(f"{name!r} is not a valid worker name (one lower-case RFC 1123 label)")
    volume = iso_volume_id(iso)
    if volume != WORKER_VOLUME_ID:
        raise ProvisionError(f"{iso} is not a worker ISO: its volume is {volume}")
    if inside_git_work_tree(workers_dir):
        raise ProvisionError(f"{workers_dir} is inside a git work tree")
    try:
        registry = workers.load_registry(registry_path)
    except workers.RegistryError as err:
        raise ProvisionError(str(err)) from err
    if not replace:
        held = _held_fingerprint(name, registry, known_hosts)
        if held is not None:
            raise ProvisionError(
                f"{name} is already paired with {held}; pass --replace to replace it"
            )
    return registry


def _held_fingerprint(
    name: str, registry: Mapping[str, workers.WorkerEntry], known_hosts: Path
) -> str | None:
    """The fingerprint of a key ``name`` already holds -- recorded or pinned -- if any."""
    if name in registry:
        return registry[name].host_key_fingerprint
    try:
        lines = known_hosts.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise ProvisionError(f"{known_hosts}: cannot read it ({err})") from err
    for line in lines:
        if name not in workers._hosts(line):
            continue
        fields = line.split()
        key = fields[2:4] if fields[0].startswith("@") else fields[1:3]
        try:
            return kyomei_protocol.fingerprint(" ".join(key))
        except ValueError, IndexError:
            return f"an unreadable key ({' '.join(key) or 'empty'})"
    return None


def _tool(runner: Runner, step: str, argv: list[str]) -> subprocess.CompletedProcess[Any]:
    """Run ``argv``; a non-zero exit is :class:`_StepFailed` with its stderr."""
    try:
        done = runner(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as err:
        raise _StepFailed(step, f"cannot run {argv[0]} ({err})") from err
    if done.returncode != 0:
        detail = (done.stderr or "").strip() or f"{argv[0]} exited {done.returncode}"
        raise _StepFailed(step, detail)
    return done


def _fingerprint(runner: Runner, pub: Path) -> str:
    """OpenSSH's own ``SHA256:`` fingerprint of the public key file ``pub``."""
    done = _tool(runner, "ssh-keygen", ["ssh-keygen", "-lf", str(pub)])
    fields = (done.stdout or "").split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise _StepFailed("ssh-keygen", f"no fingerprint for {pub}: {done.stdout!r}")
    return fields[1]


def _write_private(path: Path, content: bytes) -> None:
    """Write ``content`` to a new owner-only file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)


def _chmod_private(root: Path) -> None:
    """Every directory under ``root`` 0700, every file 0600 (R1.1)."""
    root.chmod(0o700)
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)


# --- verifying the copy ---------------------------------------------------------------

#: The fields of an ``El Torito boot img`` line kept: platform, bootable flag, emulation
#: and load size (``N Pltf B Emul Ld_seg Hdpt Ldsiz LBA``); the LBA moves in every copy.
_TORITO_FIELDS = (1, 2, 3, 6)


@dataclasses.dataclass(frozen=True)
class _BootShape:
    """What a PC boots from, without the block positions a correct copy moves."""

    el_torito: tuple[tuple[str, ...], ...]
    system_area: frozenset[str]
    gpt_paths: tuple[str, ...]


def _text(output: object) -> str:
    """A tool's captured output as text, whether the runner captured str or bytes."""
    if isinstance(output, str):
        return output
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode(errors="replace")
    return ""


def _boot_shape(runner: Runner, iso: Path) -> _BootShape:
    """The boot-relevant fields of xorriso's El Torito and system-area reports.

    Each El Torito entry is (platform, bootable, emulation, load size, image path, image
    options): GRUB's BIOS image needs ``boot-info-table`` / ``grub2-boot-info``.
    """
    report = _tool(
        runner,
        "verify",
        _XORRISO
        + ["-indev", str(iso)]
        + ["-report_el_torito", "plain", "-report_system_area", "plain"],
    ).stdout
    images: dict[str, tuple[str, ...]] = {}
    paths: dict[str, str] = {}
    opts: dict[str, str] = {}
    kinds: frozenset[str] = frozenset()
    gpt: list[str] = []
    for line in _text(report).splitlines():
        key, _sep, value = line.partition(":")
        fields = value.split()
        match key.strip():
            case "El Torito boot img" if len(fields) > max(_TORITO_FIELDS):
                images[fields[0]] = tuple(fields[i] for i in _TORITO_FIELDS)
            case "El Torito img path" if len(fields) >= 2:
                paths[fields[0]] = fields[1]
            case "El Torito img opts" if fields:
                opts[fields[0]] = " ".join(fields[1:])
            case "System area summary":
                kinds = frozenset(
                    f for f in fields if f != "APM" and not f.startswith("cyl-align-")
                )
            case "GPT partition path" if len(fields) >= 2:
                gpt.append(fields[1])
    torito = tuple(sorted((*images[n], paths.get(n, ""), opts.get(n, "")) for n in images))
    return _BootShape(torito, kinds, tuple(sorted(gpt)))


#: What ``-compare_r`` prints when the tree on disk and the one on the medium match, and
#: when they do not. A difference is printed, NOT signalled by the exit code.
_COMPARE_MATCH = "Both file objects match"
_COMPARE_DIFFER = "Differences detected"

#: The differences ``-compare_r`` may report and the copy still be right: the owner and
#: modes :data:`_MEDIUM_OWNERSHIP` sets on purpose, and the ctime that setting them
#: stamps on each node (as chown(2) does; seen 1 s apart across a second boundary). Any
#: other (content, size, mtime, a file missing on either side) refuses the copy.
_SET_ON_PURPOSE = frozenset({"st_uid", "st_gid", "st_mode", "st_ctime"})

#: One ``-compare_r`` difference: ``<type> '<path>' (DISK|ISO) : <attribute> : ...``.
_COMPARE_LINE = re.compile(r"^\S \'.*\'\s+\((?:DISK|ISO)\) : (?P<attribute>[^:]*?)\s*:")

#: One ``-exec lsdl`` line: permissions, links, uid, gid, size, date, then the path.
_LSDL_LINE = re.compile(
    r"^(?P<perms>[-dlbcps][-rwxsStT]{9})\s+\d+\s+(?P<uid>\S+)\s+(?P<gid>\S+)\s+\d+\s.*?"
    r"\'(?P<path>.*)\'$"
)


def _verify_identity(runner: Runner, copy: Path, identity: Path) -> None:
    """The copy carries ``identity`` at :data:`MEDIUM_IDENTITY`, file for file, and its
    tree is root's and closed to group and others.

    ``-compare_r`` proves the files and their contents; it also reports the owner and
    modes, which the copy changes on purpose, so those differences -- and only those
    -- are tolerated. ``-find ... -exec lsdl`` then proves the owner and modes on the
    medium itself, since a difference report says nothing when disk and medium agree.
    """
    missing = f"the copy does not carry the identity at {MEDIUM_IDENTITY}"
    try:
        done = _tool(
            runner,
            "verify",
            _XORRISO
            + ["-indev", str(copy), "-compare_r", str(identity), MEDIUM_IDENTITY]
            + ["-find", _MEDIUM_TREE, "-exec", "lsdl", "--"],
        )
    except _StepFailed as err:
        raise ProvisionError(f"{missing}: {_gist(str(err))}") from err
    out = _text(done.stdout)
    differences = [m for ln in out.splitlines() if (m := _COMPARE_LINE.match(ln.strip()))]
    unexpected = [m.string for m in differences if m["attribute"] not in _SET_ON_PURPOSE]
    if _COMPARE_DIFFER in out:
        if unexpected or not differences:
            raise ProvisionError(f"{missing}: {_gist(chr(10).join(unexpected) or out)}")
    elif _COMPARE_MATCH not in out:
        raise ProvisionError(f"{missing}: {_gist(out)}")

    listed = [m for ln in out.splitlines() if (m := _LSDL_LINE.match(ln.strip()))]
    if MEDIUM_IDENTITY not in {m["path"] for m in listed}:
        raise ProvisionError(f"{missing}: no listing of {_MEDIUM_TREE}")
    for m in listed:
        if (m["uid"], m["gid"]) != ("0", "0") or m["perms"][4:] != "------":
            raise ProvisionError(
                f"the copy's {m['path']} is {m['perms']} {m['uid']}:{m['gid']}, not root's "
                "alone: the worker's users could read its identity"
            )


def _gist(output: str) -> str:
    """The lines of xorriso's output that say what went wrong (all of it otherwise)."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    telling = [
        ln for ln in lines if any(m in ln for m in (": FAILURE :", ": SORRY :", "(ISO)", "(DISK)"))
    ]
    return "; ".join(telling[:3] or lines[-1:]) or "no comparison result"


def _verify_copy(runner: Runner, source: Path, copy: Path, identity: Path) -> None:
    """The copy boots as the source does -- same volume id, same boot shape -- and
    carries the identity."""
    want, got = iso_volume_id(source), iso_volume_id(copy)
    if got != want:
        raise ProvisionError(f"the copy's volume is {got}, the source's {want}")
    before = _boot_shape(runner, source)
    if not before.el_torito or not before.system_area:
        # Two shapes the parser could not read are equal, and prove nothing.
        raise ProvisionError(f"cannot read the boot shape of {source}")
    after = _boot_shape(runner, copy)
    for field in dataclasses.fields(_BootShape):
        if getattr(before, field.name) != getattr(after, field.name):
            raise ProvisionError(
                f"the copy's {field.name.replace('_', ' ')} differs from the source's: "
                f"{getattr(after, field.name)} instead of {getattr(before, field.name)}"
            )
    _verify_identity(runner, copy, identity)


# --- the rollback ---------------------------------------------------------------------


def _read_known_hosts(path: Path) -> tuple[str, int] | None:
    """``known_hosts`` as it stands -- text and mode -- or ``None`` when absent."""
    try:
        return path.read_text(encoding="utf-8"), stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def _rollback(
    *,
    tmp: Path | None,
    home: Path,
    installed: bool,
    old: Path | None,
    known_hosts: Path | None,
    known_hosts_before: tuple[str, int] | None,
) -> list[str]:
    """Undo a failed transaction; what could NOT be undone (empty when all was)."""
    left: list[str] = []
    if tmp is not None and tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
        if tmp.exists():
            left.append(f"{tmp} in place")
    if installed:
        shutil.rmtree(home, ignore_errors=True)
        if home.exists():
            left.append(f"the new {home} in place")
    if old is not None:
        try:
            os.replace(old, home)
        except OSError as err:
            left.append(f"the previous identity at {old} ({err})")
    if known_hosts is not None:
        try:
            if known_hosts_before is None:
                known_hosts.unlink(missing_ok=True)
            else:
                text, mode = known_hosts_before
                workers._replace(known_hosts, text)
                known_hosts.chmod(mode)
        except OSError as err:
            left.append(f"{known_hosts} changed ({err})")
    return left
