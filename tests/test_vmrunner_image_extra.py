"""The runner image beyond the authored tests (story 024, task 2.2; R2.1, R2.4).

The cases the Reviewer found uncovered in ``tests/test_vmrunner_image.py``. The callers
catch only ``vm.VmError``, so every way ``ensure_image`` can fail is a ``VmRunnerError``:

- the tarball missing: named with the preflight's fix, before podman runs;
- ``podman image exists`` answering neither 0 nor 1: podman's stderr, no import;
- a missing ``podman`` binary, at the first look or at the import: names the command;
- ``locks.LockError`` of the import lock: names the lock path;
- ``OSError`` of the lock (its directory, its file): names the lock path.

Hostile half first: what must be refused, then what must still pass.
"""

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from shidashi import locks, vmrunner
from tests.test_vmrunner_image import _Podman


@pytest.fixture
def tarball(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    path = tmp_path / "cache" / "fork-points" / "v3-systemd-20260823T153057Z-p1-k1-toolbox.tar"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"toolbox")
    return path


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    return tmp_path / "scratch" / "vm-runner.lock"


@pytest.fixture
def podman(lock: Path) -> _Podman:
    return _Podman(lock)


def _no_podman(argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(2, "No such file or directory", "podman")


# --- hostile half -----------------------------------------------------------------------


def test_a_missing_tarball_is_refused_with_the_fix_before_podman_runs(
    tarball: Path, podman: _Podman
) -> None:
    tarball.unlink()
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.ensure_image(tarball, runner=podman)
    message = str(raised.value)
    assert str(tarball) in message
    assert "shidashi factory v3 toolbox systemd" in message
    assert "shidashi worker sync push" in message
    assert podman.calls == []


def test_an_unexpected_exists_rc_is_an_error_with_podmans_stderr(tarball: Path) -> None:
    calls: list[list[str]] = []

    def broken(argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 125, "", "Error: database is locked\n")

    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.ensure_image(tarball, runner=broken)
    assert "database is locked" in str(raised.value)
    assert "125" in str(raised.value)
    assert all(c[1] != "import" for c in calls)


def test_an_unexpected_rc_at_the_second_look_releases_the_lock(tarball: Path, lock: Path) -> None:
    podman = _Podman(lock)
    looks = 0

    def flaky(argv: Sequence[str], **kw: object) -> subprocess.CompletedProcess[str]:
        nonlocal looks
        if list(argv[1:3]) == ["image", "exists"]:
            looks += 1
            if looks == 2:
                return subprocess.CompletedProcess(argv, 125, "", "Error: storage gone\n")
        return podman(argv, **kw)

    with pytest.raises(vmrunner.VmRunnerError, match="storage gone"):
        vmrunner.ensure_image(tarball, runner=flaky)
    assert podman.imports() == []
    assert not podman.lock_held()


def test_a_missing_podman_binary_names_the_command(tarball: Path) -> None:
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.ensure_image(tarball, runner=_no_podman)
    assert "podman image exists" in str(raised.value)


def test_podman_vanishing_before_the_import_names_it_and_releases_the_lock(
    tarball: Path, podman: _Podman
) -> None:
    def gone_at_import(argv: Sequence[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if argv[1] == "import":
            return _no_podman(argv)
        return podman(argv, **kw)

    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.ensure_image(tarball, runner=gone_at_import)
    assert "podman import" in str(raised.value)
    assert str(tarball) in str(raised.value)
    assert not podman.lock_held()


def test_a_lock_error_names_the_lock_path(tarball: Path, podman: _Podman, lock: Path) -> None:
    """A nested EXCLUSIVE hold of a lock this process holds SHARED is a LockError."""
    with (
        locks.hold(lock, what="a shared holder", shared=True),
        pytest.raises(vmrunner.VmRunnerError) as raised,
    ):
        vmrunner.ensure_image(tarball, runner=podman)
    assert str(lock) in str(raised.value)
    assert podman.imports() == []


def test_a_scratch_that_cannot_be_a_directory_names_the_lock_path(
    tarball: Path, podman: _Podman, lock: Path
) -> None:
    lock.parent.write_text("not a directory", encoding="utf-8")
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.ensure_image(tarball, runner=podman)
    assert str(lock) in str(raised.value)
    assert podman.imports() == []


def test_a_lock_file_that_cannot_be_opened_names_the_lock_path(
    tarball: Path, podman: _Podman, lock: Path
) -> None:
    lock.mkdir(parents=True)  # a directory where the lock file should be
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.ensure_image(tarball, runner=podman)
    assert str(lock) in str(raised.value)
    assert podman.imports() == []


# --- what must still pass ---------------------------------------------------------------


def test_a_missing_scratch_is_created_for_the_lock(tarball: Path, podman: _Podman) -> None:
    assert not podman.lock.parent.exists()
    tag = vmrunner.ensure_image(tarball, runner=podman)
    assert tag in podman.images
    assert podman.lock.is_file()


def test_podman_runs_captured_and_unchecked(tarball: Path) -> None:
    """stderr must reach the error, and the rc is read, never raised by ``check``."""
    seen: list[dict[str, object]] = []

    def recording(argv: Sequence[str], **kw: object) -> subprocess.CompletedProcess[str]:
        seen.append(kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    vmrunner.ensure_image(tarball, runner=recording)
    assert seen
    for kw in seen:
        assert kw.get("capture_output") is True
        assert kw.get("text") is True
        assert kw.get("check") is False
