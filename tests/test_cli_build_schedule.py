"""Tests of `shidashi build` distributed across the paired workers (story 014, 4.2).

``build`` resolves the image specs, discovers the nodes (``workers_dir`` = the invoking
user's, R6.1), reads each node's history, estimates, plans, records a ``plan`` step
(R5.1), then executes the plan (``--plan`` prints it and exits 0; ``--local`` plans on
the host without probing any worker). EVERY build takes that path: a host-only build is
a plan whose only node is the host, executed and reported like any other. When the plan
gives a worker an image, a dirty tree (``schedule.dirty_files()``, untracked files
included) is refused unless ``--allow-dirty`` (R3.4, C6).

Owner check (R3.3, R3.5) -- story 010's handling in ``build`` is REUSED, not rebuilt:
a worker holding the arch's lock (``_worker_holder``) means no factory and a line naming
it; otherwise the factory runs inside ``_owner_lock`` (``ownership.acquire``), released
before ``execute``; ``OwnedElsewhere`` there exits 1 naming the owner; a lock that cannot
be read exits 1 naming it. The real ``ownership`` module is used, its locks in a
temporary cache.

``execute`` gets ``options`` (``BuildOptions``), ``logs``, ``run_id`` and a ``runner``:
the CLI's own per-image launcher, which records each child as an ``assemble:<image>``
step of the build's trail. ``schedule.Interrupted(outcomes, jobs, signum)`` prints the
host results and, per worker job, ``shidashi worker logs <w> <job> -f`` and
``shidashi worker sync pull <w> --arch <arch> --job <job>``, then exits ``128 + signum``
(R4.7).

Image specs, discovery, history, the dirty-tree probe and execution are faked at
``shidashi.schedule``; ``plan``, ``estimate``, ``render_plan`` and ``render_report`` are
the real ones; the factory as in tests/test_cli_build.py.
"""

import json
import os
import pwd
import signal
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, ownership, parallel, schedule
from shidashi.assembler import AssembleResult
from shidashi.cli import app

runner = CliRunner()
LAB = "bentoo-lab"
HISTORY_RUN = "20261003T100000Z-hist01"


def _node(
    name: str, kind: str, threads: int, load1: float, *, reachable: bool = True, note: str = ""
) -> schedule.Node:
    return schedule.Node(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        threads=threads,
        load1=load1,
        runnable_arches=frozenset({"v3", "znver5"} if kind == "host" else {"v3"}),
        trunks=frozenset(),
        sec_per_pkg=None,
        reachable=reachable,
        note=note,
    )


HOST = _node("dojo", "host", 31, 3.25)
WORKER = _node(LAB, "worker", 13, 0.75)
SILENT = _node(LAB, "worker", 13, 0.75, reachable=False, note="status probe timed out after 10 s")
BUSY = _node(
    "lab-busy", "worker", 16, 0.5, reachable=False, note="job shidashi-job-fac-v3 already running"
)


class FakeHistory:
    def duration(self, _key: str) -> float | None:
        return None

    def trunk_duration(self, _key: str) -> float | None:
        return None

    def packages(self, _key: str) -> int | None:
        return None

    def sec_per_pkg(self) -> float | None:
        return None

    def run_ids(self) -> list[str]:
        return [HISTORY_RUN]


def _worker_owner(arch: str) -> ownership.Owner:
    return ownership.Owner(
        arch=arch,
        worker=LAB,
        job=f"fac-{arch}",
        commit="25aebf5" + "0" * 33,
        since="2026-10-05T14:02:00Z",
        host_pid=None,
    )


