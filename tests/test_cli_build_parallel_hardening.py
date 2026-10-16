"""Build interrupt and environment edges (story 006, task 8.4; R1.4, R1.7, R2.6, R2.7).

Contract (design.md v4, "Build edges", "Signals"):

- in concurrent mode ``SHIDASHI_JOBS`` is set to each child's SHARE before the children
  start -- never the full ``--jobs``, never a value the build inherited; ``--serial`` keeps
  the full ``--jobs`` for its in-process assembles;
- a build stopped by SIGTERM or SIGHUP exits ``128 + signum`` (143, 129) naming each image;
- an ``OSError`` while recording the children under ``Interrupted`` is printed and the exit
  stays ``128 + signum``;
- an ``OSError`` (EIO, EPIPE: the terminal is gone after SIGHUP) from the ended or summary
  lines is suppressed -- never a traceback, never exit 1 in place of 129;
- a worker-owned arch skips the factory and still runs the children (not ``--serial``).

``run_children`` is faked (its own tests run real children and real signals); it raises
``parallel.Interrupted(results, signum=...)`` as the real one does after stopping every
child's group. The factory and the in-process assembler are recorders.
"""

import contextlib
import errno
import os
import signal
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from shidashi import audit, cli, doctor, ownership, parallel, schedule
from shidashi.assembler import AssembleResult
from shidashi.cli import app
from shidashi.parallel import ChildResult

runner = CliRunner()

#: Every build is planned (story 014): plan on this host alone, never from the real
#: recipes' worlds nor the user's paired workers (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("host_only_planner")
_IMAGES = ["--images", "kde,gnome,wm"]


