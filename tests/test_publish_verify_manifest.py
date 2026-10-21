"""``publish_pool.verify`` also checks the clear-signed ``Manifest`` (story 029, task 8.2; R6.5).

Contract (story.md R6.5, design.md "shidashi/publish_pool.py"): besides every ``.sig`` member,
the gpkg's ``Manifest`` member must be clear-signed by ``pubkey`` alone; a missing, unsigned,
invalid or other-key ``Manifest`` is a named failure in the returned list -- never an
exception, never a pass. The ``.sig`` checks keep running beside it.

What the fixtures imitate (portage/gpkg.py, read on the host): ``gpkg-sign`` builds
``<base>/gpkg-1``, ``<base>/metadata.tar.gz`` and ``<base>/image.tar.gz``, a detached armored
``.sig`` for the last two, and ``_add_manifest`` writes ``<base>/Manifest`` = one line
``DATA <basename> <size> SHA512 <hex> BLAKE2B <hex>`` per member (the ``.sig`` members and
``gpkg-1`` included, ``Manifest`` itself excluded) passed through ``gpg --clear-sign``.
``_verify_binpkg`` reads the ``Manifest`` member into memory and runs
``gpg --verify --output - -`` on it, so a missing Manifest, a plain-text one, a signature
that does not match the text or a signature by an untrusted key is rejected.

Real ``gpg`` with throwaway keys (never committed, Q7); its own gpkg builder -- the builder of
tests/test_publish_verify.py is a different file's.
"""

import hashlib
import io
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import
from tests.test_binhost_cli_show import GEN, Lab, binhost, make_lab
from tests.test_binhost_refs import HEADER, entry
from tests.test_publish_verify import Keys, _gpg, _new_key

verify: Any = try_import("shidashi.publish_pool", "verify")

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgconf") is None,
    reason="needs gpg and gpgconf on the host: the signatures are real",
)


@pytest.fixture(scope="module")
def keys() -> Iterator[Keys]:
    home = Path(tempfile.mkdtemp(prefix="gpgt-"))
    home.chmod(0o700)
    out = Path(tempfile.mkdtemp(prefix="gpgp-"))
    try:
        signer = _new_key(home, "Shidashi Manifest Signer <signer@example.invalid>")
        stranger = _new_key(home, "Someone Else <stranger@example.invalid>")
        pubkey = out / "signer.asc"
        pubkey.write_bytes(_gpg(home, "--armor", "--export", signer))
        yield Keys(home, signer, stranger, pubkey)
    finally:
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def _clearsign(keys: Keys, signer: str, text: bytes, digest: str = "SHA512") -> bytes:
    return _gpg(
        keys.home, "--local-user", signer, "--digest-algo", digest, "--clear-sign", data=text
    )


def _detached(keys: Keys, signer: str, data: bytes) -> bytes:
    return _gpg(keys.home, "--local-user", signer, "--armor", "--detach-sign", data=data)


def _data_line(name: str, data: bytes) -> str:
    sha512 = hashlib.sha512(data).hexdigest()
    blake2b = hashlib.blake2b(data).hexdigest()
    return f"DATA {name} {len(data)} SHA512 {sha512} BLAKE2B {blake2b}\n"


# the ways a Manifest can be: the first three are valid (hostile half: still accepted)
VALID_MANIFESTS = ("good", "good-sha256", "good-padded")
BAD_MANIFESTS = (
    "man-missing",  # no Manifest member at all
    "man-unsigned",  # the DATA lines, no signature
    "man-tampered",  # clear-signed, then a DATA line altered
    "man-stranger",  # clear-signed by a key that is not the given one
    "man-detached",  # a detached armored signature where the clear-signed text belongs
    "man-empty",  # zero bytes
)
BAD_SIGS = ("sig-stranger", "sig-tampered")  # valid Manifest, one bad .sig


