"""Unit tests of shidashi.schedule.execute -- the plan run on every node at once.

Contract (design.md v3, story 014):
``execute(plan, specs, estimates, *, options, logs, run_id, runner=parallel.run_children)
-> list[Outcome]``.

- ONE ``runner(argvs, logs=logs, ...)`` call on the main thread holds every child (R4.1):
  the host's assemble children -- ``parallel.assemble_argv(spec.image, ...)`` exactly as
  ``cli.build`` passes it today (``jobs=job_share(options.jobs or cpu_count, n_host)``,
  ``result_file=logs/<image>.json``, ``cid=100+index`` with ``--boot-test``,
  ``verbose``, ``status_file=logs/<image>.status``) -- and ONE child per worker, key
  ``@<worker>``, running ``... worker job <worker> build-<run_id[-6:]> --results
  <scratch>/worker-results/<worker>/<job> [--allow-dirty] -- build <arch> <init> --images
  <its images> --local --skip-factory [--boot-test]`` (never ``--jobs``: the worker splits
  its own threads). Under ``sudo`` the worker child drops to ``SUDO_USER`` through
  ``setpriv --reuid/--regid/--init-groups`` with that user's ``HOME`` and every
  ``SHIDASHI_*`` kept (R6.1).
- With ``options.serial`` the host share is ONE child ``build <arch> <init> --images
  <host images> --serial --local --skip-factory ... --output-dir <out>`` with the
  unsplit ``--jobs`` (R4.8).
- After the call, a worker's outcomes come from its results directory (``<job>.rc``,
  ``<job>.runs``, ``iso/``) and the runs pulled into ``config.runs_dir()``: an image is ok
  only when the worker build run's ``assemble:<image>`` step is ok, an assemble run of it
  has every step ok and its ISO is in ``results/iso/``; its ``duration_s`` is that
  assemble run's; else it fails naming what is missing (R4.6). A failed child fails only
  its worker's images, quoting the error text of its log (``JobRefused``'s reason and
  fix) (R3.2).
- Each ok worker ISO moves into ``options.output_dir`` with its companions
  (``.DIGESTS``, ``.contents.gz``, ``.packages``, ``.spdx.json`` when present,
  ``latest-<key>.txt``) and the worker's ``*.build.tar.zst`` bundle, owned like the
  output directory (R6.3), and their lines from ``results/iso/SHA256SUMS`` are merged
  into the output directory's SHA256SUMS, never overwriting the host's (R4.2). A worker
  ISO with no ``.spdx.json`` is ``Outcome(sbom=False)`` (R4.9).
- An unplaceable image is a failed outcome "no node runs <arch>" (R1.7); no owner lock is
  taken; ``estimate_s`` comes from the estimates (R4.4).
- ``parallel.Interrupted(results, signum)`` becomes ``schedule.Interrupted(outcomes,
  jobs, signum)`` with the host outcomes and the started ``WorkerJob``s (R4.7).

The runner is a fake with the real ``run_children`` contract: it returns real
``parallel.ChildResult``s, writes each child's log at ``logs/<key>.log``, and -- like the
real one -- gives a child whose argv has no ``--result-file`` ``error="no result file"``
(a worker child has none: its outcome is read from its results directory). For a worker
child it does what ``shidashi worker job`` leaves behind.
"""

import dataclasses
import hashlib
import json
import os
import pwd
import signal
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from shidashi import ownership, parallel, schedule, vm

IMAGES = ("minimal", "kde", "gnome", "wm")
LAB = "bentoo-lab"
LAB2 = "lab-two"
SPLIT = {"minimal": "dojo", "kde": "dojo", "gnome": LAB, "wm": LAB}
RUN_ID = "20261015T120000Z-a1b2c3"
JOB = "build-a1b2c3"
DATE = "2026.10.15"


