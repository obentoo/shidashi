"""``sign_release`` + ``verify_release`` with a real gpg (story 030, task 7.1; R6.1-R6.5,
Q7, Q12).

A throwaway ed25519 key (no passphrase) generated at run time in a temporary
``GNUPGHOME`` -- never committed -- next to a second secret key, the "stranger", so an
export of the wrong key or of every key would show. The release directory holds two
ISOs' DIGESTS, a pointer file, ``release.json`` and their ``SHA256SUMS``. ``gpg`` runs as
this user (no ``sudo``), with ``GNUPGHOME`` pointing at the temporary home, so the
maintainer's own keyring is never touched.

Then, as a user who downloads the release would: in a fresh home holding only
``bentoo-release-key.asc``, ``gpg --verify SHA256SUMS.asc SHA256SUMS`` and each DIGESTS
signature pass, and ``sha256sum -c SHA256SUMS`` passes. Skipped, with the reason, on a host
without ``gpg``.
"""

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shidashi import publish
from tests._pending import try_import

sign_release: Any = try_import("shidashi.release", "sign_release")
verify_release: Any = try_import("shidashi.release", "verify_release")

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgconf") is None,
    reason="needs gpg and gpgconf on the host: the signatures are real",
)

KEY_FILE = "bentoo-release-key.asc"
DAY = "2026.10.25"


@dataclass(frozen=True)
class Keys:
    home: Path
    signer: str
    stranger: str


def _gpg(home: Path, *args: str) -> bytes:
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
        capture_output=True,
        check=True,
    )
    return done.stdout


def _new_key(home: Path, uid: str) -> str:
    _gpg(home, "--quick-gen-key", uid, "ed25519", "sign", "never")
    listing = _gpg(home, "--with-colons", "--list-secret-keys", uid).decode()
    return next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))


def _kill_agent(home: Path) -> None:
    subprocess.run(
        ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
        capture_output=True,
        check=False,
    )


@pytest.fixture(scope="module")
def keys() -> Iterator[Keys]:
    # a short path: the agent's socket lives in the home and AF_UNIX paths are short
    home = Path(tempfile.mkdtemp(prefix="gpgr-", dir="/tmp"))
    home.chmod(0o700)
    try:
        signer = _new_key(home, "Shidashi Release Test <release@example.invalid>")
        stranger = _new_key(home, "Someone Else <stranger@example.invalid>")
        yield Keys(home, signer, stranger)
    finally:
        _kill_agent(home)
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def maintainer_home(keys: Keys, monkeypatch: pytest.MonkeyPatch) -> Keys:
    """gpg finds the throwaway keyring and agent through GNUPGHOME; no sudo."""
    monkeypatch.setenv("GNUPGHOME", str(keys.home))
    monkeypatch.delenv("SUDO_USER", raising=False)
    return keys


def release_tree(directory: Path) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    isos = [
        f"bentoo-{DAY}-minimal-systemd-v3.iso",
        f"bentoo-{DAY}-kde-systemd-v3-xz.iso",
    ]
    sums: dict[str, str] = {}
    for name in isos:
        iso = directory / name
        iso.write_bytes(f"iso {name}\n".encode() * 64)
        digests, sha256 = publish.write_digests(iso)
        sums[name] = sha256
        sums[digests.name] = publish.sha256_of(digests)
    pointer = directory / "latest-minimal-systemd-v3.txt"
    pointer.write_text(f"{isos[0]}\n", encoding="utf-8")
    sums[pointer.name] = publish.sha256_of(pointer)
    manifest = directory / "release.json"
    manifest.write_text('{\n "date": "2026-10-25"\n}\n', encoding="utf-8")
    sums[manifest.name] = publish.sha256_of(manifest)
    publish.update_sha256sums(directory, sums)
    directory.chmod(0o755)
    return isos


