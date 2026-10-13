"""Run the children and wait for all (story 006, task 6.3).

Contract (design.md v2, cross-story contract C1): ``run_children(argvs, *, logs) ->
list[ChildResult]`` starts every child at once, its stdout+stderr in ``logs/<image>.log``
(``logs`` is the build run's own directory), waits for all, never kills a sibling on a
failure. Each child's result path is read from THAT argv's ``--result-file`` (the last
one); the parent reads the run id and ISOs from it, never the log. ``duration_s`` is the
parent's measured wall clock, whatever the result file says. ``exit_code`` is ALWAYS the
process's return code (127 for a child that never started). The child writes
``RESULT_FIELDS`` (image, exit_code, run_id, isos, duration_s); only ``REQUIRED_FIELDS``
(run_id, isos) are required on read. A child without a usable result file is a failure
through ``error`` -- "no result file", "unreadable result file" or "result file lacks
<field>" -- whatever its exit code. Each child runs in its own process group; on
KeyboardInterrupt the parent sends SIGTERM to every child's GROUP (then SIGKILL after
the grace period), so no grandchild (nspawn, emerge) keeps writing, records each child
with ``error="interrupted"``, and raises ``parallel.Interrupted`` (a KeyboardInterrupt
carrying the results).

The children are small Python scripts that act like ``assemble``: they write their
result to the LAST ``--result-file`` of their command line.
"""

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit
from shidashi.parallel import ChildResult, run_children, summarize

_CHILD = r"""
import json, sys, time
from pathlib import Path
args = sys.argv[1:]
image, behavior, rendezvous = args[0], args[1], Path(args[2])
result = None
if "--result-file" in args:  # the LAST one, as the CLI would take it
    result = Path(args[len(args) - args[::-1].index("--result-file")])
print(f"out of {image}", flush=True)
print(f"err of {image}", file=sys.stderr, flush=True)
(rendezvous / f"started-{image}").touch()
if behavior == "together":  # proves the children run at the same time
    deadline = time.monotonic() + 15
    while len(list(rendezvous.glob("started-*"))) < int(args[3]):
        if time.monotonic() > deadline:
            print("ran alone: the others never started", file=sys.stderr)
            sys.exit(3)
        time.sleep(0.02)
if behavior == "outlive":  # still running after a sibling failed
    deadline = time.monotonic() + 15
    while not (rendezvous / "failed-done").exists():
        if time.monotonic() > deadline:
            sys.exit(4)
        time.sleep(0.02)
    time.sleep(0.3)
code = 1 if behavior == "fail" else 0
payload = {"image": image, "exit_code": code, "run_id": f"child-{image}",
           "isos": [f"/out/bentoo-{image}.iso"] if code == 0 else [], "duration_s": 999.0}
if behavior == "lacks-isos":
    del payload["isos"]
if behavior == "lacks-optional":  # only run_id and isos are required on read
    del payload["duration_s"], payload["image"]
if behavior == "hang":
    time.sleep(120)
if behavior == "group":  # a grandchild, like nspawn under assemble
    import os, subprocess
    (rendezvous / f"pgid-{image}").write_text(str(os.getpgid(0)))
    grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (rendezvous / f"grandchild-{image}").write_text(str(grand.pid))
    time.sleep(120)
if behavior == "garbage" and result is not None:
    result.write_text("{not json")
elif behavior != "noresult" and result is not None:
    result.write_text(json.dumps(payload))
if behavior == "fail":
    (rendezvous / "rootfs-kept").mkdir()
    (rendezvous / "failed-done").touch()
sys.exit(code)
"""


@pytest.fixture
def child(tmp_path: Path) -> Path:
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    (tmp_path / "meet").mkdir()
    return script


def _run(tmp_path: Path, child: Path, behaviors: dict[str, str]) -> list[ChildResult]:
    """run_children over fake children, inside a build's audit run, with a deadline."""
    box: dict[str, Any] = {}
    with audit.run(tmp_path / "runs", command="build", argv=[]) as trail:
        logs = tmp_path / "scratch" / "logs" / f"build-{trail.run_id}"
        argvs = {
            image: [
                sys.executable,
                str(child),
                image,
                behavior,
                str(tmp_path / "meet"),
                str(len(behaviors)),
                "--result-file",
                str(logs / f"{image}.json"),
            ]
            for image, behavior in behaviors.items()
        }

        def call() -> None:
            box["results"] = run_children(argvs, logs=logs)

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(90)
        assert not thread.is_alive(), "run_children never returned"
    assert "results" in box, "run_children raised"
    return list(box["results"])


