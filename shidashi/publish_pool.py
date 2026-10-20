"""The signed publish pool: the recorded finals, copied, signed and indexed (story 029).

The build pool (the generation's PKGDIR) is never signed or written: signing rewrites a
gpkg, which would change the SHA1/MD5 the assemble checkpoints hash. ``publish`` copies the
recorded finals to ``<dest>/<same PATH>``, signs each copy in the toolbox through the
maintainer's gpg-agent *extra* socket (the only socket that cannot export the key), gives
the copies a ``Packages`` of their own, and renames the finished tree over ``dest``.
``verify`` is the reader's side: every signature of a finished pool, against one public key.
"""

import contextlib
import os
import pwd
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Protocol

from shidashi.binhost import BinhostError, BinpkgRef, PoolRecord, load_record, parse_refs, safe_path
from shidashi.seed import signature_ok
from shidashi.toolbox import WORK, Toolbox

#: Mount names inside the toolbox (``/work/<name>``): the pool being built, the signing home.
_POOL_MOUNT = "publish"
_GNUPG_MOUNT = "gnupg"
#: gpg looks for the agent at ``<homedir>/S.gpg-agent``; the extra socket is bound there.
_AGENT_SOCKET = "S.gpg-agent"
#: Portage's BINPKG_GPG_SIGNING_BASE_COMMAND (make.globals) minus its ``flock /run/lock/...``:
#: the toolbox is read-only and its /run/lock may not exist, and files are signed one at a
#: time here, so there is no concurrent gpg to serialise.
_SIGNING_COMMAND = "/usr/bin/gpg --sign --armor [PORTAGE_CONFIG]"
_HOST_TIMEOUT_S = 60
_OUTPUT_TAIL_LINES = 20


class ToolboxFactory(Protocol):
    """What the CLI hands ``publish``: a toolbox with ``rw`` mounted and ``file_binds`` bound."""

    def __call__(
        self, rw: Mapping[str, Path], file_binds: Sequence[tuple[Path, Path]]
    ) -> Toolbox: ...


@dataclass(frozen=True)
class PublishResult:
    dest: Path
    published: int
    #: instances in no stage's finals or transients (OLD included): publish sees no repos
    skipped_unrecorded: int


def plan_publish(refs: Sequence[BinpkgRef], record: PoolRecord) -> list[BinpkgRef]:
    """The refs that are a recorded final of some stage, and nothing else. Pure.

    Fail-closed: an unrecorded instance, or one only a USE cut built, is never published.
    Matched on ``(cpv, build_id)``: two builds of one CPV differ.
    """
    finals = frozenset().union(*record.finals.values())
    return [ref for ref in refs if (ref.cpv, ref.build_id) in finals]


# --- the maintainer's agent and keyring, on the host ----------------------------------------


def _as_maintainer(argv: list[str]) -> list[str]:
    """``argv``, run as ``SUDO_USER`` with their home when this is root under ``sudo``.

    The agent and the keyring are the maintainer's, never root's (same rule as
    ``schedule._invoking_user``, which this does not import: it would pull in the scheduler).
    """
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return argv
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError as err:
        raise BinhostError(f"SUDO_USER={user!r} is not a user of this system") from err
    return ["runuser", "-u", user, "--", "env", f"HOME={home}", *argv]


def _host_gpg(argv: list[str], what: str, stdin: bytes | None = None) -> bytes:
    """Stdout of a host ``gpg``/``gpgconf`` command; any failure is a :class:`BinhostError`."""
    try:
        done = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            check=False,
            timeout=_HOST_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise BinhostError(f"cannot {what}: {err}") from err
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise BinhostError(f"cannot {what} (exit {done.returncode}): {said}")
    return done.stdout


def _agent_extra_socket() -> Path:
    out = _host_gpg(
        _as_maintainer(["gpgconf", "--list-dirs", "agent-extra-socket"]),
        "ask gpgconf for the agent extra socket",
    )
    where = out.decode("utf-8", "replace").strip()
    if not where or not Path(where).exists():
        raise BinhostError(
            f"the gpg-agent extra socket {where or '(none reported)'!r} does not exist: start "
            "the agent of the maintainer's session and enable its extra socket, then retry"
        )
    return Path(where)


def _public_key(key: str) -> bytes:
    exported = _host_gpg(
        _as_maintainer(["gpg", "--batch", "--export", "--armor", "--", key]),
        f"export the public key {key}",
    )
    if not exported.strip():  # gpg exits 0 and prints nothing for a key it does not have
        raise BinhostError(f"the keyring has no public key {key}: check the fingerprint")
    return exported


