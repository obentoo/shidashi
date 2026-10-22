"""Sign a release and check every signature (story 030, R6).

``SHA256SUMS.asc`` is what makes a release directory signed: a rerun refuses a directory
that holds one (R1.7). So it must never exist unverified (R6.6) -- not after a gpg
failure or a bad signature, not after a Ctrl-C or a SIGTERM during the check, and not
after a SIGKILL or a power cut that no handler sees. :func:`sign_and_verify` therefore
signs ``SHA256SUMS`` through a copy in a hidden staging directory, checks the signature
there, and only then moves it in with ``os.replace``, which is atomic on one filesystem.

The same rule makes one release per directory at a time: :func:`claim_release_dir` holds
``.release.lock`` from the preflight through the signing, so a second release the same UTC
day cannot add ``SHA256SUMS`` lines after this one's signature was checked.

Split from ``release.py`` (which re-exports :func:`sign_release`, :func:`verify_release`
and the file names) to keep both modules under 500 lines. The gpg seams are reached
through their module attributes (``gpgsign.*``, ``seed.gpg_verify``), so a test fake
replaces them here too.
"""

import contextlib
import os
import shutil
import stat
from collections.abc import Generator
from pathlib import Path

from shidashi import audit, gpgsign, locks, publish, seed
from shidashi.build_outcome import ReleaseError

#: The exported public key of ``--key``: what every signature is verified against (R6.1, R6.5).
RELEASE_KEY_FILE = "bentoo-release-key.asc"
#: The checksum list, signed last (R6.3).
SUMS_FILE = "SHA256SUMS"
#: Where ``SHA256SUMS`` is signed before its signature is verified: never listed in
#: ``SHA256SUMS``, removed once the signing ends however it ends.
STAGE_DIR = ".signing"
#: Held by the one release writing a directory; never listed in ``SHA256SUMS``.
LOCK_FILE = ".release.lock"
#: The release directory and the stage: the maintainer's gpg runs as ``SUDO_USER`` and must
#: read what it signs.
RELEASE_DIR_MODE = 0o755


def _refuse_unsignable(dest: Path) -> None:
    """``ReleaseError`` when ``dest`` is already signed (R1.7), or when its ``.signing`` is
    no directory: the signing could not stage there and would fail hours later. Such a file
    was planted, so it is named and left alone, never removed."""
    if os.path.lexists(dest / f"{SUMS_FILE}.asc"):
        raise ReleaseError(f"{dest} is already signed: a published release is never replaced")
    try:
        mode = os.lstat(dest / STAGE_DIR).st_mode
    except FileNotFoundError:
        return
    except OSError as err:
        raise ReleaseError(f"cannot check {dest / STAGE_DIR}: {err}") from err
    if not stat.S_ISDIR(mode):
        raise ReleaseError(
            f"{dest / STAGE_DIR} is not a directory: the signing stages there; "
            "remove it, then rerun"
        )


@contextlib.contextmanager
def claim_release_dir(dest: Path) -> Generator[None]:
    """Create ``dest`` and hold it for one release, from the preflight through the signing.

    Refused (``ReleaseError``, nothing built yet): ``dest`` already signed, checked again
    once the lock is held -- another release may have signed and let go meanwhile; a
    ``.signing`` that is no directory; another release holding ``dest``, named with its pid.
    """
    _refuse_unsignable(dest)  # before the mkdir: a signed directory stays as it is
    try:
        dest.mkdir(parents=True, exist_ok=True, mode=RELEASE_DIR_MODE)
    except OSError as err:
        raise ReleaseError(f"cannot create the release directory {dest}: {err}") from err
    with contextlib.ExitStack() as held:  # only the acquire's errors are the lock's
        try:
            held.enter_context(
                locks.hold(dest / LOCK_FILE, what=f"the release directory {dest}", wait=False)
            )
        except locks.LockBusy as err:
            raise ReleaseError(f"{err}: another release is writing it; wait for it to end") from err
        except (locks.LockError, OSError) as err:
            raise ReleaseError(f"cannot lock the release directory {dest}: {err}") from err
        _refuse_unsignable(dest)
        yield


def sign_release(directory: Path, *, key: str, stage: Path | None = None) -> list[Path]:
    """Export ``key``'s public key and list it in ``SHA256SUMS`` (R6.1), sign every
    ``*.iso.DIGESTS`` (R6.2), then ``SHA256SUMS`` last (R6.3). Returns the signatures. I/O.

    A signature covers its file, so no ``.asc`` is ever listed in ``SHA256SUMS`` (GOTCHA:
    signing order); any write to it after the last signature would break that signature.
    A failure re-raises and leaves no ``SHA256SUMS.asc`` (R6.6): a half-signed release must
    not look signed. With ``stage``, ``SHA256SUMS`` is signed through a copy there, so its
    signature is ``stage/SHA256SUMS.asc`` until the caller has verified it.
    """
    sums = directory / SUMS_FILE
    made: list[Path] = []
    try:
        public = gpgsign.export_public(key, directory / RELEASE_KEY_FILE)
        publish.update_sha256sums(directory, {public.name: publish.sha256_of(public)})
        for digests in sorted(directory.glob("*.iso.DIGESTS")):
            made.append(gpgsign.detach_sign(digests, key=key))
        if stage is not None:  # after the key's line: the copy is the final SHA256SUMS
            sums = Path(shutil.copy2(sums, stage / SUMS_FILE))
        made.append(gpgsign.detach_sign(sums, key=key))
    except BaseException:
        # BaseException: a Ctrl-C between the two last calls must not leave it either
        sums.with_name(f"{SUMS_FILE}.asc").unlink(missing_ok=True)
        raise
    return made


