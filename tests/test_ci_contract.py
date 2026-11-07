"""The three tracks agree: pool <-> worker <-> guest (story 034, task 5.4; R3.4, R5.2, R5.5,
R6.4, R6.10, R7.1, R7.2).

Each check reads one side's output and the other side's input, never a constant twice:

- every option the pool's dispatch passes to ``shidashi ci-guest run`` is an option of that
  command, and ``--cpus`` is not passed (the worker derives the vCPUs, R7.1);
- the credentials ``run_job`` hands the VM are the ones the guest's runner unit imports and
  its hook reads; the console lines the worker waits for are the ones the guest prints;
- the labels a minted runner carries include the label the pool filters on;
- the dispatched unit gets the guest's memory plus QEMU's room (``MemoryMax``);
- the unit the dispatch creates is the one the worker's status lists as a CI job (and the
  pool's ``busy()`` probe greps: ``shidashi-ci-*``);
- the liveness probe of ``sweep`` names the containers ``run_job`` and ``prepare`` start:
  a running job's and a running preparation's directories are kept;
- the marker the worker waits for is printed by the rendered user-data only once the
  prepare check passed (R3.4);
- the ``results/<job>.json`` ``run_job`` writes is what the pool's ``SshWorker.results()``
  reads: a failed job's file on the worker becomes the pool's ``ci_job_failed`` (R6.10).
"""

import fnmatch
import hashlib
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import typer
import yaml
from typer.testing import CliRunner

from shidashi import cli, config
from tests._fake_ci_guest import complete_guest, host_bin_without_fakes, run_marker_step
from tests._fake_ci_pool import level_of, pool_events, write_config
from tests._fake_ci_toolbox import (
    PREPARED_MARKER,
    RESULT_PREFIX,
    Boot,
    FakeCiToolbox,
    no_download,
    option_values,
    stand_in_toolbox_host,
)
from tests._fake_github_api import Reply, mint_jit_config, serving_fake_github
from tests._fake_worker import FakeWorker
from tests._pending import try_import
from tests.test_ci_guest_seed import write_pins

ci_guest_cli: Any = try_import("shidashi", "ci_guest_cli")
dispatch_argv: Any = try_import("shidashi.ci_pool", "dispatch_argv")
load_config: Any = try_import("shidashi.ci_pool", "load_config")
run_job: Any = try_import("shidashi.ci_guest", "run_job")
prepare: Any = try_import("shidashi.ci_guest", "prepare")
Limits: Any = try_import("shidashi.ci_guest", "Limits")
load_pins: Any = try_import("shidashi.ci_guest_seed", "load_pins")
prepare_user_data: Any = try_import("shidashi.ci_guest_seed", "prepare_user_data")
prepared_key: Any = try_import("shidashi.ci_guest_seed", "prepared_key")
repo_jit_config_request: Any = try_import("shidashi.github_api", "repo_jit_config_request")

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO = "obentoo/private-app"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def repo_pins() -> Any:
    return load_pins(config.seeds_dir() / "ci-guest.toml", REPO_ROOT / "lab" / "vm" / "runner.env")


def dispatched_words(tmp_path: Path) -> list[str]:
    cfg = load_config(write_config(tmp_path / "cfg"))
    command = dispatch_argv("9001", REPO, COMMIT, cfg.vm)
    words = shlex.split(command.replace("&&", " && ").replace(";", " ; "))
    assert "ci-guest" in words, command
    return words


def test_contract_every_dispatched_option_exists_on_ci_guest_run(tmp_path: Path) -> None:
    words = dispatched_words(tmp_path)
    tail = words[words.index("ci-guest") + 2 :]
    passed = {w.split("=", 1)[0] for w in tail if w.startswith("--")}
    command = typer.main.get_command(ci_guest_cli.app)
    run = getattr(command, "commands", {})["run"]
    accepted = {opt for param in run.params for opt in getattr(param, "opts", [])}
    assert {
        "--job",
        "--repo",
        "--memory-gib",
        "--disk-gib",
        "--timeout-min",
        "--min-free-gib",
    } <= passed
    assert passed <= accepted, passed - accepted
    assert "--cpus" not in passed and "--cpus" not in accepted, (passed, accepted)


def test_contract_dispatched_unit_has_room_for_the_guests_memory(tmp_path: Path) -> None:
    words = dispatched_words(tmp_path)
    memory = [w.split("=", 1)[1] for w in words if w.startswith("MemoryMax=")]
    memory += [w.split("MemoryMax=", 1)[1] for w in words if w.startswith("--property=MemoryMax=")]
    assert len(memory) == 1, words
    match = re.fullmatch(r"([0-9]+)G", memory[0])
    assert match and int(match.group(1)) >= 8 + 1