class _Lab:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[str] = []
        self.child_jobs_env: str | None = None
        self.serial_jobs_env: list[str | None] = []
        self.signum: int | None = None
        self.results: list[ChildResult] = []


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Lab:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    # recorded, so whatever the build writes into os.environ is undone after the test
    monkeypatch.setenv("SHIDASHI_JOBS", "unset")
    monkeypatch.delenv("SHIDASHI_JOBS")
    state = _Lab(tmp_path)

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            self.recipe = recipe

        def build(self, **_k: object) -> None:
            state.calls.append(f"factory:{self.recipe.flavor}")

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            state.calls.append(f"in-process:{self.recipe.flavor}")
            state.serial_jobs_env.append(os.environ.get("SHIDASHI_JOBS"))
            iso = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    def run_children(argvs: Mapping[str, list[str]], *, logs: Path) -> list[ChildResult]:
        state.calls.append("children:" + ",".join(argvs))
        state.child_jobs_env = os.environ.get("SHIDASHI_JOBS")
        stopped = state.signum is not None
        state.results = [
            ChildResult(
                image=image,
                exit_code=-signal.SIGTERM if stopped else 0,
                run_id=f"child-{image}",
                log=logs / f"{image}.log",
                duration_s=12.5,
                isos=() if stopped else (tmp_path / f"bentoo-{image}.iso",),
                error="interrupted" if stopped else None,
            )
            for image in argvs
        ]
        if state.signum is not None:
            raise parallel.Interrupted(state.results, signum=state.signum)
        return state.results

    @contextlib.contextmanager
    def owner_lock(arch: str, command: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(doctor, "require_build_host", lambda _work_dir: None)
    monkeypatch.setattr(ownership, "current", lambda _arch: None)
    monkeypatch.setattr(cli, "_owner_lock", owner_lock)
    monkeypatch.setattr(parallel, "watch", lambda *_a, **_k: contextlib.nullcontext())
    monkeypatch.setattr(cli, "Factory", FakeFactory)
    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    monkeypatch.setattr(parallel, "run_children", run_children)
    return state


def _flat(text: str) -> str:
    return " ".join(text.split())


# --- SHIDASHI_JOBS: the child's share ------------------------------------------------------


def test_a_childs_environment_carries_its_share_of_jobs(lab: _Lab) -> None:
    done = runner.invoke(
        app, ["build", "v3", "systemd", *_IMAGES, "--skip-factory", "--jobs", "24"]
    )
    assert done.exit_code == 0, done.output
    assert lab.child_jobs_env == "8", f"children inherit SHIDASHI_JOBS={lab.child_jobs_env}"


def test_an_inherited_shidashi_jobs_is_replaced_by_the_share(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --jobs, 24 CPUs, and a SHIDASHI_JOBS=64 in the build's own environment: each of
    three children must see 8, never 64 each (R1.4)."""
    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    if hasattr(os, "process_cpu_count"):
        monkeypatch.setattr(os, "process_cpu_count", lambda: 24)
    monkeypatch.setenv("SHIDASHI_JOBS", "64")
    done = runner.invoke(app, ["build", "v3", "systemd", *_IMAGES, "--skip-factory"])
    assert done.exit_code == 0, done.output
    assert lab.child_jobs_env == "8", f"children inherit SHIDASHI_JOBS={lab.child_jobs_env}"


def test_serial_keeps_the_full_jobs_for_its_in_process_assembles(lab: _Lab) -> None:
    """Wrongly collapsed: the share is the children's; --serial runs every image with all."""
    argv = ["build", "v3", "systemd", *_IMAGES, "--skip-factory", "--serial", "--jobs", "24"]
    # the host's serial share: the one child a planned --serial build starts
    done = runner.invoke(app, argv, env={schedule.SERIAL_SHARE_ENV: "1"})
    assert done.exit_code == 0, done.output
    assert lab.serial_jobs_env == ["24", "24", "24"]


# --- a stopped build exits 128 + signum ----------------------------------------------------


@pytest.mark.parametrize(("signum", "code"), [(signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_a_stopped_build_exits_128_plus_the_signal_naming_each_image(
    lab: _Lab, signum: int, code: int
) -> None:
    lab.signum = signum
    done = runner.invoke(app, ["build", "v3", "systemd", *_IMAGES, "--skip-factory"])
    assert done.exit_code == code, done.output
    text = _flat(done.output)
    assert "interrupted" in text
    for image in ("kde", "gnome", "wm"):
        assert image in text


def test_an_oserror_recording_the_children_keeps_143_and_prints_the_error(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    lab.signum = signal.SIGTERM

    def amend(self: Any, path: str, **_fields: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device", "events.jsonl")

    monkeypatch.setattr(audit.Recorder, "amend", amend)
    done = runner.invoke(app, ["build", "v3", "systemd", *_IMAGES, "--skip-factory"])
    captured = capfd.readouterr()
    out = _flat(done.output + captured.out + captured.err)
    assert done.exit_code == 143, f"{done.exception!r}: {out}"
    assert not isinstance(done.exception, OSError), done.exception
    assert "No space left on device" in out, out
    assert "Traceback" not in out


@pytest.mark.parametrize("code", [errno.EIO, errno.EPIPE])
def test_a_dead_terminal_after_sighup_still_exits_129_without_a_traceback(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], code: int
) -> None:
    """SIGHUP: the terminal is gone, so the ended lines and the summary fail to write."""
    lab.signum = signal.SIGHUP
    real_print = cli._err_console.print
    failed: list[str] = []

    def print_(*args: Any, **kw: Any) -> None:
        ended = {str(parallel.ended_line(r)) for r in lab.results}
        if args and str(args[0]) in ended:
            failed.append(str(args[0]))
            raise OSError(code, os.strerror(code))
        real_print(*args, **kw)

    def echo(*_a: Any, **_k: Any) -> None:
        failed.append("summary")
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(cli._err_console, "print", print_)
    monkeypatch.setattr(typer, "echo", echo)
    done = runner.invoke(app, ["build", "v3", "systemd", *_IMAGES, "--skip-factory"])
    captured = capfd.readouterr()
    out = done.output + captured.out + captured.err
    assert failed, "no ended or summary line was written: the fixture did not run"
    assert done.exit_code == 129, f"{done.exception!r}: {out}"
    assert not isinstance(done.exception, OSError), done.exception
    assert "Traceback" not in out


# --- the worker-owned path ----------------------------------------------------------------


def test_a_worker_owned_arch_skips_the_factory_and_runs_the_children(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = SimpleNamespace(arch="v3", worker="bentoo-lab", job="j-42", commit="abc")
    monkeypatch.setattr(ownership, "current", lambda _arch: holder)
    done = runner.invoke(app, ["build", "v3", "systemd", *_IMAGES])
    assert done.exit_code == 0, done.output
    assert not any(c.startswith("factory:") for c in lab.calls), lab.calls
    assert lab.calls == ["children:gnome,kde,wm"], lab.calls
    assert "factory skipped" in _flat(done.output)