def _key_matches(fingerprint: str, key: str) -> bool:
    """Whether ``fingerprint`` is ``key``: the whole fingerprint, or a key ID ending it."""
    wanted = key.replace(" ", "").upper().removeprefix("0X")
    return len(wanted) >= 16 and fingerprint.upper().endswith(wanted)


def _foreign_signer(status: str | None, key: str) -> str | None:
    """Why a verified signature is not ``key``'s, from gpg's ``VALIDSIG`` lines, or ``None``.

    ``VALIDSIG <signing-key fpr> ... <primary-key fpr>``: either may be ``key`` (a subkey
    signs for its primary). A keyring holding a second key verifies that key's signatures
    too, so a good signature alone is not proof it is ``key``'s. ``status`` is ``None`` for
    a verifier that reports through its exceptions only (the unit tests' fake gpg).
    """
    if status is None:
        return None
    signers = [
        (fields[2], fields[-1])
        for fields in (line.split() for line in status.splitlines())
        if fields[:2] == ["[GNUPG:]", "VALIDSIG"] and len(fields) > 2
    ]
    if not signers:
        return "gpg reported no VALIDSIG"
    for signing, primary in signers:
        if not (_key_matches(signing, key) or _key_matches(primary, key)):
            return f"signed by {primary}, not by {key}"
    return None


def verify_release(directory: Path, *, key: str, sums_signature: Path | None = None) -> list[str]:
    """Check every signature of ``directory`` against ``bentoo-release-key.asc`` alone, and
    that ``key`` made it (R6.5). Returns one ``"<asc>: <reason>"`` per failure; empty = all
    good. I/O.

    The exported key ends in ``.asc`` but signs nothing: it is the keyring, never a
    signature. A DIGESTS or ``SHA256SUMS`` without its signature is a failure too.
    ``sums_signature`` is where ``SHA256SUMS``'s signature is when it is not yet in the
    directory (:func:`sign_and_verify`'s stage).
    """
    keyring = directory / RELEASE_KEY_FILE
    in_place = directory / f"{SUMS_FILE}.asc"
    data_of = {p.with_name(f"{p.name}.asc"): p for p in directory.glob("*.iso.DIGESTS")}
    data_of.update(
        (p, p.with_name(p.name.removesuffix(".asc")))
        for p in directory.glob("*.asc")
        if p.name != RELEASE_KEY_FILE and p != in_place
    )
    data_of[sums_signature or in_place] = directory / SUMS_FILE
    failures: list[str] = []
    for asc, data in sorted(data_of.items(), key=lambda pair: pair[0].name):
        if not asc.is_file():
            failures.append(f"{asc.name}: missing")
            continue
        try:
            # typed wider than seed's ``str``: see _foreign_signer
            status: str | None = seed.gpg_verify(asc, data, keyring=keyring)
        except seed.SeedError as err:
            failures.append(f"{asc.name}: {err}")
            continue
        if (why := _foreign_signer(status, key)) is not None:
            failures.append(f"{asc.name}: {why}")
    return failures


def sign_and_verify(trail: audit.Recorder, dest: Path, key: str) -> list[str]:
    """Sign the release in ``dest`` and check every signature; the failures, empty = signed
    (R6). ``SHA256SUMS.asc`` appears only once every signature verified (R6.6)."""
    stage = dest / STAGE_DIR
    staged = stage / f"{SUMS_FILE}.asc"
    try:
        shutil.rmtree(stage, ignore_errors=True)  # what a killed run left; never a symlink's target
        stage.mkdir(mode=RELEASE_DIR_MODE)
        with trail.step("sign"):
            sign_release(dest, key=key, stage=stage)
        with trail.step("verify signatures") as step:
            failures = verify_release(dest, key=key, sums_signature=staged)
            step.add(failures=failures)
        # Under the lock publish.update_sha256sums writes with: a writer the release lock
        # does not stop (a `shidashi build` by hand) cannot land between check and move.
        with locks.hold(dest / f".{SUMS_FILE}.lock", what=f"{SUMS_FILE} of {dest}"):
            if not failures and (stage / SUMS_FILE).read_bytes() != (dest / SUMS_FILE).read_bytes():
                failures = [f"{SUMS_FILE}: changed while it was signed; a rerun signs it again"]
            if not failures:
                os.replace(staged, dest / f"{SUMS_FILE}.asc")
    except (gpgsign.GpgError, seed.SeedError, publish.PublishError, OSError) as err:
        failures = [str(err)]
    finally:
        # the stage holds nothing that counts: a rerun clears whatever this cannot
        shutil.rmtree(stage, ignore_errors=True)
    return failures


__all__ = [
    "LOCK_FILE",
    "RELEASE_DIR_MODE",
    "RELEASE_KEY_FILE",
    "STAGE_DIR",
    "SUMS_FILE",
    "claim_release_dir",
    "sign_and_verify",
    "sign_release",
    "verify_release",
]