def _iso_name(image: str) -> str:
    return f"bentoo-{DATE}-{image}-systemd-v3.iso"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class WorkerBehaviour:
    """What one worker's ``worker job`` child does, image by image."""

    exit_code: int = 0
    log_text: str = "job build-a1b2c3 on bentoo-lab exited 0\n"
    write_rc: bool = True
    no_run: set[str] = field(default_factory=set)  # its assemble run is not pulled
    no_iso: set[str] = field(default_factory=set)  # its ISO did not arrive
    failed: set[str] = field(default_factory=set)  # its assemble failed on the worker
    no_sbom: set[str] = field(default_factory=set)  # syft absent: no .spdx.json


@dataclass
class Lab:
    out: Path
    logs: Path
    scratch: Path
    runs: Path
    calls: list[dict[str, list[str]]] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)
    behaviour: dict[str, WorkerBehaviour] = field(default_factory=dict)
    host_rc: dict[str, int] = field(default_factory=dict)
    host_error: dict[str, str] = field(default_factory=dict)
    interrupt: int | None = None
    acquired: list[object] = field(default_factory=list)
    booted: list[object] = field(default_factory=list)
    chowned: list[tuple[str, int, int]] = field(default_factory=list)
    worker_sums: dict[str, dict[str, str]] = field(default_factory=dict)

    def host_argvs(self) -> dict[str, list[str]]:
        (call,) = self.calls
        return {k: v for k, v in call.items() if not _is_worker_child(v)}

    def worker_argvs(self) -> dict[str, list[str]]:
        (call,) = self.calls
        return {k: v for k, v in call.items() if _is_worker_child(v)}


def _is_worker_child(argv: list[str]) -> bool:
    return any(a == "worker" and b == "job" for a, b in zip(argv, argv[1:], strict=False))


def _job_part(argv: list[str]) -> tuple[list[str], list[str]]:
    """``(worker job ... options, remote build args)`` of a worker child's argv."""
    start = next(i for i in range(len(argv) - 1) if argv[i] == "worker" and argv[i + 1] == "job")
    rest = argv[start:]
    cut = len(rest) - 1 - rest[::-1].index("--")
    return rest[:cut], rest[cut + 1 :]


def _option(argv: list[str], name: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _write_run(runs: Path, run_id: str, manifest: dict[str, Any]) -> None:
    run = runs / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "manifest.json").write_text(json.dumps({"run": run_id, **manifest}))


