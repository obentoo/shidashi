"""The parallel launcher of ``shidashi build`` (story 006, cross-story contract C1).

``build`` assembles its ISO targets as concurrent child processes, one ``assemble`` per
image. This module holds what that needs; this part is pure planning:

- :func:`job_share` -- how many jobs each child gets (R1.3, R1.4);
- :func:`assemble_argv` -- the child's command line. The package has no ``__main__``, so
  the child runs ``python -c "from shidashi.cli import app; app()"`` exactly as
  ``lab/vm/guest-job.sh`` does;
- :class:`ChildResult`, :data:`RESULT_FIELDS`, :data:`REQUIRED_FIELDS` -- what a child
  reports back through its ``--result-file``;
- :func:`summarize` -- the build's exit code and one report line per image (R2.2, R2.4,
  R2.5);
- :func:`run_children` -- the I/O part: start one child per image, wait for all, read
  their result files (R1.1, R1.5, R2.1, R2.3, R2.5, R2.6);
- :func:`watch` and :func:`ended_line` -- each image's current step while the children
  run, then how it ended (R1.9, R1.10).

Story 014 calls :func:`assemble_argv` and :func:`run_children` and reads
:class:`ChildResult` too: keep their signatures. The library raises, it does not log --
the command reports.

:func:`run_children` must run on the MAIN thread for a signal to stop the build: only
there may it install its SIGINT, SIGTERM and SIGHUP handlers (``signal.signal``).
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Collection, Generator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from shidashi import progress

#: The command a child runs before its ``assemble`` arguments (no ``__main__`` in the package).
#: ``-P``: ``-c`` would put the working directory first on ``sys.path``, so a build started
#: from a directory holding a ``shidashi/`` or ``typer.py`` would run that code, as root.
CHILD_COMMAND = ("-P", "-c", "from shidashi.cli import app; app()")

#: What a child writes into its result file, field by field (C1).
RESULT_FIELDS = ("image", "exit_code", "run_id", "isos", "duration_s")

#: What the parent requires on read; a missing written-only field never fails an image (C1).
REQUIRED_FIELDS = ("run_id", "isos")

#: ``assemble --compression`` values by the compressions they produce.
_COMPRESSION_VALUES = {
    ("zstd",): "zstd",
    ("xz",): "xz",
    ("xz", "zstd"): "both",
}

#: What ``assemble`` produces without ``--compression``.
_DEFAULT_COMPRESSION = "zstd"

#: ``exit_code`` of a child that never started (the shell's "command not found").
NOT_STARTED = 127

#: How long an interrupted child's process group gets to stop after SIGTERM (R2.6).
_GRACE_S = 30.0

#: How often the waiting parent wakes; a stop signal is acted on within this delay.
_POLL_S = 0.2

#: How often :func:`watch` reads the children's status files.
_WATCH_POLL_S = 1.0

#: The signals that stop a concurrent build like Ctrl+C (R2.6; SIGTERM and SIGHUP by the
#: author's decision): a ``kill`` or a closed terminal stops the children's groups too.
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

#: ``error`` of an image stopped by an interrupt (R2.6).
_INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class ChildResult:
    """One image's outcome in a concurrent build (C1).

    ``exit_code`` is always the process's own return code; ``duration_s`` the parent's
    measured wall clock; ``run_id`` and ``isos`` come from the child's result file.
    ``error`` says why an image failed beyond its exit code ("no result file", ...) and is
    ``None`` for a child that succeeded.
    """

    image: str
    exit_code: int
    run_id: str | None
    log: Path
    duration_s: float
    isos: tuple[Path, ...]
    error: str | None

    @property
    def failed(self) -> bool:
        """An image fails on a non-zero exit OR a recorded error, whatever its exit code."""
        return self.exit_code != 0 or self.error is not None


class Interrupted(KeyboardInterrupt):
    """A concurrent build stopped by a signal, carrying one :class:`ChildResult` per image.

    Every child whose process group still had a member has been stopped -- the whole
    group -- and carries ``error="interrupted"``; nothing was cleaned (R2.6). ``signum``
    is the signal that stopped the build (SIGINT for Ctrl+C), so the command can exit 130
    for SIGINT and ``128 + signum`` otherwise.
    """

    def __init__(self, results: Sequence[ChildResult], *, signum: int = signal.SIGINT) -> None:
        super().__init__(f"interrupted with {len(results)} image(s)")
        self.results: tuple[ChildResult, ...] = tuple(results)
        self.signum: int = int(signum)


def job_share(jobs: int, images: int) -> int:
    """Each of ``images`` concurrent children's share of ``jobs``: ``max(1, jobs // images)``.

    No job is held back for the parent: it only waits. Ints only -- the caller resolves an
    absent ``--jobs`` to the CPU count (C1).
    """
    for name, value in (("jobs", jobs), ("images", images)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"job_share: {name} must be an int, not {type(value).__name__}")
    if images < 1:
        raise ValueError(f"job_share: images must be at least 1, not {images}")
    return max(1, jobs // images)


def _compression_value(compressions: Sequence[str]) -> str:
    """The ``--compression`` value producing ``compressions`` (``("zstd", "xz")`` is both)."""
    key = tuple(sorted(set(compressions)))
    try:
        return _COMPRESSION_VALUES[key]
    except KeyError:
        raise ValueError(f"no assemble --compression produces {tuple(compressions)!r}") from None


def assemble_argv(
    target: str,
    *,
    arch: str,
    init: str,
    jobs: int,
    output_dir: Path,
    result_file: Path,
    compressions: Sequence[str] = ("zstd",),
    stage4: bool = False,
    sbom: bool = True,
    fresh: bool = False,
    trunk: bool = True,
    download: bool = True,
    keep: bool = False,
    boot_test: bool = False,
    cid: int | None = None,
    verbose: bool = False,
    status_file: Path | None = None,
) -> list[str]:
    """The command line of the child ``assemble`` of image ``target``.

    ``--jobs``, ``--output-dir`` and ``--result-file`` are always passed; every other option
    only when it differs from ``assemble``'s default. ``--work-dir`` is not an argument: the
    environment ``_apply_work_dir`` exports reaches the child (R1.11).
    """
    argv = [
        sys.executable,
        *CHILD_COMMAND,
        "assemble",
        arch,
        target,
        init,
        "--jobs",
        str(jobs),
        "--output-dir",
        str(output_dir),
        "--result-file",
        str(result_file),
    ]
    compression = _compression_value(compressions)
    if compression != _DEFAULT_COMPRESSION:
        argv += ["--compression", compression]
    switches = (
        (stage4, "--stage4"),
        (not sbom, "--no-sbom"),
        (fresh, "--fresh"),
        (not trunk, "--no-trunk"),
        (not download, "--no-download"),
        (keep, "--keep"),
        (boot_test, "--boot-test"),
    )
    argv += [flag for wanted, flag in switches if wanted]
    if cid is not None:
        argv += ["--cid", str(cid)]
    if verbose:
        argv.append("--verbose")
    if status_file is not None:
        argv += ["--status-file", str(status_file)]
    return argv


def _report_line(result: ChildResult) -> str:
    if result.failed:
        reason = result.error if result.error is not None else f"exit code {result.exit_code}"
        return f"{result.image}: failed: {reason} — see {result.log}"
    if not result.isos:
        return f"{result.image}: done (no ISO)"
    return f"{result.image}: " + ", ".join(str(iso) for iso in result.isos)


def summarize(results: Sequence[ChildResult]) -> tuple[int, list[str]]:
    """The build's exit code and one report line per image (R2.2, R2.4, R2.5).

    Exit 0 only when every image exited 0 with no error; an image lists its ISOs, or says
    ``failed: <error or exit code> — see <log>``.
    """
    code = 1 if any(result.failed for result in results) else 0
    return code, [_report_line(result) for result in results]


def ended_line(result: ChildResult) -> str:
    """An ended image's last line: ``<image>  done|failed  <its duration>``. Pure."""
    state = "failed" if result.failed else "done"
    return f"{result.image}  {state}  {progress.elapsed(result.duration_s)}"


