"""Verified acquisition of Shidashi's stage3 (OVERVIEW §10/§11).

Separates the **pure** logic (parsing the pinned pointer, building the mirror URL,
digest verification) from the **privileged** execution (download, GPG verification
by shelling out to ``gpg``, extraction preserving ownership). The pure logic is
unit-tested on non-Gentoo CI; download/extraction are exercised by the
host-gated integration tests.

Reproducibility: the stage3 is pinned by ``seeds/stage3.toml`` (filename +
sha512 per init) and verified by SHA-512 **and** the GPG signature of the ``.DIGESTS``
(cleartext-signed, inline PGP signature — the current layout of Gentoo's
autobuilds, which no longer publishes SHA-256 nor a separate ``.DIGESTS.asc``) before
any extraction. Uses only the stdlib (``tomllib``/``urllib``/``hashlib``/
GNU ``tar``) plus the host's ``gpg``.
"""

import hashlib
import shutil
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(frozen=True, extra="forbid")

# Defensive limit when reading the TOML pointer (small, checked-in file).
_MAX_POINTER_BYTES = 64 * 1024


class SeedError(Exception):
    """Failure to acquire/verify a stage3 (R2.2/R2.4/R2.6).

    Raised when the init has no pinned entry, when digest/signature
    verification fails, or when ``--no-download`` is used without a cache.
    """


class Stage3Pointer(BaseModel):
    """Pinned stage3 entry per init (R2.1).

    Frozen pydantic v2 (``extra="forbid"``, the ``recipe.py`` idiom). Carries the
    resolved ``init``, the mirror's ``base_url``, the ``snapshot`` (autobuild
    directory), the tarball ``filename`` and its pinned ``sha512``.
    """

    model_config = _STRICT
    init: str
    base_url: str
    snapshot: str
    filename: str
    sha512: str


def load_pointer(init: str, *, seeds_dir: Path) -> Stage3Pointer:
    """Read the pinned entry for ``init`` from ``seeds/stage3.toml`` (R2.1/R2.2).

    ``snapshot`` and ``base_url`` are shared top-level keys; each init is
    a table with ``filename`` + ``sha512``. If ``init`` has no table, it
    raises :class:`SeedError` naming the init and the available entries.
    """
    toml_path = seeds_dir / "stage3.toml"
    try:
        raw = toml_path.read_bytes()
    except OSError as err:
        raise SeedError(f"could not read {toml_path}: {err}") from err
    if len(raw) > _MAX_POINTER_BYTES:
        raise SeedError(f"{toml_path} exceeds the expected pointer size")
    data = tomllib.loads(raw.decode("utf-8"))

    snapshot = data.get("snapshot")
    base_url = data.get("base_url")
    if not isinstance(snapshot, str) or not isinstance(base_url, str):
        raise SeedError(f"{toml_path} lacks valid top-level 'snapshot'/'base_url'")

    # init tables = every key whose value is a mapping
    inits = sorted(k for k, v in data.items() if isinstance(v, dict))
    entry = data.get(init)
    if not isinstance(entry, dict):
        disponiveis = ", ".join(inits) if inits else "(none)"
        raise SeedError(f"init {init!r} has no entry in {toml_path}; available: {disponiveis}")

    filename = entry.get("filename")
    sha512 = entry.get("sha512")
    if not isinstance(filename, str) or not isinstance(sha512, str):
        raise SeedError(f"entry {init!r} in {toml_path} lacks 'filename'/'sha512'")

    return Stage3Pointer(
        init=init,
        base_url=base_url,
        snapshot=snapshot,
        filename=filename,
        sha512=sha512,
    )


def stage3_url(pointer: Stage3Pointer) -> str:
    """Build the tarball URL on the mirror (R2.1). Pure, no doubled slashes.

    ``<base_url>/<snapshot>/<filename>`` (Gentoo's autobuild layout).
    """
    base = pointer.base_url.rstrip("/")
    return f"{base}/{pointer.snapshot}/{pointer.filename}"


def verify_digest(tarball: Path, sha512: str) -> None:
    """Compare the SHA-512 of ``tarball`` with the pinned digest (R2.3/R2.4). Pure.

    Reads in chunks so the whole tarball is not loaded into memory. On mismatch it
    raises :class:`SeedError` naming the expected and the actual value. SHA-512 is the digest
    published (and signed) by Gentoo's current autobuilds in the ``.DIGESTS``.
    """
    h = hashlib.sha512()
    with tarball.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != sha512:
        raise SeedError(f"sha512 mismatch for {tarball.name}: expected {sha512}, got {actual}")