def test_contract_the_dispatched_unit_is_what_status_and_busy_call_a_ci_job(tmp_path: Path) -> None:
    words = dispatched_words(tmp_path)
    units = [w.split("=", 1)[1] for w in words if w.startswith("--unit=")]
    units += [words[i + 1] for i, w in enumerate(words) if w in ("--unit", "-u")]
    assert len(units) == 1, words
    unit = units[0].removesuffix(".service")
    assert fnmatch.fnmatchcase(unit, "shidashi-ci-*")
    assert not fnmatch.fnmatchcase(unit, "shidashi-job-*"), "a CI unit would block Shidashi jobs"


def test_contract_minted_labels_include_the_label_the_pool_filters_on(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path / "cfg"))
    request = repo_jit_config_request(REPO, cfg.label, "bentoo-worker-9001")
    assert request.method == "POST" and request.body, request
    body = json.loads(request.body)
    assert {"self-hosted", cfg.label} <= set(body["labels"])


def _unit_and_hook(user_data: str) -> tuple[str, str]:
    doc = yaml.safe_load(user_data)
    assert isinstance(doc, dict) and doc.get("write_files"), "the user-data writes no file"
    files = {f.get("path"): str(f.get("content", "")) for f in doc["write_files"]}
    unit = files["/etc/systemd/system/shidashi-runner.service"]
    hook_path = re.search(r"ACTIONS_RUNNER_HOOK_JOB_STARTED=(\S+)", unit)
    assert hook_path, unit
    return unit, files[hook_path.group(1).strip("\"'")]


@pytest.fixture
def job_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeCiToolbox, Path, Path]:
    """One job run through ``run_job`` with the repository's own pins."""
    stand_in_toolbox_host(monkeypatch, tmp_path / "host")
    no_download(monkeypatch)
    pins = repo_pins()
    root, run_dir = tmp_path / "ci", tmp_path / "run"
    prepared = root / "prepared" / f"{prepared_key(pins, prepare_user_data(pins))}.qcow2"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"QFI\xfb prepared")
    return FakeCiToolbox(), root, run_dir


def _run(toolbox: FakeCiToolbox, root: Path, run_dir: Path, job: str) -> int:
    (run_dir / job).mkdir(parents=True, exist_ok=True)
    secret = run_dir / job / "jitconfig"
    secret.write_text(mint_jit_config(), encoding="utf-8")
    secret.chmod(0o600)
    limits = Limits(memory_gib=4, disk_gib=40, timeout_min=60, min_free_gib=0)
    return int(
        run_job(
            job, REPO, root=root, run_dir=run_dir, limits=limits, runner=toolbox, clock=lambda: 0.0
        )
    )


def test_contract_credentials_run_job_hands_over_are_the_ones_the_guest_reads(
    job_run: tuple[FakeCiToolbox, Path, Path],
) -> None:
    toolbox, root, run_dir = job_run
    toolbox.on_boot = lambda _argv: Boot(rc=0, serial=f"{RESULT_PREFIX}0\n")
    assert _run(toolbox, root, run_dir, "9001") == 0
    assert len(toolbox.boots) == 1, "the job VM did not boot exactly once"
    names = {
        v.split(",")[0].removeprefix("name=opt/io.systemd.credentials/")
        for v in option_values(toolbox.boots[0].argv, "-fw_cfg")
    }
    assert names == {"shidashi.jitconfig", "shidashi.repo"}
    unit, hook = _unit_and_hook(prepare_user_data(repo_pins()))
    imported = re.findall(r"^(?:Import|Load)Credential=([^:\s]+)", unit, re.M)
    for name in names:
        assert any(fnmatch.fnmatchcase(name, glob) for glob in imported), (name, imported)
    assert "shidashi.jitconfig" in unit and "shidashi.repo" in hook


def test_contract_the_console_lines_the_worker_waits_for_are_the_ones_the_guest_prints() -> None:
    """The worker's prepared marker and result prefix (tests/_fake_ci_toolbox.py, used by the
    prepare and run tests) are what the rendered guest prints on its console."""
    user_data = prepare_user_data(repo_pins())
    unit, _ = _unit_and_hook(user_data)
    assert re.search(rf"\b{re.escape(PREPARED_MARKER)}\b", user_data)
    assert RESULT_PREFIX in unit


def test_contract_sweep_probes_the_container_run_job_starts(
    job_run: tuple[FakeCiToolbox, Path, Path],
) -> None:
    """Job ``a``'s VM still runs (its container alive) when job ``b`` starts: ``b``'s sweep
    must keep ``a``'s directory -- the probe and the container name agree."""
    toolbox, root, run_dir = job_run
    toolbox.on_boot = lambda _argv: Boot(rc=0, serial=f"{RESULT_PREFIX}0\n")
    assert _run(toolbox, root, run_dir, "9001") == 0
    assert len(toolbox.boots) == 1, "the job VM did not boot exactly once"
    container = toolbox.boots[0].container
    assert container and "9001" in container, toolbox.boots[0]
    (root / "jobs" / "9001").mkdir(parents=True)
    (root / "jobs" / "9001" / "overlay.qcow2").write_bytes(b"in use")
    toolbox.alive = {container}
    assert _run(toolbox, root, run_dir, "9002") == 0
    assert (root / "jobs" / "9001" / "overlay.qcow2").exists(), "a running job's files were swept"


