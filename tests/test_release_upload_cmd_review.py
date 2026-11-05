"""Regression tests for the tech review of story 033 Task 4 (report 4.x-tech-review.md)."""

import json

from shidashi import cli, release_sign
from shidashi import release as release_core
from tests.test_release_core import FPR
from tests.test_release_sign_cmd import DAY, unsigned_release_tree
from tests.test_release_upload_cmd import EARLIER, UploadLab, lab, runner  # noqa: F401


def test_a_file_changed_after_signing_is_not_sent(lab: UploadLab) -> None:  # noqa: F811
    """The signature covers SHA256SUMS, not the ISO: a flipped byte must stop the upload."""
    directory = lab.release()
    iso = next(directory.glob("*.iso"))
    data = bytearray(iso.read_bytes())
    data[0] ^= 1
    iso.write_bytes(bytes(data))
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    assert iso.name in result.output, result.output
    assert lab.rclone.calls() == []


def test_a_remote_with_inline_credentials_is_refused(lab: UploadLab) -> None:  # noqa: F811
    """``:s3,secret_access_key=...:bucket`` would skip the 0600 config check."""
    directory = lab.release()
    remote = ":s3,access_key_id=AKIDEXAMPLE,secret_access_key=SECRETEXAMPLE:bentoo"
    argv = ["release", "upload", str(directory), "--remote", remote]
    result = runner.invoke(cli.app, [*argv, "--output-root", str(lab.root)])
    assert result.exit_code == 1, result.output
    assert "SECRETEXAMPLE" not in result.output and "AKIDEXAMPLE" not in result.output
    assert lab.rclone.calls() == []
    empty = runner.invoke(cli.app, [*argv[:-1], "r2:", "--output-root", str(lab.root)])
    assert empty.exit_code == 1 and lab.rclone.calls() == [], empty.output


def test_every_purge_is_tried_and_every_failure_named(lab: UploadLab) -> None:  # noqa: F811
    """Two dates beyond --keep both fail: both are named, not only the first."""
    older = [*EARLIER, "2026.09.20"]
    lab.seed(*older)
    lab.on_host(*older)
    lab.rclone.fail("purge", "releases/2026.09.", "ERROR : purge: 500 Internal Error\n")
    result = lab.upload(lab.release())
    assert result.exit_code == 1, result.output
    assert len(lab.purges()) == 2, lab.purges()
    assert "2026.09.27" in result.output and "2026.09.20" in result.output, result.output


def test_dry_run_prints_the_index_the_run_would_write(lab: UploadLab) -> None:  # noqa: F811
    """The printed index is the trimmed one, not the merged one."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER)
    result = lab.upload(lab.release(), "--dry-run")
    assert result.exit_code == 0, result.output
    assert f'"date": "{EARLIER[3]}"' not in result.output, result.output
    assert f'"date": "{DAY}"' in result.output, result.output


def test_an_iso_name_that_is_not_a_string_is_refused(lab: UploadLab) -> None:  # noqa: F811
    """A malformed release.json is a refusal naming it, never a TypeError."""
    directory = lab.root / DAY
    body = unsigned_release_tree(directory)
    broken = {**body, "signed": True, "variants": [{"isos": [1]}]}
    release_core.write_manifest(directory, broken)
    release_sign.sign_release(directory, key=FPR)
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    assert "release.json" in result.output, result.output
    assert json.loads((directory / "release.json").read_text())["signed"] is True
