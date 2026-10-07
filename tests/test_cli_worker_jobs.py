"""``shidashi worker status|run|logs|unlock|poweroff|job|sync`` (story 010, task 6).

Against a fake worker (tests/_fake_worker.py) registered through story 009's
registry. Test names are chosen for the tasks' ``-k`` selections: ``status``;
``run or logs or unlock or poweroff``; ``job or sync``.
"""

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import config, ownership, worker, workers
from shidashi.cli import app
from tests._fake_worker import (
    BUILD_ID,
    LOAD1,
    MEM_TOTAL_KB,
    WORK_AVAIL,
    ZEN3_CPUINFO_FLAGS,
    ZEN3_MODEL,
    FakeWorker,
    mentions_size,
    option_values,
    seed_host_cache,
)

cli = CliRunner()
TRUNK_FP = "9f2c4e1ab37d5c0e8f6a1b2c3d4e5f60718293a4b5c6d7e8f9"


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    w.register()
    yield w
    w.close()


def _invoke(args: list[str], capfd: pytest.CaptureFixture[str] | None = None) -> tuple[Result, str]:
    result = cli.invoke(app, ["worker", *args])
    extra = ""
    if capfd is not None:
        out = capfd.readouterr()
        extra = out.out + out.err
    return result, result.output + extra


def _no_traceback(result: Result) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception


def _trunk(fw: FakeWorker) -> None:
    fw.put(
        f"scratch/assemble/checkpoints/v3-systemd/install-{TRUNK_FP[:24]}.json",
        json.dumps(
            {"format": 3, "step": "install", "fingerprint": TRUNK_FP, "images": ["minimal"]}
        ),
    )


# =====================================================================================
# 6.1 status
# =====================================================================================


def test_status_jobs_with_shared_prefixes_stay_distinct(fw: FakeWorker) -> None:
    fw.activate_unit("shidashi-job-kde")
    fw.activate_unit("shidashi-job-kde-v3")
    fw.activate_unit("shidashi-worker-restore")  # not a job
    st = worker.status(fw.remote())
    names = sorted(j.removeprefix("shidashi-job-").removesuffix(".service") for j in st.jobs)
    assert names == ["kde", "kde-v3"]


def test_status_max_target_is_none_when_one_v3_flag_is_missing(fw: FakeWorker) -> None:
    fw.set(cpu_flags=[f for f in ZEN3_CPUINFO_FLAGS if f != "fma"])
    st = worker.status(fw.remote())
    assert not st.max_target or str(st.max_target).lower() == "none"


def test_status_df_of_an_unmounted_work_disk_is_not_free_work_space(fw: FakeWorker) -> None:
    """Unmounted, /mnt/work is a directory on the RAM root: df answers for the root."""
    fw.set(mounted=False)
    st = worker.status(fw.remote())
    assert st.work_free is None


def test_status_parses_the_probe_into_worker_status(fw: FakeWorker) -> None:
    fw.activate_unit("shidashi-job-fac-v3")
    _trunk(fw)
    st = worker.status(fw.remote())
    assert st.cpu_model == ZEN3_MODEL
    assert st.threads == 16
    assert st.max_target == "v3"
    assert st.mem_total in (MEM_TOTAL_KB, MEM_TOTAL_KB * 1024)
    assert st.mem_available and st.mem_available < st.mem_total
    assert st.work_free == WORK_AVAIL
    assert st.image == BUILD_ID
    assert st.smart and "PASSED" in st.smart
    assert [j.removeprefix("shidashi-job-").removesuffix(".service") for j in st.jobs] == ["fac-v3"]
    assert st.load1 == pytest.approx(LOAD1)
    assert st.trunks == (f"v3-systemd/install-{TRUNK_FP[:24]}",)  # <arch>-<init>/<step>-<fp24>
    assert st.reachable is True and st.reason is None
    assert "avx2" in st.cpu_flags and "pni" in st.cpu_flags
    assert st.runnable_arches == ("v3",)  # Zen 3: neither znver5 nor arrowlake
    assert st.accepts_jobs is True
    fw.assert_pinned()


def test_status_without_smartctl_has_no_verdict(fw: FakeWorker) -> None:
    fw.set(smartctl=None)
    assert worker.status(fw.remote()).smart is None


