"""Hardening of the host-wide vsock CID locks (story 006, task 8.3, security review).

- A lock file opened ``nofollow`` that has more than one link (a hardlink another user made
  to a file of yours or root's, possible where ``fs.protected_hardlinks=0``) is refused,
  naming it; its target is never written.
- A lock file owned by another user is flocked but never written: the pid record goes only
  into a file this user owns (in ``hold`` and in ``write_holder``).
- ``allocate_cid`` skips an unusable lock file (FIFO, directory, hardlinked, unreadable) as
  it skips a busy one, saying so once on stderr naming it; ``hold_cid`` (a fixed CID) keeps
  failing with ``VmError``. A planted symlink still raises (tests/test_vm_cid_host_lock.py).
- ``$SHIDASHI_CID_LOCKS`` must point under a trusted parent -- only its last component is
  checked: the refusal and the module docstring say so.
- ``contrib/tmpfiles.d/shidashi.conf`` makes root create the directory at boot (1777, no age
  cleanup); it is versioned, and the README names it with the manual fix.

Every test redirects ``$SHIDASHI_CID_LOCKS`` into ``tmp_path``: the real
``/tmp/shidashi-vm-cids`` is never touched.
"""

import fcntl
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import shidashi
from shidashi import locks, vm

_REPO = Path(shidashi.__file__).resolve().parents[1]
_TMPFILES = _REPO / "contrib" / "tmpfiles.d" / "shidashi.conf"
_RECORD = b"0000000001\n"


@pytest.fixture(autouse=True)
def cids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "host" / "shidashi-vm-cids"
    path.mkdir(parents=True)
    path.chmod(0o1777)
    monkeypatch.setenv("SHIDASHI_CID_LOCKS", str(path))
    return path


def _locked(path: Path) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class _Stat:
    """A stat result reporting another owner; everything else is the real one."""

    def __init__(self, real: os.stat_result, uid: int) -> None:
        self._real = real
        self.st_uid = uid

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _owned_by(monkeypatch: pytest.MonkeyPatch, path: Path, uid: int) -> None:
    """Every stat of ``path``'s inode (lstat, stat, fstat) reports owner ``uid``."""
    real = path.lstat()
    key = (real.st_dev, real.st_ino)

    def wrap(fn: Callable[..., os.stat_result]) -> Callable[..., Any]:
        def stat_of(*args: Any, **kw: Any) -> Any:
            got = fn(*args, **kw)
            return _Stat(got, uid) if (got.st_dev, got.st_ino) == key else got

        return stat_of

    for name in ("lstat", "stat", "fstat"):
        monkeypatch.setattr(os, name, wrap(getattr(os, name)))


# --- 1. hardlinks and foreign owners ---------------------------------------------------------


def test_a_hardlinked_lock_file_is_refused_and_its_target_unchanged(
    tmp_path: Path, cids: Path
) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious\n")
    mode = stat.S_IMODE(victim.stat().st_mode)
    lock = cids / "cid-42.lock"
    os.link(victim, lock)
    with pytest.raises(locks.LockError) as err:  # noqa: SIM117
        with locks.hold(lock, what="vsock CID 42", wait=False, mode=0o666, nofollow=True):
            pytest.fail("held a lock file with two links")
    assert str(lock) in str(err.value), str(err.value)
    assert victim.read_bytes() == b"precious\n", "the hardlink's target was written"
    assert stat.S_IMODE(victim.stat().st_mode) == mode, "the hardlink's target was chmodded"
    with pytest.raises(vm.VmError) as fixed, vm.hold_cid(42):
        pytest.fail("vm test/start took a hardlinked CID lock")
    assert str(lock) in str(fixed.value), str(fixed.value)
    assert victim.read_bytes() == b"precious\n"