def make_gpkg(path: Path, keys: Keys, how: str) -> None:
    """A gpkg at ``path`` in Portage's layout; ``how`` is one of the kinds above, or
    ``both-bad`` (a stranger-signed image .sig AND an unsigned Manifest)."""
    base = path.name.removesuffix(".gpkg.tar")
    metadata, image = f"metadata of {base}\n".encode(), f"image of {base}\n".encode()
    stranger_image = how in ("sig-stranger", "both-bad")
    members: dict[str, bytes] = {
        "gpkg-1": b"",
        "metadata.tar.gz": metadata,
        "metadata.tar.gz.sig": _detached(keys, keys.signer, metadata),
        "image.tar.gz": image + (b"changed after signing\n" if how == "sig-tampered" else b""),
        "image.tar.gz.sig": _detached(
            keys, keys.stranger if stranger_image else keys.signer, image
        ),
    }
    text = "".join(_data_line(name, data) for name, data in members.items())
    signed = text.encode()
    manifest: bytes | None
    if how in ("man-unsigned", "both-bad"):
        manifest = signed
    elif how == "man-missing":
        manifest = None
    elif how == "man-tampered":
        good = _clearsign(keys, keys.signer, signed)
        manifest = good.replace(b"DATA metadata.tar.gz ", b"DATA metadata.tar.gz  ", 1)
        assert manifest != good, "the fixture must really alter the signed text"
    elif how == "man-stranger":
        manifest = _clearsign(keys, keys.stranger, signed)
    elif how == "man-detached":
        manifest = _detached(keys, keys.signer, signed)
    elif how == "man-empty":
        manifest = b""
    elif how == "good-sha256":
        manifest = _clearsign(keys, keys.signer, signed, digest="SHA256")
    elif how == "good-padded":
        manifest = _clearsign(keys, keys.signer, signed + b"\n\n")
    else:  # good, sig-stranger, sig-tampered
        manifest = _clearsign(keys, keys.signer, signed)
    if manifest is not None:
        members["Manifest"] = manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"{base}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _rel(how: str) -> str:
    return f"app-misc/{how}/{how}-1.0-1.gpkg.tar"


def pool_of(dest: Path, keys: Keys, *kinds: str) -> None:
    for how in kinds:
        make_gpkg(dest / _rel(how), keys, how)
    blocks = [entry(f"app-misc/{how}-1.0", 1, path=_rel(how)) for how in kinds]
    (dest / "Packages").write_text("\n".join([HEADER, *blocks]), encoding="utf-8")


def _about(failures: list[str], how: str) -> list[str]:
    return [f for f in failures if _rel(how) in f]


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# --- the Manifest ------------------------------------------------------------------------


@pytest.mark.parametrize("how", BAD_MANIFESTS)
def test_a_bad_manifest_is_a_failure_named_for_the_gpkg_and_the_manifest(
    tmp_path: Path, keys: Keys, how: str
) -> None:
    """R6.5, hostile halves first: each way a Manifest can be unacceptable is named, with
    every ``.sig`` valid -- so the failure is the Manifest's alone."""
    pool_of(tmp_path, keys, how)
    failures = verify(tmp_path, pubkey=keys.pubkey)
    assert failures, f"{how}: verified as if the Manifest were fine"
    assert _about(failures, how), f"{how}: the gpkg is not named: {failures}"
    assert all("Manifest" in f for f in failures), f"{how}: not about the Manifest: {failures}"


@pytest.mark.parametrize("how", VALID_MANIFESTS)
def test_a_validly_clear_signed_manifest_passes_however_it_was_signed(
    tmp_path: Path, keys: Keys, how: str
) -> None:
    """The converse: a Manifest Portage accepts is not refused -- another digest algorithm
    or trailing blank lines in the signed text are still the signer's Manifest."""
    pool_of(tmp_path, keys, how)
    assert verify(tmp_path, pubkey=keys.pubkey) == []


def test_one_bad_manifest_among_good_gpkgs_is_the_only_one_named(
    tmp_path: Path, keys: Keys
) -> None:
    pool_of(tmp_path, keys, *VALID_MANIFESTS, "man-unsigned", "man-stranger")
    failures = verify(tmp_path, pubkey=keys.pubkey)
    assert {how for how in (*VALID_MANIFESTS, *BAD_MANIFESTS) if _about(failures, how)} == {
        "man-unsigned",
        "man-stranger",
    }, failures


