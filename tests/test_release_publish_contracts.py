"""Producer -> consumer contract of a weekly release (story 033, task 5.1; R1.1, R3.1, R6.1
rehearsed off-host, Q13).

Both sides real: story 032's ``shidashi release --unsigned`` writes the release directory
(its child builds are ``FakeChildBuilds``, plus the run bundle a real build lists beside each
ISO), this story's ``release sign`` signs it, ``upload_set`` and ``release upload`` publish it
to ``FakeRclone``. The fields are read from the manifest 032 wrote, never from a copy here:
``signed`` and ``variants[].isos``.

Two runs of the same chain: one with ``FakeMaintainerGpg``, which runs everywhere, and one
with a real gpg in a throwaway ``GNUPGHOME`` (skipped without ``gpg``), which then checks the
bucket as a user who downloads it would: ``gpg --verify SHA256SUMS.asc SHA256SUMS`` with only
the published key imported, and ``sha256sum -c --ignore-missing SHA256SUMS`` (the bucket does
not carry the run bundles SHA256SUMS lists).

Kept apart from tests/test_release_contracts.py (story 030's, frozen); tasks.md 5.1's
Validation names that file, every test here carries ``upload`` in its name.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli, gpgsign, publish, release_sign, seed
from tests._fake_rclone import FakeRclone, install_fake_rclone
from tests._pending import try_import
from tests.test_release_cli import FakeChildBuilds, Lab, patch_everywhere, release_lab
from tests.test_release_core import FPR, KEY_FILE, fake_signature, listed
from tests.test_release_sign_cmd import SIGN
from tests.test_release_sign_host import DownloadingUser, _kill_agent, _new_key
from tests.test_release_upload import INDEX
from tests.test_release_upload_cmd import UPLOAD

upload_set: Any = try_import("shidashi.release_upload", "upload_set")

#: The real gpg seams, taken before any test replaces them with ``FakeMaintainerGpg``.
REAL_GPG = {name: getattr(gpgsign, name) for name in ("check_key", "export_public", "detach_sign")}
REAL_GPG_VERIFY = seed.gpg_verify

needs_gpg = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgconf") is None,
    reason="needs gpg and gpgconf on the host: the signatures are real",
)

runner = CliRunner()


class BundlingChildBuilds(FakeChildBuilds):
    """``FakeChildBuilds`` plus the run bundle a real build writes beside each ISO and
    lists in ``SHA256SUMS`` (cli.py: ``<iso stem>.build.tar.zst``)."""

    def _build(self, arch: str, init: str, out: Path, outcome: Path, how: str) -> int:
        code = super()._build(arch, init, out, outcome, how)
        for name in self.isos(out, arch, init):
            if (out / name).is_file():
                bundle = out / f"{name.removesuffix('.iso')}.build.tar.zst"
                bundle.write_bytes(f"events and logs of {name}".encode())
                publish.update_sha256sums(out, {bundle.name: publish.sha256_of(bundle)})
        return code


def unsigned_weekly_release(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> Path:
    """What story 032's weekly job leaves: ``release v3 systemd --unsigned``."""
    builds = BundlingChildBuilds(lab.builds.pkgdir_of)
    patch_everywhere(monkeypatch, "shidashi.release", "run_build", builds)
    made = lab.release("v3", "systemd", "--unsigned")
    assert made.exit_code == 0, f"story 032's `release --unsigned` made no release:\n{made.output}"
    return lab.the_release()


