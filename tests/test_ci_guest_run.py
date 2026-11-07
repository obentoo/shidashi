"""One job, one ephemeral VM (story 034, tasks 3.1 and 3.3; R4.1-R4.6, R4.8, R5.2, R5.3,
R5.6, R7.1, R7.2, R8.1, R8.2, R9.1, Q4, Q9).

Contract (design.md, "shidashi/ci_guest.py" -> ``run``; Security Considerations; Error
Handling Strategy):

``run_job(job, repo, *, root, run_dir, limits, runner, clock, nproc=os.cpu_count,
sys_module=Path("/sys/module")) -> int`` -- ``limits`` is
``ci_guest.Limits(memory_gib, disk_gib, timeout_min, min_free_gib)``; the vCPUs are not a
limit, the worker derives them: ``cpus_for(nproc())``, half its CPUs, at least 2 (R7.1):

- ``vmrunner.preflight`` first: without the toolbox, exit 1 (or ``CiGuestError``) with its
  message, naming ``shidashi worker sync push <worker> --arch v3``, and nothing booted;
- reads and removes ``<run_dir>/<job>/jitconfig`` (a regular file of the caller's -- root
  on the worker -- mode 0600) before the VM starts (R5.3); anything else is exit 1 naming it;
- sweeps orphans first (R4.6): ``jobs/*`` and stale ``<run_dir>/*`` -- never its own JIT
  config, never ``prepare/*`` (that belongs to a preparation holding ``prepare.lock``);
- below ``min_free_gib`` free on ``root``: exit 3 before creating any job file (R4.4);
- prepares the guest when the key has no prepared image -- with one, nothing is fetched;
- ``<root>/jobs/<job>/overlay.qcow2``: a qcow2 overlay backed by the prepared image, of
  ``disk_gib`` virtual size (R4.1); the JIT config and the repository as two 0600 files in
  the job dir, handed to QEMU by ``-fw_cfg`` as ``shidashi.jitconfig``/``shidashi.repo``;
- QEMU runs in container ``shidashi-vm-ci-<job>`` started with ``--cgroups=split``: it
  stays in the dispatched unit's cgroup, under its ``CPUWeight=``/``MemoryMax=`` (R7.2);
- the guest's ``SHIDASHI-CI-RESULT=<rc>`` console line decides: 0 -> exit 0, any other or
  none -> exit 1 (R4.5); past ``timeout_min`` the VM is stopped, exit 4 (R4.3);
- the job directory is deleted in every outcome (R4.2), and with it every copy of the JIT
  config (R5.6); the JIT config is in no argv, no event, no log line (R5.2);
- ``results/<job>.json`` -- ``job``, ``repo``, ``rc``, ``ended`` -- is written for the pool in
  every outcome of a job it accepted, through a temporary file renamed over it;
- ``ci_vm_start`` (``job``, ``repo``, ``cpus``, ``memory_gib``, ``nested_kvm``,
  ``result: "started"``, ``duration_ms: 0``) before the boot and ``ci_vm_end`` (``job``,
  ``repo``, ``duration_ms``, ``result``) after it, one JSON line each (R9.1).

``nested_kvm(sys) -> bool`` reads ``kvm_amd``/``kvm_intel`` ``parameters/nested`` under
``sys`` (``1``/``Y`` on). ``sweep``: tests/test_ci_guest_sweep.py.

A job id is ``[a-z0-9-]+`` (``worker.validate_job_name``) and a repository
``OWNER/NAME`` of ``[A-Za-z0-9_.-]``; a bad one is exit 1 (or ``CiGuestError``) naming it.

The worker side is ``JobHost`` (tests/_fake_ci_job.py), its toolbox ``FakeCiToolbox``.
"""

import logging
from pathlib import Path
from typing import Any

import pytest

from tests._fake_ci_job import (
    JOB,
    REPO,
    JobHost,
    events,
    job_host,
    run,
    stopped_after_boot,
    write_sys_module,
)
from tests._fake_ci_toolbox import missing_toolbox, option_values
from tests._pending import try_import

