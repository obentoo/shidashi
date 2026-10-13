"""How ``run_children`` stops on a signal, and the review fixes of task 6.3 (story 006).

While ``run_children`` runs on the main thread, SIGINT, SIGTERM and SIGHUP only set a
flag: no ``KeyboardInterrupt`` is ever raised inside ``Popen`` (a forked child would go
unrecorded and keep running) or inside ``concurrent.futures.wait`` (a future's lock could
stay held). The flag is checked between starts and while waiting; then every started
child's process group that still has a member is stopped (SIGTERM, grace, SIGKILL) and
``Interrupted(results, signum=...)`` raised. The previous handlers come back afterwards.

Every child here records its process group (``pgid-<image>``) and any grandchild
(``grandchild-<image>``) under ``meet``; the ``reaper`` fixture kills whatever is left,
so a failing test never leaves a process running.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import parallel
from shidashi.parallel import Interrupted, run_children

_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

_CHILD = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
image, behavior, meet = args[0], args[1], Path(args[2])
result = None
if "--result-file" in args:  # the LAST one, as the CLI would take it
    result = Path(args[len(args) - args[::-1].index("--result-file")])
(meet / f"pgid-{image}").write_text(str(os.getpgid(0)))
if behavior == "stubborn":  # ignores SIGTERM: only SIGKILL ends it
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if behavior in ("group", "orphan", "stubborn"):  # a grandchild, like nspawn under assemble
    grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (meet / f"grandchild-{image}").write_text(str(grand.pid))
(meet / f"started-{image}").touch()
if behavior == "orphan":  # the leader leaves; its grandchild stays in its group
    os._exit(0)
if behavior in ("hang", "group", "stubborn"):
    time.sleep(120)
if behavior == "brief":  # runs long enough for the parent to be looked at
    time.sleep(0.5)
if behavior == "deep" and result is not None:
    result.write_text("[" * 200000)
elif behavior in ("ok", "brief") and result is not None:
    result.write_text(json.dumps({"run_id": f"child-{image}", "isos": []}))
sys.exit(0)
"""

#: Calls run_children on ITS main thread, so its signal handlers are installed.
_DRIVER = r"""
import json, signal, sys
from pathlib import Path
from shidashi.parallel import Interrupted, run_children
# known dispositions, whatever the test runner inherited (a background job ignores SIGINT)
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
spec = json.loads(sys.argv[1])
out = {}
try:
    results = run_children(spec["argvs"], logs=Path(spec["logs"]))
    out["outcome"] = "returned"
except Interrupted as stop:
    results = stop.results
    out["outcome"] = "interrupted"
    out["signum"] = getattr(stop, "signum", None)
out["results"] = [{"image": r.image, "exit_code": r.exit_code, "error": r.error} for r in results]
out["restored"] = all(signal.getsignal(s) == h for s, h in before.items())
print(json.dumps(out), flush=True)
"""


@pytest.fixture
def meet(tmp_path: Path) -> Iterator[Path]:
    """The children's meeting directory; whatever they leave running is killed after."""
    where = tmp_path / "meet"
    where.mkdir()
    yield where
    for mark in where.glob("pgid-*"):
        with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
            os.killpg(int(mark.read_text()), signal.SIGKILL)
    for mark in where.glob("grandchild-*"):
        with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
            os.kill(int(mark.read_text()), signal.SIGKILL)


@pytest.fixture(autouse=True)
def _handlers_restored() -> Iterator[None]:
    """Every test leaves the process's signal handlers as it found them."""
    before = {signum: signal.getsignal(signum) for signum in _STOP_SIGNALS}
    yield
    after = {signum: signal.getsignal(signum) for signum in _STOP_SIGNALS}
    for signum, handler in before.items():
        signal.signal(signum, handler)  # repair first, so one leak fails one test only
    assert after == before, "run_children left its signal handlers installed"


@pytest.fixture
def child(tmp_path: Path) -> Path:
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    return script


def _argv(child: Path, meet: Path, image: str, behavior: str, result: Path) -> list[str]:
    return [sys.executable, str(child), image, behavior, str(meet), "--result-file", str(result)]


