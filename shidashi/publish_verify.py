"""``binhost verify``: every signature of a finished publish pool, one public key (story 029).

The reader's side of ``publish_pool``: it holds only the public key, in a throwaway
GNUPGHOME, and judges what a Portage client with ``binpkg-request-signature`` would judge.
Portage (``portage/gpkg.py``) signs a gpkg in two ways, and both are checked here:

* ``image.tar.gz`` and ``metadata.tar.gz`` each get a detached armored ``<member>.sig``
  (``_add_signature``): R6.1-R6.4.
* ``_add_manifest`` writes a ``Manifest`` member -- one ``DATA <name> <size> <hashes>`` line
  per member -- and passes it through ``gpg --clear-sign``. ``_verify_binpkg`` reads that member
  into memory and runs ``gpg --verify --output - -`` on it, so a missing, unsigned, tampered
  or untrusted-key ``Manifest`` makes the client refuse the gpkg: R6.5.
"""

import contextlib
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import IO

from shidashi.binhost import BinhostError, BinpkgRef, parse_refs, safe_path
from shidashi.seed import signature_ok

_GPG_TIMEOUT_S = 60
#: A gpkg is an untrusted tar: its member headers and sizes are bounded before they are read.
#: 64 members is far above a real gpkg (gpkg-1, metadata, image, their .sig, Manifest).
_MAX_GPKG_MEMBERS = 64
#: An armored detached signature is under 1 KiB; this is read whole, into memory.
_MAX_SIG_BYTES = 64 * 1024
#: A clear-signed Manifest is a few hundred bytes (one line per member plus the armor); it is
#: read whole, into memory, never extracted. 1 MiB is far above any real one.
_MAX_MANIFEST_BYTES = 1024 * 1024
#: Signed members (metadata, image) are streamed to gpg, never held; a size past this (which
#: also catches a sparse member that claims terabytes) is refused instead of read.
_MAX_SIGNED_BYTES = 16 * 1024**3
_STREAM_CHUNK = 1024 * 1024
#: Portage signs the payload members; ``Manifest`` is clear-signed and ``gpkg-1`` is a marker.
_UNSIGNED_MEMBERS = frozenset({"gpkg-1", "Manifest"})
_MANIFEST_MEMBER = "Manifest"
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
            code = proc.wait(timeout=_GPG_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return "gpg --verify timed out"
        status_out.seek(0)
        status = status_out.read().decode("utf-8", "replace")
    if signature_ok(status, code):
        return None
    return _gpg_refusal(status)


def _gpg_verify_clearsigned(home: Path, message: bytes) -> str | None:
    """None when ``message`` is a clear-signed text with a good, valid signature; else why not.

    The message goes to ``gpg --verify -`` on stdin, as Portage does (``--output -`` is left
    out: the signed text is not needed, only the verdict). It is bounded by the caller.
    """
    argv = ["gpg", "--homedir", str(home), "--batch", "--status-fd", "1", "--verify", "-"]
    try:
        done = subprocess.run(
            argv,
            input=message,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_GPG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "gpg --verify timed out"
    except OSError as err:
        return f"cannot run gpg --verify: {err}"
    status = done.stdout.decode("utf-8", "replace")
    if signature_ok(status, done.returncode):
        return None
    return _gpg_refusal(status)


def _manifest_problem(home: Path, tar: tarfile.TarFile, info: tarfile.TarInfo | None) -> str | None:
    """Why the gpkg's ``Manifest`` would be refused by a Portage client; None when it is fine.

    The member is read from the tar into memory (at most ``_MAX_MANIFEST_BYTES``), never
    extracted: its name in the tar is the archive's to choose.
    """
    if info is None:
        return f"{_MANIFEST_MEMBER} is missing: a Portage client refuses a gpkg without it"
    if info.size > _MAX_MANIFEST_BYTES:
        return f"{_MANIFEST_MEMBER} is {info.size} bytes, over the {_MAX_MANIFEST_BYTES} cap"
    member = tar.extractfile(info)
    if member is None:
        return f"{_MANIFEST_MEMBER} cannot be read"
    with member:
        message = member.read(_MAX_MANIFEST_BYTES)
    if (why := _gpg_verify_clearsigned(home, message)) is None:
        return None
    return f"{_MANIFEST_MEMBER} is not clear-signed by the given key: {why}"


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
    if (why := _manifest_problem(home, tar, members.get(_MANIFEST_MEMBER))) is not None:
        problems.append(why)
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


def _import_pubkey(home: Path, pubkey: Path) -> None:
    what = f"import the public key {pubkey}"
    try:
        done = subprocess.run(
            ["gpg", "--homedir", str(home), "--batch", "--quiet", "--import", str(pubkey)],
            capture_output=True,
            check=False,
            timeout=_GPG_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise BinhostError(f"cannot {what}: {err}") from err
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise BinhostError(f"cannot {what} (exit {done.returncode}): {said}")


def _drop_gpg_home(home: Path) -> None:
    """Stop whatever gpg started in ``home`` (best effort), then remove it with its keys."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "all"],
            capture_output=True,
            check=False,
            timeout=_GPG_TIMEOUT_S,
        )
    shutil.rmtree(home, ignore_errors=True)


def verify(dest: Path, *, pubkey: Path) -> list[str]:
    """The failures of the publish pool ``dest``; empty only when every gpkg is validly signed.

    Per gpkg: every ``.sig`` member and the clear-signed ``Manifest`` must verify against
    ``pubkey``. Trust is ``pubkey`` alone: a throwaway GNUPGHOME holds only that key, so the
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
        _import_pubkey(home, pubkey)
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
