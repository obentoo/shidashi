"""Unit tests of shidashi.schedule.load_history -- each node's costs from its audit runs.

Contract (design.md v3, story 014):
``load_history(runs_dir, *, node) -> History`` with ``History.duration(image_key)``,
``History.trunk_duration(trunk_key)``, ``History.packages(image_key)``,
``History.sec_per_pkg()`` and ``History.run_ids()``. Durations are whole-node: the
``trunk`` and ``boot-test*`` steps are not the image's cost (the trunk step is the
trunk's, keyed by its ``built`` field), and a run that used ``--jobs J`` on a node of
``threads`` T counts ``J / T`` of its measured time (R2.6, R2.7). Only the runs that
``node`` recorded (the run's hostname) count; only ``assemble`` runs whose every step is
ok; the latest such run per image (``<arch>-<image>-<init>``, from the argv) gives its
duration; the package count is the length of the run's ``packages.json``. An
unreadable or partial run is skipped with one warning naming its id -- never an
exception.

v3 -- restored runs (R2.1, R2.2): the assembler marks a step that restored a checkpoint
with a ``restored`` field. A run where a step OTHER than ``trunk`` carries ``restored``
resumed from its own install or packages checkpoint: minutes, not the image's cost -- it
gives no image duration and an older full run wins. A run where only the ``trunk`` step
carries ``restored`` installed the flavor over a restored trunk: it gives the image's
duration, but ``install`` then covers only the flavor's added packages while
``packages.json`` counts them all, so seconds per package is measured only on runs where
NO step carries ``restored``. Other fields that merely mention a restore
(``trunk_restored_from`` on the ``seed`` step of a run over a restored trunk) are not a
``restored`` field.

The fixture runs are built with ``audit.build_manifest`` from the events a run
writes, so they have the real shape; each carries its hostname both as the
``hostname`` input and as ``manifest["hostname"]``.
"""

import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, schedule

HOST = "dojo"
LAB = "bentoo-lab"


def _write_run(
    runs: Path,
    run_id: str,
    *,
    hostname: str | None,
    argv: list[str],
    duration: float,
    packages: int,
    command: str = "assemble",
    status: str = "ok",
    step_status: str = "ok",
    steps: list[tuple[str, float, dict[str, Any]]] | None = None,
    threads: int | None = None,
) -> Path:
    """One run directory: events.jsonl, manifest.json and packages.json.

    ``steps`` replaces the single ``install`` step of ``duration`` seconds.
    """
    day = run_id[:8]
    started = f"{day[:4]}-{day[4:6]}-{day[6:8]}T10:00:00.000+00:00"
    ended = f"{day[:4]}-{day[4:6]}-{day[6:8]}T11:00:00.000+00:00"
    base = {"run": run_id, "step": ""}
    events: list[dict[str, Any]] = [
        {
            **base,
            "ts": started,
            "t": 0,
            "kind": "run.start",
            "command": command,
            "argv": argv,
            "pid": 1,
        },
    ]
    if hostname is not None:
        events.append(
            {**base, "ts": started, "t": 0, "kind": "input", "name": "hostname", "value": hostname}
        )
    if threads is not None:
        events.append(
            {**base, "ts": started, "t": 0, "kind": "input", "name": "threads", "value": threads}
        )
    for name, seconds, extra in steps or [("install", duration, {})]:
        events += [
            {**base, "ts": started, "t": 0, "kind": "step.start", "step": name},
            {
                **base,
                "ts": ended,
                "t": seconds,
                "kind": "step.end",
                "step": name,
                "status": step_status,
                "duration_s": seconds,
                **extra,
            },
        ]
    events += [
        {
            **base,
            "ts": ended,
            "t": duration,
            "kind": "attachment",
            "name": "packages",
            "path": "packages.json",
        },
        {
            **base,
            "ts": ended,
            "t": duration,
            "kind": "run.end",
            "status": status,
            "duration_s": duration,
        },
    ]
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    manifest = audit.build_manifest(events)
    if hostname is not None:
        manifest["hostname"] = hostname
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (run_dir / "packages.json").write_text(
        json.dumps([{"atom": f"app-misc/p{n}-1.0"} for n in range(packages)])
    )
    return run_dir


