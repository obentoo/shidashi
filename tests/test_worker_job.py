"""Jobs (shidashi/worker.py): the transient unit, the orchestration, the data contracts.

``-k unit_argv`` is pure; ``-k orchestration`` runs ``worker.job`` against a fake
worker (tests/_fake_worker.py) whose ``systemd-run`` really runs the unit's command
with a fake ``shidashi``; ``-k contract`` checks that what one side produces is
what the other consumes.

Owner lock (cross-story decision, 2026-10-05): only a job that WRITES a PKGDIR --
``factory``, or ``build`` without ``--skip-factory`` -- takes the arch's lock and
is refused by it. An ``assemble`` job only reads the binhost: no lock either way.
"""

import dataclasses
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, config, ownership, worker
from tests._fake_worker import FakeWorker, option_values, seed_host_cache, systemd_expand

COMMIT = "0123456789abcdef0123456789abcdef01234567"
FACTORY = ["factory", "v3", "minimal", "systemd"]
ASSEMBLE = ["assemble", "v3", "kde", "systemd"]


def _setenv(argv: list[str]) -> dict[str, str]:
    return dict(v.split("=", 1) for v in option_values(argv, "--setenv", "-E"))


def _wrapped(argv: list[str]) -> list[str]:
    """The unit's command: what follows systemd-run's own options."""
    i = 1
    with_value = {
        "--unit",
        "-u",
        "--setenv",
        "-E",
        "--working-directory",
        "-p",
        "--property",
        "--description",
    }
    while i < len(argv) and argv[i].startswith("-"):
        i += 1 if ("=" in argv[i] or argv[i] not in with_value) else 2
    return argv[i:]


def _run_unit_locally(argv: list[str], root: Path) -> tuple[list[str], str, str]:
    """Run the unit's command with /mnt/work under ``root`` and the shipped python
    replaced by a recorder; return (args it got, log, rc)."""
    rec = root / "args.json"
    fake = root / "python"
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"json.dump(sys.argv[1:], open({str(rec)!r}, 'w'))\nprint('ran')\nsys.exit(4)\n"
    )
    fake.chmod(0o755)
    cmd = []
    words = _wrapped(argv)
    # what systemd executes: the words after the binary pass its $-expansion
    for a in words[:1] + systemd_expand(words[1:], _setenv(argv)):
        if a.endswith("/runtime/venv/bin/python"):
            cmd.append(str(fake))
        else:
            cmd.append(a.replace("/mnt/work", str(root / "mnt/work")))
    (root / "mnt/work/out/jobs").mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=False, timeout=30)
    jobs = root / "mnt/work/out/jobs"
    log = next(jobs.glob("*.log")).read_text()
    rc = next(jobs.glob("*.rc")).read_text().strip()
    return json.loads(rec.read_text()), log, rc


# =====================================================================================
# 5.1 -- job_unit_argv (pure)
# =====================================================================================


def test_unit_argv_refuses_a_job_name_outside_a_z_0_9_dash() -> None:
    for bad in ("../x", "Kde", "a b", "a;b", "", "x/y", "$(id)"):
        with pytest.raises(ValueError):
            worker.job_unit_argv(bad, COMMIT, FACTORY)
    for good in ("kde-v3", "a1", "fac-v3"):
        worker.job_unit_argv(good, COMMIT, FACTORY)


def test_unit_argv_keeps_an_explicit_output_dir_whatever_its_spelling() -> None:
    for given in (
        ["--output-dir", "/mnt/work/out/iso/mine"],
        ["-o", "/mnt/work/out/iso/mine"],
        ["--output-dir=/mnt/work/out/iso/mine"],
    ):
        argv = worker.job_unit_argv("kde", COMMIT, [*ASSEMBLE, *given])
        tail = _wrapped(argv)
        assert len(option_values(tail, "--output-dir", "-o")) == 1, argv
        assert option_values(tail, "--output-dir", "-o") == ["/mnt/work/out/iso/mine"]


def test_unit_argv_adds_no_output_dir_to_a_factory() -> None:
    tail = _wrapped(worker.job_unit_argv("fac-v3", COMMIT, FACTORY))
    assert option_values(tail, "--output-dir", "-o") == []
    assert tail[-4:] == FACTORY