def manifest_of(directory: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    return data


def upload(fake: FakeRclone, directory: Path, root: Path) -> Result:
    argv = [*UPLOAD, str(directory), "--remote", fake.remote, "--output-root", str(root)]
    return runner.invoke(cli.app, argv)


def sign(directory: Path, key: str) -> Result:
    return runner.invoke(cli.app, [*SIGN, str(directory), "--key", key])


def assert_bucket_matches_the_contract(fake: FakeRclone, directory: Path) -> Path:
    """The layout design.md names: ``releases/<date>/<name>``, the public set only, and the
    index naming the date with 032's ISOs."""
    date = directory.name
    folder = fake.obj(f"releases/{date}")
    sent = {p.name for p in folder.iterdir()}
    bundles = {n for n in listed(directory) if n.endswith(".build.tar.zst")}
    assert bundles, "the fixture lists no run bundle"
    assert not sent & bundles, sent & bundles
    assert {"SHA256SUMS", "SHA256SUMS.asc", KEY_FILE, "release.json"} <= sent, sent
    assert set(listed(directory)) - bundles <= sent, set(listed(directory)) - bundles - sent
    index = json.loads(fake.obj(INDEX).read_bytes())
    assert index["schema"] == 1
    (mine,) = [e for e in index["releases"] if e["date"] == date]
    isos = sorted(i for v in manifest_of(directory)["variants"] for i in v["isos"])
    assert sorted(mine["isos"]) == isos, mine
    assert mine["sha256sums"] == f"releases/{date}/SHA256SUMS"
    assert mine["signature"] == f"releases/{date}/SHA256SUMS.asc"
    assert mine["pubkey"] == f"releases/{date}/{KEY_FILE}"
    return folder


def test_upload_contract_an_unsigned_032_release_signs_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1.1, R3.1: 032's ``signed: false`` becomes ``true`` and is covered by the signed
    sums; every listed name exists; the bundle stays home; the bucket is laid out as
    design.md says."""
    lab = release_lab(tmp_path, monkeypatch)
    directory = unsigned_weekly_release(lab, monkeypatch)
    written = manifest_of(directory)
    assert written["signed"] is False, "story 032's manifest does not say signed: false"

    signed = sign(directory, FPR)
    assert signed.exit_code == 0, signed.output
    assert manifest_of(directory) == {**written, "signed": True}
    sums = (directory / "SHA256SUMS").read_bytes()
    assert listed(directory)["release.json"] == publish.sha256_of(directory / "release.json")
    assert (directory / "SHA256SUMS.asc").read_text(encoding="utf-8") == fake_signature(FPR, sums)

    present = set(os.listdir(directory))
    for name in listed(directory):
        assert name in present, f"SHA256SUMS lists {name}, which is not there"
    plan = upload_set(listed(directory), present)
    planned = [*plan.data, *plan.seal]
    assert not [n for n in planned if n.endswith(".build.tar.zst")], planned
    assert planned[-1] == "SHA256SUMS.asc", planned

    fake = install_fake_rclone(tmp_path, monkeypatch)
    published = upload(fake, directory, lab.releases)
    assert published.exit_code == 0, published.output
    assert_bucket_matches_the_contract(fake, directory)


# --- the same chain with a real gpg ------------------------------------------------------------


@pytest.fixture(scope="module")
def publish_keys() -> Iterator[tuple[Path, str]]:
    """A throwaway ed25519 key, no passphrase, in a short /tmp home (the agent's socket)."""
    home = Path(tempfile.mkdtemp(prefix="gpgp-", dir="/tmp"))
    home.chmod(0o700)
    try:
        yield home, _new_key(home, "Shidashi Publish Test <publish@example.invalid>")
    finally:
        _kill_agent(home)
        shutil.rmtree(home, ignore_errors=True)


@needs_gpg
def test_upload_contract_with_a_real_gpg_verifies_from_the_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publish_keys: tuple[Path, str]
) -> None:
    """R1.1, R6.1 off-host, Q13: signed with a real key, uploaded, and checked from the
    bucket by a user who imports only the published key."""
    home, signer = publish_keys
    lab = release_lab(tmp_path, monkeypatch)
    directory = unsigned_weekly_release(lab, monkeypatch)
    for name, real in REAL_GPG.items():  # the unsigned run never needed a gpg
        monkeypatch.setattr(gpgsign, name, real)
    monkeypatch.setattr(seed, "gpg_verify", REAL_GPG_VERIFY)
    monkeypatch.setenv("GNUPGHOME", str(home))

    signed = sign(directory, signer)
    assert signed.exit_code == 0, signed.output
    assert manifest_of(directory)["signed"] is True
    assert release_sign.verify_release(directory, key=signer) == []

    fake = install_fake_rclone(tmp_path, monkeypatch)
    published = upload(fake, directory, lab.releases)
    assert published.exit_code == 0, published.output
    folder = assert_bucket_matches_the_contract(fake, directory)

    user = DownloadingUser(folder / KEY_FILE)
    try:
        assert user.keys() == [signer]
        checked = user.verify(folder / "SHA256SUMS.asc", folder / "SHA256SUMS")
        assert checked.returncode == 0, checked.stderr
        assert f"VALIDSIG {signer}" in checked.stdout
    finally:
        user.close()
    if shutil.which("sha256sum") is None:
        pytest.skip("no sha256sum on this host")
    sums = subprocess.run(
        ["sha256sum", "--quiet", "-c", "--ignore-missing", "SHA256SUMS"],
        cwd=folder,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sums.returncode == 0, sums.stdout + sums.stderr