nested_kvm: Any = try_import("shidashi.ci_guest", "nested_kvm")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobHost:
    return job_host(tmp_path, monkeypatch)


# --- 3.1 run one job --------------------------------------------------------------------------


def test_run_a_passed_job_exits_0_and_leaves_no_job_file(host: JobHost) -> None:
    """R4.5, R4.2."""
    host.guest("0")
    rc, _ = run(host)
    assert rc == 0
    assert len(host.toolbox.boots) == 1, "the job VM did not boot exactly once"
    assert not host.job_dir().exists()


@pytest.mark.parametrize(
    ("result", "qemu_rc"),
    [
        pytest.param("1", 0, id="job-failed"),
        pytest.param("143", 0, id="runner-killed"),
        pytest.param(None, 0, id="no-result-line"),
        pytest.param(None, 1, id="qemu-crashed"),
    ],
)
def test_run_a_failed_or_silent_job_exits_1_and_leaves_no_job_file(
    host: JobHost, result: str | None, qemu_rc: int
) -> None:
    """R4.5: only the guest's ``0`` is a pass; R4.2 in every outcome."""
    host.guest(result, rc=qemu_rc)
    rc, _ = run(host)
    assert rc == 1
    assert host.toolbox.boots, "the job VM never booted"
    assert not host.job_dir().exists()


def test_run_boots_an_overlay_backed_by_the_prepared_image(host: JobHost) -> None:
    """R4.1: a copy-on-write overlay of ``disk_gib``, backed by the prepared image's
    absolute path in an explicit qcow2 format, and the VM's one disk."""
    host.guest("0")
    run(host, disk_gib=40)
    creates = [c for c in host.toolbox.calls if "qemu-img" in " ".join(c) and "create" in c]
    assert len(creates) == 1, host.toolbox.everything()
    create = creates[0]
    assert option_values(create, "-b") == [str(host.prepared)]
    assert option_values(create, "-F") == ["qcow2"] and option_values(create, "-f") == ["qcow2"]
    overlay = host.job_dir() / "overlay.qcow2"
    assert str(overlay) in create
    assert create[-1] in ("40G", "40g", str(40 * 1024**3))
    drives = option_values(host.toolbox.boots[0].argv, "-drive")
    assert any(f"file={overlay}" in d.split(",") for d in drives), drives


@pytest.mark.parametrize(
    ("nproc", "cpus"),
    [pytest.param(16, 8, id="half"), pytest.param(3, 2, id="at-least-2")],
)
def test_run_gives_the_vm_half_the_workers_cpus_and_the_limits_memory(
    host: JobHost, nproc: int, cpus: int
) -> None:
    """R7.1: the worker alone knows its CPUs -- no limit carries them."""
    host.guest("0")
    run(host, nproc=nproc, memory_gib=6)
    assert len(host.toolbox.boots) == 1, "the job VM did not boot exactly once"
    argv = host.toolbox.boots[0].argv
    assert option_values(argv, "-smp")[0].split(",")[0] == str(cpus)
    assert option_values(argv, "-m")[0] in ("6G", "6g", "6144", "6144M")
    assert option_values(argv, "-cpu") == ["host"]


def test_run_boots_qemu_in_the_jobs_own_container_inside_the_units_cgroup(
    host: JobHost,
) -> None:
    """R7.2: ``--cgroups=split`` keeps QEMU under the unit's ``CPUWeight=``/``MemoryMax=``;
    the container's name is the one ``sweep`` probes; no vsock device."""
    host.guest("0")
    run(host)
    assert len(host.toolbox.boots) == 1, "the job VM did not boot exactly once"
    boot = host.toolbox.boots[0]
    assert boot.container == f"shidashi-vm-ci-{JOB}", boot.outer
    assert [w for w in boot.outer if w.startswith("--cgroups")] == ["--cgroups=split"], boot.outer
    assert not [w for w in boot.argv if "vsock" in w], boot.argv


