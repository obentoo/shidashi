"""``shidashi build`` assembling its images in parallel (story 006, task 6.4).

Contract (design.md): after the factory (still sequential, R1.7), ``build`` runs
the ISO targets as child processes through ``parallel.run_children`` (R1.1), each
with ``job_share(jobs or CPU count, images)`` jobs -- the CPU-count fallback is
resolved here, ``job_share`` takes ints only (C1) -- (R1.3, R1.4) and, with
``--boot-test``, its own CID (R4.1); one image goes through the same child path (R1.8);
an interrupt stops the children and exits 130 naming each image (R2.6); it records
per image the child's run id, exit code and duration in its audit trail (R1.6), prints
the summary and exits 1 naming
each failed image and its log (R2.2) or 0 listing every ISO (R2.4). ``--serial``
keeps the previous in-process loop (R1.2).

v3: ``--verbose`` reaches every child's argv and ``--work-dir`` reaches the children
through the environment ``_apply_work_dir`` exports (R1.11); the factory keeps running
under its owner lock, released before the first child starts.

The host check (``doctor.require_build_host``), the worker lookup (``ownership.current``)
and the factory's owner lock (``cli._owner_lock``) are stubbed here, so a failure is the
launcher's and never the doctor's or the ownership record's exit 1.

``run_children`` is faked here (its own tests run real children); the factory and
the in-process assembler are recorders, as in tests/test_cli_build.py.
"""

import contextlib
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, doctor, ownership, parallel, vm
from shidashi.assembler import AssembleResult
from shidashi.cli import app
from shidashi.factory import FactoryError
from shidashi.parallel import ChildResult

runner = CliRunner()


class _Lab:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[str] = []
        self.argvs: dict[str, list[str]] = {}
        self.jobs_seen: list[int | None] = []
        self.failing: set[str] = set()
        self.logs: Path | None = None
        self.interrupt = False
        #: whether the factory's owner lock is held right now, and every time it was taken
        self.owner_held = False
        self.owner_taken: list[str] = []
        #: the SHIDASHI_* roots the children would inherit, seen when they start
        self.child_env: dict[str, str | None] = {}
        self.started_under_owner_lock = False


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Lab:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    state = _Lab(tmp_path)

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            self.recipe = recipe

        def build(self, **_k: object) -> None:
            state.calls.append(f"factory:{self.recipe.flavor}")

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe
            state.jobs_seen.append(jobs)

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            state.calls.append(f"in-process:{self.recipe.flavor}")
            iso = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    def run_children(argvs: Mapping[str, list[str]], *, logs: Path) -> list[ChildResult]:
        state.calls.append("children:" + ",".join(argvs))
        state.started_under_owner_lock = state.started_under_owner_lock or state.owner_held
        state.child_env = {n: os.environ.get(f"SHIDASHI_{n}") for n in ("CACHE", "SCRATCH", "RUNS")}
        state.argvs = {k: list(v) for k, v in argvs.items()}
        state.logs = logs
        results = [
            ChildResult(
                image=image,
                exit_code=1 if image in state.failing or state.interrupt else 0,
                run_id=f"child-{image}",
                log=logs / f"{image}.log",
                duration_s=123.4,
                isos=()
                if image in state.failing or state.interrupt
                else (tmp_path / f"bentoo-{image}.iso",),
                error="interrupted" if state.interrupt else None,
            )
            for image in argvs
        ]
        if state.interrupt:
            raise parallel.Interrupted(results)
        return results

    @contextlib.contextmanager
    def owner_lock(arch: str, command: str) -> Iterator[None]:
        state.owner_taken.append(f"{arch}:{command}")
        state.owner_held = True
        try:
            yield
        finally:
            state.owner_held = False

    monkeypatch.setattr(doctor, "require_build_host", lambda _work_dir: None)
    monkeypatch.setattr(ownership, "current", lambda _arch: None)
    monkeypatch.setattr(cli, "_owner_lock", owner_lock)
    # the per-image status lines (task 6.5) are not under test here
    monkeypatch.setattr(
        parallel, "watch", lambda *_a, **_k: contextlib.nullcontext(), raising=False
    )
    monkeypatch.setattr(cli, "Factory", FakeFactory)
    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    monkeypatch.setattr(parallel, "run_children", run_children)
    if hasattr(cli, "run_children"):
        monkeypatch.setattr(cli, "run_children", run_children)
    return state


def _value(argv: list[str], option: str) -> str | None:
    return argv[argv.index(option) + 1] if option in argv else None


def _manifest(lab: _Lab) -> dict[str, Any]:
    (run_dir,) = (lab.tmp_path / "runs").iterdir()
    return json.loads((run_dir / "manifest.json").read_text())  # type: ignore[no-any-return]


