"""A planted ``cid-N.lock`` symlink no longer blocks CID allocation (story 006, task 9.2; R4.1).

Contract (design.md v5, "Boot tests" -- symlink-safe lock directory):

- every CID lock file is held ``nofollow`` (``O_NOFOLLOW | O_CLOEXEC``): a planted
  ``cid-N.lock`` symlink is never followed, its target never created nor written;
- ``allocate_cid`` skips it like any other unusable lock file: one ``warning:`` line on
  stderr naming the path, then the next CID;
- ``hold_cid`` (a fixed CID: ``vm test --cid``, ``vm start``) raises ``VmError`` naming it,
  and ``vm test --cid N`` exits 1 naming it, no VM started.

Hostile halves first: a symlink whose target is a lock another process HOLDS (wrongly
collapsed into "busy": it must still warn), several planted symlinks in a row; the converse,
an ordinary lock file left by an earlier boot, is still taken (wrongly split: never skipped
as unusable). Then the benign halves: one symlink to an existing file and to a missing path.

Every test redirects ``$SHIDASHI_CID_LOCKS`` into ``tmp_path``: the real
``/tmp/shidashi-vm-cids`` is never touched. Another holder is a real second process.
"""

import contextlib
import fcntl
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import shidashi
from shidashi import audit, vm
from shidashi.cli import app

_REPO = Path(shidashi.__file__).resolve().parents[1]
runner = CliRunner()

_HOLD = """
import sys
from pathlib import Path
from shidashi import locks
with locks.hold(Path(sys.argv[1]), what="vsock CID"):
    print("held", flush=True)
    sys.stdin.readline()
"""