def test_run_without_the_toolbox_names_the_sync_command_and_boots_nothing(
    host: JobHost, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    missing_toolbox(monkeypatch)
    host.guest("0")
    rc, message = run(host)
    assert rc == 1
    shown = message + capfd.readouterr().err
    assert "shidashi worker sync push" in shown and "--arch v3" in shown, shown
    assert host.toolbox.calls == []
    assert not host.job_dir().exists()


def test_run_takes_the_jitconfig_off_tmpfs_before_the_vm_starts(host: JobHost) -> None:
    """R5.3."""
    host.guest("0")
    run(host)
    assert host.seen.tmpfs_jitconfig is False
    assert not (host.run_dir / JOB / "jitconfig").exists()


def test_run_hands_the_jitconfig_and_repo_as_0600_credential_files(host: JobHost) -> None:
    """R5.2: the guest's unit imports these two credentials (task 1.2)."""
    host.guest("0")
    run(host)
    assert host.seen.creds.get("shidashi.jitconfig") == (0o600, host.jitconfig.encode()), host.seen
    mode, repo = host.seen.creds.get("shidashi.repo", (0, b""))
    assert mode == 0o600 and repo.decode().strip() == REPO


def test_run_never_puts_the_jitconfig_in_argv_or_output(
    host: JobHost, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """R5.2, Q4: not in any command the worker runs, not on stdout/stderr, not in a log."""
    caplog.set_level(logging.DEBUG)
    host.guest("0")
    run(host)
    assert host.toolbox.boots, "the job VM never booted"
    out = capfd.readouterr()
    for where, text in (
        ("argv", host.toolbox.everything()),
        ("stdout", out.out),
        ("stderr", out.err),
        ("logs", caplog.text),
    ):
        assert host.jitconfig not in text, f"the JIT config leaked to {where}"


@pytest.mark.parametrize(
    ("result", "qemu_rc", "hang"),
    [
        pytest.param("0", 0, False, id="pass"),
        pytest.param("1", 0, False, id="fail"),
        pytest.param(None, 1, False, id="crash"),
        pytest.param(None, 0, True, id="timeout"),
    ],
)
def test_run_leaves_no_copy_of_the_jitconfig_on_the_work_disk(
    host: JobHost, result: str | None, qemu_rc: int, hang: bool
) -> None:
    """R5.6, R4.2: whatever the outcome."""
    host.guest(result, rc=qemu_rc, hang=hang)
    run(host)
    assert host.seen.creds, "the VM never received its credentials"
    assert host.files_holding_the_jitconfig() == []
    assert not host.job_dir().exists()


def test_run_a_job_longer_than_timeout_min_is_stopped_and_exits_4(host: JobHost) -> None:
    """R4.3: the bound is ``timeout_min``; the VM is stopped, the files deleted."""
    host.guest(None, hang=True)
    rc, _ = run(host, timeout_min=90)
    assert rc == 4
    timeout = host.toolbox.boots[0].kwargs.get("timeout")
    assert timeout is not None and 0 < timeout <= 90 * 60, timeout
    assert stopped_after_boot(host.toolbox), "the VM's container was left running"
    assert not host.job_dir().exists()


def test_run_with_too_little_free_space_exits_3_before_creating_any_job_file(
    host: JobHost,
) -> None:
    """R4.4: no work disk has a million GiB free."""
    host.guest("0")
    rc, _ = run(host, min_free_gib=1_000_000)
    assert rc == 3
    assert not (host.root / "jobs" / JOB).exists()
    assert host.toolbox.boots == []
    assert (host.result_file() or {}).get("rc") == 3, "the pool would not learn of the low disk"
    assert not any("create" in c for c in host.toolbox.calls if "qemu-img" in " ".join(c))


@pytest.mark.parametrize("spoil", ["mode-0644", "missing", "symlink", "directory"])
def test_run_an_unsafe_or_missing_jitconfig_is_refused_naming_it(
    host: JobHost, spoil: str, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The JIT config must be a regular 0600 file of the caller's (root on the worker)."""
    path = host.run_dir / JOB / "jitconfig"
    path.unlink()
    if spoil == "mode-0644":
        host.place_jitconfig(mode=0o644)
    elif spoil == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text(host.jitconfig, encoding="utf-8")
        target.chmod(0o600)
        path.symlink_to(target)
    elif spoil == "directory":
        path.mkdir()
    host.guest("0")
    rc, message = run(host)
    assert rc == 1
    assert str(path) in message + capfd.readouterr().err
    assert host.toolbox.boots == []


@pytest.mark.parametrize(
    ("job", "repo"),
    [
        pytest.param("../9001", REPO, id="job-path"),
        pytest.param("Job9001", REPO, id="job-upper"),
        pytest.param("9001;reboot", REPO, id="job-shell"),
        pytest.param(JOB, "obentoo", id="repo-no-name"),
        pytest.param(JOB, "obentoo/app/extra", id="repo-extra"),
        pytest.param(JOB, "obentoo/app;reboot", id="repo-shell"),
        pytest.param(JOB, "obentoo/app ", id="repo-space"),
    ],
)
def test_run_refuses_a_bad_job_id_or_repository(
    host: JobHost, job: str, repo: str, capfd: pytest.CaptureFixture[str]
) -> None:
    host.guest("0")
    rc, message = run(host, job=job, repo=repo)
    assert rc == 1
    shown = message + capfd.readouterr().err
    assert (job if job != JOB else repo).strip() in shown, shown
    assert host.toolbox.boots == []
    written = [p for p in host.root.rglob("*.json") if p.parent != host.root / "results"]
    assert written == [], "a result was written outside results/"


def test_run_accepts_every_character_github_allows_in_a_repository(host: JobHost) -> None:
    """Hostile split of the repository check: dots, underscores and dashes are GitHub's."""
    host.guest("0")
    rc, _ = run(host, repo="my.org_1/re_po-2.x")
    assert rc == 0
    assert (
        host.seen.creds.get("shidashi.repo", (0, b""))[1].decode().strip() == "my.org_1/re_po-2.x"
    )


def test_run_sweeps_orphans_before_its_own_vm_and_keeps_its_own_jitconfig(
    host: JobHost,
) -> None:
    """R4.6: a crashed job's directory and a JIT config whose unit never started are gone
    before this job's VM boots. Hostile: this job's own JIT config is just as "stale" to a
    probe of running containers -- its VM has not started -- and must reach the VM."""
    orphan = host.job_dir("8000")
    orphan.mkdir(parents=True)
    (orphan / "overlay.qcow2").write_bytes(b"left by a crash")
    stale = host.place_jitconfig("8001")
    host.guest("0")
    rc, message = run(host)
    assert rc == 0, message
    assert host.seen.creds.get("shidashi.jitconfig", (0, b""))[1] == host.jitconfig.encode()
    assert "8000" not in host.seen.jobs_present
    assert not orphan.exists()
    assert not stale.parent.exists()


def test_run_leaves_a_preparations_work_dir_to_the_preparation(host: JobHost) -> None:
    """``prepare/*`` is swept only under ``prepare.lock``: a job never takes it -- a
    ``ci-guest prepare`` may be running beside it."""
    work = host.root / "prepare" / "0123456789abcdef"
    work.mkdir(parents=True)
    (work / "disk.qcow2").write_bytes(b"being prepared")
    host.guest("0")
    rc, _ = run(host)
    assert rc == 0
    assert len(host.toolbox.boots) == 1, "the job VM did not boot exactly once"
    assert (work / "disk.qcow2").exists()


@pytest.mark.parametrize(
    ("result", "qemu_rc", "hang", "expected"),
    [
        pytest.param("0", 0, False, 0, id="pass"),
        pytest.param("1", 0, False, 1, id="fail"),
        pytest.param(None, 1, False, 1, id="crash"),
        pytest.param(None, 0, True, 4, id="timeout"),
    ],
)
def test_run_writes_the_jobs_result_for_the_pool_in_every_outcome(
    host: JobHost, result: str | None, qemu_rc: int, hang: bool, expected: int
) -> None:
    """The pool reads ``results/<job>.json`` once the worker is idle (task 4.3)."""
    host.guest(result, rc=qemu_rc, hang=hang)
    rc, _ = run(host)
    assert rc == expected
    data = host.result_file()
    assert data is not None, "no results/<job>.json"
    assert str(data.get("job")) == JOB and data.get("repo") == REPO
    assert data.get("rc") == expected and data.get("ended")
    assert "run_id" not in data
    assert sorted(p.name for p in (host.root / "results").iterdir()) == [f"{JOB}.json"]
    assert host.jitconfig not in (host.root / "results" / f"{JOB}.json").read_text()


# --- 3.3 events and nested KVM -------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "hang", "expected"),
    [
        pytest.param("0", False, "pass", id="pass"),
        pytest.param("1", False, "fail", id="fail"),
        pytest.param(None, True, "timeout", id="timeout"),
    ],
)
def test_run_emits_the_start_event_before_the_boot_and_the_end_event_after(
    host: JobHost, capfd: pytest.CaptureFixture[str], result: str | None, hang: bool, expected: str
) -> None:
    """R9.1, Q9: one JSON line each, around the VM's life."""
    at_boot: list[str] = []
    host.at_boot = lambda: at_boot.append(capfd.readouterr().err)
    host.guest(result, hang=hang)
    run(host, nproc=6, memory_gib=6)
    assert len(at_boot) == 1, "the job VM did not boot exactly once"
    before, after = events(at_boot[0]), events(capfd.readouterr().err)
    start = [e for e in before if e["event"] == "ci_vm_start"]
    end = [e for e in after if e["event"] == "ci_vm_end"]
    assert len(start) == 1 and len(end) == 1, (before, after)
    assert not [e for e in before if e["event"] == "ci_vm_end"]
    assert not [e for e in after if e["event"] == "ci_vm_start"]
    assert start[0].get("job") == JOB and start[0].get("repo") == REPO
    assert start[0].get("cpus") == 3 and start[0].get("memory_gib") == 6
    assert start[0].get("result") == "started" and start[0].get("duration_ms") == 0
    assert end[0].get("job") == JOB and end[0].get("repo") == REPO
    assert end[0].get("result") == expected
    assert isinstance(end[0].get("duration_ms"), int) and end[0]["duration_ms"] >= 0
    for event in start + end:
        assert host.jitconfig not in str(event) and "run_id" not in event, event


@pytest.mark.parametrize(
    ("amd", "intel", "on"),
    [
        pytest.param("Y\n", None, True, id="nested-on"),
        pytest.param("0\n", None, False, id="nested-off"),
        pytest.param(None, None, False, id="no-kvm-module"),
    ],
)
def test_run_start_event_records_nested_kvm_as_the_worker_reports_it(
    host: JobHost,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    amd: str | None,
    intel: str | None,
    on: bool,
) -> None:
    """R8.1, R8.2: read from the ``sys_module`` it is given; the VM boots either way."""
    host.guest("0")
    rc, _ = run(host, sys_module=write_sys_module(tmp_path / "sys", amd=amd, intel=intel))
    assert rc == 0
    start = [e for e in events(capfd.readouterr().err) if e["event"] == "ci_vm_start"]
    assert len(start) == 1, start
    assert start[0].get("nested_kvm") is on
    assert option_values(host.toolbox.boots[0].argv, "-cpu") == ["host"]


def test_nested_kvm_reads_the_loaded_modules_parameter(tmp_path: Path) -> None:
    """R8.1, R8.2: read, never assumed; on is ``1`` or ``Y``."""
    table = [
        ("1\n", None, True),
        ("Y\n", None, True),
        (None, "Y\n", True),
        (None, "1\n", True),
        ("0\n", None, False),
        ("N\n", None, False),
        (None, "N\n", False),
        (None, None, False),
        ("0\n", "Y\n", True),  # either module with nesting on
    ]
    got = [
        (amd, intel, nested_kvm(write_sys_module(tmp_path / f"sys-{n}", amd=amd, intel=intel)))
        for n, (amd, intel, _) in enumerate(table)
    ]
    assert got == [(amd, intel, on) for amd, intel, on in table]
