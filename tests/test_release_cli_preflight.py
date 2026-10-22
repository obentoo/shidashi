"""``shidashi release``: the follow-ups of story 030's deviation register, task 5.1.

Kept apart from tests/test_release_cli.py: that file is the pre-authored contract and is
frozen byte for byte. Covered here:

- ``--key`` must be a fingerprint or a long key ID, refused before any build (the
  signatures are matched by fingerprint, so an e-mail would fail hours later);
- a missing or broken stage3 pin is refused before any build;
- an unreadable generation fingerprint is named in the summary and fails the release,
  which is still signed;
- a terminal's Ctrl-C reaches the running child build once, not twice.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shidashi import release_cli, seed
from shidashi.generation import FINGERPRINT_FILE
from tests.test_release_cli import Lab, line_of, release_lab
from tests.test_release_core import FPR

TERMINAL_CHILD = """
import pathlib, signal, sys, time
marks = pathlib.Path(sys.argv[1])
seen = []
signal.signal(signal.SIGINT, lambda signum, frame: seen.append(signum))
(marks / "ready").write_text("ready")
deadline = time.monotonic() + 30
while not seen and time.monotonic() < deadline:
    time.sleep(0.05)
time.sleep(1.0)  # room for a second copy to arrive
(marks / "count").write_text(str(len(seen)))
"""

#: The release on its own terminal: as a session leader, opening the pty makes it the
#: controlling terminal with this process group in the foreground.
RELEASE_ON_A_TERMINAL = """
import os, sys
terminal = os.open(sys.argv[3], os.O_RDWR)
from shidashi.release import run_build
try:
    code = run_build([sys.executable, "-c", sys.argv[1], sys.argv[2]])
except KeyboardInterrupt:
    sys.exit(42)
sys.exit(code)
"""


@pytest.fixture
def preflight_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


# --- --key ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("maintainer@example.org", id="e-mail"),
        pytest.param("Lucas <maintainer@example.org>", id="user-id"),
        pytest.param("89ABCDEF", id="short-key-id"),
        pytest.param(FPR[:-1], id="39-hex"),
        pytest.param(FPR[:-1] + "G", id="not-hex"),
    ],
)
def test_release_refuses_a_key_that_is_not_a_fingerprint_before_any_build(
    preflight_lab: Lab, key: str
) -> None:
    result = preflight_lab.release("v3", "systemd", "--key", key)
    assert result.exit_code == 1, result.output
    assert "fingerprint" in result.output
    assert preflight_lab.builds.calls == []
    assert preflight_lab.maintainer.checked == []


def test_release_takes_a_spaced_lower_case_0x_fingerprint_as_the_fingerprint(
    preflight_lab: Lab,
) -> None:
    spaced = "0x" + " ".join(FPR.lower()[i : i + 4] for i in range(0, len(FPR), 4))
    result = preflight_lab.release("v3", "systemd", "--key", spaced)
    assert result.exit_code == 0, result.output
    assert preflight_lab.maintainer.checked == [FPR]
    assert [c["key"] for c in preflight_lab.publisher.calls] == [FPR]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("0123456789abcdef", "0123456789ABCDEF"),
        ("0x" + "ab" * 32, "AB" * 32),
        (" ".join([FPR[:20], FPR[20:]]), FPR),
    ],
)
def test_signing_key_accepts_long_key_ids_and_fingerprints(given: str, expected: str) -> None:
    assert release_cli._signing_key(given) == expected


# --- the stage3 pin ---------------------------------------------------------------------------


def test_release_refuses_an_init_without_a_stage3_pin_before_any_build(
    preflight_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = seed.load_pointer

    def no_openrc(init: str, **kwargs: object) -> seed.Stage3Pointer:
        if init == "openrc":
            raise seed.SeedError("init 'openrc' has no entry in seeds/stage3.toml")
        return real(init, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(seed, "load_pointer", no_openrc)
    result = preflight_lab.release("v3", "openrc", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert "'openrc' has no entry" in result.output
    assert preflight_lab.builds.calls == []
    assert preflight_lab.maintainer.checked == []


# --- the generation fingerprint ---------------------------------------------------------------


def test_an_unreadable_generation_fingerprint_is_named_signed_and_exits_1(
    preflight_lab: Lab,
) -> None:
    pkgdir = preflight_lab.pkgdir("v3")
    pkgdir.mkdir(parents=True)
    (pkgdir / FINGERPRINT_FILE).write_text("{half a fingerprint", encoding="utf-8")
    result = preflight_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert [line for line in result.output.splitlines() if FINGERPRINT_FILE in line]
    assert "ok" in line_of(result.output, "v3", "systemd")
    entry = preflight_lab.variant("v3", "systemd")
    assert entry["toolchain"] is None and entry["generation"] == pkgdir.name
    assert (preflight_lab.the_release() / "SHA256SUMS.asc").is_file()


def test_a_child_killed_before_its_outcome_keeps_its_exit_code_in_release_json(
    preflight_lab: Lab,
) -> None:
    preflight_lab.builds.script[("v3", "systemd")] = "killed"
    result = preflight_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    entry = preflight_lab.variant("v3", "systemd")
    assert entry["exit_code"] == 137 and "no build outcome file" in entry["message"]
    assert entry["publish"] == "not run: build failed"


# --- a terminal's Ctrl-C ----------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pseudo-terminal")
def test_a_terminal_ctrl_c_reaches_the_child_build_once(tmp_path: Path) -> None:
    """R9.1 without a double delivery: the child shares the release's foreground process
    group, so the terminal already sent it its own SIGINT; ``run_build`` must not forward
    a second copy, which would cut the child's cleanup short."""
    master, slave = os.openpty()
    name = os.ttyname(slave)
    os.close(slave)
    release = subprocess.Popen(
        [sys.executable, "-P", "-c", RELEASE_ON_A_TERMINAL, TERMINAL_CHILD, str(tmp_path), name],
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "ready").exists():
            assert release.poll() is None, "the release ended before its child was ready"
            assert time.monotonic() < deadline, "the child never became ready"
            time.sleep(0.05)
        os.write(master, b"\x03")  # Ctrl-C typed at the release's terminal
        assert release.wait(timeout=30) == 42
        assert (tmp_path / "count").read_text() == "1"
    finally:
        if release.poll() is None:
            release.send_signal(signal.SIGKILL)
            release.wait()
        os.close(master)
