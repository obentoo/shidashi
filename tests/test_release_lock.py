"""One release at a time per dated directory, and a ``.signing`` that is no directory
(story 030, task 5.1 fix 2: Tech Review 2 findings A and B).

Kept apart from tests/test_release_cli.py: that file is the pre-authored contract and is
frozen byte for byte.

- A: two releases started the same UTC day write into one directory. Without a lock, the
  second one's ``SHA256SUMS`` lines can land after the first one verified its signature,
  and a signature that no longer matches is moved in. The release holds
  ``<dest>/.release.lock`` from the preflight through the signing; a second release finds
  it held and exits 1 before any build, naming the directory and the holder. One that
  takes the lock after the first one signed is refused as already signed (R1.7).
- B: a regular file or a symlink at ``<dest>/.signing`` would make every run build for
  hours and then fail with ``File exists``. The preflight refuses it, naming it, and leaves
  it as it is: it was planted, so the refusal is loud.
"""

import contextlib
import subprocess
import sys
import time
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest

from shidashi import locks, release_sign, seed
from tests.test_release_cli import Lab, patch_everywhere, release_lab, utc_day
from tests.test_release_core import FPR

#: The lock file's name: a literal, so the test does not lean on the code it checks.
LOCK_NAME = ".release.lock"

#: Holds the lock ``argv[1]`` with the project's own helper (which records its pid) until
#: killed; ``argv[2]`` appears once it is held.
HOLDER = """
import pathlib, sys, time
from shidashi import locks
with locks.hold(pathlib.Path(sys.argv[1]), what="a test's release"):
    pathlib.Path(sys.argv[2]).write_text("held")
    time.sleep(60)
"""

#: Exit 0 when ``argv[1]`` is missing or can be locked at once, 1 when another process
#: holds it.
TRY_LOCK = """
import fcntl, os, sys
try:
    fd = os.open(sys.argv[1], os.O_RDWR)
except FileNotFoundError:
    sys.exit(0)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
sys.exit(0)
"""


@pytest.fixture
def lock_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


@contextlib.contextmanager
def held_by_another_process(lock: Path) -> Iterator[subprocess.Popen[bytes]]:
    """``lock`` held by a separate process: ``locks.hold`` is re-entrant within one."""
    ready = lock.with_name("holder-ready")
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock), str(ready)])
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert holder.poll() is None, "the holder ended before it held the lock"
            assert time.monotonic() < deadline, "the holder never held the lock"
            time.sleep(0.05)
        ready.unlink()
        yield holder
    finally:
        holder.kill()
        holder.wait()


def held_elsewhere(lock: Path) -> bool:
    """Whether a process other than a fresh one holds ``lock`` (it is this test's release)."""
    return subprocess.run([sys.executable, "-c", TRY_LOCK, str(lock)], check=False).returncode == 1


# --- A: one release per directory -------------------------------------------------------------


def test_a_second_release_into_a_held_directory_exits_1_before_any_build(lock_lab: Lab) -> None:
    day = lock_lab.releases / utc_day()
    day.mkdir(parents=True)
    with held_by_another_process(day / LOCK_NAME) as holder:
        result = lock_lab.release("v3", "systemd", "--key", FPR)
    if utc_day() != day.name:
        pytest.skip("the UTC date changed during the test")
    assert result.exit_code == 1, result.output
    assert lock_lab.builds.calls == []
    flat = "".join(result.output.split())
    assert str(day) in flat, result.output
    assert f"pid{holder.pid}" in flat, result.output
    assert sorted(p.name for p in day.rglob("*.asc")) == []