def test_contract_the_marker_the_worker_waits_for_follows_the_prepare_check(
    tmp_path: Path,
) -> None:
    """R3.4: with the repository's own pins, the console line ``prepare`` accepts as success
    is printed only on a guest the check passes -- not on one missing a pinned package."""
    doc = yaml.safe_load(prepare_user_data(repo_pins()))
    assert isinstance(doc, dict), "the user-data is not a YAML mapping"
    host_bin = host_bin_without_fakes(tmp_path)
    complete = complete_guest(tmp_path / "complete", host_bin, doc)
    console, _ = run_marker_step(complete, doc)
    assert PREPARED_MARKER in console, console
    broken = complete_guest(tmp_path / "broken", host_bin, doc)
    broken.status["docker.io"] = "deinstall ok config-files"
    console, stdout = run_marker_step(broken, doc)
    assert PREPARED_MARKER not in console + stdout.splitlines()


BASE = b"QFI\xfb base image stand-in"


def test_contract_sweep_probes_the_container_prepare_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preparation whose VM still runs keeps its work dir when another preparation
    sweeps under the lock; a dead one's is removed -- the probe and the name agree."""
    stand_in_toolbox_host(monkeypatch, tmp_path / "host")
    no_download(monkeypatch)
    pins = load_pins(*write_pins(tmp_path / "pins", sha256=hashlib.sha256(BASE).hexdigest()))
    key = prepared_key(pins, prepare_user_data(pins))
    root = tmp_path / "ci"
    (root / "images").mkdir(parents=True)
    (root / "images" / f"{hashlib.sha256(BASE).hexdigest()}.img").write_bytes(BASE)
    toolbox = FakeCiToolbox(
        on_boot=lambda _argv: Boot(rc=0, serial=f"{PREPARED_MARKER}\nreboot: Power down\n")
    )
    prepare(pins, root=root, runner=toolbox)
    assert len(toolbox.boots) == 1, "the prepare VM did not boot exactly once"
    container = toolbox.boots[0].container
    assert container and key in container, toolbox.boots[0]
    running, dead = "a" * 16, "b" * 16
    for other in (running, dead):
        (root / "prepare" / other).mkdir(parents=True)
        (root / "prepare" / other / "disk.qcow2").write_bytes(b"in use")
    toolbox.alive = {container.replace(key, running)}
    (root / "prepared" / f"{key}.qcow2").unlink()
    prepare(pins, root=root, runner=toolbox)
    assert (root / "prepare" / running / "disk.qcow2").exists(), "a running preparation was swept"
    assert not (root / "prepare" / dead).exists(), "the sweep never ran"


def test_contract_the_result_run_job_writes_is_the_one_the_pool_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed job's ``results/<job>.json``, written by ``run_job`` on the worker's disk, is
    read by ``shidashi ci pool`` through ssh: one WARN ``ci_job_failed`` naming the job, and
    the file gone (R6.10 reads it before forgetting the served jobs)."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.chdir(tmp_path)
    fw = FakeWorker.install(tmp_path / "fw", monkeypatch)
    fw.register()
    worker_side = pytest.MonkeyPatch()
    try:
        stand_in_toolbox_host(monkeypatch, tmp_path / "host")
        pins = repo_pins()
        root, run_dir = fw.work / "ci", tmp_path / "run"
        prepared = root / "prepared" / f"{prepared_key(pins, prepare_user_data(pins))}.qcow2"
        prepared.parent.mkdir(parents=True)
        prepared.write_bytes(b"QFI\xfb prepared")
        toolbox = FakeCiToolbox(on_boot=lambda _argv: Boot(rc=0, serial=f"{RESULT_PREFIX}1\n"))
        # no download while the WORKER runs the job; the pool below must reach the fake API,
        # and no_download patches the OpenerDirector github_api.send uses (deviation 9)
        no_download(worker_side)
        assert _run(toolbox, root, run_dir, "9001") == 1
        worker_side.undo()
        assert list((root / "results").iterdir()), "run_job left no result"
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        cfg = write_config(tmp_path / "cfg", repos=[REPO], worker=fw.name)
        runs = f"/repos/{REPO}/actions/runs"
        with serving_fake_github(monkeypatch) as api:
            api.routes[("GET", runs)] = Reply(200, {"total_count": 0, "workflow_runs": []})
            result = CliRunner().invoke(cli.app, ["ci", "pool", "--once", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        seen = pool_events(result.stderr, caplog.records)
        failed = [e for e in seen if e.get("event") == "ci_job_failed"]
        assert len(failed) == 1 and level_of(failed[0]) == "WARNING", seen
        assert str(failed[0].get("job")) == "9001", failed
        assert list((root / "results").iterdir()) == [], "the pool left the result behind"
    finally:
        worker_side.undo()  # idempotent: also undone when the worker half failed
        fw.close()