#: The status a child writes when its assemble ends; the image's row stops its clock there.
ENDED_STEPS = ("done", "failed")


def _read_status(path: Path) -> tuple[str, float | None] | None:
    """The step a child's status file names and its ``since`` (epoch seconds, ``None`` when
    absent); ``None`` when the file is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # UnicodeDecodeError and JSONDecodeError are ValueErrors; RecursionError: nested too deep
    except OSError, ValueError, RecursionError:
        return None
    if not isinstance(data, dict):
        return None
    step, since = data.get("step"), data.get("since")
    if not isinstance(step, str) or not step:
        return None
    if isinstance(since, bool) or not isinstance(since, int | float):
        return step, None
    return step, float(since)


class _Watch:
    """Each image's last known step, polled from the status files by one thread.

    The thread owns ``steps`` while it runs; the caller's thread polls once more only
    after joining it. On a terminal the steps are a ``rich.live`` table redrawn at each
    poll; anywhere else a changed step is a line.
    """

    def __init__(
        self, status_files: Mapping[str, Path], console: Console, started: Mapping[str, float]
    ) -> None:
        self.status_files = dict(status_files)
        self.console = console
        now = time.time()
        self.started = {image: started.get(image, now) for image in self.status_files}
        self.steps: dict[str, str | None] = dict.fromkeys(self.status_files)
        self.ended: dict[str, float] = {}  # image -> when its child said done/failed
        self.stop = threading.Event()
        self.live = (
            Live(console=console, transient=True, auto_refresh=False)
            if console.is_terminal and not console.is_dumb_terminal
            else None
        )

    def _row(self, image: str, now: float) -> tuple[str, str, str]:
        # wall clock on both sides (the child's ``since`` is epoch); a clock stepped back
        # shows 0:00, never a negative time
        until = self.ended.get(image, now)
        return (
            image,
            self.steps[image] or "starting",
            progress.elapsed(max(0.0, until - self.started[image])),
        )

    def _table(self) -> Table:
        grid = Table.grid(padding=(0, 2))
        now = time.time()
        for image in self.status_files:
            grid.add_row(*(Text(cell) for cell in self._row(image, now)))
        return grid

    def poll(self) -> None:
        """Read every status file; show the change. A missing or unreadable file keeps the
        image's last known step."""
        changed = []
        for image, path in self.status_files.items():
            status = _read_status(path)
            if status is None:
                continue
            step, since = status
            if step != self.steps[image]:
                self.steps[image] = step
                changed.append(image)
                if step in ENDED_STEPS:
                    self.ended[image] = since if since is not None else time.time()
        if self.live is not None:
            self.live.update(self._table(), refresh=True)
            return
        now = time.time()
        for image in changed:
            self.console.print(Text("  ".join(self._row(image, now))), soft_wrap=True)

    def run(self) -> None:
        while not self.stop.wait(_WATCH_POLL_S):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 -- the view never fails the build; the final poll retries
                continue


