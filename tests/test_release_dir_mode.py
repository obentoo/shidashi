"""The release directory, ``.signing/`` and ``SHA256SUMS`` keep their modes under any umask
(story 030, task 10.2).

The revalidation of 2026-10-22 found that ``mkdir(mode=...)`` and ``write_text`` are masked
by the umask: under ``sudo`` with umask 077, root creates the release directory and the
signing stage 0700 and ``SHA256SUMS`` 0600, so the maintainer's gpg (run as ``SUDO_USER``,
R6.4) cannot read what it must sign, and a non-root ``sha256sum -c`` cannot read the list.
A directory left narrow by an earlier run must be widened too, not only a new one.
"""

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import audit, publish, release_sign
from shidashi.release_sign import RELEASE_DIR_MODE, STAGE_DIR, claim_release_dir, sign_and_verify

#: What every published file of a release directory is: readable by everyone (as in 9.1).
PUBLISHED_FILE_MODE = 0o644
#: Two well-formed digests: ``SHA256SUMS`` lines are ``<64 lowercase hex>  <name>``.
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


@pytest.fixture
def strict_umask() -> Iterator[None]:
    # the most restrictive umask root could run with under sudo: nothing for group or others
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.usefixtures("strict_umask")
def test_a_new_release_directory_gets_the_release_dir_mode(tmp_path: Path) -> None:
    dest = tmp_path / "releases" / "2026.10.22"
    with claim_release_dir(dest):
        assert _mode(dest) == RELEASE_DIR_MODE


@pytest.mark.usefixtures("strict_umask")
def test_an_existing_narrower_release_directory_is_widened(tmp_path: Path) -> None:
    dest = tmp_path / "2026.10.22"
    dest.mkdir()
    dest.chmod(0o700)  # what an earlier run under umask 077 left
    with claim_release_dir(dest):
        assert _mode(dest) == RELEASE_DIR_MODE


@pytest.mark.usefixtures("strict_umask")
def test_the_signing_stage_gets_the_release_dir_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gpg stays out: the stage's mode is observed where the maintainer's gpg would read it,
    # inside the signing step, before the stage is cleared away.
    seen: dict[str, int] = {}

    def observing_sign(directory: Path, *, key: str, stage: Path | None = None) -> list[Path]:
        del directory, key
        assert stage is not None
        seen["stage"] = _mode(stage)
        return []

    def refusing_verify(
        directory: Path, *, key: str, sums_signature: Path | None = None
    ) -> list[str]:
        del directory, key, sums_signature
        return ["fake verifier: nothing was signed"]

    monkeypatch.setattr(release_sign, "sign_release", observing_sign)
    monkeypatch.setattr(release_sign, "verify_release", refusing_verify)
    dest = tmp_path / "2026.10.22"
    dest.mkdir()
    dest.chmod(RELEASE_DIR_MODE)

    failures = sign_and_verify(audit.Recorder(), dest, "0x" + "F" * 40)

    assert failures == ["fake verifier: nothing was signed"]
    assert seen["stage"] == RELEASE_DIR_MODE
    assert not (dest / STAGE_DIR).exists()


@pytest.mark.usefixtures("strict_umask")
def test_sha256sums_is_0644_after_the_first_write(tmp_path: Path) -> None:
    sums = publish.update_sha256sums(tmp_path, {"release.json": DIGEST_A})
    assert _mode(sums) == PUBLISHED_FILE_MODE


@pytest.mark.usefixtures("strict_umask")
def test_sha256sums_stays_0644_after_an_update(tmp_path: Path) -> None:
    publish.update_sha256sums(tmp_path, {"release.json": DIGEST_A})
    sums = publish.update_sha256sums(tmp_path, {"bentoo-release-key.asc": DIGEST_B})
    assert sums.read_text("utf-8") == (
        f"{DIGEST_B}  bentoo-release-key.asc\n{DIGEST_A}  release.json\n"
    )
    assert _mode(sums) == PUBLISHED_FILE_MODE
