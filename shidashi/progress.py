"""Progress on the terminal: what a run is doing, pacman-style, while it runs.

A build is hours of emerge in a container whose output goes to a log file
(:class:`shidashi.container.Container`). This module turns that into something
a person can follow: one line per finished step, one per merged package, one
per download and -- on a terminal -- a footer redrawn in place with what is
running right now, a bar per download and a bar over the emerge's packages.

It listens to what the run already does, so the build code barely knows it:

- the audit steps (:mod:`shidashi.audit`) -- the vocabulary of a run (``seed``,
  ``bootstrap › gcc``, ``stages › stage:base › emerge-stage``), seen through
  :class:`Observed`, a recorder that forwards everything to the run in force;
- the lines of every container command, where emerge announces each package
  (``>>> Emerging (3 of 120) cat/pkg-1.0::gentoo``) -- :class:`EmergeTracker`;
- the downloads, copied through :func:`copy`, and the slow silent work
  (extracting a tarball, writing a fork point), announced with ``note``.

The reporter in force is a context variable, like the audit run: outside
:func:`reporting`, :func:`current` returns one that shows nothing, which keeps
library use and tests unchanged. The raw output still goes to the log file;
``verbose`` also echoes it here.
"""

import contextlib
import contextvars
import dataclasses
import re
import threading
import time
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import IO, Any, Protocol

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from shidashi import audit

__all__ = [
    "EmergeTracker",
    "MergeEvent",
    "Observed",
    "Reporter",
    "View",
    "command_label",
    "copy",
    "current",
    "duration",
    "elapsed",
    "reporting",
    "size",
]

# --- formatting (PURE) -----------------------------------------------------------