@contextlib.contextmanager
def watch(
    status_files: Mapping[str, Path], *, console: Console, started: Mapping[str, float]
) -> Generator[None]:
    """Show each image's current step while its child runs (R1.9, R1.10).

    ``status_files`` maps each image to the ``--status-file`` its child rewrites;
    ``started`` maps it to when its child was started (epoch seconds). ``build`` runs
    :func:`run_children` inside this block.

    A thread polls the files every :data:`_WATCH_POLL_S`: on a terminal, a ``rich.live``
    table of one row per image ``<image>  <step>  <elapsed since the image started>``,
    gone when the block ends; anywhere else one line per change of an image's step. A
    missing or unreadable status file keeps the image's last known step and never fails
    the block. The results are not known here: the caller shows each ended image with
    :func:`ended_line`.
    """
    view = _Watch(status_files, console, started)
    poller = threading.Thread(target=view.run, name="status-watch", daemon=True)
    if view.live is not None:
        view.live.start()
    propagating = False
    try:
        view.poll()
        poller.start()
        yield
    except BaseException:
        propagating = True
        raise
    finally:
        try:
            view.stop.set()
            if poller.is_alive():
                poller.join()
            if propagating:  # never let the view replace what is unwinding (an interrupt)
                with contextlib.suppress(Exception):
                    view.poll()
            else:
                view.poll()  # a step entered since the last poll still shows
        finally:
            if view.live is not None:
                view.live.stop()


