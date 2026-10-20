"""``publish_pool.verify`` and ``shidashi binhost verify`` (story 029, task 5.3; R6.1-R6.4).

Contract from design.md "shidashi/publish_pool.py": ``verify(dest, *, pubkey)`` reads
``dest/Packages``, opens each gpkg it names and checks every ``.sig`` member against the
member it signs, with ``gpg --verify`` in a throwaway home holding only ``pubkey``; returns
the failures (empty = every gpkg signed and valid). Fail-closed: a gpkg with no signature,
a missing ``Packages``, a block whose PATH ``parse_refs`` cannot read, no entry at all, a
missing gpkg and a non-tar gpkg are each a failure in the list -- never an exception, never
a pass.

Real ``gpg`` with throwaway keys generated in a temporary home (never committed, Q7) and
hand-built gpkg files in Portage's layout: ``<base>/gpkg-1``, ``<base>/metadata.tar.gz``
and ``<base>/image.tar.gz``, each with a detached armored ``.sig``, and a clear-signed
``<base>/Manifest`` -- as ``gpkg-sign`` writes them (portage/gpkg.py). Skipped, with the
reason, on a host without ``gpg``.
"""

import io
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import
from tests.test_binhost_cli_show import GEN, Lab, binhost, make_lab
from tests.test_binhost_refs import HEADER, entry

BinhostError: Any = try_import("shidashi.binhost", "BinhostError")
verify: Any = try_import("shidashi.publish_pool", "verify")

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgconf") is None,
    reason="needs gpg and gpgconf on the host: the signatures are real",
)


@dataclass(frozen=True)
class Keys:
    home: Path
    signer: str
    stranger: str
    pubkey: Path  # the signer's public key alone, armored


def _gpg(home: Path, *args: str, data: bytes | None = None) -> bytes:
    done = subprocess.run(
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--no-tty",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            *args,
        ],
        input=data,
        capture_output=True,
        check=True,
    )
    return done.stdout


def _new_key(home: Path, uid: str) -> str:
    _gpg(home, "--quick-gen-key", uid, "ed25519", "sign", "never")
    listing = _gpg(home, "--with-colons", "--list-secret-keys", uid).decode()
    return next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))


@pytest.fixture(scope="module")
def keys() -> Iterator[Keys]:
    home = Path(tempfile.mkdtemp(prefix="gpgt-"))
    home.chmod(0o700)
    out = Path(tempfile.mkdtemp(prefix="gpgp-"))
    try:
        signer = _new_key(home, "Shidashi Test Signer <signer@example.invalid>")
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


def _detached(keys: Keys, signer: str, data: bytes) -> bytes:
    return _gpg(keys.home, "--local-user", signer, "--armor", "--detach-sign", data=data)


def _clearsigned(keys: Keys, signer: str, text: bytes) -> bytes:
    return _gpg(keys.home, "--local-user", signer, "--clearsign", data=text)