def _assemble(arch: str, image: str, init: str, *extra: str) -> list[str]:
    return ["/usr/bin/shidashi", "assemble", arch, image, init, *extra]


def _warnings(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str], recwarn: Any
) -> str:
    out, err = capsys.readouterr()
    return "\n".join([caplog.text, out, err, *(str(w.message) for w in recwarn)])


# --- whose runs (hostile collapse first) -------------------------------------------------


def test_history_another_machines_run_is_not_this_nodes_history(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20261003T100000Z-aaaaaa",
        hostname=LAB,
        argv=_assemble("v3", "kde", "systemd"),
        duration=900.0,
        packages=1000,
    )
    assert schedule.load_history(tmp_path, node=HOST).duration("v3-kde-systemd") is None
    assert schedule.load_history(tmp_path, node=LAB).duration("v3-kde-systemd") == pytest.approx(
        900.0
    )


def test_history_another_init_or_arch_is_another_image(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20261003T100000Z-aaaaa1",
        hostname=HOST,
        argv=_assemble("v3", "kde", "openrc"),
        duration=400.0,
        packages=1000,
    )
    _write_run(
        tmp_path,
        "20261003T100000Z-aaaaa2",
        hostname=HOST,
        argv=_assemble("znver5", "kde", "systemd"),
        duration=350.0,
        packages=1000,
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") is None
    assert history.duration("v3-kde-openrc") == pytest.approx(400.0)
    assert history.duration("znver5-kde-systemd") == pytest.approx(350.0)


def test_history_a_build_run_is_not_an_assemble_of_its_images(tmp_path: Path) -> None:
    # a build covers the factory and several ISOs: its duration is no image's cost
    _write_run(
        tmp_path,
        "20261003T100000Z-bbbbbb",
        hostname=HOST,
        command="build",
        argv=["/usr/bin/shidashi", "build", "v3", "systemd", "--images", "kde"],
        duration=5000.0,
        packages=1000,
    )
    assert schedule.load_history(tmp_path, node=HOST).duration("v3-kde-systemd") is None


# --- the same image however it was launched (hostile split) ------------------------------


def test_history_the_same_image_launched_as_a_child_or_a_worker_job_is_one_image(
    tmp_path: Path,
) -> None:
    # by hand through the script; then as story 006's child / story 010's job:
    # `python -c '...' assemble v3 kde systemd --jobs 16 --output-dir ... --boot-test`
    _write_run(
        tmp_path,
        "20261001T100000Z-cccccc",
        hostname=HOST,
        argv=_assemble("v3", "kde", "systemd"),
        duration=500.0,
        packages=1000,
    )
    _write_run(
        tmp_path,
        "20261003T100000Z-dddddd",
        hostname=HOST,
        argv=[
            "-c",
            "assemble",
            "v3",
            "kde",
            "systemd",
            "--jobs",
            "16",
            "--output-dir",
            "/mnt/work/out/iso/kde-v3",
            "--boot-test",
            "--cid",
            "43",
        ],
        duration=300.0,
        packages=1000,
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") == pytest.approx(300.0)  # the latest of the two


# --- which run counts (R2.1) -------------------------------------------------------------


def test_history_takes_the_latest_successful_run(tmp_path: Path) -> None:
    kde = _assemble("v3", "kde", "systemd")
    _write_run(
        tmp_path, "20261001T100000Z-e00001", hostname=HOST, argv=kde, duration=500.0, packages=1000
    )
    _write_run(
        tmp_path, "20261003T100000Z-e00002", hostname=HOST, argv=kde, duration=300.0, packages=1000
    )
    # newer, but failed
    _write_run(
        tmp_path,
        "20261004T100000Z-e00003",
        hostname=HOST,
        argv=kde,
        duration=50.0,
        packages=1000,
        status="error",
        step_status="error",
    )
    # newer, closed "ok" but with a step that never finished
    _write_run(
        tmp_path,
        "20261005T100000Z-e00004",
        hostname=HOST,
        argv=kde,
        duration=40.0,
        packages=1000,
        step_status="unfinished",
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") == pytest.approx(300.0)
    used = history.run_ids()
    assert "20261003T100000Z-e00002" in used
    assert "20261004T100000Z-e00003" not in used and "20261005T100000Z-e00004" not in used


def test_history_run_ids_are_this_nodes_runs_only(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20261003T100000Z-f00001",
        hostname=HOST,
        argv=_assemble("v3", "kde", "systemd"),
        duration=300.0,
        packages=1000,
    )
    _write_run(
        tmp_path,
        "20261003T100000Z-f00002",
        hostname=LAB,
        argv=_assemble("v3", "kde", "systemd"),
        duration=600.0,
        packages=1000,
    )
    used = schedule.load_history(tmp_path, node=HOST).run_ids()
    assert "20261003T100000Z-f00001" in used and "20261003T100000Z-f00002" not in used


# --- seconds per package (R2.2) ----------------------------------------------------------


def test_history_seconds_per_package_is_the_nodes_own(tmp_path: Path) -> None:
    # the host installs at 0.25 s/pkg, the worker at 0.5 s/pkg
    for n, (image, secs, pkgs) in enumerate([("kde", 300.0, 1200), ("minimal", 250.0, 1000)]):
        _write_run(
            tmp_path,
            f"20261003T10000{n}Z-a1b2c{n}",
            hostname=HOST,
            argv=_assemble("v3", image, "systemd"),
            duration=secs,
            packages=pkgs,
        )
    for n, (image, secs, pkgs) in enumerate([("kde", 600.0, 1200), ("gnome", 700.0, 1400)]):
        _write_run(
            tmp_path,
            f"20261003T11000{n}Z-c3d4e{n}",
            hostname=LAB,
            argv=_assemble("v3", image, "systemd"),
            duration=secs,
            packages=pkgs,
        )
    assert schedule.load_history(tmp_path, node=HOST).sec_per_pkg() == pytest.approx(0.25)
    assert schedule.load_history(tmp_path, node=LAB).sec_per_pkg() == pytest.approx(0.5)


# --- missing, unreadable and partial history (Q4) ----------------------------------------


def test_history_of_a_node_with_no_runs_is_empty(tmp_path: Path) -> None:
    for runs in (tmp_path / "missing", tmp_path):
        history = schedule.load_history(runs, node=HOST)
        assert history.duration("v3-kde-systemd") is None
        assert history.sec_per_pkg() is None
        assert history.run_ids() == []


def test_history_skips_an_unreadable_run_with_a_warning_naming_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    recwarn: Any,
) -> None:
    _write_run(
        tmp_path,
        "20261003T100000Z-0a0a0a",
        hostname=HOST,
        argv=_assemble("v3", "kde", "systemd"),
        duration=300.0,
        packages=1000,
    )
    broken = tmp_path / "20261004T100000Z-badbad"
    broken.mkdir()
    (broken / "manifest.json").write_text('{"run": "20261004T100000Z-badbad", "command": "assem')
    with caplog.at_level("WARNING"):
        history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") == pytest.approx(300.0)
    assert "20261004T100000Z-badbad" in _warnings(caplog, capsys, recwarn)


def test_history_skips_a_partial_run_without_failing(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20261003T100000Z-0b0b0b",
        hostname=HOST,
        argv=_assemble("v3", "kde", "systemd"),
        duration=300.0,
        packages=1000,
    )
    killed = tmp_path / "20261004T100000Z-killed"
    killed.mkdir()  # a run killed mid-step: events, no manifest
    (killed / "events.jsonl").write_text(
        json.dumps(
            {
                "run": killed.name,
                "kind": "run.start",
                "command": "assemble",
                "argv": _assemble("v3", "kde", "systemd"),
                "step": "",
                "t": 0,
            }
        )
        + "\n"
        + '{"run": "20261004T100000Z-killed", "kind": "step.st'
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") == pytest.approx(300.0)
    assert "20261004T100000Z-killed" not in history.run_ids()


def test_history_reads_a_run_recorded_by_the_real_audit_trail(tmp_path: Path) -> None:
    # the seam with 2.1: what audit.run writes is what load_history reads
    runs = tmp_path / "runs"
    with audit.run(
        runs, command="assemble", argv=["shidashi", "assemble", "v3", "kde", "systemd"]
    ) as rec:
        with rec.step("install"):
            time.sleep(0.02)
        rec.attach("packages", [{"atom": f"app-misc/p{n}-1.0"} for n in range(10)])
        run_id = rec.run_id
    history = schedule.load_history(runs, node=socket.gethostname())
    seconds = history.duration("v3-kde-systemd")
    assert seconds is not None and seconds > 0
    assert run_id in history.run_ids()


# --- whole-node durations: the trunk, the boot test, the thread share (R2.6, R2.7) -------


def test_history_the_trunk_step_is_the_trunks_cost_not_the_images(tmp_path: Path) -> None:
    # the first flavor of a build installs the trunk inside its own run
    _write_run(
        tmp_path,
        "20261005T100000Z-tr0001",
        hostname=HOST,
        argv=_assemble("v3", "kde", "systemd"),
        duration=0.0,
        packages=1061,
        steps=[
            ("seed", 10.0, {}),
            ("trunk", 1163.0, {"built": "v3-desktop-systemd"}),
            ("install", 630.0, {}),
            ("squashfs:zstd", 120.0, {}),
        ],
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") == pytest.approx(760.0)
    assert history.trunk_duration("v3-desktop-systemd") == pytest.approx(1163.0)


def test_history_a_run_over_a_restored_trunk_gives_the_images_duration_only(
    tmp_path: Path,
) -> None:
    # the real shape: the seed step names the trunk it restored (trunk, trunk_restored_from)
    # and the trunk step carries restored=<trunk key>; install covers the flavor's packages
    _write_run(
        tmp_path,
        "20261005T110000Z-tr0002",
        hostname=HOST,
        argv=_assemble("v3", "gnome", "systemd"),
        duration=0.0,
        packages=766,
        steps=[
            ("seed", 4.0, {"trunk": "v3-desktop-systemd", "trunk_restored_from": "r-old"}),
            ("trunk", 0.5, {"restored": "v3-desktop-systemd", "packages": 600}),
            ("install", 500.0, {}),
        ],
    )
    history = schedule.load_history(tmp_path, node=HOST)
    # hostile collapse: trunk_restored_from is not a restore of the image's own checkpoint
    assert history.duration("v3-gnome-systemd") == pytest.approx(504.0)
    assert history.trunk_duration("v3-desktop-systemd") is None
    # 500 s for 766 counted packages is not a rate: only the flavor's ~166 were installed
    assert history.sec_per_pkg() is None


def test_history_a_run_that_restored_its_own_install_gives_no_duration(tmp_path: Path) -> None:
    kde = _assemble("v3", "kde", "systemd")
    _write_run(
        tmp_path, "20261003T100000Z-full01", hostname=HOST, argv=kde, duration=700.0, packages=1061
    )
    # newer, every step ok, but it resumed from kde's own install checkpoint: minutes
    _write_run(
        tmp_path,
        "20261005T100000Z-rest01",
        hostname=HOST,
        argv=kde,
        duration=0.0,
        packages=1061,
        steps=[
            (
                "seed",
                3.0,
                {
                    "restored": "install",
                    "restored_from": "20261003T100000Z-full01",
                    "shared_with": [],
                },
            ),
            ("install", 0.2, {"restored": "install", "packages": 1061}),
            ("squashfs:zstd", 90.0, {}),
        ],
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") == pytest.approx(700.0)  # the older full run
    assert "20261003T100000Z-full01" in history.run_ids()  # the duration's run is recorded


def test_history_a_run_that_restored_its_packages_checkpoint_gives_no_duration(
    tmp_path: Path,
) -> None:
    minimal = _assemble("v3", "minimal", "systemd")
    _write_run(
        tmp_path,
        "20261003T100000Z-full02",
        hostname=HOST,
        argv=minimal,
        duration=400.0,
        packages=475,
    )
    _write_run(
        tmp_path,
        "20261005T100000Z-rest02",
        hostname=HOST,
        argv=minimal,
        duration=0.0,
        packages=475,
        steps=[
            ("seed", 3.0, {"restored": "packages", "restored_from": "r-x", "shared_with": []}),
            ("install", 0.1, {"restored": "packages", "packages": 475}),
            ("settle", 0.1, {"restored": "packages"}),
            ("squashfs:zstd", 60.0, {}),
        ],
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-minimal-systemd") == pytest.approx(400.0)


def test_history_a_restored_own_run_alone_leaves_the_image_without_history(
    tmp_path: Path,
) -> None:
    _write_run(
        tmp_path,
        "20261005T100000Z-rest03",
        hostname=HOST,
        argv=_assemble("v3", "kde", "systemd"),
        duration=0.0,
        packages=1061,
        steps=[("seed", 3.0, {"restored": "install"}), ("install", 0.2, {"restored": "install"})],
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") is None
    assert history.sec_per_pkg() is None


def test_history_seconds_per_package_comes_only_from_runs_that_restored_nothing(
    tmp_path: Path,
) -> None:
    # one full run at 0.5 s/pkg; a run over a restored trunk and a run over its own
    # checkpoint would each drag the median far below it
    _write_run(
        tmp_path,
        "20261003T100000Z-full03",
        hostname=HOST,
        argv=_assemble("v3", "minimal", "systemd"),
        duration=500.0,
        packages=1000,
    )
    _write_run(
        tmp_path,
        "20261004T100000Z-ovtr03",
        hostname=HOST,
        argv=_assemble("v3", "gnome", "systemd"),
        duration=0.0,
        packages=1000,
        steps=[("trunk", 0.5, {"restored": "v3-desktop-systemd"}), ("install", 50.0, {})],
    )
    _write_run(
        tmp_path,
        "20261004T110000Z-ownr03",
        hostname=HOST,
        argv=_assemble("v3", "kde", "systemd"),
        duration=0.0,
        packages=1000,
        steps=[("seed", 1.0, {"restored": "install"}), ("install", 0.2, {"restored": "install"})],
    )
    assert schedule.load_history(tmp_path, node=HOST).sec_per_pkg() == pytest.approx(0.5)


def test_history_boot_test_steps_are_not_the_images_cost(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20261005T120000Z-bt0001",
        hostname=HOST,
        argv=_assemble("v3", "minimal", "systemd", "--boot-test", "--cid", "42"),
        duration=0.0,
        packages=475,
        steps=[("install", 300.0, {}), ("boot-test", 200.0, {}), ("boot-test:uefi", 90.0, {})],
    )
    assert schedule.load_history(tmp_path, node=HOST).duration(
        "v3-minimal-systemd"
    ) == pytest.approx(300.0)


def test_history_a_run_on_a_share_of_the_threads_counts_its_whole_node_time(
    tmp_path: Path,
) -> None:
    # a story 006 child with --jobs 8 on a 32-thread host took 600 s: 150 s of the whole node
    _write_run(
        tmp_path,
        "20261005T130000Z-sh0001",
        hostname=HOST,
        argv=["-c", "assemble", "v3", "kde", "systemd", "--jobs", "8"],
        duration=600.0,
        packages=1200,
        threads=32,
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.duration("v3-kde-systemd") == pytest.approx(150.0)
    assert history.sec_per_pkg() == pytest.approx(150.0 / 1200)


def test_history_without_jobs_or_threads_the_share_is_the_whole_node(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20261005T140000Z-sh0002",
        hostname=HOST,
        argv=["-c", "assemble", "v3", "kde", "systemd", "--jobs", "8"],  # threads unknown
        duration=600.0,
        packages=1200,
    )
    assert schedule.load_history(tmp_path, node=HOST).duration("v3-kde-systemd") == pytest.approx(
        600.0
    )


def test_history_packages_is_the_latest_runs_package_count(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "20261005T150000Z-pk0001",
        hostname=HOST,
        argv=_assemble("v3", "kde", "systemd"),
        duration=300.0,
        packages=1061,
    )
    history = schedule.load_history(tmp_path, node=HOST)
    assert history.packages("v3-kde-systemd") == 1061
    assert history.packages("v3-gnome-systemd") is None
