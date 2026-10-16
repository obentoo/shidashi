"""Contract tests: what the scheduler reads from stories 006, 009 and 010 exists there.

Story 014 consumes, and adds no transport of its own (design.md v3, "Integration
Points"):
- 006 ``shidashi.parallel``: ``run_children(argvs, *, logs) -> list[ChildResult]``,
  ``ChildResult(image, exit_code, run_id, log, duration_s, isos, error)``,
  ``Interrupted(results, *, signum)``, ``assemble_argv(target, *, ..., cid, verbose,
  status_file)``;
- 009 ``shidashi.workers.load_registry`` / ``WorkerEntry.name``,
  ``shidashi.remote.Remote.for_worker(entry, address=None)``; the registry name is the
  node name and the hostname a worker's runs record (C3);
- 010 ``shidashi.worker.status(remote, *, timeout) -> WorkerStatus`` with ``reachable``,
  ``reason``, ``threads``, ``load1``, ``trunks``, ``trunk_keys`` (this story),
  ``accepts_jobs``, ``jobs``, ``cpu_flags`` and ``runnable_arches``; the ``worker job``
  command (``--results``, ``--allow-dirty``) and its results layout (``<job>.rc``,
  ``<job>.runs``, ``iso/``), its runs pulled into ``config.runs_dir()``;
  ``ownership.Owner(arch, worker, ...)`` and ``OwnedElsewhere``;
- ``shidashi.checkpoint``: a checkpoint manifest names its claimants in ``images``, and
  ``install-partial`` checkpoints share the ``install-`` prefix.

These tests import the REAL producers -- no stand-ins; a disagreement is fixed in the
producer or in this story's design, never by loosening the test.
"""

import hashlib
import inspect
import json
import os
import signal
import typing
from pathlib import Path
from typing import Any

import pytest
import typer.main

from shidashi import (
    checkpoint,
    cli,
    config,
    isaguard,
    ownership,
    parallel,
    remote,
    schedule,
    worker,
    workers,
)

LAB = "bentoo-lab"
RUN_ID = "20261015T120000Z-c0ffee"
JOB = "build-c0ffee"


def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _options(out: Path, **over: Any) -> schedule.BuildOptions:
    values: dict[str, Any] = {"arch": "v3", "init": "systemd", "output_dir": out, "images": ()}
    values.update(over)
    return schedule.BuildOptions(**values)


def _entry(name: str = LAB, address: str | None = "192.0.2.10") -> workers.WorkerEntry:
    return workers.WorkerEntry(
        name=name,
        address=address,
        host_key="ssh-ed25519 AAAAC3NzaFAKE",
        host_key_fingerprint="SHA256:pin",
        paired_at="2026-10-05T10:00:00Z",
    )


# --- story 006 ---------------------------------------------------------------------------


def test_contract_child_result_has_the_fields_an_outcome_is_built_from() -> None:
    hints = _hints(parallel.ChildResult)
    assert hints["image"] is str
    assert hints["exit_code"] is int
    assert hints["run_id"] == str | None
    assert hints["log"] is Path
    assert hints["duration_s"] is float
    assert hints["isos"] == tuple[Path, ...]
    assert hints["error"] == str | None, "C1: ChildResult carries the reason of a failed child"


def test_contract_run_children_takes_the_argvs_and_a_keyword_logs_directory() -> None:
    params = inspect.signature(parallel.run_children).parameters
    assert list(params)[0] == "argvs"
    assert params["logs"].kind is inspect.Parameter.KEYWORD_ONLY


def test_contract_interrupted_carries_every_result_and_the_signal() -> None:
    result = parallel.ChildResult(
        image="kde",
        exit_code=-15,
        run_id=None,
        log=Path("/tmp/kde.log"),
        duration_s=1.0,
        isos=(),
        error="interrupted",
    )
    err = parallel.Interrupted([result], signum=signal.SIGTERM)
    assert isinstance(err, KeyboardInterrupt)
    assert err.results == (result,) and err.signum == signal.SIGTERM
    assert parallel.Interrupted([]).signum == signal.SIGINT  # 128 + 2 = 130 for Ctrl+C