def _simulate_worker_job(lab: Lab, key: str, argv: list[str]) -> parallel.ChildResult:
    """What ``shidashi worker job`` leaves on the host: results dir and pulled runs."""
    worker_name = key.removeprefix("@")
    options, remote_args = _job_part(argv)
    job = options[3]
    results = Path(_option(options, "--results") or "")
    images = (_option(remote_args, "--images") or "").split(",")
    how = lab.behaviour.setdefault(worker_name, WorkerBehaviour())
    log = lab.logs / f"{key}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(how.log_text)
    if how.write_rc:
        iso_dir = results / "iso"
        iso_dir.mkdir(parents=True, exist_ok=True)
        (results / f"{job}.log").write_text("fake-shidashi: building\n")
        build_id = f"20261015T120500Z-b{worker_name[:5]:0<5}"
        sums: dict[str, str] = {}
        steps, run_ids = [], [build_id]
        for n, image in enumerate(images):
            assemble_id = f"20261015T12060{n}Z-{image[:6]:0<6}"
            ok = image not in how.failed
            steps.append(
                {
                    "step": f"assemble:{image}",
                    "status": "ok" if ok else "error",
                    "run_id": assemble_id,
                    "exit_code": 0 if ok else 1,
                    "log": f"/mnt/work/scratch/logs/build-{build_id}/{image}.log",
                    "error": None if ok else "assemble exited 1",
                    "duration_s": 230.0,
                }
            )
            if image not in how.no_run:
                run_ids.append(assemble_id)
                _write_run(
                    lab.runs,
                    assemble_id,
                    {
                        "command": "assemble",
                        "argv": [
                            "-c",
                            "assemble",
                            "v3",
                            image,
                            "systemd",
                            "--jobs",
                            "8",
                            "--output-dir",
                            f"/mnt/work/out/iso/{job}",
                        ],
                        "hostname": worker_name,
                        "inputs": {"hostname": worker_name, "threads": 16},
                        "started": "2026-10-15T12:06:00.000+00:00",
                        "status": "ok" if ok else "error",
                        "duration_s": 222.0,
                        "steps": [
                            {"step": "seed", "status": "ok", "duration_s": 12.0},
                            {
                                "step": "install",
                                "status": "ok" if ok else "error",
                                "duration_s": 180.0,
                            },
                            {"step": "iso:zstd", "status": "ok", "duration_s": 30.0},
                        ],
                        "artifacts": [
                            {
                                "path": f"/mnt/work/out/iso/{job}/{_iso_name(image)}",
                                "role": "iso:zstd",
                            }
                        ],
                    },
                )
            if ok and image not in how.no_iso:
                iso = iso_dir / _iso_name(image)
                iso.write_bytes(f"{worker_name}-{image}".encode())
                companions = {
                    f"{iso.name}.DIGESTS": b"# SHA512 HASH\n",
                    f"{iso.name}.contents.gz": b"\x1f\x8bcontents",
                    f"{iso.name}.packages": b"app-misc/x-1\n",
                }
                if image not in how.no_sbom:
                    companions[f"{iso.name}.spdx.json"] = b'{"spdxVersion": "SPDX-2.3"}'
                for name, data in companions.items():
                    (iso_dir / name).write_bytes(data)
                    if not name.endswith(".spdx.json"):
                        sums[name] = _sha(data)
                sums[iso.name] = _sha(iso.read_bytes())
                (iso_dir / f"latest-v3-{image}-systemd.txt").write_text(f"{iso.name} 12\n")
        _write_run(
            lab.runs,
            build_id,
            {
                "command": "build",
                "argv": ["-c", *remote_args, "--output-dir", f"/mnt/work/out/iso/{job}"],
                "hostname": worker_name,
                "started": "2026-10-15T12:05:00.000+00:00",
                "status": "ok" if not how.failed else "error",
                "steps": steps,
            },
        )
        bundle = iso_dir / f"{build_id}.build.tar.zst"
        bundle.write_bytes(b"(bundle)")
        sums[bundle.name] = _sha(bundle.read_bytes())
        (iso_dir / "SHA256SUMS").write_text("".join(f"{d}  {n}\n" for n, d in sorted(sums.items())))
        lab.worker_sums[worker_name] = sums
        (results / f"{job}.runs").write_text("".join(f"{r}\n" for r in run_ids))
        (results / f"{job}.rc").write_text(f"{how.exit_code}\n")
    return parallel.ChildResult(
        image=key,
        exit_code=how.exit_code,
        run_id=None,
        log=log,
        duration_s=333.0,
        isos=(),
        error="no result file",  # the real run_children: no --result-file in its argv
    )


def _host_child(lab: Lab, key: str, argv: list[str]) -> parallel.ChildResult:
    image = key
    if "assemble" in argv:
        image = argv[argv.index("assemble") + 2]
    log = lab.logs / f"{key}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"assemble {image}\n")
    rc = lab.host_rc.get(image, 0)
    iso = lab.out / _iso_name(image)
    if rc == 0:
        iso.write_bytes(f"host-{image}".encode())
    has_result = "--result-file" in argv
    error = lab.host_error.get(image)
    if error is None and rc != 0:
        error = "exit code 1"
    if error is None and not has_result:
        error = "no result file"
    return parallel.ChildResult(
        image=key,
        exit_code=rc,
        run_id=f"20261015T120100Z-{image[:6]:0<6}",
        log=log,
        duration_s=111.0,
        isos=(iso,) if rc == 0 else (),
        error=error,
    )


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Lab]:
    state = Lab(
        out=tmp_path / "out",
        logs=tmp_path / "logs" / f"build-{RUN_ID}",
        scratch=tmp_path / "scratch",
        runs=tmp_path / "runs",
    )
    state.out.mkdir()
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(state.scratch))
    monkeypatch.setenv("SHIDASHI_RUNS", str(state.runs))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 32)

    def acquire(*a: Any, **_k: Any) -> Any:
        state.acquired.append(a)
        return a[-1]

    def boot_test(iso: Any, **_k: Any) -> dict[str, bool]:
        state.booted.append(iso)
        return {"passed": True}

    monkeypatch.setattr(ownership, "acquire", acquire)
    monkeypatch.setattr(vm, "boot_test", boot_test)
    real_chown = os.chown

    def chown(path: Any, uid: int, gid: int, *a: Any, **k: Any) -> None:
        state.chowned.append((Path(path).name, uid, gid))
        real_chown(path, uid, gid, *a, **k)

    monkeypatch.setattr(os, "chown", chown)
    yield state


