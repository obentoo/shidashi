"""Regression tests for the tech review of story 033 Task 3 (report 3.x-tech-review.md):
rclone behaviour the pre-authored FakeRclone did not model, measured with ``rclone serve
s3`` on 2026-11-04."""

from pathlib import Path

import pytest

from shidashi.release_upload import INDEX_OBJECT, RcloneRunner, UploadError
from tests._fake_rclone import FakeRclone, install_fake_rclone


@pytest.fixture
def rclone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRclone:
    return install_fake_rclone(tmp_path, monkeypatch)


def runner_for(fake: FakeRclone) -> RcloneRunner:
    return RcloneRunner(remote=fake.remote, config=fake.config)


def test_cat_of_a_missing_s3_object_is_none(rclone: FakeRclone) -> None:
    """On S3/R2 a missing object reads as nothing; --error-on-no-transfer makes it exit 9."""
    rclone.fail("cat", "index.json", "NOTICE: nothing transferred\n", code=9)
    assert runner_for(rclone).cat(INDEX_OBJECT) is None
    (call,) = rclone.calls()
    assert "--error-on-no-transfer" in call["argv"], call["argv"]


def test_every_call_refuses_to_prompt(rclone: FakeRclone, tmp_path: Path) -> None:
    """An encrypted config would prompt on the captured stderr and block forever."""
    src = tmp_path / "x.txt"
    src.write_text("x")
    runner_for(rclone).copyto(src, "releases/2026.10.25/x.txt")
    (call,) = rclone.calls()
    assert "--ask-password=false" in call["argv"], call["argv"]


@pytest.mark.parametrize(
    "path", ["", "releases", "releases/", "releases/..", "x/2026.10.25", "releases/2026.10.25/a"]
)
def test_purge_refuses_anything_but_a_dated_release(rclone: FakeRclone, path: str) -> None:
    """A bucket delete is irreversible: "" would purge the whole bucket."""
    with pytest.raises(UploadError, match="refusing to purge"):
        runner_for(rclone).purge(path)
    assert rclone.calls() == []


def test_copyto_a_relative_name_with_a_colon_stays_local(
    rclone: FakeRclone, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rclone reads ``a:b.iso`` as remote ``a``; the runner passes an absolute path."""
    monkeypatch.chdir(tmp_path)
    Path("a:b.iso").write_bytes(b"iso")
    runner_for(rclone).copyto(Path("a:b.iso"), "releases/2026.10.25/a:b.iso")
    assert rclone.obj("releases/2026.10.25/a:b.iso").read_bytes() == b"iso"


#: A wrapper like ``runuser``: forks the real work and waits; the grandchild logs its pid.
# The grandchild must not inherit rclone's pipes: holding them, it would keep the runner's
# post-kill communicate() waiting until its own sleep ends, and a grandchild the kill missed
# would be gone by the check -- the test would pass with os.kill instead of os.killpg.
_FORKING_RCLONE = r"""
import subprocess, sys
from pathlib import Path
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
(Path(__STATE__) / "grandchild.pid").write_text(str(child.pid))
child.wait()
"""


def _is_zombie(pid: int) -> bool:
    """``Z`` in ``/proc/<pid>/stat``: dead, only waiting to be reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    # The comm field is parenthesised and may hold spaces: the state follows its last ")".
    return stat.rpartition(")")[2].split()[0] == "Z"


def test_a_timeout_kills_the_grandchild_too(
    rclone: FakeRclone, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under sudo the child is runuser: killing only it would leave rclone running."""
    import os
    import time

    from shidashi import release_upload
    from tests._fake_rclone import write_script

    write_script(rclone.bin_dir / "rclone", _FORKING_RCLONE, rclone.state)
    monkeypatch.setattr(release_upload, "_SHORT_TIMEOUT_S", 1)
    with pytest.raises(UploadError, match="timed out"):
        runner_for(rclone).cat(INDEX_OBJECT)
    pid = int((rclone.state / "grandchild.pid").read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # A killed orphan stays a zombie until PID 1 reaps it, and act's PID 1
        # (`tail -f /dev/null`) never does: kill(pid, 0) still finds it there.
        if _is_zombie(pid):
            return
        time.sleep(0.05)
    pytest.fail(f"grandchild {pid} still alive after the timeout")