def test_unit_argv_runs_a_named_collected_unit_from_the_shipped_tree() -> None:
    argv = worker.job_unit_argv("kde-v3", COMMIT, ASSEMBLE)
    assert argv[0] == "systemd-run"
    assert option_values(argv, "--unit", "-u") == ["shidashi-job-kde-v3"]
    assert "--collect" in argv
    assert option_values(argv, "--working-directory") == [f"/mnt/work/src/{COMMIT}"]
    assert _setenv(argv) == {
        "SHIDASHI_CACHE": "/mnt/work/cache",
        "SHIDASHI_SCRATCH": "/mnt/work/scratch",
        "SHIDASHI_RUNS": "/mnt/work/out/runs",
        "PYTHONPATH": f"/mnt/work/src/{COMMIT}",
    }
    tail = _wrapped(argv)
    i = tail.index("/mnt/work/runtime/venv/bin/python")
    assert tail[i + 1 : i + 3] == ["-c", "from shidashi.cli import app; app()"]


def test_unit_argv_adds_an_output_dir_under_mnt_work_to_assemble_and_build() -> None:
    for args in (ASSEMBLE, ["build", "v3", "systemd", "--images", "kde"]):
        tail = _wrapped(worker.job_unit_argv("kde", COMMIT, args))
        assert option_values(tail, "--output-dir", "-o") == ["/mnt/work/out/iso/kde"]


def test_unit_argv_writes_the_log_and_exit_code_and_keeps_every_argument_whole(
    tmp_path: Path,
) -> None:
    args = [*FACTORY, "--until", "a b; $(touch PWNED) 'q'"]
    argv = worker.job_unit_argv("fac-v3", COMMIT, args)
    # the one string the worker's shell sees, parsed back as that shell would
    assert shlex.split(shlex.join(argv)) == argv
    got, log, rc = _run_unit_locally(argv, tmp_path)
    assert got[-len(args) :] == args
    assert "ran" in log
    assert rc == "4"
    assert not list(tmp_path.rglob("PWNED"))
    jobs = tmp_path / "mnt/work/out/jobs"
    assert {p.name for p in jobs.iterdir()} == {"fac-v3.log", "fac-v3.rc"}


# =====================================================================================
# 5.2 -- worker.job orchestration
# =====================================================================================


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    seed_host_cache(w)
    yield w
    w.close()


def _run_job(
    fw: FakeWorker,
    name: str,
    args: list[str],
    *,
    entry: Any = None,
    allow_dirty: bool = False,
    follow: bool = True,
    results: Path | None = None,
    default_results: bool = False,
) -> Any:
    """``worker.job``'s JobResult (contract C4)."""
    kw: dict[str, Any] = (
        {} if default_results else {"results": results or fw.base / "results" / name}
    )
    with audit.run(config.runs_dir(), command="worker-job", argv=["worker", "job", name, *args]):
        return worker.job(
            fw.remote(),
            entry or fw.entry(),
            name,
            args,
            allow_dirty=allow_dirty,
            follow=follow,
            **kw,
        )


def _job(
    fw: FakeWorker,
    name: str,
    args: list[str],
    *,
    entry: Any = None,
    allow_dirty: bool = False,
    follow: bool = True,
) -> int:
    res = _run_job(fw, name, args, entry=entry, allow_dirty=allow_dirty, follow=follow)
    return int(res.exit_code)


def _refused(call: Callable[[], Any], capfd: pytest.CaptureFixture[str]) -> str:
    """A refusal: an exception, or a non-zero return. Returns its text and output."""
    try:
        rc = call()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - either style is the contract
        out = capfd.readouterr()
        return f"{exc}\n{out.out}{out.err}"
    out = capfd.readouterr()
    assert rc != 0, "the job was not refused"
    return out.out + out.err


def _nothing_shipped(fw: FakeWorker) -> None:
    assert fw.rsync_calls() == []
    assert not any("tar -x" in c or "systemd-run" in c for c in fw.ssh_commands())
    assert fw.job_invocations() == []


def _hold_lock(arch: str = "v3", job: str = "other-fac") -> Any:
    owner = ownership.Owner(
        arch=arch,
        worker="other-box",
        job=job,
        commit="b" * 40,
        since="2026-10-05T14:02:31Z",
        host_pid=None,
    )
    return ownership.acquire(arch, owner)


# --- hostile halves first ----------------------------------------------------------


def test_orchestration_a_finished_job_does_not_block_a_new_one(
    fw: FakeWorker,
) -> None:
    fw.put("out/jobs/kde-v3.rc", "0\n")  # its unit ended (inactive), its files stay
    fw.put("out/jobs/kde-v3.log", "done\n")
    assert _job(fw, "kde", ASSEMBLE) == 0
    assert fw.job_invocations()