def _runner(lab: Lab) -> Any:
    def run(argvs: dict[str, list[str]], *_a: object, **kw: Any) -> list[parallel.ChildResult]:
        lab.calls.append({str(k): [str(a) for a in v] for k, v in argvs.items()})
        lab.threads.append(threading.current_thread())
        assert Path(kw["logs"]) == lab.logs
        results = [
            _simulate_worker_job(lab, str(k), [str(a) for a in v])
            if _is_worker_child([str(a) for a in v])
            else _host_child(lab, str(k), [str(a) for a in v])
            for k, v in argvs.items()
        ]
        if lab.interrupt is not None:
            stopped = [dataclasses.replace(r, error="interrupted") for r in results]
            raise parallel.Interrupted(stopped, signum=lab.interrupt)
        return results

    return run


def _options(lab: Lab, **over: Any) -> schedule.BuildOptions:
    values: dict[str, Any] = {
        "arch": "v3",
        "init": "systemd",
        "output_dir": lab.out,
        "images": IMAGES,
        "jobs": 24,
        "boot_test": False,
        "compressions": ("zstd",),
        "stage4": False,
        "sbom": True,
        "fresh": False,
        "trunk": True,
        "download": True,
        "keep": False,
        "allow_dirty": False,
        "serial": False,
        "verbose": False,
    }
    values.update(over)
    return schedule.BuildOptions(**values)


def _specs() -> dict[str, schedule.ImageSpec]:
    trunked = {"kde", "gnome", "wm"}
    return {
        f"v3-{n}-systemd": schedule.ImageSpec(
            key=f"v3-{n}-systemd",
            arch="v3",
            image=n,
            packages=1000,
            trunk_key="v3-desktop-systemd" if n in trunked else None,
            trunk_packages=700 if n in trunked else 0,
        )
        for n in IMAGES
    }


def _estimates(where: dict[str, str]) -> dict[tuple[str, str], schedule.Estimate]:
    return {
        (f"v3-{n}-systemd", node): schedule.Estimate(
            seconds=100.0 + i, trunk_seconds=0.0, source="history"
        )
        for i, (n, node) in enumerate(where.items())
    }


def _plan(where: dict[str, str], unplaceable: dict[str, str] | None = None) -> schedule.Plan:
    return schedule.Plan(
        assignments={f"v3-{n}-systemd": node for n, node in where.items()},
        finish={},
        makespan=0.0,
        method="exhaustive",
        skipped_nodes={},
        unplaceable=unplaceable or {},
    )


def _run(lab: Lab, where: dict[str, str] = SPLIT, **over: Any) -> list[schedule.Outcome]:
    return schedule.execute(
        _plan(where),
        _specs(),
        _estimates(where),
        options=_options(lab, **over),
        logs=lab.logs,
        run_id=RUN_ID,
        runner=_runner(lab),
        host="dojo",  # the plan's host node, as discover names it (4.1 deviation)
    )


def _name(outcome: Any) -> str:
    text = str(outcome.image)
    for name in IMAGES:
        if text == name or f"-{name}-" in f"-{text}-":
            return name
    raise AssertionError(f"no image in {text!r}")


def _outcome(outcomes: list[schedule.Outcome], name: str) -> schedule.Outcome:
    (found,) = [o for o in outcomes if _name(o) == name]
    return found


# --- one runner call, one child per worker (R4.1) ------------------------------------------


def test_execute_holds_every_child_in_one_runner_call_on_the_main_thread(lab: Lab) -> None:
    outcomes = _run(lab)
    assert len(lab.calls) == 1
    assert lab.threads == [threading.main_thread()]
    assert sorted(_name(o) for o in outcomes) == sorted(IMAGES)
    assert list(lab.worker_argvs()) == [f"@{LAB}"]  # one child for the worker's two images
    assert len(lab.host_argvs()) == 2
    assert all(o.ok for o in outcomes), [(o.image, o.error) for o in outcomes]