# --- run_children: the I/O part ---------------------------------------------------------


@dataclass
class _Child:
    """One image's child process, as the parent tracks it while it runs."""

    image: str
    argv: list[str]
    log: Path
    started: float = 0.0
    proc: subprocess.Popen[bytes] | None = None
    #: Resolves to the parent's clock when the child ended (one thread per child).
    reaped: Future[float] | None = field(default=None, repr=False)
    #: Why the child never started (the ``OSError`` text), if it did not.
    start_error: str | None = None


def _start(child: _Child, logs: Path) -> None:
    """Start ``child`` in its own process group, its stdout+stderr in its own log (R1.5).

    The result file its argv names is removed first: a child that writes nothing is then
    "no result file", never a previous run's success. An ``OSError`` -- that removal, the
    log directory, the log, or the program itself -- or a ``ValueError``/``RuntimeError``
    from ``Popen`` (a NUL byte in the argv, ...) leaves the child unstarted with the error
    text; the caller goes on with the next image.
    """
    child.started = time.monotonic()
    try:
        result = _result_path(child.argv)
        if result is not None:
            result.unlink(missing_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        with child.log.open("wb") as log:
            child.proc = subprocess.Popen(
                child.argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                # its own group: nspawn and emerge under it share it, so an interrupt
                # reaches them through os.killpg (R2.6)
                process_group=0,
            )
    except OSError as err:
        child.start_error = str(err) or type(err).__name__
    except (ValueError, RuntimeError) as err:
        child.start_error = f"{type(err).__name__}: {err}"


def _reap(proc: subprocess.Popen[bytes]) -> float:
    """Block until ``proc`` ends; return when it ended, by the parent's clock."""
    proc.wait()
    return time.monotonic()


def _result_path(argv: Sequence[str]) -> Path | None:
    """The path of ``argv``'s LAST ``--result-file`` (``--result-file=PATH`` too), if any.

    No path is rebuilt on the reading side: the child writes where its argv says (C1).
    """
    found: str | None = None
    for index, arg in enumerate(argv):
        if arg == "--result-file" and index + 1 < len(argv):
            found = argv[index + 1]
        elif arg.startswith("--result-file="):
            found = arg.removeprefix("--result-file=")
    return Path(found) if found else None


def _read_result(path: Path | None) -> tuple[str | None, tuple[Path, ...], str | None]:
    """``(run_id, isos, error)`` read from a child's result file (C1, R2.5).

    ``error`` is ``"no result file"``, ``"unreadable result file"`` (not JSON, not an
    object, or a required field of the wrong type) or ``"result file lacks <field>"`` for
    a :data:`REQUIRED_FIELDS` member whose key is absent. A present ``run_id: null`` is no
    failure: the child writes it when its audit is off or it failed before the run began.
    """
    unreadable: tuple[None, tuple[Path, ...], str] = (None, (), "unreadable result file")
    if path is None:
        return None, (), "no result file"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, (), "no result file"
    # UnicodeDecodeError and JSONDecodeError are ValueErrors; RecursionError: nested too deep
    except OSError, ValueError, RecursionError:
        return unreadable
    if not isinstance(data, dict):
        return unreadable
    for name in REQUIRED_FIELDS:
        if name not in data:
            return None, (), f"result file lacks {name}"
    run_id, isos = data["run_id"], data["isos"]
    if run_id is not None and not isinstance(run_id, str):
        return unreadable
    if not isinstance(isos, list) or not all(isinstance(iso, str) for iso in isos):
        return unreadable
    return run_id, tuple(Path(iso) for iso in isos), None


def _result(child: _Child, *, interrupted: bool = False) -> ChildResult:
    """``child``'s :class:`ChildResult` under the C1 rules; it has ended or never started."""
    if child.proc is None or child.reaped is None:
        # never started: by its own OSError, or an interrupt came before its turn
        return ChildResult(
            image=child.image,
            exit_code=NOT_STARTED,
            run_id=None,
            log=child.log,
            duration_s=0.0,
            isos=(),
            error=child.start_error or _INTERRUPTED,
        )
    ended = child.reaped.result()
    run_id, isos, error = _read_result(_result_path(child.argv))
    return ChildResult(
        image=child.image,
        exit_code=child.proc.returncode,  # ALWAYS the process's own (negative for a signal)
        run_id=run_id,
        log=child.log,
        duration_s=round(ended - child.started, 3),  # the parent's clock, never the child's
        isos=isos,
        error=_INTERRUPTED if interrupted else error,
    )


class _StopFlag:
    """The stop signals the main thread's handlers received while the children ran.

    A handler only records the signal: raising ``KeyboardInterrupt`` instead could land
    inside ``Popen`` after its fork (the child then runs unrecorded) or inside
    ``concurrent.futures.wait`` (a future's lock left held). The parent acts on the flag
    between starts and while it waits.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._signums: list[int] = []

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        self._signums.append(signum)
        self._event.set()

    def interrupt(self, signum: int = signal.SIGINT) -> None:
        """Record a stop that came as a ``KeyboardInterrupt`` rather than a handled signal."""
        self.handle(signum, None)

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def count(self) -> int:
        return len(self._signums)

    @property
    def signum(self) -> int:
        """The first stop signal received (SIGINT when none was)."""
        return self._signums[0] if self._signums else signal.SIGINT


@contextlib.contextmanager
def _catching_stop_signals(flag: _StopFlag) -> Generator[None]:
    """Route SIGINT, SIGTERM and SIGHUP to ``flag`` for the block; restore the previous
    handlers after it.

    Only the main thread may install a handler: elsewhere nothing is installed and a
    signal behaves as it did before. A signal ignored on purpose (``nohup``) stays ignored.
    """
    wanted: tuple[signal.Signals, ...] = ()
    if threading.current_thread() is threading.main_thread():
        wanted = tuple(s for s in _STOP_SIGNALS if signal.getsignal(s) is not signal.SIG_IGN)
    previous = []
    try:
        for signum in wanted:
            previous.append((signum, signal.signal(signum, flag.handle)))
        yield
    finally:
        for caught, handler in reversed(previous):
            # None: a handler not installed from Python; the default is the closest one
            signal.signal(caught, handler if handler is not None else signal.SIG_DFL)


def _wait_all(futures: Collection[Future[float]], *, flag: _StopFlag | None = None) -> None:
    """Wait for every future, waking every :data:`_POLL_S`; return early once ``flag`` is
    raised. Without a ``flag`` (the final reap), wait for all whatever arrives.

    A blocked wait without a timeout would not see a stop: the handlers only record it.
    """
    pending = set(futures)
    while pending and not (flag is not None and flag.requested):
        _, pending = wait(pending, timeout=_POLL_S)


def _signal_group(pgid: int, signum: signal.Signals) -> None:
    """Send ``signum`` to process group ``pgid``; a group already gone is no error."""
    # ProcessLookupError: nothing left in it; PermissionError: nothing this process may stop
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signum)


def _group_alive(pgid: int) -> bool:
    """Whether process group ``pgid`` still has a member (a zombie counts until reaped)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # it exists; this process only may not signal it
    return True


def _stop(children: Sequence[_Child], pool: ThreadPoolExecutor, flag: _StopFlag) -> set[str]:
    """Stop every started child's WHOLE process group; return the images stopped (R2.6).

    Every group that still has a member is stopped, its leader running or not: a leader
    that already exited can leave a grandchild (nspawn, emerge) writing in its group.
    SIGTERM to each such group, up to :data:`_GRACE_S` for them to empty, then SIGKILL to
    any still alive -- SIGTERM to the child alone would leave its nspawn and emerge
    writing. A second stop signal ends the grace period at once. Every child is reaped
    before this returns; nothing is cleaned.
    """
    stopped: list[_Child] = []
    for child in children:
        if child.proc is None:
            continue
        if child.reaped is None:  # stopped between its start and its waiting thread
            child.reaped = pool.submit(_reap, child.proc)
        # process_group=0: each child leads its own group, so its pid is the group's id
        if child.proc.returncode is None or _group_alive(child.proc.pid):
            stopped.append(child)
    groups = [child.proc.pid for child in stopped if child.proc is not None]
    for pgid in groups:
        _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _GRACE_S
    # a KeyboardInterrupt raised another way than a handled signal counts as a second one
    with contextlib.suppress(KeyboardInterrupt):
        while (
            time.monotonic() < deadline
            and flag.count < 2
            and any(_group_alive(pgid) for pgid in groups)
        ):
            time.sleep(_POLL_S)
    for pgid in groups:
        if _group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)
    _wait_all([child.reaped for child in children if child.reaped is not None])
    return {child.image for child in stopped}