def verify_signature(digests: Path) -> None:
    """Verify the inline GPG signature of the stage3 ``.DIGESTS`` (R2.3/R2.4).

    Gentoo's current autobuilds sign the ``.DIGESTS`` in *cleartext* (inline PGP
    SIGNED MESSAGE) — there is no separate ``.DIGESTS.asc`` anymore. So the
    verification is ``gpg --verify <.DIGESTS>`` with **a single** argument (the
    cleartext-signed file validates itself; passing a second data
    file would be wrong for this format). Trust comes from Gentoo's release key
    in the host keyring. Raises :class:`SeedError` if the file is
    missing, if ``gpg`` is not available, or if the verification returns
    non-zero. Never ignores the return code.
    """
    if not digests.is_file():
        raise SeedError(f".DIGESTS missing: {digests}")
    if shutil.which("gpg") is None:
        raise SeedError("gpg unavailable on the host; cannot verify the signature")
    try:
        result = subprocess.run(
            ["gpg", "--verify", str(digests)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:
        raise SeedError(f"failed to run gpg --verify: {err}") from err
    if result.returncode != 0:
        raise SeedError(f"GPG verification failed for {digests.name}:\n{result.stderr.strip()}")


def _download(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` via the stdlib ``urllib`` (privileged/network).

    Isolated in a named helper so tests can monkeypatch
    ``seed._download`` and ensure the cache-hit path does not touch the network.
    """
    try:
        with urllib.request.urlopen(url) as resp, dest.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
    except (urllib.error.URLError, OSError) as err:
        raise SeedError(f"failed to download {url}: {err}") from err


def fetch_stage3(pointer: Stage3Pointer, *, cache_dir: Path, download: bool = True) -> Path:
    """Return the verified tarball, downloading it once if needed (R2.5/R2.6).

    - If ``cache_dir/<filename>`` already exists and matches the pinned digest, reuse it
      without touching the network (R2.5).
    - Otherwise, if ``download`` is ``False``, raise an actionable :class:`SeedError`
      without network access (R2.6).
    - Else download the tarball **and** the sibling ``<filename>.DIGESTS`` (cleartext-signed)
      to temporary files, verify the digest (SHA-512) + inline GPG signature
      (deleting the partials on failure), and only then atomically move the tarball into
      the cache.
    """
    cached = cache_dir / pointer.filename
    if cached.is_file():
        try:
            verify_digest(cached, pointer.sha512)
        except SeedError:
            pass  # corrupt/stale cache → re-download below
        else:
            return cached

    if not download:
        raise SeedError(
            f"--no-download: stage3 {pointer.filename!r} missing from cache {cache_dir} "
            f"and download disabled; run without --no-download to fetch it"
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    url = stage3_url(pointer)
    digests_url = f"{url}.DIGESTS"
    tmp_dir = Path(tempfile.mkdtemp(prefix="shidashi-seed-", dir=cache_dir))
    tmp_tarball = tmp_dir / pointer.filename
    tmp_digests = tmp_dir / f"{pointer.filename}.DIGESTS"
    try:
        _download(url, tmp_tarball)
        _download(digests_url, tmp_digests)
        verify_digest(tmp_tarball, pointer.sha512)
        verify_signature(tmp_digests)
        tmp_tarball.replace(cached)
    except SeedError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return cached


#: GNU tar flags that keep a rootfs intact: owners by number (the image's, not
#: the host's name mapping), every mode bit (sticky /tmp, setuid su), and the
#: xattrs that carry file capabilities. The lab's reseed.sh used exactly these.
ROOTFS_TAR_FLAGS = ("--numeric-owner", "--preserve-permissions", "--xattrs", "--xattrs-include=*.*")


def extract_stage3(tarball: Path, rootfs: Path) -> None:
    """Extract the verified tarball into ``rootfs`` preserving ownership.

    **Privileged** (requires root): preserves the stage3's owners/permissions/devices.
    Creates ``rootfs`` under the scratch. On an extraction error raises
    :class:`SeedError`.

    GNU tar, not Python's ``tarfile``: its ``filter="tar"`` clears the setuid,
    setgid and sticky bits and group/other write -- the first real run got a
    stage3 with ``/tmp`` 0755 and ``su`` without setuid, and ``locale-gen``
    aborted -- and ``tarfile`` does not restore xattrs (file capabilities).
    """
    rootfs.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["tar", "--extract", "--file", str(tarball), "--directory", str(rootfs), *ROOTFS_TAR_FLAGS],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SeedError(f"failed to extract {tarball.name} into {rootfs}: {result.stderr.strip()}")
