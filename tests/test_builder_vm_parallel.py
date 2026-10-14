"""INTEGRATION: the concurrent build proved as root in the builder VM (story 006, 7.2).

The privileged path -- real systemd-nspawn, real btrfs checkpoints, flock across
real assembles -- runs in the builder VM (lab/vm/builder.sh), whose guest is root;
this test drives it from the host as the user (kvm group) and reads the results
the guest writes to the shared out/ directory (jobs/, runs/, the ISOs).

Opt-in: it takes about an hour and needs the VM started and set up
(``lab/vm/builder.sh start && lab/vm/builder.sh setup``), so it runs only with
``SHIDASHI_BUILDER_VM=1``; otherwise it is SKIPPED -- never green by default.
Covers R1.1, R2.1, R3.3, R3.4, R3.5, R3.6, R3.7, R3.11, R3.12 (and Q13), over three
consecutive builds.

v3: ``--fresh`` restores nothing, not even the trunk (stories 016/019), so rounds 2 and 3
no longer pass it: before each, the three flavors' own checkpoints are released from the
guest's store (``checkpoint.Store.release``, as root in the guest), keeping the trunk.
The toolbox is extracted at most once across the three rounds, and its stamp names the
tarball the assembles used.

v4: ``SHIDASHI_BUILDER_VM=local`` runs the same proof on the machine the test runs on, as
root, with the worker's work-disk layout (``/mnt/work``) -- the bentoo-lab, the project's
lab: the "guest" commands run here, each job is a ``systemd-run`` unit and the jobs, runs
and ISOs land under ``SHIDASHI_PARALLEL_OUT`` (default ``/mnt/work/out-par``).
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import shidashi

_REPO = Path(shidashi.__file__).resolve().parents[1]
_BUILDER = _REPO / "lab" / "vm" / "builder.sh"
#: "1": the builder VM; "local": this machine, as root (the bentoo-lab)
_MODE = os.environ.get("SHIDASHI_BUILDER_VM")
_LOCAL = _MODE == "local"
if _LOCAL:
    _OUT = Path(os.environ.get("SHIDASHI_PARALLEL_OUT", "/mnt/work/out-par"))
    #: where the jobs' --output-dir points, as the builder sees it
    _OUT_SEEN = str(_OUT)
else:
    _OUT = Path(os.environ.get("SHIDASHI_VM_DIR", "/var/lib/shidashi/vm")) / "builder" / "out"
    _OUT_SEEN = "/mnt/out"

pytestmark = pytest.mark.skipif(
    not (_MODE == "1" and _OUT.is_dir())
    and not (_LOCAL and os.geteuid() == 0 and Path("/mnt/work/scratch").is_dir()),
    reason="needs SHIDASHI_BUILDER_VM=1 with the builder VM started and set up (root in the "
    "guest), or SHIDASHI_BUILDER_VM=local as root on a machine with /mnt/work (the bentoo-lab)",
)


#: Inside the guest (lab/vm/guest-setup.sh), or on the bentoo-lab: the store and the toolbox.
_STORE = "/mnt/work/scratch/assemble/checkpoints/v3-systemd"
_TOOLBOX = "/mnt/work/scratch/toolbox/v3-systemd"
_KEYS = ("v3-desktop-systemd", "v3-kde-systemd", "v3-gnome-systemd", "v3-wm-systemd")
_FLAVOR_KEYS = _KEYS[1:]

#: the ``toolbox`` steps of every round so far: (round, extracted, tarball)
_TOOLBOX_STEPS: list[tuple[int, bool, str]] = []


def _guest(command: str) -> str:
    """``command`` run as root in the builder guest, its stdout."""
    if _LOCAL:
        return subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, check=True, timeout=900
        ).stdout
    vm_dir = Path(os.environ.get("SHIDASHI_VM_DIR", "/var/lib/shidashi/vm")) / "builder"
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shidashi.cli import app; app()",
            "vm",
            "run",
            "--name",
            "builder",
            "--work-dir",
            str(vm_dir / "host"),
            "--timeout",
            "600",
            command,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=900,
    )
    return done.stdout


def _guest_python(code: str) -> str:
    python = sys.executable if _LOCAL else ".venv/bin/python"
    return _guest(f"cd {_REPO} && {python} -c {json.dumps(code)}")


def _trunk_images() -> set[str]:
    """The image list of the desktop trunk's checkpoint in the guest's store."""
    out = _guest_python(
        "import json; from pathlib import Path; from shidashi import checkpoint; "
        f"s = checkpoint.open_store(Path({_STORE!r})); "
        "print(json.dumps([list(m.images) for m in s.marks(checkpoint.INSTALL) "
        "if 'v3-desktop-systemd' in m.images]))"
    )
    marks = json.loads(out.strip().splitlines()[-1])
    assert len(marks) == 1, f"expected one desktop trunk checkpoint, got {marks}"
    return set(marks[0])


def _toolbox_state() -> str:
    """The toolbox's stamp and whether its tools are there."""
    return _guest(f"cat {_TOOLBOX}.stamp; test -x {_TOOLBOX}/usr/bin/mksquashfs && echo tools-ok")


