"""The ``audit: <path>`` line survives a dead terminal (story 006, task 9.3; R2.6, R2.7).

Contract (design.md v5, "Build edges"):

- the ``audit: <path>`` line every audited command prints when its run ends runs under the
  same dead-terminal guard as the ended and summary lines: an ``OSError`` (EIO from a
  hung-up tty, EPIPE from a closed pipe) or rich's broken-pipe ``SystemExit(1)`` there is
  dropped and the stream pointed at ``/dev/null`` (so the interpreter's flush at exit
  cannot fail too and exit 120);
- so the command's own exit is never replaced by a traceback, ``1`` or ``120``: a build
  stopped by SIGHUP exits 129 (SIGTERM 143), a successful command 0, a failing one its own
  failure (exit 1, its own error -- never the terminal's);
- on a live terminal the line is still shown.

Only the audit line fails here: the ended and summary lines have their own tests
(tests/test_cli_build_parallel_hardening.py). ``run_children`` is faked as there (it raises
``parallel.Interrupted`` after "stopping" every child); the factory is skipped.
"""

import contextlib
import errno
import os
import signal
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, TextIO

import pytest
from typer.testing import CliRunner, Result

from shidashi import audit, cli, doctor, ownership, parallel, vm
from shidashi.cli import app
from shidashi.parallel import ChildResult

runner = CliRunner()
_BUILD = ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"]


class _Lab:
    def __init__(self) -> None:
        self.signum: int | None = None
        self.fail: bool = False
        self.audit_lines: list[str] = []
        self.devnull: list[bool] = []


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Lab:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    monkeypatch.setenv("SHIDASHI_JOBS", "unset")
    monkeypatch.delenv("SHIDASHI_JOBS")
    monkeypatch.setattr(audit, "repo_state", lambda: {})
    state = _Lab()

    def run_children(argvs: Mapping[str, list[str]], *, logs: Path) -> list[ChildResult]:
        stopped = state.signum is not None
        results = [
            ChildResult(
                image=image,
                exit_code=(-signal.SIGTERM if stopped else (1 if state.fail else 0)),
                run_id=f"child-{image}",
                log=logs / f"{image}.log",
                duration_s=12.5,
                isos=() if stopped or state.fail else (tmp_path / f"bentoo-{image}.iso",),
                error="interrupted" if stopped else ("boom" if state.fail else None),
            )
            for image in argvs
        ]
        if state.signum is not None:
            raise parallel.Interrupted(results, signum=state.signum)
        return results

    @contextlib.contextmanager
    def owner_lock(arch: str, command: str) -> Iterator[None]:
        yield

    real_to_devnull = cli._to_devnull

    def to_devnull(stream: TextIO) -> None:
        state.devnull.append(stream is sys.stderr)
        real_to_devnull(stream)

    monkeypatch.setattr(doctor, "require_build_host", lambda _work_dir: None)
    monkeypatch.setattr(ownership, "current", lambda _arch: None)
    monkeypatch.setattr(cli, "_owner_lock", owner_lock)
    monkeypatch.setattr(parallel, "watch", lambda *_a, **_k: contextlib.nullcontext())
    monkeypatch.setattr(parallel, "run_children", run_children)
    monkeypatch.setattr(cli, "_to_devnull", to_devnull)
    # rich's broken-pipe handling sets quiet: restored after every test
    monkeypatch.setattr(cli._err_console, "quiet", False)
    return state


def _dead_at_the_audit_line(lab: _Lab, monkeypatch: pytest.MonkeyPatch, fail: Any) -> None:
    """The terminal is gone exactly when the ``audit: <path>`` line is written."""
    real_print = cli._err_console.print

    def print_(*args: Any, **kw: Any) -> None:
        if args and str(args[0]).startswith("audit: "):
            lab.audit_lines.append(str(args[0]))
            fail()
        real_print(*args, **kw)

    monkeypatch.setattr(cli._err_console, "print", print_)


def _os_error(code: int) -> Any:
    def fail() -> None:
        raise OSError(code, os.strerror(code))

    return fail


def _rich_broken_pipe() -> None:
    """What rich does on EPIPE (``Console.on_broken_pipe``): go quiet, ``SystemExit(1)``."""
    cli._err_console.quiet = True
    raise SystemExit(1)


def _out(done: Result, capfd: pytest.CaptureFixture[str]) -> str:
    captured = capfd.readouterr()
    return done.output + captured.out + captured.err