def test_status_of_a_silent_worker_is_unreachable_within_the_timeout(fw: FakeWorker) -> None:
    fw.fail(host=fw.name, code=255, stderr="Connection timed out\n", stall=30)
    t0 = time.monotonic()
    st = worker.status(fw.remote(), timeout=2.0)  # never raises for a silent worker (C4)
    assert time.monotonic() - t0 < 8  # the whole command is bounded, not only the connect
    assert st.reachable is False and st.reason
    assert st.accepts_jobs is False
    assert st.threads == 0 and st.load1 == 0.0  # typed, never None (014 sums them)
    assert isinstance(st.mem_total, int) and isinstance(st.mem_available, int)


def test_status_refreshes_the_registrys_cpu_flags(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.register(fw.entry(cpu_flags=("sse2",)))  # stale flags from an older pairing
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 0, out
    registry = workers.load_registry(config.workers_dir() / "workers.json")
    assert "avx2" in registry[fw.name].cpu_flags  # refreshed from the probe (R1.6)


def test_status_name_prints_every_field(fw: FakeWorker, capfd: pytest.CaptureFixture[str]) -> None:
    fw.activate_unit("shidashi-job-fac-v3")
    _trunk(fw)
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 0, out
    for part in (
        ZEN3_MODEL,
        "16",
        "v3",
        BUILD_ID,
        "fac-v3",
        "PASSED",
        f"{LOAD1:.2f}",
        TRUNK_FP[:12],
    ):
        assert part in out, part
    assert mentions_size(out, MEM_TOTAL_KB * 1024)
    assert mentions_size(out, WORK_AVAIL)


def test_status_name_without_a_work_disk_says_so(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.set(mounted=False)
    _, out = _invoke(["status", fw.name], capfd)
    assert "no work disk" in out.lower()


def test_status_name_states_the_image_has_no_smartctl_or_shows_a_failing_verdict(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.set(smartctl="FAILED!")
    _, out = _invoke(["status", fw.name], capfd)
    assert "FAILED" in out and "PASSED" not in out
    fw.set(smartctl=None)
    _, out = _invoke(["status", fw.name], capfd)
    assert "no smartctl" in out.lower()


def test_status_lists_every_worker_reachable_or_unreachable_within_5_s(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.register(
        fw.entry(),
        fw.entry(name="dead-box", address="192.0.2.99"),
        fw.entry(name="stalled-box", address="192.0.2.98"),
    )
    fw.fail(host="dead-box", code=255, stderr="Connection timed out\n", hang=30)
    # accepts the connection, then says nothing: only a whole-command timeout ends it
    fw.fail(host="stalled-box", code=255, stderr="Connection timed out\n", stall=30)
    t0 = time.monotonic()
    _, out = _invoke(["status"], capfd)
    assert time.monotonic() - t0 < 20  # at most 5 s each, three workers
    lines = out.splitlines()
    (alive,) = [line for line in lines if "bentoo-lab" in line]
    (dead,) = [line for line in lines if "dead-box" in line]
    (stalled,) = [line for line in lines if "stalled-box" in line]
    assert "reachable" in alive.lower() and "unreachable" not in alive.lower()
    assert "unreachable" in dead.lower() and "unreachable" in stalled.lower()


# =====================================================================================
# 6.2 run, logs, unlock, poweroff
# =====================================================================================


def test_run_quotes_every_argument_and_exits_with_the_remote_code(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    words = ["a b", "$(id)", "it's", ";", "*"]
    result, out = _invoke(["run", fw.name, "--", "printf", "%s\\n", *words], capfd)
    assert result.exit_code == 0, out
    got = [line for line in out.splitlines() if line in words]
    assert got == words
    result, _ = _invoke(["run", fw.name, "--", "sh", "-c", "exit 3"], capfd)
    assert result.exit_code == 3
    fw.assert_pinned()


def test_logs_prints_that_name_only(fw: FakeWorker, capfd: pytest.CaptureFixture[str]) -> None:
    fw.put("out/jobs/kde.log", "log of kde\n")
    fw.put("out/jobs/kde.rc", "0\n")
    fw.put("out/jobs/kde-v3.log", "log of kde-v3\n")
    result, out = _invoke(["logs", fw.name, "kde"], capfd)
    assert result.exit_code == 0, out
    assert "log of kde\n" in out + "\n" and "log of kde-v3" not in out


def test_logs_follow_ends_when_the_rc_file_appears(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    log = fw.put("out/jobs/kde.log", "first line\n")

    def finish() -> None:
        time.sleep(1.5)
        with log.open("a") as f:
            f.write("last line\n")
        time.sleep(0.5)
        fw.put("out/jobs/kde.rc", "0\n")

    threading.Thread(target=finish, daemon=True).start()
    t0 = time.monotonic()
    result, out = _invoke(["logs", fw.name, "kde", "-f"], capfd)
    assert result.exit_code == 0, out
    assert "first line" in out and "last line" in out
    assert time.monotonic() - t0 < 30


def test_logs_refuses_a_name_outside_the_alphabet(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    result, _ = _invoke(["logs", fw.name, "../../etc/shadow"], capfd)
    assert result.exit_code != 0
    assert not any("shadow" in c for c in fw.ssh_commands())


def _lock(job: str, worker_name: str = "bentoo-lab") -> Any:
    return ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker=worker_name,
            job=job,
            commit="a" * 40,
            since="2026-10-05T14:02:31Z",
            host_pid=None,
        ),
    )


def test_unlock_releases_when_only_a_longer_named_unit_is_active(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    _lock("fac")
    fw.put("out/jobs/fac.rc", "0\n")  # fac has ended: its rc, and no unit of its own
    fw.activate_unit("shidashi-job-fac-v3")
    result, out = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 0, out
    assert ownership.current("v3") is None


def test_unlock_refuses_while_the_owning_unit_is_active(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    held = _lock("fac-v3")
    fw.activate_unit("shidashi-job-fac-v3")
    result, out = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 1
    assert "fac-v3" in out and "--force" in out
    assert ownership.current("v3") == held
    result, _ = _invoke(["unlock", "v3", "--force"], capfd)
    assert result.exit_code == 0 and ownership.current("v3") is None


def test_unlock_releases_once_the_owner_has_ended(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    _lock("fac-v3")
    fw.put("out/jobs/fac-v3.rc", "0\n")  # ended: its rc written, its unit gone
    result, out = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 0, out
    assert ownership.current("v3") is None


def test_unlock_needs_force_when_the_owner_is_unreachable(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    held = _lock("fac-v3")
    fw.unreachable()
    result, _ = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 1 and ownership.current("v3") == held
    result, _ = _invoke(["unlock", "v3", "--force"], capfd)
    assert result.exit_code == 0 and ownership.current("v3") is None


def _host_lock(pid: int) -> Any:
    return ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker="host:bentoo",
            job="factory",
            commit="a" * 40,
            since="2026-10-05T14:02:31Z",
            host_pid=pid,
        ),
    )


def test_unlock_refuses_a_host_holder_whose_process_is_alive(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:

    held = _host_lock(1)  # a pid this user cannot signal (EPERM): alive, not dead
    result, out = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 1 and "--force" in out
    assert ownership.current("v3") == held


def test_unlock_releases_a_host_holder_whose_process_is_gone(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    import subprocess
    import sys

    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    _host_lock(int(child.stdout))  # a pid that has exited
    result, out = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 0, out
    assert ownership.current("v3") is None


def test_poweroff_counts_the_connection_closing_as_success(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.fail(r"poweroff", code=255, stderr="Connection to 192.0.2.10 closed by remote host.\n")
    result, out = _invoke(["poweroff", fw.name], capfd)
    assert result.exit_code == 0, out


def test_poweroff_reports_another_failure(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.fail(r"poweroff", code=1, stderr="Failed to power off system: Access denied\n")
    result, out = _invoke(["poweroff", fw.name], capfd)
    assert result.exit_code == 1 and "Access denied" in out
    _no_traceback(result)


def test_poweroff_ignores_units_that_are_not_shidashi_jobs(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.activate_unit("shidashi-worker-restore")
    result, out = _invoke(["poweroff", fw.name], capfd)
    assert result.exit_code == 0, out
    assert fw.powered_off()


def test_poweroff_refuses_while_a_unit_of_shidashi_is_active_unless_forced(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.activate_unit("shidashi-job-fac-v3")
    result, out = _invoke(["poweroff", fw.name], capfd)
    assert result.exit_code == 1 and "fac-v3" in out
    assert not fw.powered_off()
    result, _ = _invoke(["poweroff", fw.name, "--force"], capfd)
    assert result.exit_code == 0 and fw.powered_off()


# =====================================================================================
# 6.3 job and sync
# =====================================================================================


@pytest.fixture
def cached(fw: FakeWorker) -> dict[str, Path]:
    return seed_host_cache(fw)


def test_job_exits_with_the_jobs_code_and_brings_the_iso(
    fw: FakeWorker, cached: dict[str, Path], tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.set_job(rc=5)
    res = tmp_path / "res"
    result, out = _invoke(
        ["job", fw.name, "kde", "--results", str(res), "--", "assemble", "v3", "kde", "systemd"],
        capfd,
    )
    assert result.exit_code == 5, out
    assert any(p.suffix == ".iso" for p in res.rglob("*"))
    assert "fake-shidashi: assemble v3 kde systemd" in out


def test_job_for_an_owned_arch_exits_1_naming_the_owner(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    ownership.acquire(
        "v3",
        ownership.Owner(
            arch="v3",
            worker="other-box",
            job="fac-x",
            commit="b" * 40,
            since="2026-10-05T14:02:31Z",
            host_pid=None,
        ),
    )
    result, out = _invoke(
        ["job", fw.name, "fac-v3", "--", "factory", "v3", "minimal", "systemd"], capfd
    )
    assert result.exit_code == 1
    assert "other-box" in out and "shidashi worker unlock v3" in out
    _no_traceback(result)


def test_job_for_an_arch_with_an_unknown_flag_exits_1_naming_it(
    fw: FakeWorker,
    cached: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    from tests.test_isaguard import _future_arch

    _future_arch(tmp_path, monkeypatch, "avx10_2")
    result, out = _invoke(
        ["job", fw.name, "fut", "--", "factory", "future", "minimal", "systemd"], capfd
    )
    assert result.exit_code == 1 and "avx10_2" in out
    _no_traceback(result)
    assert fw.rsync_calls() == []


def test_job_on_a_changed_host_key_exits_1(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    fw.host_key_changed()
    result, out = _invoke(["job", fw.name, "kde", "--", "assemble", "v3", "kde", "systemd"], capfd)
    assert result.exit_code == 1 and "host key" in out.lower()
    _no_traceback(result)


def test_job_on_an_unreachable_worker_exits_1(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    fw.unreachable()
    result, out = _invoke(["job", fw.name, "kde", "--", "assemble", "v3", "kde", "systemd"], capfd)
    assert result.exit_code == 1 and fw.name in out
    _no_traceback(result)


def test_job_no_follow_returns_and_keeps_a_factory_jobs_lock(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    fw.set_job(hold_until_release=True)
    result, out = _invoke(
        ["job", fw.name, "fac-v3", "--no-follow", "--", "factory", "v3", "minimal", "systemd"],
        capfd,
    )
    assert result.exit_code == 0, out
    assert ownership.current("v3") is not None


def test_sync_push_fills_the_worker_within_the_bwlimit(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    result, out = _invoke(["sync", "push", fw.name, "--arch", "v3", "--bwlimit", "700"], capfd)
    assert result.exit_code == 0, out
    rel = cached["binpkg"].relative_to(fw.host_cache)
    assert (fw.work / "cache" / rel).is_file()
    assert all(option_values(c["argv"], "--bwlimit") == ["700"] for c in fw.rsync_calls())


def test_sync_pull_job_of_the_holders_factory_job_brings_binpkgs_and_releases(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    gen = fw.config()["generation"]
    _lock("fac-v3")  # the interrupted factory job still owns v3 (R3.8 → R6.9)
    fw.put("out/jobs/fac-v3.rc", "0\n")  # it has ended: rc present, unit inactive
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n")
    fw.put("out/iso/fac-v3/bentoo-kde.iso", "ISO\n")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code == 0, out
    assert (fw.host_cache / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").is_file()
    assert ownership.current("v3") is None  # released after the successful pull


def test_sync_pull_job_while_the_holders_job_still_runs_keeps_the_lock_and_the_binhost(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    gen = fw.config()["generation"]
    held = _lock("fac-v3")
    fw.activate_unit("shidashi-job-fac-v3")  # still running: no rc yet
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code == 0, out
    assert not (fw.host_cache / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").exists()
    assert ownership.current("v3") == held  # kept until the job ends (R6.11)
    assert not fw.calls("emaint")


def test_sync_pull_without_job_never_touches_the_binhost(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    gen = fw.config()["generation"]
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3"], capfd)  # v3 is free
    assert result.exit_code == 0, out
    assert not (fw.host_cache / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").exists()
    assert not fw.calls("emaint")
    assert ownership.current("v3") is None  # and no lock was taken (R6.10)
    assert all("v3.owner.json" not in c["locks"] for c in fw.rsync_calls())


def test_sync_pull_while_another_writer_holds_the_arch_skips_the_binhost(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    gen = fw.config()["generation"]
    held = _lock("fac-v3", worker_name="other-box")
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n")
    fw.put("out/iso/kde/bentoo-kde.iso", "ISO\n")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "kde"], capfd)
    assert result.exit_code == 0, out
    assert not (fw.host_cache / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").exists()
    assert "binhost" in out.lower() and "other-box" in out  # says why it was skipped
    assert ownership.current("v3") == held


def test_job_with_an_invalid_name_exits_1_before_any_ssh(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    result, out = _invoke(
        ["job", fw.name, "Bad_Name", "--", "assemble", "v3", "kde", "systemd"], capfd
    )
    assert result.exit_code == 1 and "Bad_Name" in out
    _no_traceback(result)
    assert fw.ssh_commands() == [] and fw.rsync_calls() == []


def test_sync_pull_into_an_unwritable_cache_exits_1_naming_the_path(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("root writes anywhere")
    pkgdir = cached["binpkg"].parent.parent
    pkgdir.chmod(0o555)
    try:
        result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3"], capfd)
    finally:
        pkgdir.chmod(0o755)
    assert result.exit_code == 1 and str(pkgdir) in out
    _no_traceback(result)


# =====================================================================================
# group 6 review: regressions
# =====================================================================================


def test_unlock_refuses_a_worker_holder_that_may_still_be_starting(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    """The lock is taken before the push: no unit and no rc yet is not an end."""
    held = _lock("fac-v3")
    result, out = _invoke(["unlock", "v3"], capfd)
    assert result.exit_code == 1
    assert "starting" in out and "--force" in out
    assert ownership.current("v3") == held
    _no_traceback(result)
    result, _ = _invoke(["unlock", "v3", "--force"], capfd)
    assert result.exit_code == 0 and ownership.current("v3") is None


def test_unlock_refuses_while_the_owning_unit_is_activating_or_deactivating(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    held = _lock("fac-v3")
    fw.put("out/jobs/fac-v3.rc", "0\n")  # an rc alone is not the end
    for state in ("activating", "deactivating"):
        fw.activate_unit("shidashi-job-fac-v3", state)
        result, out = _invoke(["unlock", "v3"], capfd)
        assert result.exit_code == 1 and state in out and "--force" in out
        assert ownership.current("v3") == held


def test_sync_pull_job_while_the_holders_job_is_starting_keeps_the_lock_and_the_binhost(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    gen = fw.config()["generation"]
    held = _lock("fac-v3")  # taken, pushing: no unit, no rc yet
    fw.put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code == 0, out
    assert not (fw.host_cache / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").exists()
    assert ownership.current("v3") == held
    assert not fw.calls("emaint")


def test_sync_pull_job_while_the_holders_unit_is_deactivating_keeps_the_lock(
    fw: FakeWorker, cached: dict[str, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    held = _lock("fac-v3")
    fw.put("out/jobs/fac-v3.rc", "0\n")
    fw.activate_unit("shidashi-job-fac-v3", "deactivating")
    result, out = _invoke(["sync", "pull", fw.name, "--arch", "v3", "--job", "fac-v3"], capfd)
    assert result.exit_code == 0, out
    assert ownership.current("v3") == held
    assert not fw.calls("emaint")


def test_poweroff_queues_the_shutdown_without_blocking(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    result, out = _invoke(["poweroff", fw.name], capfd)
    assert result.exit_code == 0, out
    assert any("--no-block" in c and "poweroff" in c for c in fw.ssh_commands())
    assert fw.powered_off()


def test_poweroff_counts_a_connection_reset_as_success(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.fail(
        r"poweroff",
        code=255,
        stderr="Read from remote host 192.0.2.10: Connection reset by peer\r\n",
    )
    result, out = _invoke(["poweroff", fw.name], capfd)
    assert result.exit_code == 0, out


def test_status_name_of_a_disk_smartctl_cannot_identify_says_no_smart_device(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.set(smartctl="UNDETECTED")  # a device-mapper disk: smartctl's usage error
    assert worker.status(fw.remote()).smart == "unknown: no SMART device"
    _, out = _invoke(["status", fw.name], capfd)
    assert "no SMART device" in out and "usage summary" not in out