def _job(name: str, *args: str) -> None:
    rc = _OUT / "jobs" / f"{name}.rc"
    rc.unlink(missing_ok=True)
    if not _LOCAL:
        subprocess.run([str(_BUILDER), "job", name, *args], check=True, timeout=900)
        return
    # as lab/vm/builder.sh's job, on this machine: a transient unit, the log and rc in out/jobs
    (_OUT / "jobs").mkdir(parents=True, exist_ok=True)
    (_OUT / "runs").mkdir(parents=True, exist_ok=True)
    subprocess.run(["systemctl", "reset-failed", f"shidashi-{name}"], check=False)
    subprocess.run(
        [
            "systemd-run",
            f"--unit=shidashi-{name}",
            "--collect",
            "--quiet",
            f"--working-directory={_REPO}",
            "--setenv=SHIDASHI_CACHE=/mnt/work/cache",
            "--setenv=SHIDASHI_SCRATCH=/mnt/work/scratch",
            f"--setenv=SHIDASHI_RUNS={_OUT}/runs",
            f"--setenv=PYTHONPATH={_REPO}",
            "/bin/bash",
            "-c",
            'job=$1; shift; "$@" > "$job.log" 2>&1; '
            'echo $? > "$job.rc.tmp" && mv -f "$job.rc.tmp" "$job.rc"',
            "bash",
            str(_OUT / "jobs" / name),
            sys.executable,
            "-c",
            "from shidashi.cli import app; app()",
            *args,
        ],
        check=True,
        timeout=60,
    )


def _wait(name: str, timeout: float) -> int:
    rc = _OUT / "jobs" / f"{name}.rc"
    deadline = time.monotonic() + timeout
    while not rc.is_file() or not rc.read_text().strip():
        assert time.monotonic() < deadline, f"job {name} still running after {timeout}s"
        time.sleep(10)
    return int(rc.read_text().strip())


def _log(name: str) -> str:
    return (_OUT / "jobs" / f"{name}.log").read_text(errors="replace")


def _manifests(since: float) -> list[dict[str, Any]]:
    found = []
    for path in (_OUT / "runs").glob("*/manifest.json"):
        if path.stat().st_mtime >= since:
            found.append(json.loads(path.read_text()))
    return found


