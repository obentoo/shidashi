"""UNIT (R2.1-R2.6) — pure seed logic: pointer, URL, digest, fetch (cache).

Everything here is deterministic on the non-Gentoo CI host. The download and the
privileged extraction are NOT exercised here (extract_stage3 is integration, task 2.3);
``fetch_stage3`` is tested through the cache-hit and ``download=False`` paths without
touching the network (monkeypatch). ``verify_signature`` runs the real ``gpg``
against a key generated for the test (skipped without gpg); the rest is the pure,
verifiable contract: ``load_pointer``, ``stage3_url``, ``verify_digest``.

Contract (design.md §seed): ``Stage3Pointer(init, base_url, snapshot, filename,
sha512)`` frozen pydantic; ``SeedError(Exception)``; ``load_pointer(init, *,
seeds_dir)`` (SeedError listing the entries if the init is missing); ``stage3_url`` pure;
``verify_digest(tarball, sha512)`` raises SeedError on mismatch;
``fetch_stage3(pointer, *, cache_dir, download=True)`` reuses the cache, and
``download=False`` without a cache raises SeedError.
"""

import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import seed
from shidashi.seed import (
    SeedError,
    Stage3Pointer,
    fetch_stage3,
    load_pointer,
    signature_ok,
    stage3_url,
    verify_digest,
    verify_signature,
)

_SNAPSHOT = "20260518T170330Z"
_BASE_URL = "https://distfiles.gentoo.org/releases/amd64/autobuilds"
_SHA512_SYSTEMD = "1" * 128
_SHA512_OPENRC = "2" * 128
_TOML = f"""\
snapshot = "{_SNAPSHOT}"
base_url = "{_BASE_URL}"

[systemd]
filename = "stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz"
sha512   = "{_SHA512_SYSTEMD}"

[openrc]
filename = "stage3-amd64-nomultilib-openrc-{_SNAPSHOT}.tar.xz"
sha512   = "{_SHA512_OPENRC}"
"""


@pytest.fixture
def seeds_dir(tmp_path: Path) -> Path:
    d = tmp_path / "seeds"
    d.mkdir()
    (d / "stage3.toml").write_text(_TOML, encoding="utf-8")
    return d


# --- SeedError is an exception -------------------------------------------------


def test_seed_error_is_exception_subclass() -> None:
    assert issubclass(SeedError, Exception)


# --- load_pointer (R2.1, R2.2) -----------------------------------------------


def test_load_pointer_reads_pinned_entry(seeds_dir: Path) -> None:
    p = load_pointer("systemd", seeds_dir=seeds_dir)
    assert p.init == "systemd"
    assert p.snapshot == _SNAPSHOT
    assert p.base_url == _BASE_URL
    assert p.filename == f"stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz"
    assert p.sha512 == _SHA512_SYSTEMD


def test_load_pointer_unknown_init_lists_available(seeds_dir: Path) -> None:
    with pytest.raises(SeedError) as excinfo:
        load_pointer("upstart", seeds_dir=seeds_dir)
    msg = str(excinfo.value)
    assert "upstart" in msg
    # names the available entries
    assert "systemd" in msg
    assert "openrc" in msg


def test_stage3_pointer_is_frozen() -> None:
    p = Stage3Pointer(
        init="systemd",
        base_url=_BASE_URL,
        snapshot=_SNAPSHOT,
        filename="x.tar.xz",
        sha512="0" * 128,
    )
    with pytest.raises(Exception):  # noqa: B017 (frozen → ValidationError/Error)
        p.init = "openrc"


# --- stage3_url (R2.1) -------------------------------------------------------


def test_stage3_url_builds_mirror_path() -> None:
    p = Stage3Pointer(
        init="systemd",
        base_url=_BASE_URL,
        snapshot=_SNAPSHOT,
        filename=f"stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz",
        sha512="0" * 128,
    )
    url = stage3_url(p)
    assert url.startswith(_BASE_URL)
    assert _SNAPSHOT in url
    assert url.endswith(p.filename)
    # no doubled slashes in the middle (clean join)
    assert "//releases" not in url.replace("https://", "")