@dataclass
class Lab:
    root: Path
    nodes: list[schedule.Node] = field(default_factory=lambda: [HOST])
    calls: list[str] = field(default_factory=list)
    discovered: list[dict[str, Any]] = field(default_factory=list)
    executed: list[tuple[Any, Any, dict[str, Any]]] = field(default_factory=list)
    failing: dict[str, Path] = field(default_factory=dict)
    no_sbom: set[str] = field(default_factory=set)
    dirty: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    lock_at_execute: list[Any] = field(default_factory=list)
    interrupt: BaseException | None = None
    run_through_runner: bool = False
    children: list[str] = field(default_factory=list)


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Lab]:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.delenv("SUDO_USER", raising=False)
    state = Lab(root=tmp_path)

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            self.recipe = recipe

        def build(self, **_k: object) -> None:
            holder = ownership.current(self.recipe.arch)
            state.calls.append(f"factory:{self.recipe.flavor}")
            state.order.append(f"factory(held={holder is not None and holder.worker})")

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            state.calls.append(f"assemble:{self.recipe.flavor}")
            iso = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    monkeypatch.setattr(cli, "Factory", FakeFactory)
    monkeypatch.setattr(cli, "Assembler", FakeAssembler)

    def run_children(argvs: Any, *_a: object, **kw: Any) -> list[parallel.ChildResult]:
        out = []
        for key in argvs:
            state.children.append(str(key))
            out.append(
                parallel.ChildResult(
                    image=str(key),
                    exit_code=0,
                    run_id=f"20261015T120100Z-{str(key)[:6]:0<6}",
                    log=Path(kw["logs"]) / f"{key}.log",
                    duration_s=1.0,
                    isos=(tmp_path / f"bentoo-x-{key}.iso",),
                    error=None,
                )
            )
        return out

    monkeypatch.setattr(parallel, "run_children", run_children)

    def discover(**kw: Any) -> list[schedule.Node]:
        state.discovered.append(kw)
        return (
            [n for n in state.nodes if n.kind == "host"] if kw.get("local") else list(state.nodes)
        )

    def image_specs(arch: str, init: str, images: Any, **_k: Any) -> dict[str, Any]:
        return {
            f"{arch}-{i}-{init}": schedule.ImageSpec(
                key=f"{arch}-{i}-{init}", arch=arch, image=i, packages=500
            )
            for i in images
        }

    def execute(plan: Any, specs: Any, estimates: Any, **kw: Any) -> list[schedule.Outcome]:
        state.executed.append((plan, specs, kw))
        state.order.append("execute")
        state.lock_at_execute.append(ownership.current("v3"))
        if state.run_through_runner:
            host = [specs[k].image for k, n in plan.assignments.items() if n == "dojo"]
            kw["runner"]({image: ["true", image] for image in host}, logs=kw["logs"])
        if state.interrupt is not None:
            raise state.interrupt
        outcomes = []
        for key, node in plan.assignments.items():
            log = state.failing.get(key)
            outcomes.append(
                schedule.Outcome(
                    image=key,
                    node=node,
                    ok=log is None,
                    duration_s=100.0,
                    estimate_s=90.0,
                    log=log,
                    isos=() if log else (tmp_path / f"{key}.iso",),
                    error="job exited 1" if log else "",
                    sbom=key not in state.no_sbom,
                )
            )
        return outcomes

    for name, fake in (
        ("image_specs", image_specs),
        ("discover", discover),
        ("load_history", lambda *_a, **_k: FakeHistory()),
        ("dirty_files", lambda *_a, **_k: list(state.dirty)),
        ("execute", execute),
    ):
        monkeypatch.setattr(schedule, name, fake)
        if hasattr(cli, name):  # in case cli imported the name itself
            monkeypatch.setattr(cli, name, fake)
    yield state


def _build(*args: str) -> Any:
    return runner.invoke(app, ["build", "v3", "systemd", "--images", "minimal,kde", *args])


def _build_manifest(root: Path) -> dict[str, Any]:
    for path in sorted((root / "runs").glob("*/manifest.json")):
        manifest = json.loads(path.read_text())
        if manifest.get("command") == "build":
            return manifest
    raise AssertionError("no build run")


def _plan_step(root: Path) -> dict[str, Any]:
    manifest = _build_manifest(root)
    steps = [s for s in manifest["steps"] if str(s.get("step")).split("/")[-1] == "plan"]
    assert steps, [s.get("step") for s in manifest["steps"]]
    return steps[0]


