"""The maintainer's gpg on the host: key check, detached signatures, public key export.

Under ``sudo`` root's agent and keyring are not the maintainer's (story 029 R5.13, story
030 R6.4): every gpg call here runs as ``SUDO_USER`` with their ``HOME`` through
``runuser``. That user cannot write into the root-owned release directory, so gpg writes
to stdout and the files are written by root, through a temp file and ``os.replace`` --
a gpg that dies leaves no half-written ``.asc`` behind (R6.6).
"""

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

from shidashi.binhost import BinhostError
from shidashi.invoker import invoking_user

_GPG_HOST_TIMEOUT_S = 60
#: A signature or a public key is published next to the release: world-readable.
_PUBLISHED_MODE = 0o644


class GpgError(BinhostError):
    """A gpg call of the maintainer that failed, or answered nothing usable.

    A :class:`BinhostError` so story 029's ``except BinhostError`` keeps catching it.
    """


def as_maintainer(argv: list[str]) -> list[str]:
    """``argv``, run as ``SUDO_USER`` with their home when this is root under ``sudo``.

    The agent and the keyring are the maintainer's, never root's.
    """
    try:
        user = invoking_user()
    except LookupError as err:
        raise GpgError(str(err)) from err
    if user is None:
        return argv
    return ["runuser", "-u", user.name, "--", "env", f"HOME={user.home}", *argv]


def host_gpg(argv: list[str], what: str, stdin: bytes | None = None) -> bytes:
    """Stdout of a host ``gpg``/``gpgconf`` command; any failure is a :class:`GpgError`."""
    try:
        done = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            check=False,
            timeout=_GPG_HOST_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise GpgError(f"cannot {what}: {err}") from err
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise GpgError(f"cannot {what} (exit {done.returncode}): {said}")
    return done.stdout


def check_key(key: str) -> None:
    """Refuse a ``key`` the maintainer holds no secret of, or one no agent can reach (R1.6).

    Run before any build: a weekly release must not find a missing key hours in.
    """
    # Outside the ``try``: an unknown SUDO_USER is its own error, not a missing key.
    argv = as_maintainer(["gpg", "--batch", "--list-secret-keys", "--", key])
    try:
        host_gpg(argv, f"list the secret key {key}")
    except GpgError as err:
        raise GpgError(
            f"no secret key {key} in the maintainer's keyring, or no gpg-agent answers: {err}"
        ) from err


def _write_published_file(dest: Path, content: bytes) -> None:
    """``dest`` holds ``content`` or is untouched: a temp file beside it, then ``os.replace``."""
    try:
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    except OSError as err:
        raise GpgError(f"cannot write {dest}: {err}") from err
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
        tmp.chmod(_PUBLISHED_MODE)  # mkstemp creates 0600
        os.replace(tmp, dest)
    except BaseException as err:  # a Ctrl-C mid-write must not leave the temp file either
        with contextlib.suppress(OSError):
            tmp.unlink()
        if isinstance(err, OSError):
            raise GpgError(f"cannot write {dest}: {err}") from err
        raise


def detach_sign(path: Path, *, key: str) -> Path:
    """Write ``<path>.asc``, the armored detached signature of ``path`` by ``key``.

    The name appends ``.asc`` (``x.iso.DIGESTS`` -> ``x.iso.DIGESTS.asc``): a suffix swap
    would name a signature of another file. ``path`` must be readable by the maintainer
    (the release directory is created 0755). Returns the signature's path.
    """
    signature = host_gpg(
        as_maintainer(
            [
                "gpg",
                "--batch",
                "--yes",
                "--armor",
                "--local-user",
                key,
                "--detach-sign",
                "--output",
                "-",
                "--",
                str(path),
            ]
        ),
        f"sign {path} with {key}",
    )
    if not signature.strip():
        raise GpgError(f"gpg made an empty signature of {path} with {key}")
    asc = path.with_name(f"{path.name}.asc")
    _write_published_file(asc, signature)
    return asc


def export_public(key: str, dest: Path) -> Path:
    """Write the armored public key of ``key`` to ``dest``; returns ``dest``."""
    exported = host_gpg(
        as_maintainer(["gpg", "--batch", "--armor", "--export", "--", key]),
        f"export the public key {key}",
    )
    if not exported.strip():  # gpg exits 0 and prints nothing for a key it does not have
        raise GpgError(f"unknown key {key}: the maintainer's keyring has no public key for it")
    _write_published_file(dest, exported)
    return dest


__all__ = ["GpgError", "as_maintainer", "check_key", "detach_sign", "export_public", "host_gpg"]