def test_a_lock_file_owned_by_another_user_is_locked_but_not_written(
    cids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = cids / "cid-42.lock"
    lock.write_bytes(_RECORD)
    lock.chmod(0o666)
    foreign = os.geteuid() + 4242
    with monkeypatch.context() as patched:
        _owned_by(patched, lock, foreign)
        with vm.allocate_cid(42) as cid:
            assert cid == 42
            assert _locked(lock), "a foreign-owned lock file was not flocked"
            assert lock.read_bytes() == _RECORD, "the pid record went into another's file"
            locks.write_holder(lock, os.getppid())
            assert lock.read_bytes() == _RECORD, "write_holder wrote into another's file"
    assert not _locked(lock)


def test_a_lock_file_this_user_owns_still_gets_the_record(cids: Path) -> None:
    """Wrongly collapsed: the owner rule must not silence the record of one's own file."""
    lock = cids / "cid-42.lock"
    lock.write_bytes(_RECORD)
    lock.chmod(0o666)
    with vm.allocate_cid(42):
        assert locks.holder_pid(lock) == os.getpid()
        locks.write_holder(lock, os.getppid())
        assert locks.holder_pid(lock) == os.getppid()


def test_a_new_lock_file_is_created_with_one_link_and_the_holders_record(cids: Path) -> None:
    with vm.allocate_cid(42):
        lock = cids / "cid-42.lock"
        assert lock.lstat().st_nlink == 1
        assert locks.holder_pid(lock) == os.getpid()
    assert sorted(p.name for p in cids.iterdir()) == ["cid-42.lock"], "a temp file was left"


# --- 2. allocate_cid skips an unusable lock file --------------------------------------------


def _plant_fifo(path: Path) -> None:
    os.mkfifo(path)


def _plant_directory(path: Path) -> None:
    path.mkdir()


def _plant_hardlink(path: Path) -> None:
    victim = path.parent.parent / "victim"
    victim.write_bytes(b"precious\n")
    os.link(victim, path)


def _plant_unreadable(path: Path) -> None:
    path.write_bytes(_RECORD)
    path.chmod(0)


@pytest.mark.parametrize(
    "plant", [_plant_fifo, _plant_directory, _plant_hardlink, _plant_unreadable]
)
def test_allocate_cid_skips_an_unusable_lock_file_with_one_warning(
    cids: Path, capfd: pytest.CaptureFixture[str], plant: Callable[[Path], None]
) -> None:
    if plant is _plant_unreadable and os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file")
    planted = cids / "cid-42.lock"
    plant(planted)
    with vm.allocate_cid(42) as cid:
        assert cid == 43, f"allocate_cid did not skip the unusable {planted}"
    err = capfd.readouterr().err
    lines = [line for line in err.splitlines() if str(planted) in line]
    assert len(lines) == 1 and lines[0].startswith("warning:"), err
    victim = cids.parent / "victim"
    if victim.exists():
        assert victim.read_bytes() == b"precious\n"


def test_hold_cid_still_refuses_an_unusable_lock_file(cids: Path) -> None:
    planted = cids / "cid-42.lock"
    os.mkfifo(planted)
    with pytest.raises(vm.VmError) as err, vm.hold_cid(42):
        pytest.fail("vm test/start took a FIFO as its CID lock")
    assert str(planted) in str(err.value), str(err.value)


# --- 3. a trusted parent ----------------------------------------------------------------------


def test_the_refusal_and_the_docstring_say_the_parent_must_be_trusted(
    cids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patched:
        _owned_by(patched, cids, os.geteuid() + 4242)
        with pytest.raises(vm.VmError) as err, vm.allocate_cid(42):
            pytest.fail("allocated in a foreign directory")
    message = str(err.value)
    assert str(cids) in message and "SHIDASHI_CID_LOCKS" in message, message
    assert "trusted parent" in message, message
    doc = vm.__doc__ or ""
    assert "SHIDASHI_CID_LOCKS" in doc and "trusted parent" in doc


# --- 4. the tmpfiles.d snippet and its README sentence -------------------------------------


def test_the_tmpfiles_snippet_creates_the_directory_1777_without_age_cleanup() -> None:
    assert _TMPFILES.is_file(), f"{_TMPFILES} is missing"
    lines = _TMPFILES.read_text().splitlines()
    assert any(line.startswith("#") for line in lines), "no comment line"
    entries = [line.split() for line in lines if line.strip() and not line.startswith("#")]
    assert entries == [["d", str(vm._CID_LOCKS), "1777", "root", "root", "-"]], entries


def test_the_tmpfiles_snippet_is_versioned() -> None:
    if shutil.which("git") is None or not (_REPO / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "-C", str(_REPO), "check-ignore", "-q", str(_TMPFILES)],
        check=False,
        timeout=30,
    )
    assert ignored.returncode == 1, f"{_TMPFILES} is ignored by git: it would never ship"


def test_systemd_tmpfiles_accepts_the_snippet(tmp_path: Path) -> None:
    tool = shutil.which("systemd-tmpfiles")
    if tool is None:
        pytest.skip("systemd-tmpfiles is not on this host")
    done = subprocess.run(
        [tool, "--dry-run", "--create", f"--root={tmp_path / 'root'}", str(_TMPFILES)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert "shidashi-vm-cids" in done.stdout + done.stderr
    assert not (tmp_path / "root").exists(), "the dry run created something"


def test_the_readme_names_the_snippet_and_the_manual_fix() -> None:
    readme = (_REPO / "README.md").read_text()
    assert "contrib/tmpfiles.d/shidashi.conf" in readme
    assert "rm -r /tmp/shidashi-vm-cids" in readme