# --- verify_digest (R2.3, R2.4) ----------------------------------------------


def test_verify_digest_passes_on_match(tmp_path: Path) -> None:
    blob = b"stage3 contents"
    tarball = tmp_path / "s.tar.xz"
    tarball.write_bytes(blob)
    good = hashlib.sha512(blob).hexdigest()
    # match → does not raise, returns None
    assert verify_digest(tarball, good) is None  # type: ignore[func-returns-value]


def test_verify_digest_raises_on_mismatch(tmp_path: Path) -> None:
    tarball = tmp_path / "s.tar.xz"
    tarball.write_bytes(b"stage3 contents")
    with pytest.raises(SeedError):
        verify_digest(tarball, "deadbeef" * 8)


# --- verify_signature (R2.3, R2.4) -------------------------------------------


def test_signature_ok_needs_goodsig_validsig_and_exit_zero() -> None:
    good = "[GNUPG:] GOODSIG EC59 Gentoo\n[GNUPG:] VALIDSIG E1D6 2026-09-20\n"
    assert signature_ok(good, 0)
    assert not signature_ok(good, 1)
    assert not signature_ok("[GNUPG:] GOODSIG EC59 Gentoo\n", 0)  # e.g. an expired key
    assert not signature_ok("[GNUPG:] BADSIG EC59 Gentoo\n", 1)


_NO_GPG = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg is not installed")


