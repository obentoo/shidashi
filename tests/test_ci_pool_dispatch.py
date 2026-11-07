"""The pool's dispatch command (story 034, task 4.4; R5.1-R5.3, R6.4, R7.2, Q4).

Contract (design.md, "shidashi/ci_pool.py + shidashi ci pool", step 4; Error Handling
Strategy): ``dispatch_argv(job, repo, commit, limits) -> str`` is the remote command -- the
JIT config from ssh STDIN to ``/run/shidashi/ci/<job>/jitconfig`` (0600, directory 0700),
then ``systemd-run --unit=shidashi-ci-<job> -p CPUWeight=20 -p MemoryMax=<memory+1>G`` of
``shidashi ci-guest run --job --repo --memory-gib --disk-gib --timeout-min --min-free-gib``
(no ``--cpus``: the worker derives them) with the shipped tree and venv (R5.1, R5.2, R7.2).
A ``systemd-run`` that fails removes ``/run/shidashi/ci/<job>`` and the command exits
non-zero: no JIT config is left behind (R5.3). A job or repository that could reach the
shell never becomes a command.

The command runs under bash with ``/run/shidashi`` moved into a temporary directory and a
``systemd-run`` that records its argv. ``SshWorker`` (commit from ``source``, ``busy()``,
``results()``) is exercised through ``shidashi ci pool``: tests/test_ci_pool_cli.py.
"""

import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from shidashi import worker
from tests._fake_ci_pool import write_config
from tests._fake_github_api import mint_jit_config
from tests._pending import try_import

load_config: Any = try_import("shidashi.ci_pool", "load_config")
dispatch_argv: Any = try_import("shidashi.ci_pool", "dispatch_argv")
CiDispatchError: Any = try_import("shidashi.ci_pool", "CiDispatchError")

REPO = "obentoo/private-app"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return load_config(write_config(tmp_path / "cfg"))


def bash_dispatch(
    tmp_path: Path, command: str, jitconfig: str, *, systemd_run_rc: int = 0
) -> tuple[Path, Any, Path]:
    """Run the remote command under bash: ``/run/shidashi`` moved to ``tmp_path``, a
    ``systemd-run`` that records its argv and exits ``systemd_run_rc``."""
    run = tmp_path / "run" / "shidashi"
    shims = tmp_path / "shims"
    shims.mkdir()
    record = tmp_path / "systemd-run.json"
    (shims / "systemd-run").write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"json.dump({{'argv': sys.argv[1:]}}, open({str(record)!r}, 'w'))\n"
        f"sys.exit({systemd_run_rc})\n",
        encoding="utf-8",
    )
    (shims / "systemd-run").chmod(0o755)
    assert command.strip(), "dispatch_argv returned no command"
    moved = command.replace("/run/shidashi", str(run))
    done = subprocess.run(
        ["/bin/bash", "-c", moved],
        input=jitconfig,
        text=True,
        capture_output=True,
        env={"PATH": f"{shims}:/usr/bin:/bin"},
        timeout=30,
        check=False,
    )
    return run, done, record


def run_dispatch(tmp_path: Path, command: str, jitconfig: str) -> tuple[Path, list[str]]:
    run, done, record = bash_dispatch(tmp_path, command, jitconfig)
    assert done.returncode == 0, done
    assert record.exists(), "systemd-run never ran"
    return run, json.loads(record.read_text())["argv"]


def properties(argv: list[str]) -> list[str]:
    props = []
    for i, word in enumerate(argv):
        if word in ("-p", "--property") and i + 1 < len(argv):
            props.append(argv[i + 1])
        elif word.startswith("--property="):
            props.append(word.split("=", 1)[1])
    return props


def unit_of(argv: list[str]) -> str | None:
    for i, word in enumerate(argv):
        if word in ("--unit", "-u") and i + 1 < len(argv):
            return argv[i + 1]
        if word.startswith("--unit="):
            return word.split("=", 1)[1]
    return None


def test_dispatch_writes_the_jit_secret_from_stdin_0600_and_starts_the_unit(
    tmp_path: Path, cfg: Any
) -> None:
    """R5.1, R5.2: the JIT config reaches the worker's tmpfs through stdin, never argv."""
    jitconfig = mint_jit_config()
    command = dispatch_argv("9016", REPO, COMMIT, cfg.vm)
    assert isinstance(command, str)
    assert jitconfig not in command
    run, argv = run_dispatch(tmp_path, command, jitconfig)
    target = run / "ci" / "9016" / "jitconfig"
    assert target.read_text(encoding="utf-8").strip() == jitconfig
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert unit_of(argv) in ("shidashi-ci-9016", "shidashi-ci-9016.service")


