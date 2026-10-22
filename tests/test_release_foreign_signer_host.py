"""``verify_release`` with a real gpg when ``bentoo-release-key.asc`` holds a second key
(story 030, task 8.3; R6.5).

Commits the check task 4.3 made by hand. A key file holding both the signer's and a
stranger's public key verifies the stranger's signatures too, so a good signature alone
is no proof it is the ``--key``'s: ``verify_release`` must name every ``.asc`` the
stranger made, and must not flag the signer's own signatures because the key file holds
another key.

The throwaway keys, the release tree and the signed directory are the fixtures of
``tests/test_release_sign_host.py`` (task 7.1), imported -- that file stays frozen. Skipped,
with the reason, on a host without ``gpg`` and ``gpgconf``.
"""

import shutil
from pathlib import Path

import pytest

from shidashi import publish
from shidashi.release import verify_release
from tests.test_release_sign_host import (
    KEY_FILE,
    Keys,
    _gpg,
    keys,
    maintainer_home,
    signed,
)

# imported fixtures: pytest registers them for this module by their names
__all__ = ["keys", "maintainer_home", "signed"]

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgconf") is None,
    reason="needs gpg and gpgconf on the host: the signatures are real",
)


def detach_sign(home: Path, key: str, data: Path) -> Path:
    """Sign ``data`` with ``key`` into ``<data>.asc``, replacing what was there."""
    asc = data.with_name(f"{data.name}.asc")
    _gpg(
        home,
        "--yes",
        "--armor",
        "--local-user",
        key,
        "--output",
        str(asc),
        "--detach-sign",
        str(data),
    )
    return asc


def hold_both_keys(directory: Path, keys: Keys) -> None:
    """Replace the exported key file by one holding the signer AND the stranger, keep its
    ``SHA256SUMS`` line right and sign ``SHA256SUMS`` again with the signer -- so the only
    thing a check could object to is which key made a signature."""
    key_file = directory / KEY_FILE
    key_file.write_bytes(_gpg(keys.home, "--armor", "--export", keys.signer, keys.stranger))
    publish.update_sha256sums(directory, {KEY_FILE: publish.sha256_of(key_file)})
    detach_sign(keys.home, keys.signer, directory / "SHA256SUMS")


def two_key_listing(home: Path, key_file: Path) -> list[str]:
    listing = _gpg(home, "--with-colons", "--show-keys", str(key_file)).decode()
    return [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]


def test_a_digests_signed_by_the_stranger_is_one_failure_naming_its_asc(
    signed: tuple[Path, list[str]], maintainer_home: Keys
) -> None:
    """Hostile half first: the stranger's key is in the key file, so gpg calls the
    signature good -- the release must still refuse it."""
    directory, isos = signed
    hold_both_keys(directory, maintainer_home)
    assert set(two_key_listing(maintainer_home.home, directory / KEY_FILE)) == {
        maintainer_home.signer,
        maintainer_home.stranger,
    }
    foreign = detach_sign(
        maintainer_home.home, maintainer_home.stranger, directory / f"{isos[0]}.DIGESTS"
    )

    failures = verify_release(directory, key=maintainer_home.signer)

    assert len(failures) == 1, failures
    assert failures[0].startswith(f"{foreign.name}: "), failures
    assert maintainer_home.stranger in failures[0], failures


def test_every_signature_by_the_signer_passes_though_the_key_file_holds_two_keys(
    signed: tuple[Path, list[str]], maintainer_home: Keys
) -> None:
    """The converse: a second key in the key file is not, by itself, a failure."""
    directory, _isos = signed
    hold_both_keys(directory, maintainer_home)

    assert verify_release(directory, key=maintainer_home.signer) == []
