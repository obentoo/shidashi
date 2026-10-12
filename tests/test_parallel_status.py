"""One line per image while the children run (story 006, task 6.5; R1.9, R1.10).

Contract (design.md v3, "Child status and the parent's lines"):

- a child ``assemble`` given the hidden ``--status-file PATH`` rewrites it (temp file +
  ``replace()``) each time a TOP-LEVEL step starts, as one JSON line
  ``{"step": "<name>", "since": <epoch>}``; a nested step does not rewrite it; a status
  file that cannot be written never fails the assemble;
- ``parallel.watch(status_files, *, console, started)`` is a context manager polling the
  status files every second while the children run: on a terminal one row per image
  ``<image>  <step>  <elapsed since the image started>``; off a terminal one plain line
  per change of an image's step, and none while nothing changes; an unreadable or
  missing status file keeps the image's last known step and never fails the build;
- ``build`` gives each child ``--status-file <build run dir>/<image>.status``; an
  ended image shows ``done`` or ``failed``.

``shidashi.parallel`` is imported inside each test, so a test of the child's option
fails on the option, not on the module.
"""

import contextlib
import importlib
import io
import json
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from shidashi import audit, cli, doctor, ownership
from shidashi.assembler import AssembleResult
from shidashi.cli import app

runner = CliRunner()


def _parallel() -> ModuleType:
    return importlib.import_module("shidashi.parallel")


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    monkeypatch.setattr(doctor, "require_build_host", lambda _work_dir: None)
    monkeypatch.setattr(ownership, "current", lambda _arch: None)
    return tmp_path