def test_orchestration_any_running_job_refuses_another_naming_it(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.activate_unit("shidashi-job-kde-v3")  # one job at a time per worker (C4, R3.5)
    text = _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    assert "kde-v3" in text
    _nothing_shipped(fw)
    assert ownership.current("v3") is None


def test_orchestration_an_assemble_job_neither_takes_nor_is_refused_by_the_lock(
    fw: FakeWorker,
) -> None:
    held = _hold_lock("v3")
    assert _job(fw, "kde", ASSEMBLE) == 0
    (inv,) = fw.job_invocations()
    assert inv["host_locks"] == {"v3.owner.json": json.loads(held.model_dump_json())}
    assert ownership.current("v3") == held  # untouched


def test_orchestration_a_lock_on_another_arch_does_not_refuse_a_factory_job(
    fw: FakeWorker,
) -> None:
    _hold_lock("znver5")
    assert _job(fw, "fac-v3", FACTORY) == 0


def test_orchestration_an_untracked_file_does_not_make_the_tree_dirty(
    fw: FakeWorker,
) -> None:
    assert (fw.repo / ".env").exists()  # untracked, beside a clean tree
    assert _job(fw, "fac-v3", FACTORY) == 0
    assert not (fw.work / "src" / fw.head / ".env").exists()  # and never shipped (Q8)


# --- the sequence (R3.1, R3.3, R3.4, R3.7, R3.9, R5.1, R5.3, R6.3) -------------------


def test_orchestration_ships_runs_follows_pulls_and_releases_a_factory_job(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    gen = fw.config()["generation"]
    fw.set_job(lines=["fake-shidashi: emerging app-misc/built-by-job"])
    assert _job(fw, "fac-v3", FACTORY) == 0
    out = capfd.readouterr()
    assert "emerging app-misc/built-by-job" in out.out + out.err  # R3.4: streamed

    (inv,) = fw.job_invocations()
    src = fw.work / "src" / fw.head
    assert inv["args"][-4:] == FACTORY and inv["cwd"] == str(src)  # R3.1: HEAD's tree
    assert inv["env"]["PYTHONPATH"] == str(src) and inv["shipped_cli"] == "# committed\n"
    assert inv["env"]["SHIDASHI_CACHE"] == str(fw.work / "cache")
    lock = inv["host_locks"]["v3.owner.json"]  # R5.1: held while the job runs
    assert (lock["worker"], lock["job"], lock["commit"]) == ("bentoo-lab", "fac-v3", fw.head)
    assert lock["since"]
    jobs = fw.work / "out" / "jobs"  # R3.3
    assert (jobs / "fac-v3.rc").read_text().strip() == "0" and (jobs / "fac-v3.log").is_file()
    host_pkg = fw.host_cache / "binpkgs" / "v3" / gen / "app-misc" / "built-by-job-1.gpkg.tar"
    assert host_pkg.is_file()  # R3.7 / R6.3: pulled
    assert ownership.current("v3") is None  # R5.3: released after the pull
    fw.assert_pinned()

    commands = fw.ssh_commands()
    start = next(i for i, c in enumerate(commands) if "systemd-run" in c)
    ship = next(i for i, c in enumerate(commands) if "tar -x" in c)
    index = next(i for i, c in enumerate(commands) if "emaint" in c)
    assert ship < start < index
    # the lock is held through the push AND the pull: taken before, released after
    assert fw.rsync_calls()
    assert all("v3.owner.json" in c["locks"] for c in fw.rsync_calls())


def test_orchestration_records_the_job_in_the_audit_trail(fw: FakeWorker) -> None:
    fw.set_job(rc=7)
    assert _job(fw, "fac-v3", FACTORY) == 7  # R3.7: the job's exit code
    events: list[dict[str, Any]] = []
    for path in config.runs_dir().rglob("events.jsonl"):
        lines = path.read_text().splitlines()
        events += [json.loads(line) for line in lines if line.strip()]
    events = [e for e in events if not e.get("fake")]
    blob = json.dumps(events)
    assert "bentoo-lab" in blob and fw.head in blob and "shidashi-job-fac-v3" in blob

    def numbers(*words: str) -> list[Any]:
        found = []
        for e in events:
            for k, v in e.items():
                ctx = f"{e.get('kind')} {e.get('name', '')} {k}".lower()
                if isinstance(v, int) and all(
                    any(w in ctx for w in alt.split("|")) for alt in words
                ):
                    found.append(v)
                if (
                    k == "value"
                    and isinstance(v, int)
                    and all(
                        any(w in str(e.get("name", "")).lower() for w in alt.split("|"))
                        for alt in words
                    )
                ):
                    found.append(v)
        return found

    assert numbers("byte", "push|sent"), "bytes pushed are not in the audit trail"
    assert numbers("byte", "pull|receiv"), "bytes pulled are not in the audit trail"
    assert any("duration" in k for e in events for k in e)
    assert 7 in numbers("exit|rc|code")


# --- refusals before any transfer --------------------------------------------------


def test_orchestration_refuses_a_dirty_tree_listing_the_changed_files(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    changed = fw.dirty()
    text = _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    for path in changed:
        assert path in text
    _nothing_shipped(fw)
    assert ownership.current("v3") is None


def test_orchestration_allow_dirty_ships_head_and_says_the_changes_stay_behind(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.dirty()
    assert _job(fw, "fac-v3", FACTORY, allow_dirty=True) == 0
    out = capfd.readouterr()
    assert "not included" in (out.out + out.err).lower()
    assert (fw.work / "src" / fw.head / "shidashi" / "cli.py").read_text() == "# committed\n"


def test_orchestration_refuses_an_arch_the_workers_cpu_cannot_run(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    text = _refused(lambda: _job(fw, "z5", ["factory", "znver5", "minimal", "systemd"]), capfd)
    assert "avx512f" in text
    _nothing_shipped(fw)
    assert ownership.current("znver5") is None


def test_orchestration_runs_an_archless_command_without_the_cpu_guard(fw: FakeWorker) -> None:
    assert _job(fw, "doc", ["doctor"], entry=fw.entry(cpu_flags=())) == 0
    assert fw.job_invocations()[0]["args"][-1:] == ["doctor"]


def test_orchestration_refuses_without_a_work_disk_before_shipping(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.set(mounted=False)
    text = _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    assert "/mnt/work" in text or "work disk" in text.lower()
    _nothing_shipped(fw)
    assert ownership.current("v3") is None


def test_orchestration_refuses_a_job_whose_name_is_already_running(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.activate_unit("shidashi-job-fac-v3")
    text = _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    assert "fac-v3" in text
    _nothing_shipped(fw)


def test_orchestration_refuses_a_factory_job_while_another_writer_owns_the_arch(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    held = _hold_lock("v3")
    text = _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    assert "other-box" in text and "shidashi worker unlock v3" in text
    _nothing_shipped(fw)
    assert ownership.current("v3") == held


# --- after the start: interrupted, lost, unpulled (R3.8, R5.4) ---------------------


def _assert_resume_commands(text: str, job: str) -> None:
    assert f"shidashi worker logs bentoo-lab {job} -f" in text
    assert f"shidashi worker sync pull bentoo-lab --arch v3 --job {job}" in text


def test_orchestration_ctrl_c_leaves_the_job_running_and_the_lock_held(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.set_job(hold_until_release=True)

    def interrupt() -> None:
        if fw.wait_for(lambda: bool(fw.job_invocations()), 60):
            time.sleep(1.5)
            os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    res = _run_job(fw, "fac-v3", FACTORY)  # never SystemExit from the library
    assert res.followed is False and res.exit_code is None
    out = capfd.readouterr()
    _assert_resume_commands(out.out + out.err, "fac-v3")
    assert ownership.current("v3") is not None
    assert (fw.state / "units" / "shidashi-job-fac-v3.active").exists()  # still running
    assert not any(c["kind"] == "emaint" for c in fw.calls())  # nothing pulled


def test_orchestration_a_lost_connection_leaves_the_job_running_and_the_lock_held(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.set_job(hold_until_release=True)
    fw.fail(
        r"^(?!.*systemd-run).*out/jobs/fac-v3\.log",
        code=255,
        stderr="Connection to 192.0.2.10 closed by remote host.\n",
    )
    res = _run_job(fw, "fac-v3", FACTORY)
    assert res.followed is False and res.exit_code is None
    out = capfd.readouterr()
    _assert_resume_commands(out.out + out.err, "fac-v3")
    assert ownership.current("v3") is not None
    assert not any(c["kind"] == "emaint" for c in fw.calls())  # nothing pulled


def test_orchestration_a_failed_pull_keeps_the_lock_and_prints_retry_and_unlock(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.fail(r"rsync --server --sender", code=12, stderr="connection reset\n")
    text = _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    assert "shidashi worker sync pull bentoo-lab --arch v3 --job fac-v3" in text
    assert "shidashi worker unlock v3" in text
    assert ownership.current("v3") is not None


def test_orchestration_without_follow_returns_with_the_lock_still_held(
    fw: FakeWorker,
) -> None:
    fw.set_job(hold_until_release=True)
    _job(fw, "fac-v3", FACTORY, follow=False)
    assert ownership.current("v3") is not None  # released only once results are pulled
    assert not any(c["kind"] == "emaint" for c in fw.calls())


# =====================================================================================
# 7.1 -- data contracts
# =====================================================================================


def test_contract_the_jobs_cache_is_where_the_push_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "host-cache"))
    gen = "20260823T153057Z"
    plan = {h.rstrip("/"): w.rstrip("/") for h, w in worker.push_plan("v3", gen)}
    dirs: tuple[tuple[str, Callable[[], Path]], ...] = (
        ("pkgdir", lambda: config.pkgdir("v3", gen)),
        ("distfiles", config.distdir),
        ("ccache", config.ccache_dir),
        ("sccache", config.sccache_dir),
    )
    host = {name: str(fn()) for name, fn in dirs}
    env = _setenv(worker.job_unit_argv("fac-v3", COMMIT, FACTORY))
    monkeypatch.setenv("SHIDASHI_CACHE", env["SHIDASHI_CACHE"])
    on_worker = {
        "pkgdir": str(config.pkgdir("v3", gen)),
        "distfiles": str(config.distdir()),
        "ccache": str(config.ccache_dir()),
        "sccache": str(config.sccache_dir()),
    }
    for name, path in host.items():
        assert plan[path] == on_worker[name], name


def test_contract_what_the_job_writes_is_what_the_pull_brings_back(
    fw: FakeWorker, tmp_path: Path
) -> None:
    tail = _wrapped(worker.job_unit_argv("kde", COMMIT, ASSEMBLE))
    (iso_dir,) = option_values(tail, "--output-dir", "-o")
    runs = _setenv(worker.job_unit_argv("kde", COMMIT, ASSEMBLE))["SHIDASHI_RUNS"]
    fw.put(iso_dir.removeprefix("/mnt/work/") + "/bentoo-kde.iso", "ISO")
    fw.put(runs.removeprefix("/mnt/work/") + "/20261005T1-cafe01/events.jsonl", "{}\n")
    fw.put("out/jobs/kde.runs", "20261005T1-cafe01\n")  # as the job's wrapper records it
    got = worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "res")
    assert any(p.name == "bentoo-kde.iso" for p in (tmp_path / "res").rglob("*"))
    assert any(p.name == "bentoo-kde.iso" for p in got.isos)
    assert (config.runs_dir() / "20261005T1-cafe01" / "events.jsonl").is_file()


def test_contract_status_and_owner_carry_the_fields_their_readers_use() -> None:
    fields = {f.name for f in dataclasses.fields(worker.WorkerStatus)}
    assert {
        "cpu_model",
        "threads",
        "max_target",
        "mem_total",
        "mem_available",
        "work_free",
        "image",
        "smart",
        "jobs",
        "load1",
        "trunks",
        "reachable",
        "reason",
        "cpu_flags",
        "runnable_arches",
        "accepts_jobs",
    } <= fields
    assert {"exit_code", "log", "isos", "run_ids", "duration_s", "followed"} <= {
        f.name for f in dataclasses.fields(worker.JobResult)
    }
    assert {"arch", "worker", "job", "commit", "since", "host_pid"} <= set(
        ownership.Owner.model_fields
    )


# --- corrections at review (R3.11–R3.14, contract C4) -----------------------------------


def test_orchestration_refuses_an_invalid_name_before_any_contact(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    text = _refused(lambda: _job(fw, "Bad_Name", FACTORY), capfd)
    assert "Bad_Name" in text
    assert fw.ssh_commands() == []  # not even the status probe
    _nothing_shipped(fw)
    assert ownership.current("v3") is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_orchestration_refuses_an_unwritable_results_directory_before_any_transfer(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    res = fw.base / "ro-results"
    res.mkdir()
    res.chmod(0o555)
    try:
        text = _refused(lambda: _run_job(fw, "kde", ASSEMBLE, results=res / "kde").exit_code, capfd)
    finally:
        res.chmod(0o755)
    assert str(res) in text
    _nothing_shipped(fw)


def test_orchestration_a_push_failure_after_the_lock_releases_it(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.fail(r"rsync --server(?! --sender)", code=12, stderr="No space left on device\n")
    _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    assert fw.job_invocations() == []  # nothing runs on the worker
    assert ownership.current("v3") is None  # so nothing may keep the lock (R3.13)


def test_orchestration_a_unit_that_fails_to_start_releases_the_lock(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.fail(r"systemd-run", code=1, stderr="Failed to start transient service unit\n")
    text = _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    assert "Failed to start" in text
    assert ownership.current("v3") is None


def test_orchestration_returns_a_job_result_with_host_paths(fw: FakeWorker) -> None:
    fw.set_job(rc=0, lines=["fake-shidashi: assembling"])
    res = _run_job(fw, "kde", ASSEMBLE)
    assert res.exit_code == 0
    assert res.log is not None and res.log.is_file()  # pulled to the host
    assert "fake-shidashi: assembling" in res.log.read_text()
    assert res.isos and all(p.is_file() and p.suffix == ".iso" for p in res.isos)
    assert all(str(p).startswith(str(fw.base / "results" / "kde")) for p in res.isos)
    assert res.duration_s > 0
    assert isinstance(res.run_ids, tuple)


def test_orchestration_results_default_to_worker_results_worker_job(
    fw: FakeWorker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    res = _run_job(fw, "kde", ASSEMBLE, default_results=True)
    base = (tmp_path / "worker-results" / fw.name / "kde").resolve()
    assert res.isos and all(base in p.resolve().parents for p in res.isos)


def test_orchestration_an_assemble_job_leaves_the_hosts_binhost_untouched(
    fw: FakeWorker,
) -> None:
    gen = fw.config()["generation"]
    index = fw.host_cache / "binpkgs" / "v3" / gen / "Packages"
    before = index.read_text() if index.exists() else None
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/stray-1.gpkg.tar", "stray\n")
    assert _job(fw, "kde", ASSEMBLE) == 0
    assert (index.read_text() if index.exists() else None) == before
    assert not (index.parent / "app-misc" / "stray-1.gpkg.tar").exists()
    assert not fw.calls("emaint")


def test_contract_a_checkpoint_where_the_assembler_writes_it_is_a_reported_trunk(
    fw: FakeWorker,
) -> None:
    scratch = _setenv(worker.job_unit_argv("kde", COMMIT, ASSEMBLE))["SHIDASHI_SCRATCH"]
    fp = "9f2c4e1ab37d5c0e8f6a1b2c3d4e5f6071"
    rel = f"assemble/checkpoints/v3-systemd/install-{fp[:24]}.json"
    fw.put(
        scratch.removeprefix("/mnt/work/") + "/" + rel,
        json.dumps({"format": 3, "step": "install", "fingerprint": fp, "images": ["kde"]}),
    )
    assert f"v3-systemd/install-{fp[:24]}" in worker.status(fw.remote()).trunks


# --- second review (items 4, 5, 7, 8, 11) ----------------------------------------------


def test_orchestration_a_pre_transfer_refusal_is_a_job_refused_with_reason_and_fix(
    fw: FakeWorker,
) -> None:
    with pytest.raises(worker.JobRefused) as err:
        _run_job(fw, "z5", ["assemble", "znver5", "minimal", "systemd"])  # CPU guard
    assert err.value.reason and err.value.fix
    _nothing_shipped(fw)


def test_orchestration_bwlimit_reaches_every_rsync(fw: FakeWorker) -> None:
    with audit.run(config.runs_dir(), command="worker-job", argv=["worker", "job"]):
        worker.job(
            fw.remote(),
            fw.entry(),
            "kde",
            ASSEMBLE,
            allow_dirty=False,
            follow=True,
            results=fw.base / "results" / "kde",
            bwlimit=900,
        )
    assert fw.rsync_calls()
    assert all(option_values(c["argv"], "--bwlimit") == ["900"] for c in fw.rsync_calls())


def test_orchestration_takes_the_init_from_the_job_target(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real_push, real_pull = worker.push, worker.pull

    def push(*a: Any, **k: Any) -> Any:
        seen.append(f"push:{k.get('init')}")
        return real_push(*a, **k)

    def pull(*a: Any, **k: Any) -> Any:
        seen.append(f"pull:{k.get('init')}")
        return real_pull(*a, **k)

    monkeypatch.setattr(worker, "push", push)
    monkeypatch.setattr(worker, "pull", pull)
    _run_job(fw, "kde-or", ["assemble", "v3", "kde", "openrc"])
    assert seen and all(s.endswith(":openrc") for s in seen)


def test_orchestration_an_archless_job_ships_only_the_runtime_and_the_tree(fw: FakeWorker) -> None:
    fw.put("out/iso/doc/stray.iso", "x")
    assert _job(fw, "doc", ["doctor"], entry=fw.entry(cpu_flags=())) == 0
    pushed = [c for c in fw.rsync_calls() if c.get("direction", "push") == "push"]
    assert pushed and all(
        "runtime/venv" in " ".join(c["argv"])
        for c in pushed
        if "--sender" not in " ".join(c["argv"])
    )
    assert not any(
        "binpkgs" in " ".join(c["argv"]) or "distfiles" in " ".join(c["argv"])
        for c in fw.rsync_calls()
    )
    assert not (fw.base / "results" / "doc" / "iso").exists()  # no ISO pulled for it


def test_contract_run_ids_are_exactly_the_runs_the_job_created_and_resolve_on_the_host(
    fw: FakeWorker,
) -> None:
    fw.put("out/runs/20261004T090000Z-old000/events.jsonl", "{}\n")  # an older run
    res = _run_job(fw, "kde", ASSEMBLE)
    assert "20261004T090000Z-old000" not in res.run_ids
    for rid in res.run_ids:
        assert (config.runs_dir() / rid).is_dir()  # what story 014 resolves


def test_contract_a_worker_jobs_owner_names_its_arch_and_no_host_pid(fw: FakeWorker) -> None:
    fw.set_job(hold_until_release=True)
    _run_job(fw, "fac-v3", FACTORY, follow=False)
    held = ownership.current("v3")
    assert held is not None and held.arch == "v3" and held.host_pid is None


# --- tech review of 5.1/5.2 (regressions) ----------------------------------------------

DOLLARS = ["$HOME", "${HOME}", "pre${USER}post", "a$$b", "$", "x$", "$(id)"]


def test_unit_argv_starts_an_exec_unit_so_the_start_means_the_command_ran() -> None:
    argv = worker.job_unit_argv("fac-v3", COMMIT, FACTORY)
    assert "--service-type=exec" in argv[: argv.index("/bin/bash")]


def test_unit_argv_doubles_every_dollar_so_systemd_passes_arguments_whole(
    tmp_path: Path,
) -> None:
    args = [*FACTORY, "--until", *DOLLARS]
    argv = worker.job_unit_argv("fac-v3", COMMIT, args)
    words = _wrapped(argv)
    # systemd expands ${VAR}, whole-word $VAR and $$ with the unit's environment
    env = {"HOME": "/root", "USER": "root", **_setenv(argv)}
    executed = systemd_expand(words[1:], env)
    assert executed[-len(args) :] == args
    script = executed[executed.index("-c") + 1]
    assert '"$@"' in script and "rc=$?" in script
    # and the wrapper still runs as bash once systemd has un-doubled it
    got, log, rc = _run_unit_locally(argv, tmp_path)
    assert got[-len(args) :] == args
    assert "ran" in log and rc == "4"


def test_orchestration_dollar_signs_reach_the_job_whole(fw: FakeWorker) -> None:
    args = ["doctor", *DOLLARS]
    assert _job(fw, "doc", args, entry=fw.entry(cpu_flags=())) == 0
    (inv,) = fw.job_invocations()
    assert inv["args"][-len(DOLLARS) :] == DOLLARS


def test_orchestration_a_stale_rc_does_not_end_the_follow_of_a_new_job(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fw.put("out/jobs/fac-v3.rc", "0\n")  # an earlier run of the same name
    fw.put("out/jobs/fac-v3.rc.tmp", "0\n")
    fw.put("out/jobs/fac-v3.log", "OLD LOG\n")
    fw.set_job(rc=5, lines=["NEW LOG"])
    real = worker.job_unit_argv

    def slow_to_start(job: str, commit: str, args: Any) -> list[str]:
        # the unit's command starts late: its own cleanup comes after the follow began
        argv = real(job, commit, args)
        i = argv.index("/bin/bash") + 2
        argv[i] = "sleep 2; " + argv[i]
        return argv

    monkeypatch.setattr(worker, "job_unit_argv", slow_to_start)
    res = _run_job(fw, "fac-v3", FACTORY)
    assert res.exit_code == 5 and res.followed is True
    assert res.log is not None and "NEW LOG" in res.log.read_text()
    assert "OLD LOG" not in res.log.read_text()
    start = next(c for c in fw.ssh_commands() if "systemd-run" in c)
    assert start.index("fac-v3.rc.tmp") < start.index("systemd-run")
    assert ownership.current("v3") is None


def test_orchestration_a_lost_connection_at_the_start_keeps_the_lock_and_says_how_to_resume(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.fail(r"systemd-run", code=255, stderr="Connection to 192.0.2.10 closed by remote host.\n")
    res = _run_job(fw, "fac-v3", FACTORY)
    assert res.followed is False and res.exit_code is None
    out = capfd.readouterr()
    text = out.out + out.err
    _assert_resume_commands(text, "fac-v3")
    assert "shidashi worker unlock v3" in text
    assert ownership.current("v3") is not None  # the unit may be running


def test_orchestration_ctrl_c_during_the_start_keeps_the_lock(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fw.set_job(hold_until_release=True)
    real = worker._start_unit

    def started_then_interrupted(*a: Any, **k: Any) -> None:
        real(*a, **k)
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "_start_unit", started_then_interrupted)
    res = _run_job(fw, "fac-v3", FACTORY)
    assert res.followed is False and res.exit_code is None
    out = capfd.readouterr()
    _assert_resume_commands(out.out + out.err, "fac-v3")
    assert ownership.current("v3") is not None
    assert fw.wait_for(lambda: bool(fw.job_invocations()), 30)  # it does run


def test_orchestration_an_audit_failure_after_the_lock_releases_it(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = audit.Run.event

    def event(self: audit.Run, kind: str, **fields: Any) -> None:
        if kind == "worker.lock" and fields.get("action") == "acquired":
            raise OSError("No space left on device")
        real(self, kind, **fields)

    monkeypatch.setattr(audit.Run, "event", event)
    with pytest.raises(OSError):
        _run_job(fw, "fac-v3", FACTORY)
    assert ownership.current("v3") is None
    _nothing_shipped(fw)


def test_orchestration_a_log_line_the_terminal_cannot_encode_is_replaced(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fw.set_job(lines=["fake-shidashi: café ✓"])
    raw = io.BytesIO()
    terminal = io.TextIOWrapper(raw, encoding="ascii", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", terminal)
    res = _run_job(fw, "fac-v3", FACTORY)
    assert res.followed is True and res.exit_code == 0
    assert b"fake-shidashi: caf? ?" in raw.getvalue()
    assert ownership.current("v3") is None


def test_orchestration_a_follow_that_fails_keeps_the_lock_and_says_how_to_resume(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fw.set_job(hold_until_release=True)

    def broken_sink(line: str) -> None:
        raise RuntimeError("the terminal went away")

    monkeypatch.setattr(worker, "_echo", broken_sink)
    res = _run_job(fw, "fac-v3", FACTORY)
    assert res.followed is False and res.exit_code is None
    out = capfd.readouterr()
    _assert_resume_commands(out.out + out.err, "fac-v3")
    assert ownership.current("v3") is not None
    kinds = [
        json.loads(line).get("kind")
        for path in config.runs_dir().rglob("events.jsonl")
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    assert "worker.job.detached" in kinds


# --- group 6 review: an earlier run's rc must not end a job that is still starting ---


def test_orchestration_clears_a_stale_rc_as_soon_as_the_lock_is_taken(
    fw: FakeWorker,
) -> None:
    fw.put("out/jobs/fac-v3.rc", "0\n")  # an earlier fac-v3 that ended
    fw.put("out/jobs/fac-v3.runs", "20261001T000000Z-old001\n")
    assert _job(fw, "fac-v3", FACTORY) == 0
    calls = fw.calls()
    clear = next(
        i
        for i, c in enumerate(calls)
        if c["kind"] == "ssh"
        and "fac-v3.rc" in c["command"]
        and "rm -f" in c["command"]
        and "systemd-run" not in c["command"]
    )
    first_rsync = next(i for i, c in enumerate(calls) if c["kind"] == "rsync")
    assert clear < first_rsync  # gone before the push, while the lock is held
    assert "v3.owner.json" in calls[first_rsync]["locks"]


def test_orchestration_a_failed_clear_of_stale_files_releases_the_lock(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.fail(r"fac-v3\.runs$", code=1, stderr="rm: cannot remove: Read-only file system\n")
    text = _refused(lambda: _job(fw, "fac-v3", FACTORY), capfd)
    assert "Read-only" in text
    _nothing_shipped(fw)
    assert ownership.current("v3") is None


def test_orchestration_an_assemble_job_takes_no_lock_and_clears_nothing_early(
    fw: FakeWorker,
) -> None:
    assert _job(fw, "kde", ASSEMBLE) == 0
    early = [
        c for c in fw.ssh_commands() if "rm -f" in c and "kde.rc" in c and "systemd-run" not in c
    ]
    assert early == []