@pytest.mark.parametrize("memory_gib", [8, 12])
def test_dispatch_unit_has_a_low_cpu_weight_and_room_above_the_guests_memory(
    tmp_path: Path, memory_gib: int
) -> None:
    """R7.2: a fifth of a Shidashi job's CPU weight (100); MemoryMax covers QEMU above the
    guest's RAM (design gotcha: +1 GiB), or the OOM killer takes QEMU mid-job."""
    cfg = load_config(write_config(tmp_path / "cfg", vm={"memory_gib": memory_gib}))
    _, argv = run_dispatch(tmp_path, dispatch_argv("9017", REPO, COMMIT, cfg.vm), "x")
    props = properties(argv)
    assert "CPUWeight=20" in props, props
    memory = [p.split("=", 1)[1] for p in props if p.startswith("MemoryMax=")]
    assert memory == [f"{memory_gib + 1}G"], props


def test_dispatch_runs_ci_guest_run_from_the_shipped_tree_and_venv(
    tmp_path: Path, cfg: Any
) -> None:
    """The unit runs ``shidashi ci-guest run`` with every limit, from the tree and venv
    ``shidashi worker job`` ships (story 010)."""
    _, argv = run_dispatch(tmp_path, dispatch_argv("9018", REPO, COMMIT, cfg.vm), "x")
    joined = " ".join(argv)
    shipped = [a for a in worker.job_unit_argv("probe", COMMIT, ["status"]) if a.startswith("--")]
    for option in shipped:
        if option.startswith(("--working-directory=", "--setenv=PYTHONPATH=")):
            assert option in argv, (option, argv)
    assert f"{worker.WORKER_VENV}/bin/" in joined
    tail = argv[argv.index("ci-guest") :] if "ci-guest" in argv else []
    assert tail[:6] == ["ci-guest", "run", "--job", "9018", "--repo", REPO], argv
    for option, value in (
        ("--memory-gib", "8"),
        ("--disk-gib", "64"),
        ("--timeout-min", "360"),
        ("--min-free-gib", "20"),
    ):
        assert option in tail and tail[tail.index(option) + 1] == value, (option, tail)
    assert not [w for w in argv if w.startswith("--cpus")], "the worker derives the vCPUs"


def test_dispatch_keeps_a_repository_with_dots_and_dashes_whole(tmp_path: Path, cfg: Any) -> None:
    _, argv = run_dispatch(
        tmp_path, dispatch_argv("9019", "my.org_1/re_po-2.x", COMMIT, cfg.vm), "x"
    )
    assert argv[argv.index("--repo") + 1] == "my.org_1/re_po-2.x"


@pytest.mark.parametrize(
    ("job", "repo"),
    [
        pytest.param("9020;reboot", REPO, id="job-shell"),
        pytest.param("../9020", REPO, id="job-path"),
        pytest.param("9020", "obentoo/app;reboot", id="repo-shell"),
        pytest.param("9020", "obentoo/app $(id)", id="repo-subst"),
        pytest.param("9020", "obentoo", id="repo-no-name"),
    ],
)
def test_dispatch_refuses_a_job_or_repository_that_could_reach_the_shell(
    cfg: Any, job: str, repo: str
) -> None:
    """Hostile: these never become a remote command, quoted or not."""
    with pytest.raises((ValueError, CiDispatchError)):
        dispatch_argv(job, repo, COMMIT, cfg.vm)


def test_dispatch_a_failing_systemd_run_leaves_no_jit_secret_and_exits_non_zero(
    tmp_path: Path, cfg: Any
) -> None:
    """R5.3: the unit never started, so nothing would ever read -- or delete -- the JIT
    config on tmpfs; the same command removes it, and its failure reaches the pool."""
    jitconfig = mint_jit_config()
    run, done, record = bash_dispatch(
        tmp_path, dispatch_argv("9021", REPO, COMMIT, cfg.vm), jitconfig, systemd_run_rc=1
    )
    assert record.exists(), "systemd-run never ran"
    assert done.returncode != 0, done
    assert not (run / "ci" / "9021").exists(), "the JIT config was left on tmpfs"
    assert jitconfig not in done.stdout + done.stderr
