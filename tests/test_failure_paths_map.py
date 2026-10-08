"""The 40 rows of design.md's Failure-path test map marked PLANNED 11.1 (story 010).

One test per row, named as the map names it, each asserting the row's outcome: the
exit code or the exception, and its message fragment. Against a fake worker
(tests/_fake_worker.py), like tests/test_cli_worker_jobs.py, tests/test_worker_job.py
and tests/test_failure_paths.py; their fixtures are copied here, not imported. A path
that is hard to reach for real (a probe that cannot exit non-zero, a runs directory
that fills up) is faked at the boundary the row names. No production change.
"""

import errno
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import audit, cli, config, ownership, remote, worker, workers
from shidashi.assembler import AssembleResult
from shidashi.cli import app
from shidashi.remote import HostKeyMismatch, RemoteResult, SyncError
from tests._fake_worker import ZEN3_MODEL, FakeWorker, seed_host_cache

cli_runner = CliRunner()
COMMIT = "0123456789abcdef0123456789abcdef01234567"
FACTORY = ["factory", "v3", "minimal", "systemd"]
ASSEMBLE = ["assemble", "v3", "kde", "systemd"]
_ROOT = os.geteuid() == 0


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    w.register()
    yield w
    w.close()


@pytest.fixture
def cached(fw: FakeWorker) -> dict[str, Path]:
    return seed_host_cache(fw)


@pytest.fixture
def gen(fw: FakeWorker) -> str:
    return str(fw.config()["generation"])


def _invoke(args: list[str], capfd: pytest.CaptureFixture[str]) -> tuple[Result, str]:
    """``shidashi worker ARGS``: its result and everything it printed, whitespace folded
    (rich wraps a long error line)."""
    result = cli_runner.invoke(app, ["worker", *args])
    out = capfd.readouterr()
    return result, _flat(result.output + out.out + out.err)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _no_traceback(result: Result) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception


def _owner(job: str, worker_name: str = "bentoo-lab", arch: str = "v3") -> ownership.Owner:
    return ownership.Owner(
        arch=arch,
        worker=worker_name,
        job=job,
        commit="a" * 40,
        since="2026-10-05T14:02:31Z",
        host_pid=None,
    )


def _lock(job: str, worker_name: str = "bentoo-lab", arch: str = "v3") -> ownership.Owner:
    return ownership.acquire(arch, _owner(job, worker_name, arch))


def _run_job(
    fw: FakeWorker, name: str, args: list[str], *, follow: bool = True
) -> worker.JobResult:
    """``worker.job`` inside an audited run, as the CLI calls it."""
    with audit.run(config.runs_dir(), command="worker-job", argv=["worker", "job", name, *args]):
        return worker.job(
            fw.remote(),
            fw.entry(),
            name,
            args,
            allow_dirty=False,
            follow=follow,
            results=fw.base / "results" / name,
        )


def _nothing_shipped(fw: FakeWorker) -> None:
    assert fw.rsync_calls() == []
    assert not any("tar -x" in c or "systemd-run" in c for c in fw.ssh_commands())
    assert fw.job_invocations() == []


def _no_contact(fw: FakeWorker) -> None:
    assert fw.ssh_commands() == [] and fw.rsync_calls() == []


# =====================================================================================
# R1 -- status
# =====================================================================================