def _assert_kept(lab: _Lab, done: Any, out: str, code: int) -> None:
    assert lab.audit_lines, "no audit line was written: the fixture did not run"
    assert done.exit_code == code, f"exit {done.exit_code}, not {code}: {done.exception!r}: {out}"
    assert not isinstance(done.exception, OSError), done.exception
    assert "Traceback" not in out, out
    assert True in lab.devnull, "the stream was not pointed at /dev/null after the audit line"


# --- hostile halves: the audit line must never replace the exit -----------------------------


@pytest.mark.parametrize("code", [errno.EIO, errno.EPIPE])
@pytest.mark.parametrize(("signum", "exit_code"), [(signal.SIGHUP, 129), (signal.SIGTERM, 143)])
def test_a_dead_terminal_at_the_audit_line_keeps_128_plus_the_signal(
    lab: _Lab,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    code: int,
    signum: int,
    exit_code: int,
) -> None:
    lab.signum = signum
    _dead_at_the_audit_line(lab, monkeypatch, _os_error(code))
    done = runner.invoke(app, _BUILD)
    out = _out(done, capfd)
    _assert_kept(lab, done, out, exit_code)
    assert os.strerror(code) not in out, f"the terminal's error replaced the interrupt: {out}"


def test_richs_broken_pipe_exit_at_the_audit_line_keeps_129(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """rich answers EPIPE with ``SystemExit(1)``: that 1 must not become the build's exit."""
    lab.signum = signal.SIGHUP
    _dead_at_the_audit_line(lab, monkeypatch, _rich_broken_pipe)
    done = runner.invoke(app, _BUILD)
    _assert_kept(lab, done, _out(done, capfd), 129)


@pytest.mark.parametrize("code", [errno.EIO, errno.EPIPE])
def test_a_dead_terminal_at_the_audit_line_keeps_a_successful_build_at_0(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], code: int
) -> None:
    _dead_at_the_audit_line(lab, monkeypatch, _os_error(code))
    done = runner.invoke(app, _BUILD)
    _assert_kept(lab, done, _out(done, capfd), 0)


@pytest.mark.parametrize("code", [errno.EIO, errno.EPIPE])
def test_a_dead_terminal_at_the_audit_line_keeps_a_failing_builds_own_failure(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], code: int
) -> None:
    """Exit 1 either way -- so the failure reported must be the build's (the failed images),
    never the terminal's ``error: [Errno N] ...``."""
    lab.fail = True
    _dead_at_the_audit_line(lab, monkeypatch, _os_error(code))
    done = runner.invoke(app, _BUILD)
    out = _out(done, capfd)
    _assert_kept(lab, done, out, 1)
    assert os.strerror(code) not in out, f"the terminal's error replaced the build's: {out}"
    assert "kde" in out and "gnome" in out, f"the failed images are not named: {out}"


@pytest.mark.usefixtures("qemu_host")
def test_a_dead_terminal_at_the_audit_line_keeps_vm_test_at_0(
    lab: _Lab,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Every audited command, not only build."""
    iso = tmp_path / "bentoo-x-kde.iso"
    iso.write_bytes(b"ISO")
    monkeypatch.setattr(
        vm, "read_build_info", lambda _iso: {"arch": "v3", "flavor": "kde", "init": "systemd"}
    )
    firmware = {"passed": True, "ssh_after_s": 1.0, "checks": [], "metrics": {}}
    monkeypatch.setattr(
        vm, "boot_test", lambda *_a, **_k: {"passed": True, "firmwares": {"bios": firmware}}
    )
    _dead_at_the_audit_line(lab, monkeypatch, _os_error(errno.EIO))
    done = runner.invoke(app, ["vm", "test", str(iso), "--firmware", "bios", "--cid", "42"])
    _assert_kept(lab, done, _out(done, capfd), 0)


# --- then a live terminal still shows the line -----------------------------------------------


def test_a_live_terminal_still_shows_the_audit_line(
    lab: _Lab, capfd: pytest.CaptureFixture[str]
) -> None:
    done = runner.invoke(app, _BUILD)
    out = _out(done, capfd)
    assert done.exit_code == 0, out
    runs = Path(os.environ["SHIDASHI_RUNS"])
    lines = [line for line in out.splitlines() if line.startswith("audit: ")]
    assert len(lines) == 1, out
    assert lines[0].removeprefix("audit: ").startswith(str(runs)), lines[0]
    assert lab.devnull == [], "a live terminal was pointed at /dev/null"