@pytest.fixture(autouse=True)
def cids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The host CID lock directory, redirected and already shared (1777, this user's)."""
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    path = tmp_path / "host" / "shidashi-vm-cids"
    monkeypatch.setenv("SHIDASHI_CID_LOCKS", str(path))
    path.mkdir(parents=True)
    path.chmod(0o1777)
    return path


def _locked(path: Path) -> bool:
    """Whether any open file description holds ``path``'s flock (this process's too)."""
    if not path.exists() or path.is_symlink():
        return False
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


@contextlib.contextmanager
def _elsewhere(lock: Path) -> Iterator[subprocess.Popen[str]]:
    """A real second process holding ``lock`` until the block ends."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(lock)],
        cwd=_REPO,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        if proc.stdout.readline().strip() != "held":
            proc.wait(timeout=10)
            pytest.fail(f"the other process never got its lock: {proc.stderr.read()}")
        yield proc
    finally:
        if proc.poll() is None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write("\n")
                proc.stdin.flush()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _warnings(err: str, planted: Path) -> list[str]:
    return [line for line in err.splitlines() if str(planted) in line]


def _assert_one_warning(err: str, planted: Path) -> None:
    lines = _warnings(err, planted)
    assert len(lines) == 1, f"expected one line naming {planted}: {err!r}"
    assert lines[0].startswith("warning:"), lines[0]


def _victim(tmp_path: Path, exists: bool) -> Path:
    target = tmp_path / "victim"
    if exists:
        target.write_text("precious\n")
    return target


def _assert_untouched(target: Path, existed: bool) -> None:
    if existed:
        assert target.read_text() == "precious\n", "the symlink's target was written"
    else:
        assert not target.exists(), "the symlink's target was created"


# --- hostile halves -------------------------------------------------------------------------


def test_a_symlink_to_a_held_lock_is_skipped_with_a_warning_not_as_busy(
    cids: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Wrongly collapsed: ``cid-42.lock`` -> ``cid-43.lock``, which another boot holds.
    42 is unusable (warned, never followed), 43 busy (silent): the allocation gets 44."""
    planted, held = cids / "cid-42.lock", cids / "cid-43.lock"
    planted.symlink_to(held)
    with _elsewhere(held):
        capfd.readouterr()
        with vm.allocate_cid(42) as cid:
            assert cid == 44, f"expected 44 past the symlink (42) and the held 43, got {cid}"
            assert _locked(cids / "cid-44.lock")
    err = capfd.readouterr().err
    _assert_one_warning(err, planted)
    assert not _warnings(err, held), f"the busy CID 43 was warned about: {err!r}"


def test_several_planted_symlinks_are_all_skipped(
    tmp_path: Path, cids: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    targets = {n: tmp_path / f"victim-{n}" for n in (42, 43, 44)}
    targets[43].write_text("precious\n")
    for n, target in targets.items():
        (cids / f"cid-{n}.lock").symlink_to(target)
    capfd.readouterr()
    with vm.allocate_cid(42) as cid:
        assert cid == 45, f"three planted symlinks were not all skipped: got {cid}"
        assert _locked(cids / "cid-45.lock")
    err = capfd.readouterr().err
    for n in (42, 43, 44):
        _assert_one_warning(err, cids / f"cid-{n}.lock")
    assert targets[43].read_text() == "precious\n"
    assert not targets[42].exists() and not targets[44].exists()


def test_an_ordinary_lock_file_left_by_an_earlier_boot_is_still_taken(
    cids: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Wrongly split: a regular, unheld ``cid-42.lock`` is a free CID, never skipped."""
    with vm.allocate_cid(42):
        pass
    capfd.readouterr()
    with vm.allocate_cid(42) as cid:
        assert cid == 42
    assert "warning:" not in capfd.readouterr().err


# --- one planted symlink: skipped by allocation, refused for a fixed CID --------------------


@pytest.mark.parametrize("target_exists", [False, True])
def test_allocation_skips_a_planted_symlink_naming_it_once(
    tmp_path: Path, cids: Path, capfd: pytest.CaptureFixture[str], target_exists: bool
) -> None:
    target = _victim(tmp_path, target_exists)
    planted = cids / "cid-42.lock"
    planted.symlink_to(target)
    capfd.readouterr()
    with vm.allocate_cid(42) as cid:
        assert cid == 43, f"allocation did not skip the planted symlink: got {cid}"
        assert _locked(cids / "cid-43.lock")
    _assert_one_warning(capfd.readouterr().err, planted)
    assert planted.is_symlink(), "the planted symlink was replaced"
    _assert_untouched(target, target_exists)


@pytest.mark.parametrize("target_exists", [False, True])
def test_hold_cid_refuses_a_planted_symlink_naming_it(
    tmp_path: Path, cids: Path, target_exists: bool
) -> None:
    target = _victim(tmp_path, target_exists)
    planted = cids / "cid-42.lock"
    planted.symlink_to(target)
    with pytest.raises(vm.VmError) as err, vm.hold_cid(42):
        pytest.fail("a fixed CID was held through a planted symlink")
    assert str(planted) in str(err.value), str(err.value)
    _assert_untouched(target, target_exists)


def test_vm_test_on_a_planted_symlink_exits_1_naming_it(
    tmp_path: Path, cids: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    iso = tmp_path / "bentoo-x-kde.iso"
    iso.write_bytes(b"ISO")
    monkeypatch.setattr(
        vm, "read_build_info", lambda _iso: {"arch": "v3", "flavor": "kde", "init": "systemd"}
    )
    monkeypatch.setattr(audit, "repo_state", lambda: {})
    booted: list[object] = []

    def boot_test(_iso: Path, **kw: Any) -> dict[str, Any]:
        booted.append(kw.get("cid"))
        return {"passed": True, "firmwares": {}}

    monkeypatch.setattr(vm, "boot_test", boot_test)
    target = _victim(tmp_path, True)
    planted = cids / "cid-42.lock"
    planted.symlink_to(target)
    done = runner.invoke(app, ["vm", "test", str(iso), "--firmware", "bios", "--cid", "42"])
    captured = capfd.readouterr()
    out = done.output + captured.out + captured.err
    for box in "│╭╮╰╯─":
        out = out.replace(box, " ")
    out = " ".join(out.split())
    assert done.exit_code == 1, out
    assert str(planted) in out, out
    assert "Traceback" not in out
    assert booted == [], "vm test booted on a planted symlink's CID"
    _assert_untouched(target, True)