def test_contract_assemble_argv_takes_the_cid_verbose_and_status_file(tmp_path: Path) -> None:
    params = inspect.signature(parallel.assemble_argv).parameters
    for name in (
        "arch",
        "init",
        "jobs",
        "output_dir",
        "result_file",
        "boot_test",
        "cid",
        "verbose",
        "status_file",
    ):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
    argv = parallel.assemble_argv(
        "kde",
        arch="v3",
        init="systemd",
        jobs=8,
        output_dir=tmp_path,
        result_file=tmp_path / "kde.json",
        boot_test=True,
        cid=100,
        verbose=True,
        status_file=tmp_path / "kde.status",
    )
    assert argv[argv.index("assemble") : argv.index("assemble") + 4] == [
        "assemble",
        "v3",
        "kde",
        "systemd",
    ]
    assert argv[argv.index("--cid") + 1] == "100"
    assert "--verbose" in argv and "--boot-test" in argv
    assert argv[argv.index("--status-file") + 1] == str(tmp_path / "kde.status")
    assert parallel.job_share(24, 2) == 12


def test_contract_an_outcome_is_traced_from_a_real_child_result(tmp_path: Path) -> None:
    iso = tmp_path / "out" / "bentoo-2026.10.15-kde-systemd-v3.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"iso")
    logs = tmp_path / "logs"

    def runner(argvs: Any, **kw: Any) -> list[parallel.ChildResult]:
        return [
            parallel.ChildResult(
                image=str(k),
                exit_code=0,
                run_id="r-kde",
                log=Path(kw["logs"]) / f"{k}.log",
                duration_s=321.0,
                isos=(iso,),
                error=None,
            )
            for k in argvs
        ]

    spec = schedule.ImageSpec(key="v3-kde-systemd", arch="v3", image="kde", packages=1000)
    plan = schedule.Plan(
        assignments={"v3-kde-systemd": "dojo"},
        finish={"dojo": 300.0},
        makespan=300.0,
        method="exhaustive",
        skipped_nodes={},
    )
    estimates = {
        ("v3-kde-systemd", "dojo"): schedule.Estimate(
            seconds=300.0, trunk_seconds=0.0, source="history"
        )
    }
    (outcome,) = schedule.execute(
        plan,
        {spec.key: spec},
        estimates,
        options=_options(iso.parent, images=("kde",), jobs=8),
        logs=logs,
        run_id=RUN_ID,
        runner=runner,
        host="dojo",
    )
    assert outcome.ok and outcome.node == "dojo", outcome.error
    assert outcome.duration_s == pytest.approx(321.0)
    assert outcome.log is not None and Path(outcome.log).parent == logs
    assert tuple(map(Path, outcome.isos)) == (iso,)
    assert outcome.estimate_s == pytest.approx(300.0)


# --- story 009 ---------------------------------------------------------------------------


def test_contract_the_registry_lists_workers_by_name_and_reaches_them(tmp_path: Path) -> None:
    path = tmp_path / "workers.json"
    workers.save_registry(path, {LAB: _entry()})
    registry = workers.load_registry(path)
    assert list(registry) == [LAB] and registry[LAB].name == LAB  # C3: name is the key
    target = remote.Remote.for_worker(registry[LAB], "192.0.2.77")
    assert (target.name, target.address) == (LAB, "192.0.2.77")
    with pytest.raises(remote.RemoteUnreachable):
        remote.Remote.for_worker(_entry(address=None))  # no address: resolve it first
    assert callable(remote.resolve_address)


# --- story 010 ---------------------------------------------------------------------------


def test_contract_worker_status_has_the_fields_a_node_is_built_from() -> None:
    hints = _hints(worker.WorkerStatus)
    assert hints["name"] is str
    assert hints["reachable"] is bool
    assert hints["reason"] == str | None
    assert hints["threads"] is int
    assert hints["load1"] is float
    assert hints["cpu_flags"] == tuple[str, ...]
    assert hints["runnable_arches"] == tuple[str, ...]
    assert hints["trunks"] == tuple[str, ...]
    assert hints.get("trunk_keys") == tuple[str, ...], "v3: WorkerStatus.trunk_keys (task 3.3)"
    assert hints["accepts_jobs"] is bool
    assert hints["jobs"] == tuple[str, ...]
    assert worker.WorkerStatus(name=LAB, reachable=False, reason="down").trunk_keys == ()
    assert "timeout" in inspect.signature(worker.status).parameters