def _status(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    assert isinstance(data, dict)
    return data


# --- the child's status file ------------------------------------------------------------


def test_a_child_rewrites_its_status_file_at_each_top_level_step_only(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = lab / "scratch" / "logs" / "build-R" / "minimal.status"
    seen: list[tuple[str, dict[str, Any], float, float]] = []

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            run = audit.current()
            before = time.time()
            with run.step("seed"):
                seen.append(("seed", _status(status), before, time.time()))
                with run.step("fetch"):  # nested: the status stays "seed"
                    seen.append(("nested", _status(status), before, time.time()))
            before = time.time()
            with run.step("install"):
                seen.append(("install", _status(status), before, time.time()))
            iso = output_dir / "bentoo-x-minimal.iso"
            iso.parent.mkdir(parents=True, exist_ok=True)
            iso.write_bytes(b"ISO")
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    status.parent.mkdir(parents=True)
    done = runner.invoke(
        app,
        ["assemble", "v3", "minimal", "systemd", "-o", str(lab / "out"), "--status-file", str(status)],
    )
    assert done.exit_code == 0, done.output
    assert [(where, data["step"]) for where, data, _b, _a in seen] == [
        ("seed", "seed"),
        ("nested", "seed"),
        ("install", "install"),
    ]
    for _where, data, before, after in (seen[0], seen[2]):
        assert before - 1 <= float(data["since"]) <= after, data  # the step's own start
    assert seen[1][1]["since"] == seen[0][1]["since"]  # untouched by the nested step
    assert not [p for p in status.parent.iterdir() if p != status], "a temp file was left"


def test_an_unwritable_status_file_does_not_fail_the_assemble(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            pass

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            with audit.current().step("seed"):
                pass
            with audit.current().step("install"):
                pass
            iso = output_dir / "bentoo-x-minimal.iso"
            iso.parent.mkdir(parents=True, exist_ok=True)
            iso.write_bytes(b"ISO")
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    blocker = lab / "not-a-directory"
    blocker.write_text("a file where the status directory should be")
    done = runner.invoke(
        app,
        [
            "assemble",
            "v3",
            "minimal",
            "systemd",
            "-o",
            str(lab / "out"),
            "--status-file",
            str(blocker / "minimal.status"),
        ],
    )
    assert done.exit_code == 0, done.output
    assert (lab / "out" / "bentoo-x-minimal.iso").is_file()


# --- the parent's watch -------------------------------------------------------------------


def _write(path: Path, step: str, since: float) -> None:
    path.write_text(json.dumps({"step": step, "since": since}) + "\n")


def _lines(text: str, *words: str) -> list[str]:
    return [line for line in text.splitlines() if all(w in line for w in words)]


def test_on_a_terminal_watch_shows_one_row_per_image_with_its_step_and_elapsed(
    tmp_path: Path,
) -> None:
    parallel = _parallel()
    now = time.time()
    files = {"kde": tmp_path / "kde.status", "gnome": tmp_path / "gnome.status"}
    _write(files["kde"], "install", now - 60)
    _write(files["gnome"], "squashfs:zstd", now - 30)
    out = io.StringIO()
    console = Console(file=out, force_terminal=True, width=120, color_system=None)
    with parallel.watch(files, console=console, started={"kde": now - 3725, "gnome": now - 125}):
        time.sleep(1.5)
    text = out.getvalue()
    kde, gnome = _lines(text, "kde", "install"), _lines(text, "gnome", "squashfs:zstd")
    assert kde and gnome, text
    # elapsed since the IMAGE started (1h02m05s, 2m05s), not since its step did
    assert any("1:02:0" in line or "1h02m" in line for line in kde), kde
    assert any("2:0" in line or "2m0" in line for line in gnome), gnome


def test_off_a_terminal_watch_prints_one_line_per_step_change_and_none_otherwise(
    tmp_path: Path,
) -> None:
    parallel = _parallel()
    now = time.time()
    kde = tmp_path / "kde.status"
    _write(kde, "seed", now)
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    with parallel.watch({"kde": kde}, console=console, started={"kde": now}):
        time.sleep(2.5)  # two polls or more, nothing changed
        _write(kde, "install", now + 2)
        time.sleep(2.5)
    text = out.getvalue()
    assert len(_lines(text, "kde", "seed")) <= 1, text  # never repeated while unchanged
    assert len(_lines(text, "kde", "install")) == 1, text


def test_an_unreadable_or_missing_status_file_keeps_the_last_known_step(
    tmp_path: Path,
) -> None:
    parallel = _parallel()
    now = time.time()
    kde, gnome = tmp_path / "kde.status", tmp_path / "gnome.status"  # gnome's never appears
    _write(kde, "install", now)
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    with parallel.watch(
        {"kde": kde, "gnome": gnome}, console=console, started={"kde": now, "gnome": now}
    ):
        time.sleep(1.5)
        kde.write_text('{"step": "squa')  # a half-written file
        time.sleep(1.5)
        kde.unlink()
        time.sleep(1.5)
    text = out.getvalue()
    assert _lines(text, "kde", "install"), text
    kde_lines = _lines(text, "kde")
    assert all("install" in line for line in kde_lines), f"the last known step was lost: {text}"


# --- the build gives each child its status file ------------------------------------------


@pytest.fixture
def build_lab(lab: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    parallel = _parallel()
    state: dict[str, Any] = {"argvs": {}, "logs": None, "failing": set()}

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            pass

        def build(self, **_k: object) -> None:
            pass

    def run_children(argvs: Mapping[str, list[str]], *, logs: Path) -> list[Any]:
        state["argvs"] = {k: list(v) for k, v in argvs.items()}
        state["logs"] = logs
        return [
            parallel.ChildResult(
                image=image,
                exit_code=1 if image in state["failing"] else 0,
                run_id=f"child-{image}",
                log=logs / f"{image}.log",
                duration_s=1.0,
                isos=() if image in state["failing"] else (lab / f"bentoo-{image}.iso",),
                error=None,
            )
            for image in argvs
        ]

    @contextlib.contextmanager
    def owner_lock(_arch: str, _command: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(cli, "Factory", FakeFactory)
    monkeypatch.setattr(cli, "_owner_lock", owner_lock)
    monkeypatch.setattr(parallel, "run_children", run_children)
    if hasattr(cli, "run_children"):
        monkeypatch.setattr(cli, "run_children", run_children)
    return state


def _value(argv: list[str], option: str) -> str | None:
    return argv[argv.index(option) + 1] if option in argv else None


def test_the_build_passes_each_child_its_status_file_in_the_run_dir(
    build_lab: dict[str, Any],
) -> None:
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"])
    assert done.exit_code == 0, done.output
    logs = build_lab["logs"]
    assert logs is not None and logs.name.startswith("build-")
    paths = {image: _value(argv, "--status-file") for image, argv in build_lab["argvs"].items()}
    assert paths == {image: str(logs / f"{image}.status") for image in ("kde", "gnome")}


def test_an_ended_image_shows_done_or_failed(build_lab: dict[str, Any]) -> None:
    build_lab["failing"] = {"kde"}
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"])
    assert done.exit_code == 1
    assert _lines(done.output, "gnome", "done"), done.output
    assert _lines(done.output, "kde", "failed"), done.output