def _host_only(lab: Lab) -> None:
    ((plan, _specs, _kw),) = lab.executed
    assert sorted(plan.assignments) == ["v3-kde-systemd", "v3-minimal-systemd"]
    assert set(plan.assignments.values()) == {"dojo"}


# --- the new options are plain bool flags ------------------------------------------------


def test_build_takes_plan_local_and_allow_dirty_as_bool_flags() -> None:
    # a value-taking option would make isaguard.job_target swallow the arch after it
    import typer.main

    from shidashi import isaguard

    command = typer.main.get_command(app).commands["build"]  # type: ignore[attr-defined]
    flags = {
        opt: getattr(p, "is_flag", None) for p in command.params for opt in getattr(p, "opts", ())
    }
    for name in ("--plan", "--local", "--allow-dirty"):
        assert flags.get(name) is True, name
    args = ["build", "--plan", "--local", "--allow-dirty", "v3", "systemd"]
    assert isaguard.job_target(args) == ("v3", "systemd")


# --- --plan (R1.5) -----------------------------------------------------------------------


def test_build_plan_prints_each_images_node_and_builds_nothing(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER]
    result = _build("--plan")
    assert result.exit_code == 0, result.output
    assert "minimal" in result.output and "kde" in result.output
    assert "dojo" in result.output and LAB in result.output  # the plan splits these two
    assert lab.calls == [] and lab.executed == [] and lab.children == []
    assert lab.discovered and not lab.discovered[0].get("local")
    assert ownership.current("v3") is None  # no lock taken for a preview


def test_build_plan_names_the_silent_and_the_busy_worker_with_their_reasons(lab: Lab) -> None:
    lab.nodes = [HOST, SILENT, BUSY]
    result = _build("--plan")
    assert result.exit_code == 0, result.output
    assert LAB in result.output and "timed out" in result.output
    assert "lab-busy" in result.output and "fac-v3" in result.output


# --- whose workers (R6.1) ----------------------------------------------------------------