def _until(predicate: Any, *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _gone(pid: int, *, timeout: float = 5.0) -> bool:
    def dead() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # a zombie no one reaps yet (reparented) counts as gone: it runs nothing
        with contextlib.suppress(OSError):
            return Path(f"/proc/{pid}/stat").read_text().split(") ")[1].startswith("Z")
        return False

    return _until(dead, timeout=timeout)


# --- Interrupted carries the signal -------------------------------------------------------


def test_interrupted_is_still_built_from_results_alone_and_defaults_to_sigint() -> None:
    stop = Interrupted([])
    assert isinstance(stop, KeyboardInterrupt)
    assert stop.results == ()
    assert stop.signum == signal.SIGINT


def test_interrupted_carries_the_signal_that_stopped_the_build() -> None:
    assert Interrupted([], signum=signal.SIGTERM).signum == signal.SIGTERM


# --- the handlers ---------------------------------------------------------------------------


def test_run_children_on_the_main_thread_catches_the_stop_signals_while_it_runs(
    tmp_path: Path, child: Path, meet: Path
) -> None:
    """SIGINT, SIGTERM and SIGHUP are run_children's own while it runs, the previous
    handlers afterwards (the autouse fixture checks the restore)."""
    seen: dict[int, Any] = {}
    argvs = {"kde": _argv(child, meet, "kde", "hang", tmp_path / "kde.json")}

    def look_then_interrupt() -> None:
        if _until(lambda: (meet / "started-kde").exists()):
            seen.update({signum: signal.getsignal(signum) for signum in _STOP_SIGNALS})
        import _thread

        _thread.interrupt_main()

    before = {signum: signal.getsignal(signum) for signum in _STOP_SIGNALS}
    threading.Thread(target=look_then_interrupt, daemon=True).start()
    with pytest.raises(Interrupted):
        run_children(argvs, logs=tmp_path / "logs")
    assert set(seen) == set(_STOP_SIGNALS)
    for signum in _STOP_SIGNALS:
        assert seen[signum] != before[signum], f"{signal.Signals(signum).name} not caught"


def test_an_ignored_signal_stays_ignored(tmp_path: Path, child: Path, meet: Path) -> None:
    """``nohup shidashi build``: SIGHUP was ignored on purpose and must not stop the build."""
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    seen: list[Any] = []

    def look() -> None:
        if _until(lambda: (meet / "started-kde").exists()):
            seen.append(signal.getsignal(signal.SIGHUP))

    try:
        threading.Thread(target=look, daemon=True).start()
        results = run_children(
            {"kde": _argv(child, meet, "kde", "brief", tmp_path / "kde.json")},
            logs=tmp_path / "logs",
        )
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert results[0].exit_code == 0 and results[0].error is None
    assert seen == [signal.SIG_IGN]


def test_run_children_off_the_main_thread_installs_nothing(
    tmp_path: Path, child: Path, meet: Path
) -> None:
    """signal.signal works on the main thread only: off it, nothing is installed and the
    run still completes (interrupts then need the caller's own handling)."""
    seen: list[dict[int, Any]] = []
    before = {signum: signal.getsignal(signum) for signum in _STOP_SIGNALS}
    argvs = {"kde": _argv(child, meet, "kde", "brief", tmp_path / "kde.json")}
    box: dict[str, Any] = {}

    def call() -> None:
        box["results"] = run_children(argvs, logs=tmp_path / "logs")

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while thread.is_alive() and time.monotonic() < deadline:
        seen.append({signum: signal.getsignal(signum) for signum in _STOP_SIGNALS})
        time.sleep(0.01)
    assert not thread.is_alive(), "run_children never returned"
    assert box["results"][0].exit_code == 0
    assert seen and all(snapshot == before for snapshot in seen)


# --- finding 1: a signal inside Popen --------------------------------------------------------


def test_a_signal_inside_popen_never_loses_the_child_it_forked(
    tmp_path: Path, child: Path, meet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl+C lands inside ``Popen`` after its fork: the child exists but ``Popen`` never
    returned. It must still be recorded, stopped and reaped -- never left running, never
    reported as "never started"."""
    real_popen = subprocess.Popen
    forked: list[subprocess.Popen[bytes]] = []
    leaked: list[BaseException] = []

    def popen_hit_by_ctrl_c(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)
        forked.append(proc)
        try:
            if len(forked) == 1:
                signal.raise_signal(signal.SIGINT)  # still inside "Popen"
        except KeyboardInterrupt as err:
            leaked.append(err)
            raise
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen_hit_by_ctrl_c)
    argvs = {
        "kde": _argv(child, meet, "kde", "hang", tmp_path / "kde.json"),
        "gnome": _argv(child, meet, "gnome", "hang", tmp_path / "gnome.json"),
    }
    try:
        with pytest.raises(Interrupted) as caught:
            run_children(argvs, logs=tmp_path / "logs")
    finally:
        for proc in forked:  # whatever the outcome, nothing outlives the test
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
    assert not leaked, "a KeyboardInterrupt was raised inside Popen"
    assert forked and forked[0].returncode is not None, "the forked child was left running"
    results = {r.image: r for r in caught.value.results}
    assert results["kde"].error == "interrupted"
    assert results["kde"].exit_code != parallel.NOT_STARTED  # it did start
    assert results["gnome"].error == "interrupted"
    assert len(forked) == 1, "a child started after the interrupt"


# --- finding 2: a signal inside concurrent.futures.wait ------------------------------------


def test_a_signal_while_waiting_never_raises_inside_futures_wait(
    tmp_path: Path, child: Path, meet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A KeyboardInterrupt inside ``concurrent.futures.wait`` can leave a future's lock
    held and hang the parent; the signal must only be recorded, then acted on."""
    real_wait = vars(parallel)["wait"]
    leaked: list[BaseException] = []
    calls: list[int] = []

    def wait_hit_by_ctrl_c(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        try:
            if len(calls) == 1 and _until(lambda: (meet / "started-kde").exists()):
                signal.raise_signal(signal.SIGINT)  # inside "wait"
            return real_wait(*args, **kwargs)
        except KeyboardInterrupt as err:
            leaked.append(err)
            raise

    monkeypatch.setattr(parallel, "wait", wait_hit_by_ctrl_c)
    argvs = {"kde": _argv(child, meet, "kde", "hang", tmp_path / "kde.json")}
    with pytest.raises(Interrupted) as caught:
        run_children(argvs, logs=tmp_path / "logs")
    assert not leaked, "a KeyboardInterrupt was raised inside concurrent.futures.wait"
    assert caught.value.signum == signal.SIGINT
    assert caught.value.results[0].error == "interrupted"


# --- decision: SIGTERM and SIGHUP stop the build like Ctrl+C -------------------------------


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_stop_signal_to_the_parent_stops_every_childs_group(
    tmp_path: Path, child: Path, meet: Path, signum: signal.Signals
) -> None:
    """The parent (a real process here) gets SIGINT, SIGTERM or SIGHUP: every child's
    whole group is stopped, Interrupted carries the signal, the handlers come back."""
    logs = tmp_path / "logs"
    argvs = {
        "kde": _argv(child, meet, "kde", "group", logs / "kde.json"),
        "gnome": _argv(child, meet, "gnome", "hang", logs / "gnome.json"),
    }
    driver = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, json.dumps({"argvs": argvs, "logs": str(logs)})],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = _until(
            lambda: (meet / "grandchild-kde").exists() and (meet / "started-gnome").exists()
        )
        assert ready, "the children never started"
        driver.send_signal(signum)
        out, err = driver.communicate(timeout=60)
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.communicate()
    assert driver.returncode == 0, f"the parent died of {driver.returncode}: {err}"
    report = json.loads(out)
    assert report["outcome"] == "interrupted"
    assert report["signum"] == signum
    assert report["restored"] is True
    assert all(r["error"] == "interrupted" and r["exit_code"] != 0 for r in report["results"])
    assert _gone(int((meet / "grandchild-kde").read_text())), "a grandchild kept running"


def test_a_second_signal_ends_the_grace_period_at_once(
    tmp_path: Path, child: Path, meet: Path
) -> None:
    """A child ignoring SIGTERM would hold the build 30 s; a second signal kills now."""
    logs = tmp_path / "logs"
    argvs = {"kde": _argv(child, meet, "kde", "stubborn", logs / "kde.json")}
    driver = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, json.dumps({"argvs": argvs, "logs": str(logs)})],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _until(lambda: (meet / "grandchild-kde").exists()), "the child never started"
        driver.send_signal(signal.SIGTERM)
        time.sleep(1.0)  # well inside the 30 s grace period
        sent = time.monotonic()
        driver.send_signal(signal.SIGTERM)
        out, err = driver.communicate(timeout=20)
        took = time.monotonic() - sent
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.communicate()
    assert driver.returncode == 0, f"the parent died of {driver.returncode}: {err}"
    report = json.loads(out)
    assert report["outcome"] == "interrupted"
    assert report["results"][0]["exit_code"] == -signal.SIGKILL
    assert took < 10, f"the second signal did not end the grace period ({took:.1f} s)"
    assert _gone(int((meet / "grandchild-kde").read_text()))


# --- finding 3: a group whose leader already exited ----------------------------------------


def test_an_interrupt_stops_a_group_whose_leader_already_exited(
    tmp_path: Path, child: Path, meet: Path
) -> None:
    """The child left, its grandchild (same group) keeps writing: the stop reaches it."""
    import _thread

    argvs = {
        "kde": _argv(child, meet, "kde", "orphan", tmp_path / "kde.json"),
        "gnome": _argv(child, meet, "gnome", "hang", tmp_path / "gnome.json"),
    }

    def interrupt_once_the_leader_is_reaped() -> None:
        def reaped() -> bool:
            if not (meet / "started-kde").exists():
                return False
            leader = int((meet / "pgid-kde").read_text())
            return not Path(f"/proc/{leader}").exists()  # exited AND reaped by its thread

        _until(reaped)
        _until(lambda: (meet / "started-gnome").exists())
        _thread.interrupt_main()

    threading.Thread(target=interrupt_once_the_leader_is_reaped, daemon=True).start()
    with pytest.raises(Interrupted) as caught:
        run_children(argvs, logs=tmp_path / "logs")
    grandchild = int((meet / "grandchild-kde").read_text())
    assert _gone(grandchild), "the orphaned grandchild kept running"
    results = {r.image: r for r in caught.value.results}
    assert results["kde"].error == "interrupted"  # its group had to be stopped
    assert results["gnome"].error == "interrupted"


# --- finding 4: a stale result file ---------------------------------------------------------


def test_a_previous_runs_result_file_is_never_read_as_this_ones(
    tmp_path: Path, child: Path, meet: Path
) -> None:
    result = tmp_path / "logs" / "kde.json"
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps({"run_id": "old-run", "isos": ["/out/old.iso"]}))
    results = run_children(
        {"kde": _argv(child, meet, "kde", "silent", result)}, logs=tmp_path / "logs"
    )
    assert results[0].exit_code == 0
    assert results[0].error == "no result file"
    assert results[0].run_id is None and results[0].isos == ()


# --- nits: start errors beyond OSError, a result file too deep to parse --------------------


def test_a_start_error_beyond_oserror_fails_that_image_alone(
    tmp_path: Path, child: Path, meet: Path
) -> None:
    argvs = {
        "kde": [sys.executable, "-c", "pass\0"],  # Popen: ValueError, embedded null byte
        "gnome": _argv(child, meet, "gnome", "ok", tmp_path / "gnome.json"),
    }
    results = {r.image: r for r in run_children(argvs, logs=tmp_path / "logs")}
    assert results["kde"].exit_code == parallel.NOT_STARTED
    assert results["kde"].error is not None and "null" in results["kde"].error
    assert results["gnome"].exit_code == 0 and results["gnome"].error is None


def test_a_result_file_too_deep_to_parse_is_unreadable(
    tmp_path: Path, child: Path, meet: Path
) -> None:
    results = run_children(
        {"kde": _argv(child, meet, "kde", "deep", tmp_path / "kde.json")}, logs=tmp_path / "l"
    )
    assert results[0].exit_code == 0
    assert results[0].error == "unreadable result file"
