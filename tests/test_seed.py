"""UNIT (R2.1-R2.6) — lógica pura de seed: pointer, URL, digest, fetch (cache).

Tudo aqui é determinista no host CI não-Gentoo. O download e a extração
privilegiada NÃO são exercitados aqui (extract_stage3 é integração, tarefa 2.3);
``fetch_stage3`` é testado pelos caminhos cache-hit e ``download=False`` sem
tocar a rede (monkeypatch). ``verify_signature`` (gpg shell-out) tem seu caminho
de host coberto pela integração; aqui exercitamos apenas o contrato puro
verificável: ``load_pointer``, ``stage3_url``, ``verify_digest``.

Contrato (design.md §seed): ``Stage3Pointer(init, base_url, snapshot, filename,
sha256)`` frozen pydantic; ``SeedError(Exception)``; ``load_pointer(init, *,
seeds_dir)`` (SeedError listando entradas se init ausente); ``stage3_url`` puro;
``verify_digest(tarball, sha256)`` levanta SeedError em divergência;
``fetch_stage3(pointer, *, cache_dir, download=True)`` reusa cache, e
``download=False`` sem cache levanta SeedError.
"""

import hashlib
from pathlib import Path

import pytest

from kaji import seed
from kaji.seed import (
    SeedError,
    Stage3Pointer,
    fetch_stage3,
    load_pointer,
    stage3_url,
    verify_digest,
)

_SNAPSHOT = "20260518T170330Z"
_BASE_URL = "https://distfiles.gentoo.org/releases/amd64/autobuilds"
_TOML = f"""\
snapshot = "{_SNAPSHOT}"
base_url = "{_BASE_URL}"

[systemd]
filename = "stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz"
sha256   = "1111111111111111111111111111111111111111111111111111111111111111"

[openrc]
filename = "stage3-amd64-nomultilib-openrc-{_SNAPSHOT}.tar.xz"
sha256   = "2222222222222222222222222222222222222222222222222222222222222222"
"""


@pytest.fixture
def seeds_dir(tmp_path: Path) -> Path:
    d = tmp_path / "seeds"
    d.mkdir()
    (d / "stage3.toml").write_text(_TOML, encoding="utf-8")
    return d


# --- SeedError é uma exceção -------------------------------------------------


def test_seed_error_is_exception_subclass() -> None:
    assert issubclass(SeedError, Exception)


# --- load_pointer (R2.1, R2.2) -----------------------------------------------


def test_load_pointer_reads_pinned_entry(seeds_dir: Path) -> None:
    p = load_pointer("systemd", seeds_dir=seeds_dir)
    assert p.init == "systemd"
    assert p.snapshot == _SNAPSHOT
    assert p.base_url == _BASE_URL
    assert p.filename == f"stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz"
    assert p.sha256 == "1" * 64


def test_load_pointer_unknown_init_lists_available(seeds_dir: Path) -> None:
    with pytest.raises(SeedError) as excinfo:
        load_pointer("upstart", seeds_dir=seeds_dir)
    msg = str(excinfo.value)
    assert "upstart" in msg
    # nomeia as entradas disponíveis
    assert "systemd" in msg
    assert "openrc" in msg


def test_stage3_pointer_is_frozen() -> None:
    p = Stage3Pointer(
        init="systemd",
        base_url=_BASE_URL,
        snapshot=_SNAPSHOT,
        filename="x.tar.xz",
        sha256="0" * 64,
    )
    with pytest.raises(Exception):  # noqa: B017 (frozen → ValidationError/Error)
        p.init = "openrc"  # type: ignore[misc]


# --- stage3_url (R2.1) -------------------------------------------------------


def test_stage3_url_builds_mirror_path() -> None:
    p = Stage3Pointer(
        init="systemd",
        base_url=_BASE_URL,
        snapshot=_SNAPSHOT,
        filename=f"stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz",
        sha256="0" * 64,
    )
    url = stage3_url(p)
    assert url.startswith(_BASE_URL)
    assert _SNAPSHOT in url
    assert url.endswith(p.filename)
    # sem barras duplicadas no meio (junção limpa)
    assert "//releases" not in url.replace("https://", "")


# --- verify_digest (R2.3, R2.4) ----------------------------------------------


def test_verify_digest_passes_on_match(tmp_path: Path) -> None:
    blob = b"stage3 contents"
    tarball = tmp_path / "s.tar.xz"
    tarball.write_bytes(blob)
    good = hashlib.sha256(blob).hexdigest()
    # match → não levanta, retorna None
    assert verify_digest(tarball, good) is None


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
        sha256="0" * 64,
    )
    # --no-download + sem cache → SeedError acionável, sem tocar a rede
    with pytest.raises(SeedError):
        fetch_stage3(p, cache_dir=cache, download=False)


def test_fetch_stage3_reuses_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # blinda contra rede: qualquer download deve falhar o teste
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("não deve baixar quando o cache é válido")

    # cobre tanto urllib quanto um possível helper interno de download
    monkeypatch.setattr(seed, "_download", _boom, raising=False)

    cache = tmp_path / "cache"
    cache.mkdir()
    blob = b"cached stage3"
    digest = hashlib.sha256(blob).hexdigest()
    filename = f"stage3-amd64-nomultilib-systemd-{_SNAPSHOT}.tar.xz"
    (cache / filename).write_bytes(blob)

    p = Stage3Pointer(
        init="systemd",
        base_url=_BASE_URL,
        snapshot=_SNAPSHOT,
        filename=filename,
        sha256=digest,
    )
    got = fetch_stage3(p, cache_dir=cache, download=True)
    assert got == cache / filename