def test_build_under_sudo_discovers_the_sudo_users_workers(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home-alice"
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1000")
    monkeypatch.setenv("HOME", str(tmp_path / "root-home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    real = pwd.getpwnam
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda name: (
            pwd.struct_passwd(("alice", "x", 1000, 1000, "", str(home), "/bin/bash"))
            if name == "alice"
            else real(name)
        ),
    )
    result = _build("--plan")
    assert result.exit_code == 0, result.output
    (kw,) = lab.discovered
    assert Path(kw["workers_dir"]) == home / ".local" / "share" / "shidashi" / "worker"


# --- --local and no reachable worker (R1.6, R3.1) ----------------------------------------


def test_build_local_plans_on_the_host_without_probing_a_worker(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER]
    result = _build("--local")
    assert result.exit_code == 0, result.output
    assert [kw.get("local") for kw in lab.discovered] == [True]
    _host_only(lab)  # executed and reported through the plan, like any build


def test_build_with_no_reachable_worker_is_a_host_only_plan_naming_the_silent_worker(
    lab: Lab,
) -> None:
    lab.nodes = [HOST, SILENT]
    result = _build()
    assert result.exit_code == 0, result.output
    _host_only(lab)
    assert LAB in result.output and "timed out" in result.output


# --- distributed (R1.1, R4.4, R4.5, R4.9) ------------------------------------------------


def test_build_with_a_reachable_worker_executes_one_plan_covering_every_image_once(
    lab: Lab,
) -> None:
    lab.nodes = [HOST, WORKER]
    result = _build()
    assert result.exit_code == 0, result.output
    ((plan, specs, kw),) = lab.executed
    assert sorted(plan.assignments) == ["v3-kde-systemd", "v3-minimal-systemd"]
    assert set(plan.assignments.values()) == {"dojo", LAB}
    # hostile: nothing is ALSO built by the old in-process loop or the old launcher
    assert [c for c in lab.calls if c.startswith("assemble:")] == []
    assert lab.children == []
    assert set(specs) >= set(plan.assignments)
    assert isinstance(kw["options"], schedule.BuildOptions)
    assert kw["run_id"] and kw["run_id"] in str(kw["logs"])


def test_build_hands_execute_a_runner_that_records_each_child_in_the_trail(lab: Lab) -> None:
    # story 006's per-image record (assemble:<image> steps) survives the scheduler
    lab.run_through_runner = True
    result = _build("--local")
    assert result.exit_code == 0, result.output
    assert sorted(lab.children) == ["kde", "minimal"]
    steps = [s["step"] for s in _build_manifest(lab.root)["steps"]]
    assert "assemble:minimal" in steps and "assemble:kde" in steps


def test_build_distributed_report_names_each_images_node_and_a_missing_sbom(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER]
    lab.no_sbom = {"v3-kde-systemd", "v3-minimal-systemd"}
    result = _build()
    assert result.exit_code == 0, result.output
    ((plan, _specs, _kw),) = lab.executed
    for node in set(plan.assignments.values()):
        assert node in result.output
    assert "SBOM" in result.output


def test_build_distributed_failure_exits_1_naming_the_image_its_node_and_log(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER]
    lab.failing = {"v3-kde-systemd": Path("/var/tmp/kde.log")}
    result = _build()
    assert result.exit_code == 1, result.output
    lines = [line for line in result.output.splitlines() if "/var/tmp/kde.log" in line]
    assert lines and "kde" in lines[0]


# --- the plan is recorded (R5.1) ---------------------------------------------------------


def test_build_records_the_plan_and_its_inputs_in_the_audit_trail(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER]
    result = _build()
    assert result.exit_code == 0, result.output
    text = json.dumps(_plan_step(lab.root))
    for needle in ("dojo", LAB, "minimal", "kde", HISTORY_RUN, "31", "13", "3.25", "0.75"):
        assert needle in text, needle


def test_build_records_the_plan_of_a_local_build_too(lab: Lab) -> None:
    lab.nodes = [HOST]
    result = _build("--local")
    assert result.exit_code == 0, result.output
    assert "dojo" in json.dumps(_plan_step(lab.root))


# --- the same commit on every node (R3.4, contract C6) -----------------------------------


def test_build_refuses_a_dirty_tree_when_a_worker_has_a_share(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER]
    lab.dirty = ["shidashi/schedule.py", "notes/untracked.txt"]
    result = _build()
    assert result.exit_code == 1, result.output
    assert "shidashi/schedule.py" in result.output and "notes/untracked.txt" in result.output
    assert lab.executed == [] and lab.calls == []  # nothing built, not even the factory


def test_build_allow_dirty_builds_a_dirty_tree(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER]
    lab.dirty = ["shidashi/schedule.py"]
    result = _build("--allow-dirty")
    assert result.exit_code == 0, result.output
    ((_plan, _specs, kw),) = lab.executed
    assert kw["options"].allow_dirty is True


def test_build_a_host_only_plan_ignores_a_dirty_tree(lab: Lab) -> None:
    lab.nodes = [HOST, WORKER]
    lab.dirty = ["shidashi/schedule.py"]
    result = _build("--local")
    assert result.exit_code == 0, result.output
    _host_only(lab)


def test_dirty_files_lists_untracked_files_and_trusts_a_checkout_root_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def porcelain(argv: list[str]) -> str:
        if "-z" in argv:
            return " M shidashi/cli.py\0?? notes/untracked.txt\0"
        return " M shidashi/cli.py\n?? notes/untracked.txt\n"

    def run(argv: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(x) for x in argv]
        seen.append(argv)
        out = porcelain(argv)
        data: Any = (
            out
            if kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
            else out.encode()
        )
        return subprocess.CompletedProcess(
            argv, 0, stdout=data, stderr="" if isinstance(data, str) else b""
        )

    def check_output(argv: Any, *a: Any, **kw: Any) -> Any:
        return run(argv, *a, **kw).stdout

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", check_output)
    files = schedule.dirty_files()
    assert any(f.endswith("shidashi/cli.py") for f in files), files
    assert any(f.endswith("notes/untracked.txt") for f in files), files  # untracked counts
    (argv,) = [a for a in seen if any(Path(x).name == "git" for x in a[:1])]
    assert any(
        argv[i] == "-c" and argv[i + 1].startswith("safe.directory=") for i in range(len(argv) - 1)
    ), argv
    assert not any(x in ("--untracked-files=no", "-uno") for x in argv)


# --- the owner of the arch's binhost (R3.3, R3.5): story 010's handling, reused ----------


def test_build_skips_the_factory_of_an_arch_a_worker_owns_and_says_who(lab: Lab) -> None:
    ownership.acquire("v3", _worker_owner("v3"))
    result = _build("--local")
    assert result.exit_code == 0, result.output
    assert not [c for c in lab.calls if c.startswith("factory:")]
    assert LAB in result.output
    _host_only(lab)  # assembles only read the binhost
    holder = ownership.current("v3")
    assert holder is not None and holder.worker == LAB  # never taken over


def test_build_runs_the_factory_when_the_worker_owns_another_arch(lab: Lab) -> None:
    # hostile: the owner of znver5's binhost is not the owner of v3's
    ownership.acquire("znver5", _worker_owner("znver5"))
    result = _build("--local")
    assert result.exit_code == 0, result.output
    assert [c for c in lab.calls if c.startswith("factory:")] == ["factory:toolbox", "factory:kde"]
    # the factory inside the host's lock, the assembles after its release
    assert all(o.startswith("factory(held=host:") for o in lab.order if o.startswith("factory"))
    assert lab.order[-1] == "execute"
    assert lab.lock_at_execute == [None]


def test_build_owned_elsewhere_from_the_owner_lock_exits_1_naming_the_owner(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the race: nobody owned v3 at the check, then a worker took it before the factory
    def acquire(arch: str, _owner: ownership.Owner) -> ownership.Owner:
        raise ownership.OwnedElsewhere(_worker_owner(arch))

    monkeypatch.setattr(ownership, "acquire", acquire)
    result = _build("--local")
    assert result.exit_code == 1, result.output
    assert LAB in result.output
    assert lab.executed == [] and lab.calls == []


def test_build_an_unreadable_owner_lock_exits_1_naming_it(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    def current(_arch: str) -> ownership.Owner | None:
        raise ownership.LockError(
            Path("/var/cache/shidashi/locks/v3.lock"), "root", "check its permissions"
        )

    monkeypatch.setattr(ownership, "current", current)
    result = _build("--local")
    assert result.exit_code == 1, result.output
    assert "/var/cache/shidashi/locks" in result.output
    assert lab.calls == [] and lab.executed == []


# --- --serial (R4.8) ---------------------------------------------------------------------


def test_build_serial_reaches_the_build_options(lab: Lab) -> None:
    result = _build("--local", "--serial")
    assert result.exit_code == 0, result.output
    ((_plan, _specs, kw),) = lab.executed
    assert kw["options"].serial is True
    assert [c for c in lab.calls if c.startswith("assemble:")] == []  # not in-process


# --- an interrupt (R4.7) -----------------------------------------------------------------


@pytest.mark.parametrize(("signum", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_build_interrupted_prints_each_worker_jobs_commands_and_exits_128_plus_signal(
    lab: Lab, signum: int, code: int
) -> None:
    lab.nodes = [HOST, WORKER]
    lab.interrupt = schedule.Interrupted(
        [
            schedule.Outcome(
                image="v3-minimal-systemd",
                node="dojo",
                ok=False,
                duration_s=10.0,
                estimate_s=90.0,
                log=Path("/var/tmp/minimal.log"),
                error="interrupted",
            )
        ],
        [schedule.WorkerJob(worker=LAB, job="build-abc123", results=Path("/var/tmp/wr"))],
        signum,
    )
    result = _build()
    assert result.exit_code == code, result.output
    assert f"shidashi worker logs {LAB} build-abc123 -f" in result.output
    assert f"shidashi worker sync pull {LAB} --arch v3 --job build-abc123" in result.output
    assert "minimal" in result.output and "interrupted" in result.output