# --- the build ---------------------------------------------------------------------------


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-_OUTPUT_TAIL_LINES:])


def _run_in_toolbox(tools: Toolbox, argv: list[str], what: str) -> None:
    try:
        tools.container.run(argv)
    except subprocess.CalledProcessError as err:
        said = _tail(f"{err.output or ''}{err.stderr or ''}")
        raise BinhostError(f"{what} failed (exit {err.returncode}):\n{said}") from err
    except OSError as err:
        raise BinhostError(f"cannot run {what} in the toolbox: {err}") from err


def _copy_planned(build_pool: Path, tmp: Path, planned: Sequence[BinpkgRef]) -> None:
    for ref in planned:
        src, dst = safe_path(build_pool, ref.path), safe_path(tmp, ref.path)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)  # a copy: gpkg-sign rewrites the file it signs
        except OSError as err:
            raise BinhostError(f"cannot copy {src} to {dst}: {err}") from err


def _swap_in(tmp: Path, dest: Path) -> None:
    """Rename ``tmp`` over ``dest``: the old pool is set aside first and restored on failure."""
    old = dest.parent / f".{dest.name}.old"
    try:
        shutil.rmtree(old, ignore_errors=True)
        had_previous = dest.exists()
        if had_previous:
            os.rename(dest, old)
        try:
            os.rename(tmp, dest)
        except OSError:
            if had_previous:
                os.rename(old, dest)
            raise
    except OSError as err:
        raise BinhostError(f"cannot move the new pool {tmp} to {dest}: {err}") from err
    try:
        shutil.rmtree(old, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError as err:
        raise BinhostError(
            f"{dest} is the new pool, but {old} could not be removed: {err}"
        ) from err


def publish(
    build_pool: Path, dest: Path, *, key: str, toolbox_for: ToolboxFactory
) -> PublishResult:
    """Build the signed publish pool of ``build_pool`` at ``dest``. I/O, PRIVILEGED.

    Every refusal comes before the first copy. The tree is built in ``dest.parent/.<name>.tmp``
    and renamed into place only when every step succeeded; a failure leaves the previous
    ``dest`` as it was and the ``.tmp`` for inspection. ``build_pool`` is only read.
    """
    index = build_pool / "Packages"
    try:
        refs = parse_refs(index.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        raise BinhostError(
            f"no binhost index {index}; nothing is built in {build_pool} yet: run "
            "`shidashi factory <arch> <target>` first"
        ) from None
    except OSError as err:
        raise BinhostError(f"cannot read the binhost index {index}: {err}") from err
    record = load_record(build_pool)
    planned = plan_publish(refs, record)
    if not planned:
        raise BinhostError(
            f"no binpkg of {build_pool} is a recorded final: run `shidashi factory` to record "
            "the finals of each shipped stage, then publish"
        )
    tmp = dest.parent / f".{dest.name}.tmp"
    for ref in planned:  # the whole plan, before the first copy (R3.11)
        safe_path(build_pool, ref.path)
    socket_path = _agent_extra_socket()
    public_key = _public_key(key)
    kept = frozenset().union(*record.finals.values(), *record.transients.values())
    skipped = sum(1 for ref in refs if (ref.cpv, ref.build_id) not in kept)

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if tmp.exists():  # a failed run's leftover
            shutil.rmtree(tmp)
        tmp.mkdir()
    except OSError as err:
        raise BinhostError(f"cannot prepare {tmp}: {err}") from err
    _copy_planned(build_pool, tmp, planned)

    gnupghome = Path(tempfile.mkdtemp(prefix="shidashi-gnupg-"))  # public key only, mode 0700
    try:
        _host_gpg(
            ["gpg", "--homedir", str(gnupghome), "--batch", "--import"],
            "import the public key into a throwaway GNUPGHOME",
            stdin=public_key,
        )
        home_inside = WORK / _GNUPG_MOUNT
        # ONE file: the socket's directory also holds the full-access standard socket
        tools = toolbox_for(
            rw={_POOL_MOUNT: tmp, _GNUPG_MOUNT: gnupghome},
            file_binds=[(socket_path, home_inside / _AGENT_SOCKET)],
        )
        for ref in planned:
            _run_in_toolbox(
                tools,
                [
                    "env",
                    f"BINPKG_GPG_SIGNING_KEY={key}",
                    f"BINPKG_GPG_SIGNING_GPG_HOME={home_inside}",
                    f"BINPKG_GPG_SIGNING_BASE_COMMAND={_SIGNING_COMMAND}",
                    "gpkg-sign",
                    str(tools.path(safe_path(tmp, ref.path))),
                ],
                f"gpkg-sign of {ref.path}",
            )
        _run_in_toolbox(
            tools,
            ["env", f"PKGDIR={tools.path(tmp)}", "emaint", "binhost", "--fix"],
            f"emaint binhost --fix over {tmp}",
        )
    finally:
        shutil.rmtree(gnupghome, ignore_errors=True)
    _swap_in(tmp, dest)
    return PublishResult(dest=dest, published=len(planned), skipped_unrecorded=skipped)


# --- verify: every signature of the pool, against the one public key given ---------------------

#: A gpkg is an untrusted tar: its member headers and sizes are bounded before they are read.
#: 64 members is far above a real gpkg (gpkg-1, metadata, image, their .sig, Manifest).
_MAX_GPKG_MEMBERS = 64
#: An armored detached signature is under 1 KiB; this is read whole, into memory.
_MAX_SIG_BYTES = 64 * 1024
#: Signed members (metadata, image) are streamed to gpg, never held; a size past this (which
#: also catches a sparse member that claims terabytes) is refused instead of read.
_MAX_SIGNED_BYTES = 16 * 1024**3
_STREAM_CHUNK = 1024 * 1024
#: Portage signs the payload members; ``Manifest`` is clear-signed and ``gpkg-1`` is a marker.
_UNSIGNED_MEMBERS = frozenset({"gpkg-1", "Manifest"})
_SIG_SUFFIX = ".sig"
#: gpg --status-fd keywords that say why a signature was refused.
_REFUSALS = ("BADSIG", "ERRSIG", "NO_PUBKEY", "EXPSIG", "EXPKEYSIG", "REVKEYSIG")


def _unreadable_paths(index_text: str, usable: set[str]) -> list[str]:
    """PATHs that blocks of ``index_text`` name and :func:`parse_refs` did not return. Pure.

    ``parse_refs`` skips a block without a ``CPV`` or a numeric ``BUILD_ID``; for the
    verifier such a block would be a gpkg it silently never checks.
    """
    named: list[str] = []
    for block in index_text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("PATH: ") and line[6:] not in usable:
                named.append(line[6:])
    return named


def _read_index(dest: Path) -> tuple[list[BinpkgRef], list[str]]:
    """The refs of ``dest/Packages`` and the failures of reading it. Never raises."""
    index = dest / "Packages"
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], [f"{index}: no Packages index in the pool"]
    except OSError as err:
        return [], [f"{index}: cannot read the Packages index: {err}"]
    refs = parse_refs(text)
    failures = [
        f"{path}: named by a Packages block that parse_refs cannot read (no CPV or BUILD_ID)"
        for path in _unreadable_paths(text, {ref.path for ref in refs})
    ]
    if not refs:
        failures.append(f"{index}: lists no binpkg to verify")
    return refs, failures


def _gpg_refusal(status: str) -> str:
    for line in status.splitlines():
        for keyword in _REFUSALS:
            if line.startswith(f"[GNUPG:] {keyword} "):
                return keyword
    return "no valid signature"


def _gpg_verify_stream(home: Path, signature: bytes, data: IO[bytes]) -> str | None:
    """None when ``signature`` is a good, valid signature of ``data``; else why not.

    ``data`` goes to ``gpg --verify <sig> -`` in chunks: a multi-GiB image is never held.
    The signature is written to a file this function names, in the throwaway ``home``.
    """
    sig_file = home / "member.sig"
    sig_file.write_bytes(signature)
    argv = ["gpg", "--homedir", str(home), "--batch", "--status-fd", "1", "--verify"]
    with tempfile.TemporaryFile() as status_out:
        try:
            proc = subprocess.Popen(
                [*argv, str(sig_file), "-"],
                stdin=subprocess.PIPE,
                stdout=status_out,
                stderr=subprocess.DEVNULL,
            )
        except OSError as err:
            return f"cannot run gpg --verify: {err}"
        assert proc.stdin is not None  # stdin=PIPE
        try:
            while chunk := data.read(_STREAM_CHUNK):
                proc.stdin.write(chunk)
        except BrokenPipeError:  # gpg refused the signature before reading the data
            pass
        finally:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.close()
        try:
            code = proc.wait(timeout=_HOST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return "gpg --verify timed out"
        status_out.seek(0)
        status = status_out.read().decode("utf-8", "replace")
    if signature_ok(status, code):
        return None
    return _gpg_refusal(status)


def _gpkg_problems(home: Path, tar: tarfile.TarFile) -> list[str]:
    """What is wrong with the signatures of the gpkg ``tar``; empty when all are valid."""
    members: dict[str, tarfile.TarInfo] = {}
    for count, info in enumerate(tar):
        if count >= _MAX_GPKG_MEMBERS:
            return [f"more than {_MAX_GPKG_MEMBERS} members: not a gpkg"]
        if info.isdir():
            continue
        name = PurePosixPath(info.name).name
        if not info.isreg():
            return [f"member {name!r} is not a regular file"]
        if name in members:
            return [f"member {name!r} appears twice: which one is signed is ambiguous"]
        members[name] = info
    sigs = [name for name in members if name.endswith(_SIG_SUFFIX)]
    if not sigs:
        return [f"no {_SIG_SUFFIX} member: the gpkg is not signed"]
    problems = [
        f"{name!r} has no {_SIG_SUFFIX} member"
        for name in members
        if name not in _UNSIGNED_MEMBERS and not name.endswith(_SIG_SUFFIX)
        if f"{name}{_SIG_SUFFIX}" not in members
    ]
    for sig_name in sigs:
        target = sig_name.removesuffix(_SIG_SUFFIX)
        if target not in members:
            problems.append(f"{sig_name!r} signs no member of the gpkg")
        elif (why := _member_refusal(home, tar, members[sig_name], members[target])) is not None:
            problems.append(f"{sig_name!r} does not verify {target!r}: {why}")
    return problems


def _member_refusal(
    home: Path, tar: tarfile.TarFile, sig: tarfile.TarInfo, signed: tarfile.TarInfo
) -> str | None:
    if sig.size > _MAX_SIG_BYTES:
        return f"the signature is {sig.size} bytes, over the {_MAX_SIG_BYTES} cap"
    if signed.size > _MAX_SIGNED_BYTES:
        return f"the signed member is {signed.size} bytes, over the {_MAX_SIGNED_BYTES} cap"
    sig_file, data_file = tar.extractfile(sig), tar.extractfile(signed)
    if sig_file is None or data_file is None:
        return "a member cannot be read"
    with sig_file, data_file:
        return _gpg_verify_stream(home, sig_file.read(_MAX_SIG_BYTES), data_file)


def _check_gpkg(home: Path, path: Path, label: str) -> list[str]:
    """The failures of one gpkg; a gpkg that is missing or not a tar is one, never a raise."""
    try:
        with tarfile.open(path, "r:") as tar:  # "r:" = uncompressed only: no decompression bomb
            return [f"{label}: {why}" for why in _gpkg_problems(home, tar)]
    except FileNotFoundError:
        return [f"{label}: listed in Packages but missing from the pool"]
    except (tarfile.TarError, EOFError) as err:
        return [f"{label}: not a readable tar archive ({err})"]
    except OSError as err:
        return [f"{label}: cannot be read: {err}"]


def _drop_gpg_home(home: Path) -> None:
    """Stop whatever gpg started in ``home`` (best effort), then remove it with its keys."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "all"],
            capture_output=True,
            check=False,
            timeout=_HOST_TIMEOUT_S,
        )
    shutil.rmtree(home, ignore_errors=True)


def verify(dest: Path, *, pubkey: Path) -> list[str]:
    """The failures of the publish pool ``dest``; empty only when every gpkg is validly signed.

    Trust is ``pubkey`` alone: a throwaway GNUPGHOME holds only that key, so the
    verifier's own keyring never counts. Fail-closed: whatever cannot be checked (no
    ``Packages``, an unreadable block, no entry, a missing or non-tar gpkg) is a named
    failure, never a pass and never an exception. A missing or unusable ``pubkey`` is a
    :class:`BinhostError`: there is nothing to judge against.
    """
    if not pubkey.is_file():
        raise BinhostError(f"the public key file {pubkey} does not exist")
    refs, failures = _read_index(dest)
    home = Path(tempfile.mkdtemp(prefix="shidashi-verify-"))  # mode 0700; the public key only
    try:
        _host_gpg(
            ["gpg", "--homedir", str(home), "--batch", "--quiet", "--import", str(pubkey)],
            f"import the public key {pubkey}",
        )
        for ref in refs:
            try:
                gpkg = safe_path(dest, ref.path)
            except BinhostError as err:
                failures.append(f"{ref.path}: {err}")
                continue
            failures.extend(_check_gpkg(home, gpkg, ref.path))
    finally:
        _drop_gpg_home(home)
    return failures