def test_execute_gives_each_worker_its_own_child(lab: Lab) -> None:
    where = {"minimal": "dojo", "kde": LAB2, "gnome": LAB, "wm": LAB}
    _run(lab, where)
    assert sorted(lab.worker_argvs()) == sorted([f"@{LAB}", f"@{LAB2}"])
    for key, argv in lab.worker_argvs().items():
        _options_part, remote_args = _job_part(argv)
        images = sorted((_option(remote_args, "--images") or "").split(","))
        assert images == (["kde"] if key == f"@{LAB2}" else ["gnome", "wm"]), key


def test_execute_the_worker_child_runs_its_images_as_one_skip_factory_build(lab: Lab) -> None:
    _run(lab)
    (argv,) = lab.worker_argvs().values()
    job_options, remote_args = _job_part(argv)
    assert job_options[:4] == ["worker", "job", LAB, JOB]  # build-<run id tail>
    results = Path(_option(job_options, "--results") or "")
    assert results == lab.scratch / "worker-results" / LAB / JOB
    assert remote_args[:3] == ["build", "v3", "systemd"]
    assert sorted((_option(remote_args, "--images") or "").split(",")) == ["gnome", "wm"]
    # the worker neither re-plans nor takes the arch's owner lock for a factory
    assert "--local" in remote_args and "--skip-factory" in remote_args
    assert "--jobs" not in remote_args  # the worker splits its own threads (C4)
    assert "--allow-dirty" not in job_options


def test_execute_allow_dirty_reaches_the_worker_job(lab: Lab) -> None:
    _run(lab, allow_dirty=True)
    (argv,) = lab.worker_argvs().values()
    job_options, remote_args = _job_part(argv)
    assert "--allow-dirty" in job_options and "--allow-dirty" not in remote_args


def test_execute_without_sudo_runs_the_worker_child_as_this_user(lab: Lab) -> None:
    _run(lab)
    (argv,) = lab.worker_argvs().values()
    assert "setpriv" not in argv