class DownloadingUser:
    """Someone with only ``bentoo-release-key.asc`` imported, in a home of their own."""

    def __init__(self, pubkey: Path) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="gpgu-", dir="/tmp"))
        self.home.chmod(0o700)
        subprocess.run(
            ["gpg", "--homedir", str(self.home), "--batch", "--import", str(pubkey)],
            capture_output=True,
            check=True,
        )

    def keys(self) -> list[str]:
        listing = subprocess.run(
            ["gpg", "--homedir", str(self.home), "--batch", "--with-colons", "--list-keys"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]

    def verify(self, signature: Path, data: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "gpg",
                "--homedir",
                str(self.home),
                "--batch",
                "--status-fd",
                "1",
                "--verify",
                str(signature),
                str(data),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def close(self) -> None:
        _kill_agent(self.home)
        shutil.rmtree(self.home, ignore_errors=True)


@pytest.fixture
def signed(tmp_path: Path, maintainer_home: Keys) -> Iterator[tuple[Path, list[str]]]:
    directory = tmp_path / DAY
    isos = release_tree(directory)
    sign_release(directory, key=maintainer_home.signer)
    yield directory, isos


def test_a_signed_release_verifies_against_the_exported_key_alone(
    signed: tuple[Path, list[str]], maintainer_home: Keys
) -> None:
    """R6.1, R6.2, R6.5, Q12."""
    directory, isos = signed
    assert verify_release(directory, key=maintainer_home.signer) == []
    pubkey = directory / KEY_FILE
    assert pubkey.is_file(), "the public key was not exported"
    user = DownloadingUser(pubkey)
    try:
        assert user.keys() == [maintainer_home.signer], "the export is not the --key alone"
        for data in [directory / f"{iso}.DIGESTS" for iso in isos] + [directory / "SHA256SUMS"]:
            asc = data.with_name(data.name + ".asc")
            assert asc.is_file(), f"{asc.name} was not written"
            assert asc.read_text(encoding="ascii").startswith("-----BEGIN PGP SIGNATURE-----")
            checked = user.verify(asc, data)
            assert checked.returncode == 0, checked.stderr
            assert f"VALIDSIG {maintainer_home.signer}" in checked.stdout
    finally:
        user.close()


def test_sha256sum_checks_every_line_including_the_key_and_release_json(
    signed: tuple[Path, list[str]], maintainer_home: Keys
) -> None:
    """R6.1, R6.3, Q12: ``sha256sum -c SHA256SUMS`` -- what the README tells a user to run."""
    directory, _isos = signed
    verify_release(directory, key=maintainer_home.signer)
    text = (directory / "SHA256SUMS").read_text(encoding="utf-8")
    assert f"  {KEY_FILE}\n" in text and "  release.json\n" in text
    assert ".asc\n" not in text.replace(f"  {KEY_FILE}\n", "")
    if shutil.which("sha256sum") is None:
        pytest.skip("no sha256sum on this host")
    checked = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_a_sha256sums_changed_after_signing_fails_verify_release(
    signed: tuple[Path, list[str]], maintainer_home: Keys
) -> None:
    """R6.5: the real gpg says BAD; verify_release names SHA256SUMS."""
    directory, _isos = signed
    assert verify_release(directory, key=maintainer_home.signer) == []
    with (directory / "SHA256SUMS").open("a", encoding="utf-8") as sums:
        sums.write("0" * 64 + "  extra.iso\n")
    failures = verify_release(directory, key=maintainer_home.signer)
    assert failures and any("SHA256SUMS" in f for f in failures), failures


def test_no_secret_key_material_is_left_in_the_release_directory(
    signed: tuple[Path, list[str]], maintainer_home: Keys
) -> None:
    """Q7, Constraints: the private key never leaves the agent."""
    directory, _isos = signed
    verify_release(directory, key=maintainer_home.signer)
    assert (directory / KEY_FILE).is_file()
    for path in directory.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"PRIVATE KEY" not in data, path
    exported = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--show-keys", str(directory / KEY_FILE)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert not [line for line in exported.splitlines() if line.startswith("sec")], exported
    assert maintainer_home.stranger not in exported