def test_status_listing_prints_refused_and_exits_1_on_a_host_key_mismatch(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 4: one worker's changed key refuses its line; the others are still listed."""
    fw.register(fw.entry(), fw.entry(name="other-box", address="192.0.2.77"))
    fw.fail(
        host="other-box",
        code=255,
        stderr=(
            "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
            "Host key verification failed.\n"
        ),
    )
    result, out = _invoke(["status"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "other-box refused: other-box: the host key does not match the pin" in out
    assert "bentoo-lab reachable" in out  # the others are still listed


def test_status_name_on_a_changed_host_key_exits_1(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 5."""
    fw.host_key_changed()
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "error: bentoo-lab: the host key does not match the pin" in out


def test_status_of_an_unpaired_name_exits_1_naming_the_paired_workers(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 6: NAME not paired, then a registry that is not one."""
    result, out = _invoke(["status", "ghost"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "no paired worker named 'ghost' (paired: bentoo-lab)" in out
    (config.workers_dir() / "workers.json").write_text("{not json\n")
    result, out = _invoke(["status", "ghost"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "not a worker registry" in out
    assert fw.ssh_commands() == []


def test_status_with_an_unwritable_registry_warns_and_still_prints(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 7: the refreshed CPU flags cannot be saved; the probe still stands."""
    fw.register(fw.entry(cpu_flags=("sse2",)))  # stale flags: the status refreshes them

    def read_only(_path: Path, _entries: Any) -> None:
        raise OSError(errno.EROFS, "Read-only file system", str(_path))

    monkeypatch.setattr(workers, "save_registry", read_only)
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 0, out
    _no_traceback(result)
    assert "bentoo-lab's CPU flags not recorded" in out and "Read-only file system" in out
    assert ZEN3_MODEL in out and "bentoo-lab (192.0.2.10): reachable" in out


# =====================================================================================
# R2, R3, R6, R7 -- an unpaired name, Ctrl+C
# =====================================================================================


def test_worker_commands_refuse_an_unpaired_name_with_exit_1(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 9: run, logs, job, sync push/pull, poweroff."""
    for args in (
        ["run", "ghost", "--", "true"],
        ["logs", "ghost", "kde"],
        ["job", "ghost", "kde", "--", *ASSEMBLE],
        ["sync", "push", "ghost", "--arch", "v3"],
        ["sync", "pull", "ghost", "--arch", "v3", "--job", "kde"],
        ["poweroff", "ghost"],
    ):
        result, out = _invoke(args, capfd)
        assert result.exit_code == 1, (args, out)
        _no_traceback(result)
        assert "no paired worker named 'ghost' (paired: bentoo-lab)" in out, args
    _no_contact(fw)


def test_ctrl_c_in_the_worker_commands_exits_130(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 10: Ctrl+C in run, logs, job and sync push."""

    def interrupted(*_a: Any, **_k: Any) -> Any:
        raise KeyboardInterrupt

    for name in ("run_command", "logs", "job", "push"):
        monkeypatch.setattr(worker, name, interrupted)
    for args in (
        ["run", fw.name, "--", "true"],
        ["logs", fw.name, "kde", "-f"],
        ["job", fw.name, "kde", "--", *ASSEMBLE],
    ):
        result, out = _invoke(args, capfd)
        assert result.exit_code == 130, (args, out)
        _no_traceback(result)
    result, out = _invoke(["sync", "push", fw.name, "--arch", "v3"], capfd)
    assert result.exit_code == 130, out
    _no_traceback(result)
    assert "interrupted: run the same command again to resume the transfer" in out


# =====================================================================================
# R3 -- jobs
# =====================================================================================


def test_unit_argv_refuses_a_commit_that_is_not_hex() -> None:
    """Row 14: the commit becomes a path on the worker and a word of the unit."""
    for bad in ("", "HEAD", "ABCDEF0123", "abc;rm -rf /", "../x", "0123 4567"):
        with pytest.raises(ValueError, match="invalid commit"):
            worker.job_unit_argv("kde", bad, ASSEMBLE)
    assert f"--working-directory=/mnt/work/src/{COMMIT}" in worker.job_unit_argv(
        "kde", COMMIT, ASSEMBLE
    )


def test_job_outside_a_git_checkout_is_refused(
    fw: FakeWorker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 15: Shidashi imported from a directory that is no git checkout."""
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    monkeypatch.setattr(worker, "_host_repo", lambda: plain)
    with pytest.raises(worker.JobRefused) as refused:
        _run_job(fw, "kde", ASSEMBLE)
    assert "failed in the Shidashi checkout" in refused.value.reason
    assert str(plain) in refused.value.reason
    assert "run shidashi from a git checkout" in refused.value.fix
    _no_contact(fw)


def test_job_probe_exiting_non_zero_is_refused(
    fw: FakeWorker, cached: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 22: the probe script ends ``; true``, so only a remote shell that cannot
    start exits non-zero (not 255): faked at ``worker.run``."""
    real = remote.run  # what ``worker.run`` is bound to

    def shell_cannot_start(remote: Any, command: str, **kw: Any) -> RemoteResult:
        if "echo work=mounted" in command:
            return RemoteResult(command, 127, "", "bash: /bin/bash: No such file\n", 0.01)
        return real(remote, command, **kw)

    monkeypatch.setattr(worker, "run", shell_cannot_start)
    with pytest.raises(worker.JobRefused) as refused:
        _run_job(fw, "fac-v3", FACTORY)
    assert "the probe of bentoo-lab failed (exit 127)" in refused.value.reason
    assert "shidashi worker status bentoo-lab" in refused.value.fix
    _nothing_shipped(fw)
    assert ownership.current("v3") is None


def test_job_start_failing_otherwise_keeps_the_lock_and_reraises(
    fw: FakeWorker,
    cached: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Row 31: the unit may run, so the lock stays; the error is re-raised."""

    def key_changed(*_a: Any, **_k: Any) -> None:
        raise HostKeyMismatch("bentoo-lab", "SHA256:pinned", "SHA256:presented")

    monkeypatch.setattr(worker, "_start_unit", key_changed)
    with pytest.raises(HostKeyMismatch):
        _run_job(fw, "fac-v3", FACTORY)
    out = capfd.readouterr()
    text = _flat(out.out + out.err)
    assert "the start failed: the start of job fac-v3 on bentoo-lab was not confirmed" in text
    assert "shidashi worker logs bentoo-lab fac-v3 -f" in text
    assert "shidashi worker sync pull bentoo-lab --arch v3 --job fac-v3" in text
    assert "shidashi worker unlock v3" in text
    held = ownership.current("v3")
    assert held is not None and held.job == "fac-v3"


def test_job_follow_of_a_unit_stopped_without_an_rc_returns_unfollowed(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 36: the log stream exits 1 (not 255): the unit stopped without an rc."""
    fw.fail(r"tail -n \+1 -F", code=1)
    res = _run_job(fw, "fac-v3", FACTORY)
    assert res.followed is False and res.exit_code is None
    assert res.log is None and res.isos == () and res.run_ids == ()
    out = capfd.readouterr()
    text = _flat(out.out + out.err)
    assert "the log stream ended (exit 1) before shidashi-job-fac-v3 wrote its exit code" in text
    assert ownership.current("v3") is not None  # the lock stays with the job
    assert not fw.calls("emaint")  # nothing pulled


def test_cli_job_exits_130_when_the_follow_breaks(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 37: exit 130; worker.job printed the resume commands, the CLI does not again."""
    fw.fail(r"tail -n \+1 -F", code=1)
    result, out = _invoke(["job", fw.name, "kde", "--", *ASSEMBLE], capfd)
    assert result.exit_code == 130, out
    _no_traceback(result)
    assert "job kde keeps running on bentoo-lab" in out
    assert out.count("shidashi worker logs bentoo-lab kde -f") == 1
    assert "exited" not in out


def test_job_with_an_unwritable_runs_dir_warns_and_runs_unaudited(
    fw: FakeWorker,
    cached: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Row 39: the run cannot be written (here: the disk is full); the job still runs."""

    def disk_full(root: Path, **_k: Any) -> Any:
        raise OSError(errno.ENOSPC, "No space left on device", str(root))

    monkeypatch.setattr(audit, "Run", disk_full)
    result, out = _invoke(
        ["job", fw.name, "kde", "--results", str(fw.base / "res"), "--", *ASSEMBLE], capfd
    )
    assert result.exit_code == 0, out
    _no_traceback(result)
    assert "warning: audit trail disabled" in out and "No space left on device" in out
    assert fw.job_invocations()  # the job ran


def test_job_naming_an_unknown_arch_is_refused(fw: FakeWorker, cached: dict[str, Path]) -> None:
    """Row 46."""
    with pytest.raises(worker.JobRefused) as refused:
        _run_job(fw, "fac-nope", ["factory", "nope", "minimal", "systemd"])
    assert "unknown arch 'nope'" in refused.value.reason
    assert "name an arch fragment of variants/arch/" in refused.value.fix
    _no_contact(fw)
    assert ownership.current("nope") is None


# =====================================================================================
# R5 -- the owner lock
# =====================================================================================


@pytest.mark.skipif(_ROOT, reason="root reads and writes anywhere")
def test_current_and_release_of_an_unreadable_lock_raise_lock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 54: a lock that cannot be read, then one that cannot be removed."""
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    held = _lock("fac-v3")
    lock = ownership.locks_dir() / "v3.owner.json"
    lock.chmod(0o000)
    try:
        with pytest.raises(ownership.LockError) as err:
            ownership.current("v3")
        assert str(lock) in str(err.value)
        with pytest.raises(ownership.LockError) as err:
            ownership.release("v3", expected=held)
        assert str(lock) in str(err.value)
    finally:
        lock.chmod(0o664)
    ownership.locks_dir().chmod(0o555)  # readable, but the lock cannot be unlinked
    try:
        with pytest.raises(ownership.LockError) as err:
            ownership.release("v3", expected=held)
        assert str(lock) in str(err.value)
    finally:
        ownership.locks_dir().chmod(0o2775)
    assert ownership.current("v3") == held


def test_holder_alive_is_false_for_a_host_holder_without_a_pid() -> None:
    """Row 59: a host holder is judged by its pid only; the worker probe is not asked."""
    asked: list[ownership.Owner] = []

    def probe(owner: ownership.Owner) -> bool:
        asked.append(owner)
        return True

    pidless = _owner("factory", worker_name="host:bentoo")
    assert ownership.holder_alive(pidless, probe=probe) is False
    assert asked == []


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A host ``build`` whose factory and assembler only record that they ran
    (tests/test_cli_owner_lock.py)."""
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    built: list[str] = []

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            built.append(f"factory:{recipe.arch}")
            self.recipe = recipe

        def build(self, **_k: object) -> None:
            raise SystemExit(0)

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            built.append(f"assembler:{recipe.arch}")
            self.recipe = recipe

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            iso = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    monkeypatch.setattr(cli, "Factory", FakeFactory)
    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    return built


@pytest.mark.skipif(_ROOT, reason="root reads anywhere")
def test_build_with_an_unreadable_lock_exits_1(lab: list[str]) -> None:
    """Row 65: the host build cannot tell who owns v3: exit 1 before anything starts."""
    _lock("fac-v3")
    lock = ownership.locks_dir() / "v3.owner.json"
    lock.chmod(0o000)
    runs = config.runs_dir()
    before = set(runs.iterdir()) if runs.is_dir() else set()
    try:
        result = cli_runner.invoke(app, ["build", "v3", "systemd", "--images", "minimal"])
    finally:
        lock.chmod(0o664)
    out = _flat(result.output)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "cannot write the owner lock at" in out and str(lock) in out
    assert lab == []  # neither the factory nor an assemble ran
    after = set(runs.iterdir()) if runs.is_dir() else set()
    assert after == before  # refused before the build's audited run opened


def test_sync_pull_ctrl_c_as_the_owner_keeps_the_lock_and_exits_130(
    fw: FakeWorker,
    cached: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Row 68."""
    held = _lock("fac-v3")
    fw.put("out/jobs/fac-v3.rc", "0\n")  # ended: rc present, unit inactive -> the owner

    def interrupted(*_a: Any, **_k: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "pull", interrupted)
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code == 130, out
    _no_traceback(result)
    assert "v3's owner lock stays held by job fac-v3" in out
    assert "shidashi worker unlock v3" in out
    assert ownership.current("v3") == held


def test_unlock_of_a_free_arch_says_so(fw: FakeWorker, capfd: pytest.CaptureFixture[str]) -> None:
    """Row 69."""
    result, out = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 0, out
    _no_traceback(result)
    assert "v3 is not locked" in out
    assert fw.ssh_commands() == []


@pytest.mark.skipif(_ROOT, reason="root reads and writes anywhere")
def test_unlock_with_an_unreadable_lock_exits_1(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 70: the lock cannot be read, then it cannot be released."""
    held = _lock("fac-v3")
    lock = ownership.locks_dir() / "v3.owner.json"
    lock.chmod(0o000)
    try:
        result, out = _invoke(["unlock", "v3", "--force"], capfd)
    finally:
        lock.chmod(0o664)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "cannot write the owner lock at" in out and str(lock) in out
    ownership.locks_dir().chmod(0o555)
    try:
        result, out = _invoke(["unlock", "v3", "--force"], capfd)
    finally:
        ownership.locks_dir().chmod(0o2775)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert str(lock) in out
    assert ownership.current("v3") == held


def test_unlock_needs_force_when_the_holders_worker_is_not_paired(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 76: the holder's worker was unpaired: its job's state cannot be read."""
    held = _lock("fac-v3", worker_name="gone-box")
    result, out = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "cannot tell whether gone-box (job fac-v3, unit shidashi-job-fac-v3) still runs" in out
    assert "no paired worker named 'gone-box'" in out
    assert "shidashi worker unlock v3 --force" in out
    assert ownership.current("v3") == held
    assert fw.ssh_commands() == []


def test_job_state_raises_sync_error_when_systemctl_fails(
    fw: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 77: ``systemctl show`` fails, then prints no state."""
    fw.fail(r"systemctl show", code=1, stderr="Failed to connect to bus: No such file\n")
    with pytest.raises(SyncError) as err:
        worker.job_state(fw.remote(), "fac-v3")
    assert err.value.step == "probe shidashi-job-fac-v3"
    assert "Failed to connect to bus" in str(err.value)

    def no_state(remote: Any, command: str, **_k: Any) -> RemoteResult:
        return RemoteResult(command, 0, "rc=present\nstate=\n", "", 0.01)

    monkeypatch.setattr(worker, "run", no_state)
    with pytest.raises(SyncError) as err:
        worker.job_state(fw.remote(), "fac-v3")
    assert err.value.step == "probe shidashi-job-fac-v3"


# =====================================================================================
# R6 -- push and pull
# =====================================================================================


def test_push_refuses_a_venv_whose_pyvenv_cfg_names_no_interpreter(
    fw: FakeWorker, cached: dict[str, Path]
) -> None:
    """Row 81: no ``home =``, then no pyvenv.cfg at all; refused before any contact."""
    cfg = fw.venv / "pyvenv.cfg"
    cfg.write_text("version_info = 3.14\nhome =\n")
    with pytest.raises(SyncError) as err:
        worker.push(fw.remote(), "v3", bwlimit=None)
    assert err.value.step == "runtime"
    assert "names no interpreter" in str(err.value)
    cfg.unlink()
    with pytest.raises(SyncError) as err:
        worker.push(fw.remote(), "v3", bwlimit=None)
    assert err.value.step == "runtime" and f"cannot read {cfg}" in str(err.value)
    _no_contact(fw)


def test_sync_push_failure_exits_1_without_a_traceback(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 82: an rsync exit."""
    fw.fail(r"rsync --server", code=12, stderr="rsync: connection unexpectedly closed\n")
    result, out = _invoke(["sync", "push", fw.name, "--arch", "v3"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "push runtime/venv failed (exit 12)" in out
    assert "connection unexpectedly closed" in out


def test_pull_without_a_job_or_an_arch_raises_value_error(fw: FakeWorker, tmp_path: Path) -> None:
    """Row 84."""
    with pytest.raises(ValueError, match="a pull names a job, an arch or both"):
        worker.pull(fw.remote(), None, None, results=tmp_path / "results")
    _no_contact(fw)


def test_pull_as_an_owner_without_an_arch_raises_value_error(
    fw: FakeWorker, tmp_path: Path
) -> None:
    """Row 85."""
    with pytest.raises(ValueError, match="an archless pull has no binhost to bring back"):
        worker.pull(fw.remote(), None, "kde", results=tmp_path / "results", owner=_owner("kde"))
    _no_contact(fw)


def test_pull_when_the_listing_fails_raises_sync_error(fw: FakeWorker, tmp_path: Path) -> None:
    """Row 90: the ssh that lists what the worker has exits non-zero."""
    fw.put("out/jobs/kde.log", "log\n")
    fw.fail(r"for p in ", code=2, stderr="bash: fork: Resource temporarily unavailable\n")
    with pytest.raises(SyncError) as err:
        worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results")
    assert err.value.step == "pull: list the job's results"
    assert "Resource temporarily unavailable" in str(err.value)
    assert fw.rsync_calls() == []


def test_pull_when_emaint_leaves_no_packages_raises_sync_error(
    fw: FakeWorker, cached: dict[str, Path], gen: str, tmp_path: Path
) -> None:
    """Row 92: ``emaint`` exits 0 but writes no index."""
    owner = _lock("kde")
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n")
    fw.fail(r"emaint binhost --fix", code=0)  # "succeeds" without running
    index = cached["index"]
    before = index.read_text()
    with pytest.raises(SyncError) as err:
        worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results", owner=owner)
    assert f"/mnt/work/cache/binpkgs/v3/{gen}/Packages does not exist after the regeneration" in (
        str(err.value)
    )
    assert index.read_text() == before  # the host's index is untouched


def test_pull_of_a_runs_file_with_an_invalid_run_id_raises_sync_error(
    fw: FakeWorker, tmp_path: Path
) -> None:
    """Row 93: ``<job>.runs`` lists a word that is not a run id."""
    fw.put("out/jobs/kde.log", "log\n")
    fw.put("out/jobs/kde.runs", "20261005T110000Z-abc123\n-rf\n")
    fw.put("out/runs/20261005T110000Z-abc123/events.jsonl", "{}\n")
    with pytest.raises(SyncError) as err:
        worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results")
    assert err.value.step == "pull out/runs"
    assert "lists an invalid run id '-rf'" in str(err.value)
    assert not (config.runs_dir() / "20261005T110000Z-abc123").exists()  # none fetched


def test_pull_of_an_archless_job_says_it_has_no_binhost(fw: FakeWorker, tmp_path: Path) -> None:
    """Row 96 (a degrade): the log and rc still come back."""
    fw.put("out/jobs/kde.log", "log of kde\n")
    fw.put("out/jobs/kde.rc", "0\n")
    got = worker.pull(fw.remote(), None, "kde", results=tmp_path / "results")
    assert got.binhost is False
    assert got.binhost_reason == "an archless job has no binhost"
    assert got.log == tmp_path / "results" / "kde.log" and got.log.read_text() == "log of kde\n"


def test_job_of_a_factory_that_built_nothing_says_binhost_not_pulled(
    fw: FakeWorker,
    cached: dict[str, Path],
    gen: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Row 97 (a degrade): no PKGDIR on the worker at the pull; the lock is released."""
    import shutil

    real = worker.pull

    def built_nothing(*a: Any, **k: Any) -> worker.PullResult:
        shutil.rmtree(fw.work / "cache" / "binpkgs" / "v3", ignore_errors=True)
        return real(*a, **k)

    monkeypatch.setattr(worker, "pull", built_nothing)
    res = _run_job(fw, "fac-v3", FACTORY)
    assert res.followed is True and res.exit_code == 0
    out = capfd.readouterr()
    text = _flat(out.out + out.err)
    assert (
        f"binhost not pulled: /mnt/work/cache/binpkgs/v3/{gen} does not exist on bentoo-lab "
        "(the job built no binpkg)"
    ) in text
    assert ownership.current("v3") is None  # released all the same


def test_sync_pull_job_when_nobody_holds_the_lock_says_why(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 100 (a degrade)."""
    fw.put("out/jobs/fac-v3.rc", "0\n")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code == 0, out
    _no_traceback(result)
    assert "binhost not pulled: job fac-v3 does not hold v3's owner lock (nobody does)" in out
    assert not fw.calls("emaint")


def test_sync_pull_job_when_the_jobs_state_cannot_be_read_exits_1(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 104: ``job_ended`` raises; nothing is pulled, the lock stays."""
    held = _lock("fac-v3")
    fw.put("out/jobs/fac-v3.rc", "0\n")
    fw.fail(r"systemctl show", code=1, stderr="Failed to connect to bus: No such file\n")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "probe shidashi-job-fac-v3 failed (exit 1)" in out
    assert "Failed to connect to bus" in out
    assert ownership.current("v3") == held
    assert fw.rsync_calls() == []


def test_sync_pull_without_arch_or_job_exits_2(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 105: a usage error, before any contact."""
    result, out = _invoke(["sync", "pull", fw.name], capfd)
    assert result.exit_code == 2, out
    assert "--arch" in out
    _no_contact(fw)


def test_sync_pull_of_an_unknown_arch_exits_1_before_any_contact(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 106. Hostile fixture: a lock left behind by an arch fragment since removed,
    held by this worker's job -- a guard that came later would probe the worker."""
    _lock("kde", arch="nope")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "nope", "--job", "kde"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "unknown arch 'nope'" in out
    _no_contact(fw)


def test_sync_pull_of_an_invalid_job_name_exits_1_before_any_contact(
    fw: FakeWorker, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 107. The name becomes a results path: one that climbs out of
    ``worker-results/`` is refused for its name, before anything resolves that path."""
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "Bad_Name"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "invalid job name 'Bad_Name'" in out
    if not _ROOT:
        (tmp_path / "worker-results" / fw.name).mkdir(parents=True)
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            result, out = _invoke(
                ["sync", "pull", fw.name, "--arch", "v3", "--job", "../../ro"], capfd
            )
        finally:
            ro.chmod(0o755)
        assert result.exit_code == 1, out
        _no_traceback(result)
        assert "invalid job name '../../ro'" in out
    _no_contact(fw)


def test_sync_pull_whose_release_fails_exits_1(
    fw: FakeWorker,
    cached: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Row 109: the pull succeeded, the release after it does not."""
    _lock("fac-v3")
    fw.put("out/jobs/fac-v3.rc", "0\n")
    lock = ownership.locks_dir() / "v3.owner.json"

    def cannot_release(_arch: str, *, expected: ownership.Owner) -> None:
        raise ownership.LockError(lock, "root:0", "give the user's group write access")

    monkeypatch.setattr(ownership, "release", cannot_release)
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "pulled from bentoo-lab" in out  # the pull itself went through
    assert f"error: cannot write the owner lock at {lock}" in out
    assert "released v3's owner lock" not in out


# =====================================================================================
# R7 -- logs, poweroff
# =====================================================================================


def test_logs_when_ssh_fails_exits_1_saying_the_connection_failed(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 113."""
    fw.unreachable()
    result, out = _invoke(["logs", fw.name, "kde"], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "the connection to bentoo-lab failed or was lost (ssh exit 255)" in out


def test_poweroff_when_listing_the_jobs_fails_exits_1(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 115: ``systemctl list-units`` fails; nothing is powered off."""
    fw.fail(r"systemctl list-units", code=1, stderr="Failed to connect to bus: No such file\n")
    result, out = _invoke(["poweroff", fw.name], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "list the jobs failed (exit 1)" in out and "Failed to connect to bus" in out
    assert not fw.powered_off()


def test_poweroff_when_ssh_fails_otherwise_exits_1(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """Row 118: ssh 255 whose message is not a closed connection."""
    fw.fail(
        r"poweroff",
        code=255,
        stderr="ssh: connect to host 192.0.2.10 port 22: Connection refused\r\n",
    )
    result, out = _invoke(["poweroff", fw.name], capfd)
    assert result.exit_code == 1, out
    _no_traceback(result)
    assert "bentoo-lab (192.0.2.10) is unreachable" in out and "Connection refused" in out
    assert f"{fw.name} is powering off" not in out