def test_contract_a_node_is_traced_from_a_real_worker_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status = worker.WorkerStatus(
        name=LAB,
        reachable=True,
        reason=None,
        threads=16,
        load1=0.5,
        cpu_flags=("avx2",),
        runnable_arches=("v1", "v2", "v3"),
        accepts_jobs=True,
        trunks=("v3-systemd/install-" + "a" * 24,),
        trunk_keys=("v3-desktop-systemd",),
        jobs=(),
    )
    wdir = tmp_path / "worker"
    wdir.mkdir()
    workers.save_registry(wdir / "workers.json", {LAB: _entry()})
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 32)
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(isaguard, "local_cpu_flags", lambda *_a: ("avx512f",))
    monkeypatch.setattr(isaguard, "runnable", lambda flags: ("v3", "znver5"))
    monkeypatch.setattr(worker, "status", lambda *_a, **_k: status)
    nodes = {n.name: n for n in schedule.discover(local=False, workers_dir=wdir)}
    lab = nodes[LAB]  # C3: named by its registry entry
    assert lab.reachable, lab.note
    # the status's tuples become the node's frozensets (the conversion, not the same type)
    assert isinstance(lab.trunks, frozenset) and isinstance(lab.runnable_arches, frozenset)
    assert lab.threads == 16 and lab.load1 == pytest.approx(0.5)
    assert lab.trunks == frozenset({"v3-desktop-systemd"})  # trunk_keys, never checkpoint ids
    assert "v3" in lab.runnable_arches and "znver5" not in lab.runnable_arches


def test_contract_the_worker_job_command_takes_results_and_allow_dirty() -> None:
    root = typer.main.get_command(cli.app)
    job = root.commands["worker"].commands["job"]  # type: ignore[attr-defined]
    opts = {opt for p in job.params for opt in getattr(p, "opts", ())}
    assert {"--results", "--allow-dirty"} <= opts
    flags = {opt: getattr(p, "is_flag", None) for p in job.params for opt in getattr(p, "opts", ())}
    assert flags["--allow-dirty"] is True and flags["--results"] is False


def test_contract_a_job_leaves_its_rc_its_runs_and_its_isos_under_results(
    tmp_path: Path,
) -> None:
    argv = worker.job_unit_argv(JOB, "0123abcd", ["build", "v3", "systemd", "--images", "kde"])
    script = " ".join(argv)
    assert f"{JOB}.rc" in script and f"{JOB}.runs" in script and f"{JOB}.log" in script
    assert f"/out/iso/{JOB}" in script  # the build's --output-dir on the worker
    dests = worker.pull_destinations("v3", results=tmp_path / "r")
    assert tmp_path / "r" / "iso" in dests and config.runs_dir() in dests


