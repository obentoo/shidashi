"""SHA256SUMS kept whole by concurrent publishers (story 006, task 5.2).

Contract (design.md): the read-modify-write of ``update_sha256sums`` runs under
``<directory>/.SHA256SUMS.lock``; concurrent publishers into one directory keep
every line (R3.7). The publishers are REAL processes.
"""

import contextlib
import hashlib
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import publish

# every digest is 64 hex digits: a wrong-shaped line is refused (story 014 R4.12)
_PUBLISHER = """
import hashlib
import sys
from pathlib import Path
from shidashi import publish
directory, prefix, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
print("ready", flush=True)
sys.stdin.readline()
for k in range(count):
    digest = hashlib.sha256(f"{prefix}{k:04d}".encode()).hexdigest()
    publish.update_sha256sums(directory, {f"{prefix}-{k}.iso": digest})
"""


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


_HOLDER = """
import sys
from pathlib import Path
from shidashi import locks
with locks.hold(Path(sys.argv[1]), what="SHA256SUMS"):
    print("held", flush=True)
    sys.stdin.readline()
"""


@contextlib.contextmanager
def _held_elsewhere(path: Path) -> Iterator[subprocess.Popen[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        if proc.stdout.readline() != "held\n":
            proc.wait(timeout=10)
            pytest.fail(f"the holder never took {path}: {proc.stderr.read()}")
        yield proc
    finally:
        if proc.poll() is None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write("\n")
                proc.stdin.flush()
            proc.wait(timeout=10)


def _lines(directory: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, _, name = line.partition("  ")
        found[name] = digest
    return found


def test_concurrent_publishers_keep_every_line(tmp_path: Path) -> None:
    (tmp_path / "SHA256SUMS").write_text(f"{_digest('aaaa')}  older.iso\n")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _PUBLISHER, str(tmp_path), name, "25"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("kde", "gnome", "wm", "minimal")
    ]
    try:
        for proc in procs:
            assert proc.stdout is not None and proc.stdout.readline() == "ready\n"
        for proc in procs:
            assert proc.stdin is not None
            proc.stdin.write("go\n")
            proc.stdin.flush()
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    expected = {"older.iso": _digest("aaaa")} | {
        f"{p}-{k}.iso": _digest(f"{p}{k:04d}")
        for p in ("kde", "gnome", "wm", "minimal")
        for k in range(25)
    }
    assert _lines(tmp_path) == expected


def test_an_update_waits_for_the_sums_lock(tmp_path: Path) -> None:
    (tmp_path / "SHA256SUMS").write_text(f"{_digest('aaaa')}  kde.iso\n")
    done = threading.Event()

    def update() -> None:
        publish.update_sha256sums(tmp_path, {"gnome.iso": _digest("bbbb")})
        done.set()

    with _held_elsewhere(tmp_path / ".SHA256SUMS.lock"):
        thread = threading.Thread(target=update, daemon=True)
        thread.start()
        time.sleep(0.5)
        assert not done.is_set(), "SHA256SUMS was rewritten while another publisher held it"
    thread.join(20)
    assert done.is_set()
    assert _lines(tmp_path) == {"kde.iso": _digest("aaaa"), "gnome.iso": _digest("bbbb")}


def test_two_output_directories_do_not_wait_for_each_other(tmp_path: Path) -> None:
    """Wrongly collapsed: the lock is the directory's, not a global one."""
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    with _held_elsewhere(one / ".SHA256SUMS.lock"):
        thread = threading.Thread(
            target=publish.update_sha256sums, args=(two, {"x.iso": _digest("cccc")}), daemon=True
        )
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
    assert _lines(two) == {"x.iso": _digest("cccc")}
    assert (one / ".SHA256SUMS.lock").exists()
    assert not (one / "SHA256SUMS").exists()


def test_a_wait_for_the_sums_lock_says_which_directory_and_pid(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.9 on the SHA256SUMS lock, reported on stderr with no on_wait passed."""
    with _held_elsewhere(tmp_path / ".SHA256SUMS.lock") as holder:
        thread = threading.Thread(
            target=lambda: publish.update_sha256sums(tmp_path, {"gnome.iso": _digest("bbbb")}),
            daemon=True,
        )
        thread.start()
        time.sleep(0.5)
    thread.join(20)
    err = capfd.readouterr().err
    assert err.count("waiting for SHA256SUMS of") == 1
    assert f"(pid {holder.pid})" in err