def test_execute_under_sudo_the_worker_child_drops_to_the_invoking_user(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home-alice"
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1000")
    monkeypatch.setenv("HOME", str(tmp_path / "root-home"))
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
    _run(lab)
    (argv,) = lab.worker_argvs().values()
    head = argv[: argv.index("worker")]
    assert any(Path(a).name == "setpriv" for a in head), head
    assert "--init-groups" in head
    assert _option(head, "--reuid") in ("1000", "alice")
    assert _option(head, "--regid") in ("1000", "alice")
    assert f"HOME={home}" in head  # the user's pairing key and known_hosts, not root's
    if "-i" in head or "--ignore-environment" in head:
        # a cleared environment must carry every SHIDASHI_* forward itself
        assert f"SHIDASHI_SCRATCH={lab.scratch}" in head
        assert f"SHIDASHI_RUNS={lab.runs}" in head


# --- the host share is cli.build's (story 006) -----------------------------------------------


def test_execute_the_host_children_are_the_argvs_cli_build_passes(lab: Lab) -> None:
    _run(lab, jobs=24)
    got = sorted(lab.host_argvs().values())
    want = sorted(
        parallel.assemble_argv(
            image,
            arch="v3",
            init="systemd",
            jobs=parallel.job_share(24, 2),
            output_dir=lab.out,
            result_file=lab.logs / f"{image}.json",
            compressions=("zstd",),
            stage4=False,
            sbom=True,
            fresh=False,
            trunk=True,
            download=True,
            keep=False,
            boot_test=False,
            cid=None,
            verbose=False,
            status_file=lab.logs / f"{image}.status",
        )
        for image in ("minimal", "kde")
    )
    assert [[str(a) for a in w] for w in want] == got


def test_execute_without_jobs_splits_the_cpu_count(lab: Lab) -> None:
    _run(lab, jobs=None)
    for argv in lab.host_argvs().values():
        assert _option(argv, "--jobs") == str(parallel.job_share(32, 2))


def test_execute_boot_test_gives_each_host_child_its_own_cid_from_100(lab: Lab) -> None:
    _run(lab, boot_test=True)
    cids = sorted(int(_option(argv, "--cid") or -1) for argv in lab.host_argvs().values())
    assert cids == [100, 101]
    assert all("--boot-test" in argv for argv in lab.host_argvs().values())
    (argv,) = lab.worker_argvs().values()
    _job_options, remote_args = _job_part(argv)
    assert "--boot-test" in remote_args  # the worker boot-tests what it built (R4.3)
    assert lab.booted == []  # a worker's ISO is not re-tested on the host


def test_execute_without_boot_test_asks_no_node_for_one(lab: Lab) -> None:
    _run(lab, boot_test=False)
    assert not any("--boot-test" in argv for argv in lab.host_argvs().values())
    (argv,) = lab.worker_argvs().values()
    assert "--boot-test" not in _job_part(argv)[1]


def test_execute_serial_makes_the_host_share_one_serial_build_child(lab: Lab) -> None:
    # R4.8: --serial assembles the host's images one at a time, in one child
    _run(lab, serial=True, jobs=24)
    (argv,) = lab.host_argvs().values()
    assert "assemble" not in argv
    start = argv.index("build")
    assert argv[start : start + 3] == ["build", "v3", "systemd"]
    assert sorted((_option(argv, "--images") or "").split(",")) == ["kde", "minimal"]
    for flag in ("--serial", "--local", "--skip-factory"):
        assert flag in argv, flag
    assert _option(argv, "--jobs") == "24"  # the unsplit jobs
    assert Path(_option(argv, "--output-dir") or "") == lab.out
    assert list(lab.worker_argvs()) == [f"@{LAB}"]  # the workers still run at once


# --- outcomes: node, estimate, duration (R4.4) -------------------------------------------------


def test_execute_reports_each_image_on_its_node_with_its_estimate(lab: Lab) -> None:
    outcomes = _run(lab)
    estimates = _estimates(SPLIT)
    for name, node in SPLIT.items():
        got = _outcome(outcomes, name)
        assert got.node == node, name
        assert got.estimate_s == pytest.approx(estimates[(f"v3-{name}-systemd", node)].seconds)


def test_execute_a_host_images_outcome_is_its_childs_result(lab: Lab) -> None:
    kde = _outcome(_run(lab), "kde")
    assert kde.ok and kde.duration_s == pytest.approx(111.0)
    assert kde.log is not None and Path(kde.log).parent == lab.logs
    assert [Path(p).name for p in kde.isos] == [_iso_name("kde")]


def test_execute_a_worker_images_duration_is_its_assemble_runs(lab: Lab) -> None:
    gnome = _outcome(_run(lab), "gnome")
    assert gnome.ok and gnome.duration_s == pytest.approx(222.0)  # not the child's 333 s


# --- results on the host (R4.2, R4.9, R6.3) -------------------------------------------------


def test_execute_a_worker_iso_its_companions_and_the_bundle_land_in_the_output_dir(
    lab: Lab,
) -> None:
    outcomes = _run(lab)
    gnome = _outcome(outcomes, "gnome")
    assert [Path(p) for p in gnome.isos] == [lab.out / _iso_name("gnome")]
    iso = _iso_name("gnome")
    for name in (
        iso,
        f"{iso}.DIGESTS",
        f"{iso}.contents.gz",
        f"{iso}.packages",
        f"{iso}.spdx.json",
        "latest-v3-gnome-systemd.txt",
    ):
        assert (lab.out / name).is_file(), name
    assert list(lab.out.glob("*.build.tar.zst")), "the worker's build bundle"
    results_iso = lab.scratch / "worker-results" / LAB / JOB / "iso"
    assert not (results_iso / iso).exists()  # moved, not copied


def test_execute_merges_the_workers_sums_next_to_the_hosts_never_over_them(lab: Lab) -> None:
    host_line = f"{'0' * 64}  bentoo-2026.10.01-older-systemd-v3.iso\n"
    (lab.out / "SHA256SUMS").write_text(host_line)
    _run(lab)
    sums = (lab.out / "SHA256SUMS").read_text()
    assert host_line in sums  # the host's own lines are kept
    lines = sums.splitlines()
    for name, digest in lab.worker_sums[LAB].items():
        assert lines.count(f"{digest}  {name}") == 1, name
    assert sum(1 for line in lines if line.endswith(f"  {_iso_name('wm')}")) == 1


def test_execute_moved_results_are_owned_like_the_output_dir(lab: Lab) -> None:
    _run(lab)
    want = (lab.out.stat().st_uid, lab.out.stat().st_gid)
    iso = _iso_name("gnome")
    for name in (iso, f"{iso}.DIGESTS", f"{iso}.packages"):
        st = (lab.out / name).stat()
        assert (st.st_uid, st.st_gid) == want, name
    assert all((uid, gid) == want for _n, uid, gid in lab.chowned), lab.chowned


def test_execute_a_worker_iso_without_an_sbom_says_so(lab: Lab) -> None:
    # hostile half first: the image without .spdx.json, then its sibling with one
    lab.behaviour[LAB] = WorkerBehaviour(no_sbom={"gnome"})
    outcomes = _run(lab)
    gnome, wm = _outcome(outcomes, "gnome"), _outcome(outcomes, "wm")
    assert gnome.ok and gnome.sbom is False
    assert wm.ok and wm.sbom is True
    assert _outcome(outcomes, "kde").sbom is True  # the host's ISOs are not judged here


# --- a worker's image that did not make it (R4.6, R3.2) --------------------------------------


def test_execute_a_worker_image_without_its_iso_fails_naming_it(lab: Lab) -> None:
    lab.behaviour[LAB] = WorkerBehaviour(no_iso={"gnome"})
    outcomes = _run(lab)
    gnome = _outcome(outcomes, "gnome")
    assert not gnome.ok and gnome.node == LAB and "iso" in gnome.error.lower()
    assert _outcome(outcomes, "wm").ok


def test_execute_a_stale_iso_left_in_the_results_is_not_this_jobs_iso(lab: Lab) -> None:
    # hostile collapse: a pull never deletes on the host, so an earlier gnome ISO may sit
    # in results/iso/; it is not the ISO this job's gnome assemble produced
    lab.behaviour[LAB] = WorkerBehaviour(no_iso={"gnome"})
    stale_dir = lab.scratch / "worker-results" / LAB / JOB / "iso"
    stale_dir.mkdir(parents=True)
    (stale_dir / "bentoo-2026.10.01-gnome-systemd-v3.iso").write_bytes(b"old gnome")
    outcomes = _run(lab)
    gnome = _outcome(outcomes, "gnome")
    assert not gnome.ok and "iso" in gnome.error.lower()
    assert not (lab.out / "bentoo-2026.10.01-gnome-systemd-v3.iso").exists()


def test_execute_a_worker_image_without_its_pulled_run_fails_naming_it(lab: Lab) -> None:
    lab.behaviour[LAB] = WorkerBehaviour(no_run={"wm"})
    outcomes = _run(lab)
    wm = _outcome(outcomes, "wm")
    assert not wm.ok and wm.node == LAB and "run" in wm.error.lower()
    assert _outcome(outcomes, "gnome").ok


def test_execute_a_worker_image_whose_assemble_failed_there_fails_alone(lab: Lab) -> None:
    lab.behaviour[LAB] = WorkerBehaviour(exit_code=1, failed={"wm"})
    outcomes = _run(lab)
    wm = _outcome(outcomes, "wm")
    assert not wm.ok and wm.node == LAB and wm.log is not None
    assert _outcome(outcomes, "gnome").ok  # its sibling made it


def test_execute_an_exit_0_child_without_the_jobs_rc_is_not_a_success(lab: Lab) -> None:
    # the job's end was never seen: no .rc, nothing pulled
    lab.behaviour[LAB] = WorkerBehaviour(exit_code=0, write_rc=False)
    outcomes = _run(lab)
    for name in ("gnome", "wm"):
        got = _outcome(outcomes, name)
        assert not got.ok and got.error, name
    assert _outcome(outcomes, "kde").ok


def test_execute_a_refused_child_fails_its_workers_images_quoting_reason_and_fix(
    lab: Lab,
) -> None:
    lab.behaviour[LAB] = WorkerBehaviour(
        exit_code=1,
        write_rc=False,
        log_text=(
            "error: bentoo-lab has no work disk mounted at /mnt/work\n"
            "  fix: run shidashi worker disk-init bentoo-lab\n"
        ),
    )
    outcomes = _run(lab)
    child_log = lab.logs / f"@{LAB}.log"
    job_log = lab.scratch / "worker-results" / LAB / JOB / f"{JOB}.log"
    for name in ("gnome", "wm"):
        got = _outcome(outcomes, name)
        assert not got.ok and got.node == LAB
        assert "no work disk" in got.error and "disk-init" in got.error
        assert got.log is not None and Path(got.log) in (child_log, job_log)
    assert _outcome(outcomes, "minimal").ok and _outcome(outcomes, "kde").ok


def test_execute_one_failed_worker_does_not_fail_another(lab: Lab) -> None:
    lab.behaviour[LAB2] = WorkerBehaviour(exit_code=255, write_rc=False, log_text="error: lost\n")
    outcomes = _run(lab, {"minimal": "dojo", "kde": LAB2, "gnome": LAB, "wm": LAB})
    assert not _outcome(outcomes, "kde").ok
    assert _outcome(outcomes, "gnome").ok and _outcome(outcomes, "wm").ok


# --- host failures (C1) ------------------------------------------------------------------------


def test_execute_a_failed_host_child_does_not_stop_the_workers(lab: Lab) -> None:
    lab.host_rc["kde"] = 1
    outcomes = _run(lab)
    kde = _outcome(outcomes, "kde")
    assert not kde.ok and kde.log is not None and Path(kde.log).parent == lab.logs
    assert kde.error
    assert _outcome(outcomes, "gnome").ok and _outcome(outcomes, "wm").ok


def test_execute_a_host_child_with_exit_0_but_an_error_is_a_failure_carrying_it(
    lab: Lab,
) -> None:
    lab.host_error["minimal"] = "result file lacks isos"
    outcomes = _run(lab)
    minimal = _outcome(outcomes, "minimal")
    assert not minimal.ok and "result file lacks isos" in minimal.error
    assert _outcome(outcomes, "kde").ok and _outcome(outcomes, "kde").error == ""


def test_execute_takes_no_owner_lock_for_assembles(lab: Lab) -> None:
    # assembles only READ the binhost; the owner lock would make a split release refuse itself
    _run(lab)
    assert lab.acquired == []


def test_execute_an_unplaceable_image_is_a_failed_outcome_naming_the_arch(lab: Lab) -> None:
    plan = _plan({"kde": "dojo"}, unplaceable={"v3-wm-systemd": "no node runs v3"})
    outcomes = schedule.execute(
        plan,
        _specs(),
        _estimates({"kde": "dojo"}),
        options=_options(lab),
        logs=lab.logs,
        run_id=RUN_ID,
        runner=_runner(lab),
        host="dojo",  # the plan's host node, as discover names it (4.1 deviation)
    )
    wm = _outcome(outcomes, "wm")
    assert not wm.ok and "no node runs v3" in wm.error
    assert _outcome(outcomes, "kde").ok
    assert lab.worker_argvs() == {}


# --- an interrupt (R4.7) ------------------------------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_execute_an_interrupt_carries_the_host_outcomes_the_worker_jobs_and_the_signal(
    lab: Lab, signum: int
) -> None:
    lab.interrupt = signum
    with pytest.raises(schedule.Interrupted) as caught:
        _run(lab)
    err = caught.value
    assert err.signum == signum
    assert {"minimal", "kde"} <= {_name(o) for o in err.outcomes}
    assert all(not o.ok for o in err.outcomes if _name(o) in ("minimal", "kde"))
    (job,) = err.jobs
    assert job.worker == LAB and job.job == JOB
    assert Path(job.results) == lab.scratch / "worker-results" / LAB / JOB