def _gpg(home: str, *args: str) -> str:
    return subprocess.run(
        ["gpg", "--homedir", home, "--batch", "--pinentry-mode", "loopback", "--passphrase", ""]
        + list(args),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def signed_digests(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A cleartext-signed ``.DIGESTS`` (the autobuilds' layout) and a keyring
    holding only its signer's public key, standing in for gentoo-release.asc."""
    # a short home: gpg-agent's socket path must fit in sun_path
    home = tempfile.mkdtemp(prefix="shidashi-t-")
    try:
        _gpg(home, "--quick-gen-key", "Release <releng@test.invalid>", "ed25519", "sign", "never")
        keyring = tmp_path / "release.asc"
        keyring.write_text(_gpg(home, "--armor", "--export"))
        plain = tmp_path / "DIGESTS"
        plain.write_text(f"# SHA512 HASH\n{'0' * 128}  stage3.tar.xz\n")
        signed = tmp_path / "stage3.tar.xz.DIGESTS"
        _gpg(home, "--clearsign", "--output", str(signed), str(plain))
        yield signed, keyring
    finally:
        subprocess.run(["gpgconf", "--homedir", home, "--kill", "all"], check=False)
        shutil.rmtree(home, ignore_errors=True)


@_NO_GPG
def test_verify_signature_trusts_the_keyring_not_the_callers_home(
    signed_digests: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (2026-10-04): under sudo, ``gpg --verify`` used root's empty
    ~/.gnupg and refused the stage3 with "No public key"."""
    signed, keyring = signed_digests
    empty = tmp_path / "root-gnupg"
    empty.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(empty))
    verify_signature(signed, keyring=keyring)


@_NO_GPG
def test_verify_signature_refuses_a_signer_outside_the_keyring(
    signed_digests: tuple[Path, Path], tmp_path: Path
) -> None:
    signed, _ = signed_digests
    home = tempfile.mkdtemp(prefix="shidashi-t-")
    try:
        _gpg(home, "--quick-gen-key", "Other <other@test.invalid>", "ed25519", "sign", "never")
        other = tmp_path / "other.asc"
        other.write_text(_gpg(home, "--armor", "--export"))
    finally:
        subprocess.run(["gpgconf", "--homedir", home, "--kill", "all"], check=False)
        shutil.rmtree(home, ignore_errors=True)
    with pytest.raises(SeedError, match="GPG verification failed"):
        verify_signature(signed, keyring=other)


@_NO_GPG
def test_verify_signature_names_a_keyring_that_imports_nothing(
    signed_digests: tuple[Path, Path], tmp_path: Path
) -> None:
    signed, _ = signed_digests
    empty = tmp_path / "empty.asc"
    empty.write_text("")
    with pytest.raises(SeedError, match="failed to import .*empty.asc"):
        verify_signature(signed, keyring=empty)


@_NO_GPG
def test_verify_signature_refuses_a_tampered_digests(signed_digests: tuple[Path, Path]) -> None:
    signed, keyring = signed_digests
    signed.write_text(signed.read_text().replace("0" * 128, "1" * 128))
    with pytest.raises(SeedError, match="GPG verification failed"):
        verify_signature(signed, keyring=keyring)


@_NO_GPG
def test_verify_signature_names_a_missing_keyring(
    signed_digests: tuple[Path, Path], tmp_path: Path
) -> None:
    signed, _ = signed_digests
    with pytest.raises(SeedError, match="openpgp-keys-gentoo-release"):
        verify_signature(signed, keyring=tmp_path / "absent.asc")


# --- fetch_stage3 cache/no-download (R2.5, R2.6) -----------------------------


def test_fetch_stage3_no_download_no_cache_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    p = Stage3Pointer(
        init="systemd",
        base_url=_BASE_URL,
        snapshot=_SNAPSHOT,
        filename="absent.tar.xz",
        sha512="0" * 128,
    )
    # --no-download + no cache → actionable SeedError, without touching the network
    with pytest.raises(SeedError):
        fetch_stage3(p, cache_dir=cache, download=False)


def test_fetch_stage3_reuses_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # guard against the network: any download must fail the test
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not download when the cache is valid")

    # covers both urllib and a possible internal download helper
    monkeypatch.setattr(seed, "_download", _boom, raising=False)

    cache = tmp_path / "cache"
    cache.mkdir()
    blob = b"cached stage3"
    digest = hashlib.sha512(blob).hexdigest()
    filename = f"stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz"
    (cache / filename).write_bytes(blob)

    p = Stage3Pointer(
        init="systemd",
        base_url=_BASE_URL,
        snapshot=_SNAPSHOT,
        filename=filename,
        sha512=digest,
    )
    got = fetch_stage3(p, cache_dir=cache, download=True)
    assert got == cache / filename


def test_extract_stage3_keeps_sticky_setuid_and_group_write(tmp_path: Path) -> None:
    """Regression (2026-09-26): the stage3 came out with /tmp 0755 and su without
    setuid, and the bootstrap's locale-gen aborted. GNU tar, as the lab's reseed."""
    import subprocess

    from shidashi.seed import extract_stage3

    src = tmp_path / "src"
    (src / "tmp").mkdir(parents=True)
    (src / "tmp").chmod(0o1777)
    (src / "usr" / "bin").mkdir(parents=True)
    (src / "usr" / "bin" / "su").write_text("x")
    (src / "usr" / "bin" / "su").chmod(0o4755)
    tarball = tmp_path / "stage3.tar.xz"
    subprocess.run(["tar", "-cJf", str(tarball), "-C", str(src), "."], check=True)

    rootfs = tmp_path / "rootfs"
    extract_stage3(tarball, rootfs)

    assert oct((rootfs / "tmp").stat().st_mode & 0o7777) == "0o1777"
    assert oct((rootfs / "usr/bin/su").stat().st_mode & 0o7777) == "0o4755"


# --- fetch_stage3 download → verify → store (R2.3, R2.4) — task 8.1 ----------
#
# The network is replaced by a fake ``seed._download`` that writes canned bytes
# to whatever destination fetch_stage3 hands it, and the GPG check by a fake
# ``seed.verify_signature``. What is asserted is the contract, not the steps:
# a verified tarball ends up in the cache under its pinned name and nothing
# else does; a failed check raises before anything lands in the cache.

_FILENAME = f"stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz"
_BLOB = b"downloaded stage3"


def _pointer(sha512: str) -> Stage3Pointer:
    return Stage3Pointer(
        init="systemd",
        base_url=_BASE_URL,
        snapshot=_SNAPSHOT,
        filename=_FILENAME,
        sha512=sha512,
    )


class _FakeMirror:
    """Serves the tarball (and anything else asked for, e.g. its signed
    ``.DIGESTS``) and remembers every destination it wrote."""

    def __init__(self, blob: bytes) -> None:
        self.blob = blob
        self.urls: list[str] = []
        self.dests: list[Path] = []

    def __call__(self, url: str, dest: Path) -> None:
        self.urls.append(url)
        self.dests.append(dest)
        dest.write_bytes(self.blob if url == stage3_url(_pointer("0" * 128)) else b"signed")


def _entries(cache: Path) -> list[str]:
    # every entry, hidden ones too: a leftover ".tmp"/"shidashi-seed-*" counts
    return sorted(p.name for p in cache.iterdir()) if cache.exists() else []


def test_fetch_stage3_download_verified_is_stored_under_pinned_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    mirror = _FakeMirror(_BLOB)
    signature_checked: list[Path] = []

    def _signature(digests: Path, **_k: object) -> None:
        # the signature is checked BEFORE the tarball is stored: a cache hit
        # only re-checks the digest, so a stored-then-refused tarball would be
        # reused next time without its signature ever having held
        assert not (cache / _FILENAME).exists(), "tarball stored before its signature was checked"
        assert digests.is_file()
        signature_checked.append(digests)

    monkeypatch.setattr(seed, "_download", mirror)
    monkeypatch.setattr(seed, "verify_signature", _signature)

    got = fetch_stage3(_pointer(hashlib.sha512(_BLOB).hexdigest()), cache_dir=cache)

    assert got == cache / _FILENAME
    assert got.read_bytes() == _BLOB
    assert stage3_url(_pointer("0" * 128)) in mirror.urls
    assert len(signature_checked) == 1
    # stored atomically: the cache holds the tarball and nothing else, and no
    # destination the downloads went to survives outside the cache either
    assert _entries(cache) == [_FILENAME]
    assert [d for d in mirror.dests if d.exists() and d != got] == []


def test_fetch_stage3_download_failed_digest_leaves_nothing_in_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    mirror = _FakeMirror(b"tampered stage3")
    monkeypatch.setattr(seed, "_download", mirror)
    monkeypatch.setattr(seed, "verify_signature", lambda *_a, **_k: None)

    with pytest.raises(SeedError, match="sha512"):
        fetch_stage3(_pointer(hashlib.sha512(_BLOB).hexdigest()), cache_dir=cache)

    assert _entries(cache) == []
    assert [d for d in mirror.dests if d.exists()] == []


def test_fetch_stage3_download_failed_signature_leaves_nothing_in_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    mirror = _FakeMirror(_BLOB)

    def _refuse(*_a: object, **_k: object) -> None:
        raise SeedError("GPG verification failed for the .DIGESTS")

    monkeypatch.setattr(seed, "_download", mirror)
    monkeypatch.setattr(seed, "verify_signature", _refuse)

    # the digest matches: only the signature fails, and that alone must refuse
    with pytest.raises(SeedError, match="GPG"):
        fetch_stage3(_pointer(hashlib.sha512(_BLOB).hexdigest()), cache_dir=cache)

    assert _entries(cache) == []
    assert [d for d in mirror.dests if d.exists()] == []


def test_fetch_stage3_download_replaces_a_corrupt_file_under_the_pinned_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile half of R2.5: a file carrying the pinned NAME is not the pinned
    tarball until its digest says so — it is re-downloaded, never reused."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / _FILENAME).write_bytes(b"truncated by a killed run")
    mirror = _FakeMirror(_BLOB)
    monkeypatch.setattr(seed, "_download", mirror)
    monkeypatch.setattr(seed, "verify_signature", lambda *_a, **_k: None)

    got = fetch_stage3(_pointer(hashlib.sha512(_BLOB).hexdigest()), cache_dir=cache)

    assert stage3_url(_pointer("0" * 128)) in mirror.urls
    assert got.read_bytes() == _BLOB
    assert _entries(cache) == [_FILENAME]