def _by_image(results: list[ChildResult]) -> dict[str, ChildResult]:
    found = {r.image: r for r in results}
    assert len(found) == len(results), "an image reported twice"
    return found


def test_run_children_starts_every_child_at_once_and_reads_its_result(
    tmp_path: Path, child: Path
) -> None:
    results = _by_image(_run(tmp_path, child, dict.fromkeys(("kde", "gnome", "wm"), "together")))
    assert set(results) == {"kde", "gnome", "wm"}
    for image, result in results.items():
        assert result.exit_code == 0, result.log.read_text()
        assert result.run_id == f"child-{image}"
        assert tuple(result.isos) == (Path(f"/out/bentoo-{image}.iso"),)
        assert isinstance(result.duration_s, int | float) and result.duration_s >= 0
        # C1: the parent's measured wall clock, never the child's own claim (999 s)
        assert result.duration_s < 999
        assert result.error is None
    assert summarize(list(results.values()))[0] == 0


def test_run_children_keeps_each_childs_output_in_its_own_log(tmp_path: Path, child: Path) -> None:
    results = _by_image(_run(tmp_path, child, dict.fromkeys(("kde", "gnome"), "ok")))
    logs = {r.log for r in results.values()}
    assert len(logs) == 2  # distinct
    for image, result in results.items():
        assert result.log.parent.parent == tmp_path / "scratch" / "logs"
        assert result.log.parent.name.startswith("build-")  # the build run's own directory
        assert result.log.name == f"{image}.log"
        text = result.log.read_text()
        assert f"out of {image}" in text and f"err of {image}" in text  # stdout and stderr
        other = "gnome" if image == "kde" else "kde"
        assert f"of {other}" not in text  # never the sibling's


def test_run_children_never_stops_a_sibling_when_one_fails(tmp_path: Path, child: Path) -> None:
    """R2.1, R2.3: kde fails at once; gnome is still running then and must finish;
    nothing of the failed one is cleaned up."""
    results = _by_image(_run(tmp_path, child, {"kde": "fail", "gnome": "outlive"}))
    assert results["kde"].exit_code == 1
    assert results["gnome"].exit_code == 0, results["gnome"].log.read_text()
    assert results["gnome"].run_id == "child-gnome"
    assert (tmp_path / "meet" / "rootfs-kept").is_dir()
    code, lines = summarize(list(results.values()))
    assert code == 1
    assert any("kde" in line and str(results["kde"].log) in line for line in lines)


@pytest.mark.parametrize("behavior", ["noresult", "garbage"])
def test_run_children_a_success_without_a_readable_result_is_a_failure(
    tmp_path: Path, child: Path, behavior: str
) -> None:
    results = _by_image(_run(tmp_path, child, {"kde": behavior, "gnome": "ok"}))
    code, lines = summarize(list(results.values()))
    assert code == 1, "an exit 0 without a result file counted as success"
    kde = [line for line in lines if "kde" in line]
    assert kde and not any("/out/bentoo-kde.iso" in line for line in kde)
    # C1: exit_code is ALWAYS the process's return code; the failure is in `error`
    assert results["kde"].exit_code == 0
    expected = {"noresult": "no result file", "garbage": "unreadable result file"}[behavior]
    assert results["kde"].error == expected
    assert any(expected in line for line in kde), lines
    assert results["gnome"].exit_code == 0


def test_run_children_a_child_that_cannot_start_fails_alone(tmp_path: Path, child: Path) -> None:
    logs = tmp_path / "scratch" / "logs"
    argvs = {
        "kde": ["/nonexistent/python3", "-c", "pass"],
        "gnome": [
            sys.executable,
            str(child),
            "gnome",
            "ok",
            str(tmp_path / "meet"),
            "2",
            "--result-file",
            str(logs / "gnome.json"),
        ],
    }
    results = _by_image(run_children(argvs, logs=logs))
    assert results["kde"].exit_code != 0
    assert results["gnome"].exit_code == 0
    code, lines = summarize(list(results.values()))
    assert code == 1
    said = " ".join(lines)
    if results["kde"].log.is_file():
        said += results["kde"].log.read_text()
    assert "/nonexistent/python3" in said or "No such file" in said


