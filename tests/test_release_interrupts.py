"""``shidashi release`` interrupted, or failing after the builds (story 030, task 5.1 fix 1).

Kept apart from tests/test_release_cli.py: that file is the pre-authored contract and is
frozen byte for byte. Each test pins one finding of the 5.1 Tech Review:

- an interrupt while the signatures are checked leaves no ``SHA256SUMS.asc``, and that file
  never exists before every signature is verified, so not even a SIGKILL can leave an
  unverified one (R6.6); a rerun is then not refused as "already signed";
- a SIGINT the release did not forward does not use up the forward: a later SIGTERM still
  reaches the child, and a second forwarded signal is still held back (R9.1);
- a SIGHUP while a child runs stops the release once the child ended, and is not sent on:
  a hangup reaches the child's process group itself. Outside a child build it stops the
  release like SIGTERM. A SIGHUP ignored on purpose (``nohup``) stays ignored;
- an interrupt while ``release.json`` is written leaves no temporary file (R5.3);
- a publish that fails on its recipe, and an outcome file that cannot be cleared, are
  recorded, and the release goes on to ``release.json`` and the signature (R4.4, R2.4);
- the interrupted line names the UTC day on which a rerun continues the directory (R1.8).

Fix 2 (Tech Review 2, findings C and D):

- an interrupt while ``SHA256SUMS`` or a signature is written leaves no temporary file;
- the interrupted line says the release is signed when ``SHA256SUMS.asc`` was moved in
  before the stop, and "nothing signed" only when it was not.
"""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pydantic
import pytest

from shidashi import gpgsign, publish, release, seed
from shidashi.recipe import RecipeChainError, RecipeFileError
from tests.test_release_cli import Lab, line_of, patch_everywhere, release_lab, utc_day
from tests.test_release_core import FPR

#: A child build that records every stop signal it receives and ends on its own.
RECORDING_CHILD = """
import pathlib, signal, sys, time
marks = pathlib.Path(sys.argv[1])
got = []
def note(signum, frame):
    got.append(signum)
    (marks / "signals").write_text(" ".join(str(s) for s in got))
for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(s, note)
(marks / "ready").write_text("ready")
deadline = time.monotonic() + float(sys.argv[2])
while time.monotonic() < deadline:
    time.sleep(0.05)
"""

#: The release side, in its own process and session (no controlling terminal), so a
#: signal sent to it never reaches pytest. argv[3] == "foreground" takes every SIGINT for
#: a terminal's Ctrl-C, as a release in its terminal's foreground group does.
RELEASE_SIDE = """
import signal, sys
from shidashi import release
if sys.argv[4] == "foreground":
    release._terminal_sent_it_to_the_child = lambda signum: signum == signal.SIGINT
try:
    code = release.run_build([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
except KeyboardInterrupt:
    sys.exit(42)
sys.exit(code)
"""


@pytest.fixture
def interrupt_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


def run_release_side(
    tmp_path: Path, signals: Sequence[signal.Signals], *, lifetime: float, foreground: bool
) -> tuple[int, str]:
    """The release's exit code and the signals its child received, in order."""
    for signum in signals:
        if signal.getsignal(signum) is signal.SIG_IGN:
            pytest.skip(f"{signum.name} is ignored here; the release keeps it ignored")
    mode = "foreground" if foreground else "background"
    side = subprocess.Popen(
        [sys.executable, "-P", "-c", RELEASE_SIDE, RECORDING_CHILD, str(tmp_path)]
        + [str(lifetime), mode],
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "ready").exists():
            assert side.poll() is None, "the release ended before its child was ready"
            assert time.monotonic() < deadline, "the child never became ready"
            time.sleep(0.05)
        for signum in signals:
            side.send_signal(signum)
            time.sleep(0.5)
        code = side.wait(timeout=30)
    finally:
        if side.poll() is None:
            side.kill()
            side.wait()
    got = tmp_path / "signals"
    return code, got.read_text() if got.exists() else ""


# --- signing (R6.6) ---------------------------------------------------------------------------