def test_contract_an_outcome_is_traced_from_a_results_dir_and_the_pulled_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # C3: the pulled run names the worker by the same name the registry gives it
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path / "runs"))
    monkeypatch.delenv("SUDO_USER", raising=False)
    out = tmp_path / "out"
    out.mkdir()
    iso_name = "bentoo-2026.10.15-gnome-systemd-v3.iso"

    def runner(argvs: Any, **kw: Any) -> list[parallel.ChildResult]:
        (key,) = argvs
        argv = [str(a) for a in argvs[key]]
        results = Path(argv[argv.index("--results") + 1])
        job = results.name
        (results / "iso").mkdir(parents=True)
        iso = results / "iso" / iso_name
        iso.write_bytes(b"iso")
        (results / "iso" / "SHA256SUMS").write_text(
            f"{hashlib.sha256(b'iso').hexdigest()}  {iso_name}\n"
        )
        build_id, asm_id = "20261015T120500Z-bbuild", "20261015T120600Z-agnome"
        runs = config.runs_dir()
        for run_id, manifest in (
            (
                build_id,
                {
                    "command": "build",
                    "argv": ["-c", "build", "v3", "systemd", "--images", "gnome"],
                    "hostname": LAB,
                    "status": "ok",
                    "steps": [
                        {
                            "step": "assemble:gnome",
                            "status": "ok",
                            "run_id": asm_id,
                            "exit_code": 0,
                            "duration_s": 230.0,
                        }
                    ],
                },
            ),
            (
                asm_id,
                {
                    "command": "assemble",
                    "argv": ["-c", "assemble", "v3", "gnome", "systemd"],
                    "hostname": LAB,
                    "status": "ok",
                    "duration_s": 222.0,
                    "steps": [{"step": "install", "status": "ok", "duration_s": 222.0}],
                    "artifacts": [
                        {"path": f"/mnt/work/out/iso/{job}/{iso_name}", "role": "iso:zstd"}
                    ],
                },
            ),
        ):
            (runs / run_id).mkdir(parents=True)
            (runs / run_id / "manifest.json").write_text(json.dumps({"run": run_id, **manifest}))
        (results / f"{job}.runs").write_text(f"{build_id}\n{asm_id}\n")
        (results / f"{job}.rc").write_text("0\n")
        (results / f"{job}.log").write_text("ok\n")
        log = Path(kw["logs"]) / f"{key}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("job exited 0\n")
        return [
            parallel.ChildResult(
                image=str(key),
                exit_code=0,
                run_id=None,
                log=log,
                duration_s=333.0,
                isos=(),
                error="no result file",  # what the real run_children says of it
            )
        ]

    spec = schedule.ImageSpec(key="v3-gnome-systemd", arch="v3", image="gnome", packages=766)
    plan = schedule.Plan(
        assignments={"v3-gnome-systemd": LAB},
        finish={LAB: 300.0},
        makespan=300.0,
        method="exhaustive",
        skipped_nodes={},
    )
    estimates = {
        ("v3-gnome-systemd", LAB): schedule.Estimate(
            seconds=300.0, trunk_seconds=0.0, source="history"
        )
    }
    (outcome,) = schedule.execute(
        plan,
        {spec.key: spec},
        estimates,
        options=_options(out, images=("gnome",)),
        logs=tmp_path / "logs",
        run_id=RUN_ID,
        runner=runner,
    )
    assert outcome.ok and outcome.node == LAB, outcome.error
    assert outcome.duration_s == pytest.approx(222.0)
    assert [Path(p) for p in outcome.isos] == [out / iso_name]
    assert outcome.sbom is False  # no .spdx.json came back (R4.9)


# --- checkpoints (R2.5) ------------------------------------------------------------------


def test_contract_a_checkpoint_manifest_names_its_claimant_image_keys(tmp_path: Path) -> None:
    store = checkpoint.Store(root=tmp_path / "v3-systemd", backend=None)  # type: ignore[arg-type]
    store.root.mkdir()
    mark = checkpoint.Mark(
        step=checkpoint.INSTALL,
        fingerprint="d" * 64,
        path=store.root / f"{checkpoint.INSTALL}-{'d' * 24}",
        run_id="r1",
        created="2026-10-15T12:00:00+00:00",
        images=("v3-desktop-systemd", "v3-kde-systemd"),
    )
    store._write(mark)
    raw = json.loads((store.root / f"install-{'d' * 24}.json").read_text())
    assert raw["images"] == ["v3-desktop-systemd", "v3-kde-systemd"]
    # install-partial-*.json also matches install-*.json: discover must exclude it
    assert checkpoint.PARTIAL.startswith(checkpoint.INSTALL + "-")


# --- the binhost's owner (R3.3, R3.5) ----------------------------------------------------


def test_contract_the_binhost_owner_names_its_arch_and_worker() -> None:
    assert {"arch", "worker", "job"} <= set(ownership.Owner.model_fields)
    holder = ownership.Owner(
        arch="v3", worker=LAB, job="fac-v3", commit="a" * 40, since="2026-10-05T14:02:00Z"
    )
    err = ownership.OwnedElsewhere(holder)
    assert err.holder is holder and LAB in str(err)
    assert callable(ownership.current) and callable(ownership.acquire)
    assert callable(cli._worker_holder) and callable(cli._owner_lock)