def test_build_assembles_the_images_as_children_after_the_factory(lab: _Lab) -> None:
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome,wm"])
    assert done.exit_code == 0, done.output
    assert lab.calls[-1] == "children:gnome,kde,wm"  # chain order, all at once
    assert lab.calls[:-1] == ["factory:toolbox", "factory:gnome", "factory:kde", "factory:wm"]
    for image, argv in lab.argvs.items():
        assert argv[4:8] == ["assemble", "v3", image, "systemd"]


@pytest.mark.parametrize(("jobs", "share"), [("24", "8"), ("2", "1")])
def test_each_child_gets_its_share_of_jobs(lab: _Lab, jobs: str, share: str) -> None:
    done = runner.invoke(
        app,
        ["build", "v3", "systemd", "--images", "kde,gnome,wm", "--skip-factory", "--jobs", jobs],
    )
    assert done.exit_code == 0, done.output
    assert {_value(a, "--jobs") for a in lab.argvs.values()} == {share}


def test_without_jobs_the_cpu_count_is_split(lab: _Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1.4: three children on 24 CPUs get 8 jobs each, never every CPU each."""
    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    if hasattr(os, "process_cpu_count"):
        monkeypatch.setattr(os, "process_cpu_count", lambda: 24)
    done = runner.invoke(
        app, ["build", "v3", "systemd", "--images", "kde,gnome,wm", "--skip-factory"]
    )
    assert done.exit_code == 0, done.output
    assert {_value(a, "--jobs") for a in lab.argvs.values()} == {"8"}


def test_build_options_reach_every_child(lab: _Lab) -> None:
    done = runner.invoke(
        app,
        [
            "build",
            "v3",
            "systemd",
            "--images",
            "kde,gnome",
            "--skip-factory",
            "--compression",
            "both",
            "--stage4",
            "--no-sbom",
            "--fresh",
            "--no-trunk",
            "--no-download",
            "--keep",
            "-o",
            str(lab.tmp_path / "out"),
        ],
    )
    assert done.exit_code == 0, done.output
    for argv in lab.argvs.values():
        assert _value(argv, "--compression") == "both"
        assert _value(argv, "--output-dir") == str(lab.tmp_path / "out")
        for flag in ("--stage4", "--no-sbom", "--fresh", "--no-trunk", "--no-download", "--keep"):
            assert flag in argv, flag


def test_serial_keeps_the_in_process_loop(lab: _Lab) -> None:
    done = runner.invoke(
        app,
        [
            "build",
            "v3",
            "systemd",
            "--images",
            "kde,gnome",
            "--skip-factory",
            "--serial",
            "--jobs",
            "24",
        ],
    )
    assert done.exit_code == 0, done.output
    assert lab.calls == ["in-process:gnome", "in-process:kde"]  # one after another, in order
    assert lab.jobs_seen == [24, 24]  # unsplit, as before
    steps = [s["step"] for s in _manifest(lab)["steps"]]
    assert steps == ["assemble:gnome", "assemble:kde"]


def test_the_trail_records_each_childs_run_id_exit_code_and_duration(lab: _Lab) -> None:
    lab.failing = {"kde"}
    runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"])
    steps = {s["step"]: s for s in _manifest(lab)["steps"]}
    for image, code in (("gnome", 0), ("kde", 1)):
        step = steps[f"assemble:{image}"]
        assert step["run_id"] == f"child-{image}"
        assert step["exit_code"] == code
        assert 123.4 in step.values(), step  # the child's own duration


def test_a_failed_image_fails_the_build_naming_it_and_its_log(lab: _Lab) -> None:
    lab.failing = {"kde", "wm"}
    done = runner.invoke(
        app, ["build", "v3", "systemd", "--images", "kde,gnome,wm", "--skip-factory"]
    )
    assert done.exit_code == 1
    text = done.output.replace("\n", "")
    assert lab.logs is not None and lab.logs.parent == lab.tmp_path / "scratch" / "logs"
    for image in ("kde", "wm"):
        assert str(lab.logs / f"{image}.log") in text
    assert str(lab.tmp_path / "bentoo-gnome.iso") in text  # the image that made it


def test_every_success_exits_0_listing_each_iso(lab: _Lab) -> None:
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"])
    assert done.exit_code == 0, done.output
    text = done.output.replace("\n", "")
    for image in ("kde", "gnome"):
        assert str(lab.tmp_path / f"bentoo-{image}.iso") in text


def test_boot_test_gives_each_child_its_own_cid(lab: _Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    def boot_test(iso: Path, **_k: object) -> dict[str, object]:
        raise AssertionError("the parent booted an ISO: the children boot-test their own")

    monkeypatch.setattr(vm, "boot_test", boot_test)
    done = runner.invoke(
        app, ["build", "v3", "systemd", "--images", "kde,gnome,wm", "--skip-factory", "--boot-test"]
    )
    assert done.exit_code == 0, done.output
    cids = [_value(a, "--cid") for a in lab.argvs.values()]
    assert all("--boot-test" in a for a in lab.argvs.values())
    assert None not in cids and len(set(cids)) == 3
    # never the hand-run default 42 a concurrent `assemble --boot-test` would take
    assert all(int(c) >= 100 for c in cids if c is not None)


def test_a_failed_factory_starts_no_child(lab: _Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            pass

        def build(self, **_k: object) -> None:
            raise FactoryError("toolbox failed")

    monkeypatch.setattr(cli, "Factory", BrokenFactory)
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome"])
    assert done.exit_code == 1
    assert not any(c.startswith("children") for c in lab.calls)


def test_each_child_writes_its_result_into_the_build_runs_own_directory(lab: _Lab) -> None:
    """C1: the parent puts <logs>/<image>.json in the argv; run_children reads it there."""
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"])
    assert done.exit_code == 0, done.output
    assert lab.logs is not None and lab.logs.name.startswith("build-")
    for image, argv in lab.argvs.items():
        assert _value(argv, "--result-file") == str(lab.logs / f"{image}.json")


def test_job_share_is_given_ints_the_build_resolves_an_absent_jobs(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    real = parallel.job_share

    def job_share(jobs: int, images: int) -> int:
        seen.append(jobs)
        return real(jobs, images)

    monkeypatch.setattr(parallel, "job_share", job_share)
    if hasattr(cli, "job_share"):
        monkeypatch.setattr(cli, "job_share", job_share)
    monkeypatch.setattr(os, "cpu_count", lambda: None)  # unknown CPU count -> 1
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"])
    assert done.exit_code == 0, done.output
    assert seen and all(isinstance(j, int) for j in seen)
    assert {_value(a, "--jobs") for a in lab.argvs.values()} == {"1"}


def test_a_single_image_goes_through_the_same_child_path(lab: _Lab) -> None:
    """R1.8: one image is not a special case -- one child, its own log and result."""
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde", "--skip-factory"])
    assert done.exit_code == 0, done.output
    assert lab.calls == ["children:kde"]
    assert not any(c.startswith("in-process") for c in lab.calls)


def test_an_interrupt_exits_130_naming_each_interrupted_image(lab: _Lab) -> None:
    """R2.6: the children were stopped by run_children; the build reports and exits 130."""
    lab.interrupt = True
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"])
    assert done.exit_code == 130, done.output
    text = done.output.replace("\n", "")
    for image in ("kde", "gnome"):
        assert image in text
    assert "interrupted" in text


# --- v3: --verbose, --work-dir, the factory's owner lock ---------------------------------


def test_verbose_reaches_every_childs_argv(lab: _Lab) -> None:
    done = runner.invoke(
        app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory", "--verbose"]
    )
    assert done.exit_code == 0, done.output
    assert lab.argvs and all("--verbose" in a for a in lab.argvs.values()), lab.argvs


def test_without_verbose_no_child_is_verbose(lab: _Lab) -> None:
    """Hostile half: the flag is passed through, never added on its own."""
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome", "--skip-factory"])
    assert done.exit_code == 0, done.output
    assert not any("--verbose" in a or "-v" in a for a in lab.argvs.values()), lab.argvs


def test_a_work_dir_builds_children_see_the_same_roots(lab: _Lab) -> None:
    work = lab.tmp_path / "work"
    done = runner.invoke(
        app,
        [
            "build",
            "v3",
            "systemd",
            "--images",
            "kde,gnome",
            "--skip-factory",
            "--work-dir",
            str(work),
        ],
    )
    assert done.exit_code == 0, done.output
    assert lab.calls == ["children:gnome,kde"], lab.calls
    assert lab.child_env == {
        "CACHE": str(work / "cache"),
        "SCRATCH": str(work / "scratch"),
        "RUNS": str(work / "runs"),
    }
    assert lab.logs is not None and lab.logs.parent == work / "scratch" / "logs"


def test_no_child_starts_while_the_factorys_owner_lock_is_held(lab: _Lab) -> None:
    done = runner.invoke(app, ["build", "v3", "systemd", "--images", "kde,gnome"])
    assert done.exit_code == 0, done.output
    assert lab.owner_taken, "the factory ran without its owner lock"
    assert lab.calls[-1] == "children:gnome,kde", lab.calls
    assert not lab.started_under_owner_lock, "children started under the factory's owner lock"