def run_children(argvs: Mapping[str, list[str]], *, logs: Path) -> list[ChildResult]:
    """Run one child per image at once, wait for all, return one result per image (C1).

    ``argvs`` maps each image to its child's command line (:func:`assemble_argv`);
    ``logs`` is the build run's own directory, where each child's stdout and stderr go to
    ``<image>.log`` (R1.5). A failing child never stops a sibling (R2.1) and nothing is
    cleaned (R2.3). Results follow ``argvs``' order: ``exit_code`` the process's own
    (:data:`NOT_STARTED` when it never started), ``duration_s`` the parent's clock,
    ``run_id`` and ``isos`` from the file at the argv's last ``--result-file`` (R2.5).

    On SIGINT, SIGTERM or SIGHUP -- caught only when called on the main thread, the
    previous handlers restored on return -- no further child starts, every started
    child's process group that still has a member is stopped, and :class:`Interrupted`
    is raised with every image's result, the stopped and unstarted ones carrying
    ``error="interrupted"``, and the signal (R2.6). A ``KeyboardInterrupt`` raised
    another way takes the same path as SIGINT.
    """
    children = [_Child(image, list(argv), logs / f"{image}.log") for image, argv in argvs.items()]
    if not children:
        return []
    flag = _StopFlag()
    stopped: set[str] | None = None
    with (
        _catching_stop_signals(flag),
        # one thread per child, only to block on its wait(); the children do the work
        ThreadPoolExecutor(max_workers=len(children), thread_name_prefix="child-wait") as pool,
    ):
        try:
            for child in children:
                if flag.requested:
                    break
                _start(child, logs)
                if child.proc is not None:
                    child.reaped = pool.submit(_reap, child.proc)
            _wait_all([c.reaped for c in children if c.reaped is not None], flag=flag)
        except KeyboardInterrupt:
            flag.interrupt()
        if flag.requested:
            # inside the pool: leaving it joins threads still blocked on a running child
            stopped = _stop(children, pool, flag)
    results = [_result(child, interrupted=child.image in (stopped or ())) for child in children]
    # also a signal recorded while the pool joined its threads: every child had already
    # ended on its own then, so none is marked, but the stop is never swallowed
    if flag.requested:
        raise Interrupted(results, signum=flag.signum)
    return results