def make_gpkg(path: Path, keys: Keys, how: str) -> None:
    """A gpkg at ``path``. ``how``: ``good``, ``unsigned``, ``stranger`` (the image signed
    by another key), ``tampered`` (the image changed after it was signed) or ``swapped``
    (the image's .sig is the metadata's valid signature)."""
    base = path.name.removesuffix(".gpkg.tar")
    metadata, image = f"metadata of {base}\n".encode(), f"image of {base}\n".encode()
    members: dict[str, bytes] = {"gpkg-1": b"", "metadata.tar.gz": metadata}
    if how != "unsigned":
        members["metadata.tar.gz.sig"] = _detached(keys, keys.signer, metadata)
    members["image.tar.gz"] = image + (b"changed after signing\n" if how == "tampered" else b"")
    if how != "unsigned":
        image_sig = {
            "stranger": _detached(keys, keys.stranger, image),
            "swapped": members["metadata.tar.gz.sig"],
        }.get(how) or _detached(keys, keys.signer, image)
        members["image.tar.gz.sig"] = image_sig
    manifest = "".join(f"DATA {name} {len(data)}\n" for name, data in members.items()).encode()
    members["Manifest"] = (
        manifest if how == "unsigned" else _clearsigned(keys, keys.signer, manifest)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"{base}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


KINDS = ("good", "unsigned", "stranger", "tampered", "swapped")


def _cpv(how: str) -> str:
    return f"app-misc/{how}-1.0"


def _rel(how: str) -> str:
    return f"app-misc/{how}/{how}-1.0-1.gpkg.tar"


def publish_pool_of(dest: Path, keys: Keys, *kinds: str) -> None:
    """A publish pool: one gpkg per kind and the ``Packages`` that names them."""
    for how in kinds:
        make_gpkg(dest / _rel(how), keys, how)
    blocks = [entry(_cpv(how), 1, path=_rel(how)) for how in kinds]
    (dest / "Packages").write_text("\n".join([HEADER, *blocks]), encoding="utf-8")


def _named(failures: list[str]) -> set[str]:
    return {how for how in KINDS if any(f"{how}-1.0-1.gpkg.tar" in f for f in failures)}


def test_every_unsigned_or_invalid_gpkg_is_named(tmp_path: Path, keys: Keys) -> None:
    """R6.2, hostile halves first: no signature at all, a signature by another key, a
    member changed after signing, a valid signature of ANOTHER member -- each named; the
    good gpkg beside them is not."""
    publish_pool_of(tmp_path, keys, *KINDS)
    failures = verify(tmp_path, pubkey=keys.pubkey)
    assert _named(failures) == {"unsigned", "stranger", "tampered", "swapped"}, failures


def test_a_pool_of_valid_signatures_verifies_and_one_bad_gpkg_is_the_only_one_named(
    tmp_path: Path, keys: Keys
) -> None:
    """R6.1: all valid -> no failure; then one stranger among them is named alone."""
    good = tmp_path / "good"
    publish_pool_of(good, keys, "good")
    assert verify(good, pubkey=keys.pubkey) == []
    mixed = tmp_path / "mixed"
    publish_pool_of(mixed, keys, "good", "stranger")
    failures = verify(mixed, pubkey=keys.pubkey)
    assert len(failures) == 1 and _named(failures) == {"stranger"}, failures


def test_only_the_given_public_key_is_trusted(tmp_path: Path, keys: Keys) -> None:
    """The key in the verifier's own keyring is not the one judged: a pubkey file of the
    stranger makes every signer-made signature a failure."""
    publish_pool_of(tmp_path, keys, "good")
    other = tmp_path / "stranger.asc"
    other.write_bytes(_gpg(keys.home, "--armor", "--export", keys.stranger))
    assert _named(verify(tmp_path, pubkey=other)) == {"good"}


def test_a_missing_public_key_file_never_passes(tmp_path: Path, keys: Keys) -> None:
    """Fail-closed: an error, or every gpkg named -- never an empty list."""
    publish_pool_of(tmp_path, keys, "good")
    try:
        failures = verify(tmp_path, pubkey=tmp_path / "no-such-key.asc")
    except Exception as err:
        assert isinstance(err, BinhostError), err
    else:
        assert _named(failures) == {"good"}, failures


@pytest.mark.parametrize(
    "damage", ["no-packages", "unreadable-block", "no-entry", "missing-gpkg", "not-a-tar"]
)
def test_a_pool_that_cannot_be_checked_is_a_failure_never_a_pass_nor_an_exception(
    tmp_path: Path, keys: Keys, damage: str
) -> None:
    """R6.3, R6.4: fail-closed -- what cannot be checked is reported, by name."""
    publish_pool_of(tmp_path, keys, "good")
    index, gpkg = tmp_path / "Packages", tmp_path / _rel("good")
    if damage == "no-packages":
        index.unlink()
    elif damage == "unreadable-block":
        odd = "BUILD_ID: x\nCPV: app-misc/odd-1.0\nPATH: app-misc/odd/odd-1.0-x.gpkg.tar\n"
        index.write_text(index.read_text(encoding="utf-8") + "\n" + odd, encoding="utf-8")
    elif damage == "no-entry":
        index.write_text(HEADER, encoding="utf-8")
    elif damage == "missing-gpkg":
        gpkg.unlink()
    else:
        gpkg.write_bytes(b"this is not a tar archive\n" * 40)
    failures = verify(tmp_path, pubkey=keys.pubkey)
    assert failures, f"{damage}: verified as if nothing were wrong"
    said = " ".join(failures)
    expected = {
        "no-packages": "Packages",
        "unreadable-block": "odd-1.0-x.gpkg.tar",
        "no-entry": "Packages",
        "missing-gpkg": "good-1.0-1.gpkg.tar",
        "not-a-tar": "good-1.0-1.gpkg.tar",
    }[damage]
    assert expected in said, failures


# --- the command ----------------------------------------------------------------------------


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return make_lab(tmp_path, monkeypatch)


def test_the_verify_command_exits_0_on_a_valid_pool_and_1_naming_each_failure(
    lab: Lab, keys: Keys
) -> None:
    """R6.1, R6.2 through the CLI, on the default publish pool of the generation."""
    dest = lab.cache / "publish" / "v3" / GEN
    publish_pool_of(dest, keys, "good")
    ok = binhost("verify", "v3", "--pubkey", str(keys.pubkey))
    assert ok.exit_code == 0, ok.output

    publish_pool_of(dest, keys, "good", "unsigned", "tampered")
    bad = binhost("verify", "v3", "--pubkey", str(keys.pubkey))
    assert bad.exit_code == 1, bad.output
    flat = "".join(bad.output.split())
    assert "unsigned-1.0-1.gpkg.tar" in flat and "tampered-1.0-1.gpkg.tar" in flat
    assert "good-1.0-1.gpkg.tar" not in flat


def test_the_verify_command_fails_a_pool_without_an_index(lab: Lab, keys: Keys) -> None:
    """R6.4 through the CLI: exit 1 saying what is missing, never a pass."""
    dest = lab.root / "empty-pool"
    dest.mkdir()
    result = binhost("verify", "v3", "--pubkey", str(keys.pubkey), "--dest", str(dest))
    assert result.exit_code == 1, result.output
    assert "Packages" in result.output


def test_the_verify_command_reads_another_destination(lab: Lab, keys: Keys) -> None:
    dest = lab.root / "elsewhere"
    publish_pool_of(dest, keys, "stranger")
    result = binhost("verify", "v3", "--pubkey", str(keys.pubkey), "--dest", str(dest))
    assert result.exit_code == 1
    assert "stranger-1.0-1.gpkg.tar" in "".join(result.output.split())