@pytest.mark.parametrize("round_", [1, 2, 3])
def test_a_concurrent_build_of_three_flavors_from_one_trunk(round_: int) -> None:
    """Three consecutive concurrent builds (the corruption success metric), each into a
    FRESH output directory, so a lost SHA256SUMS line cannot hide behind an old one.

    Starting state, spelled out: round 1 begins with NO desktop trunk checkpoint and no
    flavor checkpoint, so one flavor installs the trunk while the other two wait for it
    (R3.3's one-installer/two-waiters path really runs); rounds 2 and 3 begin
    after releasing the three flavors' own checkpoints, the trunk kept -- never ``--fresh``,
    which would skip the trunk too -- so every flavor restores the trunk and branches;
    none trivially resumes from its own."""
    released = _KEYS if round_ == 1 else _FLAVOR_KEYS
    _guest_python(
        "from pathlib import Path; from shidashi import checkpoint; "
        f"s = checkpoint.open_store(Path({_STORE!r})); "
        f"[s.release(k) for k in {released!r}]"
    )
    toolbox_before = _toolbox_state()
    started = time.time()
    out = f"par-{int(started)}-{round_}"
    _job(
        f"par-build-{round_}",
        "build",
        "v3",
        "systemd",
        "--images",
        "kde,gnome,wm",
        "--skip-factory",
        "--output-dir",
        f"{_OUT_SEEN}/{out}",
    )
    assert _wait(f"par-build-{round_}", 4 * 3600) == 0, _log(f"par-build-{round_}")[-4000:]
    par = _OUT / out
    isos = {}
    for flavor in ("kde", "gnome", "wm"):
        (iso,) = par.glob(f"bentoo-*-{flavor}-systemd-v3.iso")
        isos[flavor] = iso.name
    sums = [line for line in (par / "SHA256SUMS").read_text().splitlines() if line.endswith(".iso")]
    # exactly one line per ISO, each naming its own ISO (R3.7)
    assert len(sums) == 3, sums
    assert sorted(line.split()[-1] for line in sums) == sorted(isos.values())
    assemblies = [m for m in _manifests(started) if m["command"] == "assemble"]
    assert {m["inputs"]["recipe"]["flavor"] for m in assemblies} >= {"kde", "gnome", "wm"}
    trunk_steps = [s for m in assemblies for s in m["steps"] if s["step"] == "trunk"]
    built = [s for s in trunk_steps if "built" in s]
    restored = [s for s in trunk_steps if "restored" in s]
    if round_ == 1:
        assert len(built) == 1 and len(restored) == 2, trunk_steps  # one installer, two waiters
    else:
        assert not built and len(restored) == 3, trunk_steps  # no flavor resumed past the trunk
    # the trunk's manifest lists every flavor that grew from it
    assert _trunk_images() >= set(_KEYS)
    # the toolbox was not re-extracted nor damaged by three concurrent users
    assert _toolbox_state() == toolbox_before and "tools-ok" in toolbox_before
    for m in assemblies:
        (step,) = [s for s in m["steps"] if s["step"] == "toolbox"]
        _TOOLBOX_STEPS.append((round_, bool(step["extracted"]), str(step["tarball"])))
    # extracted at most once across the rounds so far, and the stamp names the tarball used
    assert sum(extracted for _r, extracted, _t in _TOOLBOX_STEPS) <= 1, _TOOLBOX_STEPS
    tarballs = {t for _r, _e, t in _TOOLBOX_STEPS}
    assert len(tarballs) == 1, tarballs
    assert toolbox_before.split()[0] == next(iter(tarballs)), (toolbox_before, tarballs)


def test_two_hand_started_assembles_of_different_images_both_succeed() -> None:
    for name, image in (("par-min", "minimal"), ("par-wrk", "worker")):
        _job(name, "assemble", "v3", image, "systemd", "--output-dir", f"{_OUT_SEEN}/par-hand")
    assert _wait("par-min", 2 * 3600) == 0, _log("par-min")[-4000:]
    assert _wait("par-wrk", 2 * 3600) == 0, _log("par-wrk")[-4000:]
    assert "currently busy" not in _log("par-min") + _log("par-wrk")  # nspawn's toolbox lock


def test_a_second_assemble_of_the_same_image_is_refused_naming_the_first() -> None:
    started = time.time()
    _job(
        "par-first", "assemble", "v3", "minimal", "systemd", "--output-dir", f"{_OUT_SEEN}/par-same"
    )
    first_pid = None
    deadline = time.monotonic() + 300
    while first_pid is None and time.monotonic() < deadline:
        for events in (_OUT / "runs").glob("*/events.jsonl"):
            if events.stat().st_mtime < started:
                continue
            start = json.loads(events.read_text().splitlines()[0])
            if start.get("command") == "assemble":
                first_pid = start.get("pid")
        time.sleep(2)
    assert first_pid is not None, "the first assemble never opened its run"
    time.sleep(15)  # past the root and toolbox checks, into the image lock
    _job(
        "par-second",
        "assemble",
        "v3",
        "minimal",
        "systemd",
        "--output-dir",
        f"{_OUT_SEEN}/par-same",
    )
    assert _wait("par-second", 600) == 1
    said = _log("par-second")
    assert re.search(rf"being assembled by pid {first_pid}\b", said), said[-2000:]
    assert _wait("par-first", 2 * 3600) == 0, _log("par-first")[-4000:]