def test_the_lock_is_held_from_the_builds_through_the_signing(
    lock_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second release could otherwise write into the directory while the first one's
    signatures are checked, or after."""
    seen: list[tuple[str, bool]] = []
    builds = lock_lab.builds

    def watching_build(argv: list[str]) -> int:
        seen.append(("build", held_elsewhere(lock_lab.releases / utc_day() / LOCK_NAME)))
        return builds(argv)

    checked = seed.gpg_verify

    def watching_verify(*files: Path, keyring: Path = seed.GENTOO_KEYRING) -> object:
        seen.append(("verify", held_elsewhere(Path(keyring).parent / LOCK_NAME)))
        return checked(*files, keyring=keyring)

    patch_everywhere(monkeypatch, "shidashi.release", "run_build", watching_build)
    monkeypatch.setattr(seed, "gpg_verify", watching_verify)
    result = lock_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert {step for step, _held in seen} == {"build", "verify"}, seen
    assert all(held for _step, held in seen), seen
    sums = (lock_lab.the_release() / "SHA256SUMS").read_text(encoding="utf-8")
    assert LOCK_NAME not in sums


def test_a_release_that_takes_the_lock_after_another_signed_is_refused(
    lock_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both passed the first "already signed" check; the other one signed and let go."""
    real_hold = locks.hold

    @contextlib.contextmanager
    def signed_meanwhile(path: Path, **kwargs: object) -> Generator[None]:
        with real_hold(path, **kwargs):  # type: ignore[arg-type]
            if path.name == LOCK_NAME:
                (path.parent / "SHA256SUMS.asc").write_text("signed", encoding="utf-8")
            yield

    monkeypatch.setattr(locks, "hold", signed_meanwhile)
    result = lock_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert lock_lab.builds.calls == []
    assert "already signed" in result.output, result.output
    assert (lock_lab.the_release() / "SHA256SUMS.asc").read_text(encoding="utf-8") == "signed"


# --- B: a .signing that is no directory -------------------------------------------------------


@pytest.mark.parametrize("planted", ["file", "symlink"])
def test_a_signing_stage_that_is_no_directory_is_refused_before_any_build(
    lock_lab: Lab, tmp_path: Path, planted: str
) -> None:
    day = lock_lab.releases / utc_day()
    day.mkdir(parents=True)
    stage = day / release_sign.STAGE_DIR
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "kept").write_text("not the release's", encoding="utf-8")
    if planted == "file":
        stage.write_text("planted", encoding="utf-8")
    else:
        stage.symlink_to(target)
    result = lock_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert lock_lab.builds.calls == []
    assert str(stage) in "".join(result.output.split()), result.output
    assert stage.is_symlink() if planted == "symlink" else stage.read_text() == "planted"
    assert (target / "kept").read_text(encoding="utf-8") == "not the release's"


def test_a_stale_signing_directory_does_not_block_the_release(lock_lab: Lab) -> None:
    """Guard: a killed run leaves a directory, which the signing clears."""
    day = lock_lab.releases / utc_day()
    (day / release_sign.STAGE_DIR).mkdir(parents=True)
    (day / release_sign.STAGE_DIR / "SHA256SUMS.asc").write_text("old", encoding="utf-8")
    result = lock_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert not (day / release_sign.STAGE_DIR).exists()
    assert (day / "SHA256SUMS.asc").is_file()


def test_a_sha256sums_changed_after_its_signature_was_checked_is_never_signed(
    lock_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer the release lock does not stop (a ``shidashi build`` run by hand into the
    directory) adds a line once the signature of ``SHA256SUMS`` verified: the signature no
    longer matches, so it is not moved in."""
    checked = seed.gpg_verify

    def late_writer(*files: Path, keyring: Path = seed.GENTOO_KEYRING) -> object:
        status = checked(*files, keyring=keyring)
        if Path(files[-1]).name == "SHA256SUMS":
            with Path(files[-1]).open("a", encoding="utf-8") as sums:
                sums.write("b" * 64 + "  other.iso.DIGESTS\n")
        return status

    monkeypatch.setattr(seed, "gpg_verify", late_writer)
    result = lock_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert not (lock_lab.the_release() / "SHA256SUMS.asc").exists()
    assert "SHA256SUMS" in line_of_signing(result.output), result.output


def line_of_signing(output: str) -> str:
    mine = [line for line in output.splitlines() if line.startswith("signing:")]
    assert len(mine) == 1, output
    return mine[0]