def test_run_children_a_result_lacking_a_required_field_names_it(
    tmp_path: Path, child: Path
) -> None:
    results = _by_image(_run(tmp_path, child, {"kde": "lacks-isos"}))
    assert results["kde"].exit_code == 0  # the process's own code
    assert results["kde"].error == "result file lacks isos"
    assert summarize(list(results.values()))[0] == 1


def test_run_children_only_run_id_and_isos_are_required(tmp_path: Path, child: Path) -> None:
    """Hostile: a result without the written-but-optional fields is still a success."""
    results = _by_image(_run(tmp_path, child, {"kde": "lacks-optional"}))
    assert results["kde"].error is None and results["kde"].exit_code == 0
    assert results["kde"].run_id == "child-kde"
    assert results["kde"].duration_s >= 0  # measured by the parent anyway


def test_run_children_reads_the_result_path_from_each_argv(tmp_path: Path, child: Path) -> None:
    """C1: no path is rebuilt on the reading side -- an odd name in the argv still works."""
    logs = tmp_path / "scratch" / "logs" / "build-R"
    argvs = {
        image: [
            sys.executable,
            str(child),
            image,
            "ok",
            str(tmp_path / "meet"),
            "2",
            "--result-file",
            str(tmp_path / "elsewhere" / f"odd-{image}-name.json"),
        ]
        for image in ("kde", "gnome")
    }
    (tmp_path / "elsewhere").mkdir()
    results = _by_image(run_children(argvs, logs=logs))
    for image in ("kde", "gnome"):
        assert results[image].exit_code == 0 and results[image].run_id == f"child-{image}"


def test_run_children_an_interrupt_stops_the_children_and_reports_them(
    tmp_path: Path, child: Path
) -> None:
    """R2.6: Ctrl+C terminates every running child, records each as interrupted and
    raises parallel.Interrupted (a KeyboardInterrupt) carrying the results."""
    import _thread

    from shidashi.parallel import Interrupted

    logs = tmp_path / "scratch" / "logs" / "build-R"
    argvs = {
        image: [
            sys.executable,
            str(child),
            image,
            "hang",
            str(tmp_path / "meet"),
            "2",
            "--result-file",
            str(logs / f"{image}.json"),
        ]
        for image in ("kde", "gnome")
    }

    def interrupt_when_started() -> None:
        meet = tmp_path / "meet"
        for _ in range(500):
            if len(list(meet.glob("started-*"))) == 2:
                break
            threading.Event().wait(0.02)
        _thread.interrupt_main()

    threading.Thread(target=interrupt_when_started, daemon=True).start()
    with pytest.raises(Interrupted) as caught:
        run_children(argvs, logs=logs)
    assert isinstance(caught.value, KeyboardInterrupt)
    results = _by_image(list(caught.value.results))
    assert set(results) == {"kde", "gnome"}
    assert all(r.error == "interrupted" and r.exit_code != 0 for r in results.values())


def _gone(pid: int) -> bool:
    """No such process, or a zombie (or one being torn down): dead, only not yet reaped
    (a container whose PID 1 never reaps orphans keeps them so, and ``kill(pid, 0)``
    still succeeds on one)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] in ("Z", "X")


def test_an_interrupt_kills_each_childs_whole_process_group(tmp_path: Path, child: Path) -> None:
    """R2.6: SIGTERM to the child alone would leave its grandchild (nspawn, emerge)
    writing into the rootfs after Interrupted; the parent kills the group."""
    import _thread
    import os

    from shidashi.parallel import Interrupted

    meet = tmp_path / "meet"
    logs = tmp_path / "scratch" / "logs" / "build-R"
    argvs = {
        "kde": [
            sys.executable,
            str(child),
            "kde",
            "group",
            str(meet),
            "1",
            "--result-file",
            str(logs / "kde.json"),
        ]
    }

    def interrupt_when_ready() -> None:
        for _ in range(500):
            if (meet / "grandchild-kde").exists():
                break
            threading.Event().wait(0.02)
        _thread.interrupt_main()

    threading.Thread(target=interrupt_when_ready, daemon=True).start()
    with pytest.raises(Interrupted):
        run_children(argvs, logs=logs)
    child_pgid = int((meet / "pgid-kde").read_text())
    assert child_pgid != os.getpgid(0), "the child shares the parent's process group"
    grandchild = int((meet / "grandchild-kde").read_text())
    for _ in range(250):
        if _gone(grandchild):
            break
        threading.Event().wait(0.02)
    else:
        os.kill(grandchild, 9)
        pytest.fail("the grandchild kept running after Interrupted")