def test_sha256sums_asc_appears_only_after_every_signature_is_verified(
    interrupt_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No handler sees a SIGKILL: only a signature that is not there yet cannot be left
    behind unverified."""
    checked = seed.gpg_verify
    present: list[bool] = []

    def watching(*files: Path, keyring: Path = seed.GENTOO_KEYRING) -> object:
        present.append((Path(keyring).parent / "SHA256SUMS.asc").exists())
        return checked(*files, keyring=keyring)

    monkeypatch.setattr(seed, "gpg_verify", watching)
    result = interrupt_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert present and not any(present), present
    assert (interrupt_lab.the_release() / "SHA256SUMS.asc").is_file()
    assert sorted(interrupt_lab.root.rglob("SHA256SUMS.asc")) == [
        interrupt_lab.the_release() / "SHA256SUMS.asc"
    ]


def test_an_interrupt_while_signatures_are_checked_leaves_no_sha256sums_asc_and_a_rerun_signs(
    interrupt_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked = seed.gpg_verify

    def interrupted(*files: Path, keyring: Path = seed.GENTOO_KEYRING) -> object:
        raise KeyboardInterrupt  # Ctrl-C, or the release's SIGTERM handler, mid-check

    monkeypatch.setattr(seed, "gpg_verify", interrupted)
    result = interrupt_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 130, result.output
    assert sorted(interrupt_lab.root.rglob("SHA256SUMS.asc")) == []

    monkeypatch.setattr(seed, "gpg_verify", checked)
    again = interrupt_lab.release("v3", "systemd", "--key", FPR)
    assert again.exit_code == 0, again.output
    assert (interrupt_lab.the_release() / "SHA256SUMS.asc").is_file()


# --- forwarding to the child (R9.1) -----------------------------------------------------------


def test_a_sigterm_after_a_sigint_that_was_not_forwarded_still_reaches_the_child(
    tmp_path: Path,
) -> None:
    code, got = run_release_side(
        tmp_path, [signal.SIGINT, signal.SIGTERM], lifetime=3, foreground=True
    )
    assert code == 42
    assert got == str(int(signal.SIGTERM))


def test_a_second_forwarded_signal_is_held_back(tmp_path: Path) -> None:
    code, got = run_release_side(
        tmp_path, [signal.SIGTERM, signal.SIGTERM], lifetime=3, foreground=False
    )
    assert code == 42
    assert got == str(int(signal.SIGTERM))


def test_a_sighup_while_a_child_runs_stops_the_release_and_is_not_sent_on(
    tmp_path: Path,
) -> None:
    """A hangup reaches the child's process group itself: a second copy from the release
    would end the child's grace period at once (parallel._stop)."""
    code, got = run_release_side(tmp_path, [signal.SIGHUP], lifetime=2, foreground=False)
    assert code == 42
    assert got == ""


# --- SIGHUP outside a child build -------------------------------------------------------------


def hangup_then(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every child build is preceded by a SIGHUP to the release itself."""
    builds = lab.builds

    def hangup_then_build(argv: Sequence[str]) -> int:
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(0.2)  # room for the handler to run before the build
        return builds(argv)

    patch_everywhere(monkeypatch, "shidashi.release", "run_build", hangup_then_build)


def test_a_sighup_stops_the_release_like_sigterm(
    interrupt_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test's own handler stands guard: a release without one would leave it the
    SIGHUP, and the release would go on."""
    if signal.getsignal(signal.SIGHUP) is signal.SIG_IGN:
        pytest.skip("SIGHUP is ignored here; the release keeps it ignored")
    guarded: list[int] = []
    previous = signal.signal(signal.SIGHUP, lambda signum, _frame: guarded.append(signum))
    try:
        hangup_then(interrupt_lab, monkeypatch)
        result = interrupt_lab.release("v3", "systemd", "--key", FPR)
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert guarded == []
    assert result.exit_code == 130, result.output
    assert interrupt_lab.builds.calls == []
    assert sorted(interrupt_lab.root.rglob("*.asc")) == []


def test_a_sighup_ignored_on_purpose_stays_ignored(
    interrupt_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        hangup_then(interrupt_lab, monkeypatch)
        result = interrupt_lab.release("v3", "systemd", "--key", FPR)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert result.exit_code == 0, result.output


# --- release.json (R5.3) ----------------------------------------------------------------------


def test_an_interrupt_while_release_json_is_written_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "release.json").write_text('{"date": "earlier"}\n', encoding="utf-8")

    def interrupted(_fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupted)  # release.py reads it from os at call time
    with pytest.raises(KeyboardInterrupt):
        release.write_manifest(tmp_path, {"date": "now"})
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["release.json"]
    assert (tmp_path / "release.json").read_text(encoding="utf-8") == '{"date": "earlier"}\n'


# --- failures after the builds (R4.4, R2.4) ---------------------------------------------------


def validation_error() -> pydantic.ValidationError:
    try:
        pydantic.TypeAdapter(int).validate_python("not a number")
    except pydantic.ValidationError as err:
        return err
    raise AssertionError("pydantic accepted a string for an int")


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RecipeFileError("arch/v3/recipe.yaml: exclude.0: empty item"), id="file"),
        pytest.param(RecipeChainError("stage 'kde' starts a loop"), id="chain"),
        pytest.param(validation_error(), id="validation"),
    ],
)
def test_a_publish_that_fails_on_its_recipe_is_recorded_and_the_release_is_signed(
    interrupt_lab: Lab, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """``binhost_cli._toolbox_for`` loads the recipe inside the publish: one edited during
    the hours-long run must not abort the release before ``release.json``."""

    def broken(*_args: object, **_kwargs: object) -> object:
        raise error

    patch_everywhere(monkeypatch, "shidashi.publish_pool", "publish", broken)
    result = interrupt_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert "publish: failed:" in line_of(result.output, "v3", "systemd")
    entry = interrupt_lab.variant("v3", "systemd")
    assert entry["status"] == "ok"
    assert entry["publish"].startswith("failed:") and "\n" not in entry["publish"]
    assert (interrupt_lab.the_release() / "SHA256SUMS.asc").is_file()


def test_an_outcome_file_that_cannot_be_cleared_is_an_error_and_the_release_goes_on(
    interrupt_lab: Lab,
) -> None:
    day = interrupt_lab.releases / utc_day()
    day.mkdir(parents=True)
    (day / ".outcome").write_text("a file where the outcome directory goes", encoding="utf-8")
    result = interrupt_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert interrupt_lab.builds.calls == []
    entry = interrupt_lab.variant("v3", "systemd")
    assert entry["status"] == "error" and ".outcome" in entry["message"]
    assert (day / "SHA256SUMS.asc").is_file()


# --- the interrupted line (R1.8, R9.2) --------------------------------------------------------


def test_the_interrupted_line_names_the_utc_day_a_rerun_continues(interrupt_lab: Lab) -> None:
    """A rerun after 00:00 UTC starts a new directory: "rerun to continue" alone is wrong."""
    interrupt_lab.builds.script[("v3", "systemd")] = "interrupt"
    result = interrupt_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 130, result.output
    stopped = [line for line in result.output.splitlines() if line.startswith("interrupted")]
    assert len(stopped) == 1, result.output
    assert f"on {utc_day()} (UTC)" in stopped[0]
    assert str(interrupt_lab.the_release()) in stopped[0]


# --- temp files of the other writers in the release directory (R5.3) ------------------------


def test_an_interrupt_while_sha256sums_is_written_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish.update_sha256sums(tmp_path, {"earlier.iso": "a" * 64})
    before = (tmp_path / "SHA256SUMS").read_bytes()

    def interrupted(self: Path, target: object) -> Path:
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        publish.update_sha256sums(tmp_path, {"now.iso": "b" * 64})
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")) == []
    assert (tmp_path / "SHA256SUMS").read_bytes() == before


def test_an_interrupt_while_a_signature_is_written_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupted)  # gpgsign reads it from os at call time
    with pytest.raises(KeyboardInterrupt):
        gpgsign._write_published_file(tmp_path / "x.iso.DIGESTS.asc", b"signature")
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