def test_every_gpkg_is_judged_by_its_own_manifest(tmp_path: Path, keys: Keys) -> None:
    """Wrongly-collapse half: a good Manifest in one gpkg never vouches for a gpkg whose
    own Manifest is missing -- each is named separately."""
    pool_of(tmp_path, keys, "good", "man-missing", "man-empty")
    failures = verify(tmp_path, pubkey=keys.pubkey)
    assert _about(failures, "man-missing") and _about(failures, "man-empty"), failures
    assert not _about(failures, "good"), failures


# --- the .sig checks keep running --------------------------------------------------------


@pytest.mark.parametrize("how", BAD_SIGS)
def test_a_bad_sig_is_still_named_beside_a_valid_manifest_and_the_manifest_is_not(
    tmp_path: Path, keys: Keys, how: str
) -> None:
    """R6.5 adds to R6.2, never replaces it; and a good Manifest is not reported because a
    ``.sig`` is bad."""
    pool_of(tmp_path, keys, how)
    failures = verify(tmp_path, pubkey=keys.pubkey)
    assert _about(failures, how), f"{how}: {failures}"
    assert not any("Manifest" in f for f in failures), failures


def test_a_bad_sig_and_a_bad_manifest_in_one_gpkg_are_both_reported(
    tmp_path: Path, keys: Keys
) -> None:
    """The Manifest check does not stop at, nor hide behind, the first ``.sig`` failure."""
    pool_of(tmp_path, keys, "both-bad")
    failures = _about(verify(tmp_path, pubkey=keys.pubkey), "both-bad")
    assert any("Manifest" in f for f in failures), failures
    assert any("image.tar.gz.sig" in f for f in failures), failures


# --- only the given key is trusted -------------------------------------------------------


def test_the_manifest_is_judged_against_the_given_key_alone(tmp_path: Path, keys: Keys) -> None:
    """A pubkey file of the stranger makes the signer's Manifest (and signatures) a
    failure."""
    pool_of(tmp_path, keys, "good")
    other = tmp_path / "stranger.asc"
    other.write_bytes(_gpg(keys.home, "--armor", "--export", keys.stranger))
    failures = verify(tmp_path, pubkey=other)
    assert any("Manifest" in f for f in _about(failures, "good")), failures


def test_the_verifiers_own_keyring_never_vouches_for_a_manifest(
    tmp_path: Path, keys: Keys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both keys sit in the keyring the environment points gpg at; the stranger's Manifest
    is still refused, the signer's still accepted -- trust is the given file alone."""
    monkeypatch.setenv("GNUPGHOME", str(keys.home))
    pool_of(tmp_path, keys, "good", "man-stranger")
    failures = verify(tmp_path, pubkey=keys.pubkey)
    assert _about(failures, "man-stranger"), failures
    assert not _about(failures, "good"), failures


# --- reading, not extracting -------------------------------------------------------------


def test_verifying_writes_nothing_to_the_pool_nor_the_working_directory(
    tmp_path: Path, keys: Keys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Manifest is read from the tar into memory, never extracted."""
    pool, cwd = tmp_path / "pool", tmp_path / "cwd"
    cwd.mkdir()
    pool_of(pool, keys, "good", "man-unsigned")
    monkeypatch.chdir(cwd)
    before = _tree(pool)
    verify(pool, pubkey=keys.pubkey)
    assert _tree(pool) == before
    assert list(cwd.iterdir()) == []


# --- the command -------------------------------------------------------------------------


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return make_lab(tmp_path, monkeypatch)


def test_the_verify_command_exits_1_naming_a_gpkg_whose_manifest_is_bad(
    lab: Lab, keys: Keys
) -> None:
    """R6.5 through the CLI on the default publish pool, good gpkg beside the bad one."""
    dest = lab.cache / "publish" / "v3" / GEN
    pool_of(dest, keys, "good")
    ok = binhost("verify", "v3", "--pubkey", str(keys.pubkey))
    assert ok.exit_code == 0, ok.output

    pool_of(dest, keys, "good", "man-unsigned")
    bad = binhost("verify", "v3", "--pubkey", str(keys.pubkey))
    assert bad.exit_code == 1, bad.output
    flat = "".join(bad.output.split())
    assert "man-unsigned-1.0-1.gpkg.tar" in flat and "Manifest" in flat
    assert "good-1.0-1.gpkg.tar" not in flat
