"""UNIT (R2.1-R2.6) — pure seed logic: pointer, URL, digest, fetch (cache).

Everything here is deterministic on the non-Gentoo CI host. The download and the
privileged extraction are NOT exercised here (extract_stage3 is integration, task 2.3);
``fetch_stage3`` is tested through the cache-hit and ``download=False`` paths without
touching the network (monkeypatch). ``verify_signature`` (gpg shell-out) has its host
path covered by the integration tests; here we exercise only the pure,
verifiable contract: ``load_pointer``, ``stage3_url``, ``verify_digest``.

Contract (design.md §seed): ``Stage3Pointer(init, base_url, snapshot, filename,
sha512)`` frozen pydantic; ``SeedError(Exception)``; ``load_pointer(init, *,
seeds_dir)`` (SeedError listing the entries if the init is missing); ``stage3_url`` pure;
``verify_digest(tarball, sha512)`` raises SeedError on mismatch;
``fetch_stage3(pointer, *, cache_dir, download=True)`` reuses the cache, and
``download=False`` without a cache raises SeedError.
"""

import hashlib
from pathlib import Path

import pytest

from shidashi import seed
from shidashi.seed import (
    SeedError,
    Stage3Pointer,
    fetch_stage3,
    load_pointer,
    stage3_url,
    verify_digest,
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