def duration(seconds: float) -> str:
    """A finished step's time: ``0.4s``, ``14s``, ``3m12s``, ``2h05m``. Pure."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    whole = int(seconds)
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s" if whole >= 60 else f"{whole}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def elapsed(seconds: float) -> str:
    """A running step's time, as a ticking clock: ``0:14``, ``12:03``, ``1:02:13``. Pure."""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def size(n: float) -> str:
    """A byte count in binary units: ``512 B``, ``1.2 KiB``, ``288.0 MiB``. Pure."""
    unit = "B"
    for bigger in ("KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            break
        n, unit = n / 1024, bigger
    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"


def command_label(argv: Sequence[str]) -> str:
    """``argv`` as a person reads it: without the ``env K=V ...`` prefix. Pure.

    The bootstrap passes its environment that way (nspawn does not forward the
    host's), and ``env FEATURES=... MAKEOPTS=... emerge gcc`` hides the command.
    """
    args = list(argv)
    if args[:1] == ["env"]:
        args = args[1:]
        while args and "=" in args[0] and not args[0].startswith("-"):
            args = args[1:]
    return " ".join(args)


# --- emerge's own progress lines (PURE) ------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

#: Portage's status lines: ``>>> Emerging (3 of 120) cat/pkg-1.0::gentoo``, ``>>>
#: Emerging binary (...)``, ``>>> Installing (...)``, ``>>> Completed (...)``.
#: Printed whatever --jobs is; with more than one job they are all that reaches
#: the stream, the build output going to each package's own log.
_STATUS = re.compile(
    r"^>>> (?P<verb>Emerging binary|Emerging|Installing|Completed) "
    r"\((?P<n>\d+) of (?P<total>\d+)\) (?P<cpv>[^\s:]+)"
)
#: ``>>> Failed to emerge cat/pkg-1.0, Log file:`` -- also ``install`` and ``fetch``.
_FAILED = re.compile(r"^>>> Failed to (?P<action>\w+) (?P<cpv>[^\s,:]+)")
#: ``>>> Downloading 'https://.../foo-1.0.tar.gz'`` -- in the stream with one job only.
_DOWNLOADING = re.compile(r"^>>> Downloading '(?P<url>[^']+)'")


@dataclasses.dataclass
class _Merge:
    """A package emerge is working on."""

    n: int
    cpv: str
    binary: bool
    start: float
    state: str


@dataclasses.dataclass(frozen=True)
class MergeEvent:
    """A package started (``start``), merged (``done``) or failed (``failed``)."""

    kind: str
    n: int
    total: int
    cpv: str
    binary: bool = False
    seconds: float = 0.0
    #: what failed: ``emerge``, ``install``, ``fetch``
    action: str = ""


class EmergeTracker:
    """What one emerge is doing, read from its output line by line. Pure (clock injected).

    ``total`` is the size of the merge list, ``done`` how many have merged, and
    ``active`` the packages in flight in the order they started -- several at
    once under ``--jobs``. ``(n of total)`` is emerge's merge ORDER, not a count:
    with parallel jobs package 7 can finish before package 5, so the bar counts
    ``done`` and the lines keep emerge's numbers.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.total = 0
        self.done = 0
        self.active: dict[str, _Merge] = {}
        self._clock = clock

    def feed(self, line: str) -> MergeEvent | None:
        """Take one output line; return the package event it announces, if any."""
        text = _ANSI.sub("", line).strip()
        if match := _STATUS.match(text):
            n, total, cpv = int(match["n"]), int(match["total"]), match["cpv"]
            self.total = max(self.total, total)
            verb = match["verb"]
            if verb.startswith("Emerging"):
                binary = verb == "Emerging binary"
                state = "binpkg" if binary else "building"
                self.active[cpv] = _Merge(n, cpv, binary, self._clock(), state)
                return MergeEvent("start", n, total, cpv, binary=binary)
            if verb == "Installing":
                if cpv in self.active:
                    self.active[cpv].state = "installing"
                return None
            merge = self.active.pop(cpv, None)
            self.done += 1
            took = self._clock() - merge.start if merge else 0.0
            binary = merge.binary if merge else False
            return MergeEvent("done", n, total, cpv, binary=binary, seconds=took)
        if match := _FAILED.match(text):
            merge = self.active.pop(match["cpv"], None)
            return MergeEvent(
                "failed",
                merge.n if merge else 0,
                self.total,
                match["cpv"],
                binary=merge.binary if merge else False,
                seconds=self._clock() - merge.start if merge else 0.0,
                action=match["action"],
            )
        if (match := _DOWNLOADING.match(text)) and len(self.active) == 1:
            # one job: the download belongs to the only package in flight
            name = match["url"].rstrip("/").rsplit("/", 1)[-1]
            next(iter(self.active.values())).state = f"fetching {name}"
        return None


# --- the reporters ---------------------------------------------------------------


class Reporter:
    """Shows nothing: what :func:`current` returns outside :func:`reporting`."""

    def step_started(self, name: str) -> None:
        del name

    def step_ended(self, name: str, *, ok: bool) -> None:
        del name, ok

    def command_started(self, argv: Sequence[str], *, log: Path | None = None) -> None:
        del argv, log

    def output(self, line: str) -> None:
        del line

    def command_ended(self) -> None:
        pass

    def note(self, text: str) -> None:
        """Say what slow, silent work the current step is doing (``extracting ...``)."""
        del text

    @contextlib.contextmanager
    def transfer(self, label: str, total: int | None) -> Generator[Callable[[int], None]]:
        """A download for the block; the callable adds the bytes it moved."""
        del label, total
        yield lambda _n: None

    @contextlib.contextmanager
    def paused(self) -> Generator[None]:
        """Leave the terminal alone for the block (a prompt, an interactive shell)."""
        yield


@dataclasses.dataclass
class _Frame:
    """A step in progress; ``shown`` once its ``::`` header is on screen."""

    name: str
    start: float
    shown: bool = False


@dataclasses.dataclass
class _Transfer:
    label: str
    total: int | None
    start: float
    done: int = 0


#: Packages in flight listed in the footer; the rest are counted.
_MAX_ACTIVE = 8
_BAR_WIDTH = 30


class View(Reporter):
    """The progress of a run on ``console``.

    Lines that stay: ``✓``/``✗`` per finished step with its time, a ``::``
    header before the first line a step shows, then its downloads and packages.
    With ``live`` (a terminal), a footer redrawn in place shows the running step,
    its command or note, the downloads with a bar, the emerge's bar and the
    packages in flight; without it, the starts are lines too, so a log file of
    the run (``nohup``, CI) still says what was running.

    Only the caller's thread prints; the footer is drawn by ``rich``'s refresh
    thread from a copy of the state taken under ``_lock``. Nothing prints while
    holding ``_lock``: ``rich`` takes its own lock to print, and its refresh
    thread holds that one while it waits for ours.
    """

    def __init__(
        self,
        console: Console,
        *,
        live: bool,
        verbose: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.console = console
        self.verbose = verbose
        self._clock = clock
        self._lock = threading.Lock()
        self._frames: list[_Frame] = []
        #: a failure was shown; the steps it unwinds through stay quiet
        self._failed = False
        self._emerge: EmergeTracker | None = None
        self._command = ""
        self._note = ""
        self._transfers: list[_Transfer] = []
        self._logs: set[Path] = set()
        self._spinner = Spinner("dots", style="cyan")
        self._live = (
            Live(
                console=console,
                get_renderable=self._footer,
                transient=True,
                refresh_per_second=8,
            )
            if live
            else None
        )

    def __enter__(self) -> View:
        if self._live is not None:
            self._live.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._live is not None:
            self._live.stop()

    # --- the lines that stay ---------------------------------------------------------

    def _emit(self, lines: Sequence[Text]) -> None:
        for line in lines:
            self.console.print(line, soft_wrap=True, highlight=False)

    def _path(self) -> str:
        return " › ".join(frame.name for frame in self._frames)

    def _header(self, out: list[Text]) -> None:
        """The current step's ``::`` line, once, before the first line it shows."""
        if self._frames and not self._frames[-1].shown:
            self._frames[-1].shown = True
            out.append(Text.assemble(("::", "bold blue"), " ", (self._path(), "bold")))

    def _merge_line(self, event: MergeEvent) -> Text:
        line = Text.assemble("   ", (f"({event.n}/{event.total})", "dim"), " ", event.cpv, "  ")
        if event.kind == "start":
            line.append("binpkg…" if event.binary else "building…", "yellow")
        elif event.kind == "done":
            line.append("done (binpkg)" if event.binary else "done", "green")
            line.append(f"  {duration(event.seconds)}", "dim")
        else:
            line.append(f"failed to {event.action}", "bold red")
            line.append(f"  {duration(event.seconds)}", "dim")
        return line

    # --- Reporter --------------------------------------------------------------------

    def step_started(self, name: str) -> None:
        with self._lock:
            self._frames.append(_Frame(name, self._clock()))
            self._failed = False
            self._note = ""

    def step_ended(self, name: str, *, ok: bool) -> None:
        out: list[Text] = []
        with self._lock:
            if not self._frames:
                return
            path = self._path()
            frame = self._frames.pop()
            took = f"  {duration(self._clock() - frame.start)}"
            if ok:
                out.append(Text.assemble(("✓ ", "green"), path, (took, "dim")))
            elif not self._failed:
                self._failed = True
                out.append(Text.assemble(("✗ ", "bold red"), path, (took, "dim")))
            self._note = ""
        self._emit(out)

    def command_started(self, argv: Sequence[str], *, log: Path | None = None) -> None:
        out: list[Text] = []
        with self._lock:
            if log is not None and log not in self._logs:
                self._logs.add(log)
                out.append(Text.assemble(("log ", "dim"), str(log)))
            self._command = command_label(argv)
            self._emerge = EmergeTracker(self._clock)
            self._note = ""
            if self.verbose:
                self._header(out)
                out.append(Text(f"   $ {self._command}", style="dim"))
        self._emit(out)

    def output(self, line: str) -> None:
        out: list[Text] = []
        with self._lock:
            if self.verbose:
                self._header(out)
                out.append(Text.from_ansi(line.rstrip("\r\n"), style="dim"))
            event = self._emerge.feed(line) if self._emerge is not None else None
            # on a terminal a start lives in the footer; in a log it is a line
            if event is not None and (event.kind != "start" or self._live is None):
                self._header(out)
                out.append(self._merge_line(event))
        self._emit(out)

    def command_ended(self) -> None:
        with self._lock:
            self._command = ""
            self._emerge = None

    def note(self, text: str) -> None:
        out: list[Text] = []
        with self._lock:
            self._note = text
            if self._live is None:
                self._header(out)
                out.append(Text(f"   {text}", style="dim"))
        self._emit(out)

    @contextlib.contextmanager
    def transfer(self, label: str, total: int | None) -> Generator[Callable[[int], None]]:
        item = _Transfer(label, total, self._clock())
        out: list[Text] = []
        with self._lock:
            self._transfers.append(item)
            if self._live is None:
                self._header(out)
                known = f" ({size(total)})" if total else ""
                out.append(Text(f"   {label}  downloading{known}", style="dim"))
        self._emit(out)

        def advance(n: int) -> None:
            item.done += n  # one writer; the footer only reads it

        ok = False
        try:
            yield advance
            ok = True
        finally:
            out = []
            with self._lock:
                self._transfers.remove(item)
                self._header(out)
                took = f"  {duration(self._clock() - item.start)}"
                if ok:
                    out.append(Text.assemble("   ", label, f"  {size(item.done)}", (took, "dim")))
                else:
                    failed = f"failed after {size(item.done)}"
                    out.append(Text.assemble("   ", label, "  ", (failed, "bold red")))
            self._emit(out)

    @contextlib.contextmanager
    def paused(self) -> Generator[None]:
        if self._live is None or not self._live.is_started:
            yield
            return
        self._live.stop()
        try:
            yield
        finally:
            self._live.start()

    # --- the footer (drawn by rich's refresh thread) ---------------------------------

    def _footer(self) -> RenderableType:
        with self._lock:
            now = self._clock()
            parts: list[RenderableType] = []
            if self._frames:
                head = Text.assemble(
                    (self._path(), "bold"), (f"  {elapsed(now - self._frames[-1].start)}", "dim")
                )
                detail = self._note or (f"$ {self._command}" if self._command else "")
                if detail:
                    head.append(f"  {detail}", "dim")
                line = Table.grid(padding=(0, 1))
                line.add_column(no_wrap=True)
                line.add_column(no_wrap=True, overflow="ellipsis")
                line.add_row(self._spinner, head)
                parts.append(line)
            if self._transfers:
                parts.append(self._transfer_rows(now))
            emerge = self._emerge
            if emerge is not None and emerge.total:
                parts.append(self._emerge_rows(emerge, now))
        return Group(*parts)

    def _transfer_rows(self, now: float) -> Table:
        grid = Table.grid(padding=(0, 1))
        for item in self._transfers:
            spent = max(now - item.start, 1e-6)
            amount = f"{size(item.done)}/{size(item.total)}" if item.total else size(item.done)
            percent = f"{100 * item.done // item.total:3d}%" if item.total else ""
            grid.add_row(
                Text(f"  {item.label}", no_wrap=True, overflow="ellipsis"),
                ProgressBar(total=item.total or None, completed=item.done, width=_BAR_WIDTH),
                Text(percent),
                Text(amount, style="dim"),
                Text(f"{size(item.done / spent)}/s", style="dim"),
            )
        return grid

    def _emerge_rows(self, emerge: EmergeTracker, now: float) -> Table:
        grid = Table.grid(padding=(0, 1))
        percent = 100 * emerge.done // emerge.total
        grid.add_row(
            Text(f"  emerge {emerge.done}/{emerge.total}", style="bold"),
            ProgressBar(total=emerge.total, completed=emerge.done, width=_BAR_WIDTH),
            Text(f"{percent:3d}%"),
        )
        active = list(emerge.active.values())
        for merge in active[:_MAX_ACTIVE]:
            grid.add_row(
                Text.assemble("    ", (f"({merge.n}/{emerge.total})", "dim"), " ", merge.cpv),
                Text(f"{merge.state}…", style="yellow"),
                Text(elapsed(now - merge.start), style="dim"),
            )
        if len(active) > _MAX_ACTIVE:
            grid.add_row(Text(f"    … {len(active) - _MAX_ACTIVE} more", style="dim"))
        return grid


# --- the audit steps, seen ---------------------------------------------------------


class Observed(audit.Recorder):
    """The audit run in force, with its steps also shown by ``reporter``.

    Everything is forwarded to ``inner`` -- the trail is unchanged -- and each
    step's start and end reach the reporter as well.
    """

    def __init__(self, inner: audit.Recorder, reporter: Reporter) -> None:
        self.inner = inner
        self.reporter = reporter
        self.run_id = inner.run_id
        self.root = inner.root

    def event(self, kind: str, **fields: Any) -> None:
        self.inner.event(kind, **fields)

    @contextlib.contextmanager
    def step(self, name: str, **fields: Any) -> Generator[audit.Step]:
        with self.inner.step(name, **fields) as handle:
            self.reporter.step_started(name)
            ok = False
            try:
                yield handle
                ok = True
            finally:
                self.reporter.step_ended(name, ok=ok)

    def command(self, argv: Sequence[str], **fields: Any) -> None:
        self.inner.command(argv, **fields)

    def metric(self, name: str, value: float | int, unit: str = "", **fields: Any) -> None:
        self.inner.metric(name, value, unit, **fields)

    def input(self, name: str, value: Any) -> None:
        self.inner.input(name, value)

    def artifact(self, path: Path, *, role: str, digest: bool = True) -> None:
        self.inner.artifact(path, role=role, digest=digest)

    def attach(self, name: str, data: Any) -> Path | None:
        return self.inner.attach(name, data)


# --- the reporter in force -----------------------------------------------------------

_CURRENT: contextvars.ContextVar[Reporter | None] = contextvars.ContextVar(
    "shidashi_progress", default=None
)
_NULL = Reporter()


def current() -> Reporter:
    """The reporter in force, or one that shows nothing."""
    return _CURRENT.get() or _NULL


@contextlib.contextmanager
def reporting(console: Console, *, verbose: bool = False) -> Generator[Reporter]:
    """Show the progress of the block on ``console``.

    Live on a terminal, plain lines anywhere else (a pipe, a file, a dumb
    terminal). The view becomes :func:`current`, and the audit run in force is
    wrapped (:class:`Observed`) so that its steps are shown too -- open the
    audit run first.
    """
    view = View(console, live=console.is_terminal and not console.is_dumb_terminal, verbose=verbose)
    token = _CURRENT.set(view)
    try:
        with view, audit.recording(Observed(audit.current(), view)):
            yield view
    finally:
        _CURRENT.reset(token)


class _Readable(Protocol):
    def read(self, n: int, /) -> bytes: ...


def copy(
    source: _Readable,
    dest: IO[bytes],
    *,
    label: str,
    total: int | None = None,
    chunk: int = 1 << 20,
) -> int:
    """``shutil.copyfileobj`` that shows the transfer; returns the bytes copied.

    ``total`` (an HTTP response's ``length``) gives the bar its end; without
    it the transfer still shows its bytes and rate.
    """
    copied = 0
    with current().transfer(label, total) as advance:
        while block := source.read(chunk):
            dest.write(block)
            copied += len(block)
            advance(len(block))
    return copied
