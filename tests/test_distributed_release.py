"""Integration proof (story 014, 5.2): a real release split between the host and bentoo-lab.

Rung: two real machines over story 009's pinned SSH, story 010's remote jobs and sync
(run as child processes of ``build``, as the invoking user), story 006's concurrent
assemble on the host, story 024's boot test in rootful podman on the worker. Not
covered: any other worker, an arch other than v3, syft on the worker (its ISOs ship
without an SBOM until a follow-up story; the report must say so, R4.9).

Opt-in and long (hours): it runs only when ``shidashi.schedule`` exists,
``SHIDASHI_DISTRIBUTED_WORKER`` names a worker paired by the invoking user, the suite
runs as root under ``sudo`` (the agent has no root on the host: the AUTHOR runs it), and
that worker answers ready (work disk mounted, no job running). Otherwise it is SKIPPED
with the missing piece named -- a skip is not a pass. Optional:
``SHIDASHI_HOST_ONLY_SECONDS`` (the wall clock of the same four ISOs built with
``--local``) turns on the 30%-faster check of the success metric.

The builds it starts see the REAL environment: tests/conftest.py points every test's
``XDG_DATA_HOME`` and ``SHIDASHI_RUNS`` at temporary directories, which would hide the
invoking user's worker registry (no worker, no arch ownership: a host factory) and the
runs the worker's job pulls; :func:`_real_environment` puts back what the suite started with.

    sudo SHIDASHI_DISTRIBUTED_WORKER=bentoo-lab \
        uv run pytest -q tests/test_distributed_release.py -s
"""

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shidashi import config

IMAGES = ("minimal", "worker", "kde", "gnome")
WORKER = os.environ.get("SHIDASHI_DISTRIBUTED_WORKER", "")
SHIDASHI = [sys.executable, "-P", "-c", "from shidashi.cli import app; app()"]
COMPANIONS = (".DIGESTS", ".contents.gz", ".packages")
#: What tests/conftest.py redirects per test, as the suite started (read at import time,
#: before any fixture runs): ``None`` when unset.
_REAL_ENV = {name: os.environ.get(name) for name in ("XDG_DATA_HOME", "SHIDASHI_RUNS")}


@pytest.fixture(autouse=True)
def _real_environment(
    _runs_in_tmp: None, _registry_in_tmp: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undo conftest's redirections (it runs first: this fixture depends on them)."""
    for name, value in _REAL_ENV.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _missing() -> str | None:
    if not WORKER:
        return "SHIDASHI_DISTRIBUTED_WORKER is not set"
    try:
        importlib.import_module("shidashi.schedule")
    except ModuleNotFoundError:
        return "shidashi.schedule does not exist yet (story 014)"
    if os.geteuid() != 0 or not os.environ.get("SUDO_USER"):
        return "run as root under sudo by the user who paired the worker (sudo -E)"
    from shidashi import schedule

    nodes = {
        n.name: n
        for n in schedule.discover(local=False, workers_dir=schedule.invoking_user_workers_dir())
    }
    if WORKER not in nodes:
        return f"{WORKER} is not paired by {os.environ['SUDO_USER']} (story 009)"
    if not nodes[WORKER].reachable:
        return f"{WORKER} cannot take the job: {nodes[WORKER].note}"
    return None


@pytest.fixture(scope="module")
def ready() -> None:
    reason = _missing()
    if reason is not None:
        pytest.skip(reason)


def _shidashi(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*SHIDASHI, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _lines_naming(text: str, word: str) -> list[str]:
    return [line for line in text.splitlines() if word in line]


def test_distributed_plan_gives_the_worker_a_share(ready: None) -> None:
    done = _shidashi("build", "v3", "systemd", "--images", ",".join(IMAGES), "--plan", timeout=120)
    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    for image in IMAGES:
        assert _lines_naming(output, image), image
    assert _lines_naming(output, WORKER), "the plan gives the worker nothing"


def test_distributed_release_builds_every_iso_with_a_share_on_the_worker(
    ready: None, tmp_path: Path
) -> None:
    out = tmp_path / "iso"
    out.mkdir()
    started_at = time.time()
    started = time.monotonic()
    done = _shidashi(
        "build",
        "v3",
        "systemd",
        "--images",
        ",".join(IMAGES),
        "--boot-test",
        "-o",
        str(out),
        timeout=8 * 3600,
    )
    wall = time.monotonic() - started
    output = done.stdout + done.stderr
    assert done.returncode == 0, output[-4000:]

    # R4.2: every ISO is on the host, the worker's included, with its companions
    isos = sorted(out.glob("*.iso"))
    for image in IMAGES:
        assert any(f"-{image}-" in iso.name for iso in isos), (image, [i.name for i in isos])
    for iso in isos:
        for suffix in COMPANIONS:
            assert (out / f"{iso.name}{suffix}").is_file(), f"{iso.name}{suffix}"
    assert len(list(out.glob("latest-*.txt"))) >= len(IMAGES)
    assert len(list(out.glob("*.build.tar.zst"))) >= 2  # the host's build and the worker's

    # R4.2: exactly one SHA256SUMS line per ISO and per companion (one writer)
    sums = [line.split("  ", 1)[1] for line in (out / "SHA256SUMS").read_text().splitlines()]
    for iso in isos:
        assert sums.count(iso.name) == 1, (iso.name, sums)
        for suffix in (".DIGESTS", ".packages"):
            assert sums.count(f"{iso.name}{suffix}") == 1, (iso.name, suffix)

    # R6.3: everything in the output dir is owned like the output dir itself
    want = (out.stat().st_uid, out.stat().st_gid)
    for path in out.iterdir():
        assert (path.stat().st_uid, path.stat().st_gid) == want, path.name

    # R4.4: the report names the node of each image, and the worker built at least one
    worker_lines = _lines_naming(output, WORKER)
    assert worker_lines, "no image was built on the worker"
    # R4.9: a worker ISO without .spdx.json is said to have no SBOM
    for iso in isos:
        if not (out / f"{iso.name}.spdx.json").exists():
            assert any("SBOM" in line for line in worker_lines), iso.name

    # R4.2, C3: the worker's own assemble runs came back to the host's runs directory,
    # recording the worker's registry name as their hostname
    pulled = []
    for manifest_path in config.runs_dir().glob("*/manifest.json"):
        if manifest_path.stat().st_mtime < started_at:
            continue
        manifest = json.loads(manifest_path.read_text())
        host = manifest.get("hostname") or (manifest.get("inputs") or {}).get("hostname")
        if manifest.get("command") == "assemble" and host == WORKER:
            pulled.append(manifest_path.parent.name)
    assert pulled, f"no audit run of {WORKER} on the host"

    # success metric: at least 30% faster than the host alone (when the baseline is given)
    baseline = os.environ.get("SHIDASHI_HOST_ONLY_SECONDS")
    print(
        f"distributed release wall clock: {wall:.0f} s "
        f"(host-only baseline: {baseline or 'not given'})"
    )
    if baseline:
        assert wall <= 0.7 * float(baseline), (wall, baseline)