# --- the interrupted line reads the directory (R6.6, R9.2) ----------------------------------


def interrupt_the_summary_once_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop that lands after ``SHA256SUMS.asc`` was moved in: during the summary."""
    summary = release.summary

    def interrupted(*args: Any, signed: bool, **kwargs: Any) -> Any:
        if signed:
            raise KeyboardInterrupt
        return summary(*args, signed=signed, **kwargs)

    monkeypatch.setattr(release, "summary", interrupted)


def test_an_interrupt_after_the_signature_was_moved_in_says_the_release_is_signed(
    interrupt_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupt_the_summary_once_signed(monkeypatch)
    result = interrupt_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 130, result.output
    assert (interrupt_lab.the_release() / "SHA256SUMS.asc").is_file()
    stopped = [line for line in result.output.splitlines() if line.startswith("interrupted")]
    assert len(stopped) == 1, result.output
    assert "nothing signed" not in stopped[0]
    assert "signed and verified" in stopped[0]
    assert str(interrupt_lab.the_release()) in "".join(result.output.split())


def test_an_interrupt_while_signing_still_says_nothing_signed(
    interrupt_lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: the signature is not in place yet, so the release is not signed."""

    def interrupted(*files: Path, keyring: Path = seed.GENTOO_KEYRING) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(seed, "gpg_verify", interrupted)
    result = interrupt_lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 130, result.output
    stopped = [line for line in result.output.splitlines() if line.startswith("interrupted")]
    assert len(stopped) == 1 and "nothing signed" in stopped[0], result.output
