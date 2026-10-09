"""The audit trail of a run: every step, command, metric and artifact, as data.

A factory or assemble run writes one directory, ``<runs_dir>/<run id>/``:

- ``events.jsonl``: one JSON object per line, in the order things happened --
  the start and end of every step (duration, status, the CPU and memory of the
  processes it ran, the free disk before and after), every command run in a
  container (argv with secrets masked, exit code, duration), metrics, inputs and
  artifacts. Written as it happens: a killed run still leaves its trail.
- ``manifest.json``: the summary, rebuilt from the events when the run closes --
  inputs (repository commit and whether the tree was dirty, pins, fingerprint),
  every step with its timing, every artifact with its SHA-256 and size, metrics.
- ``report.md``: the same, for people.
- ``packages-<label>.json``: what an emerge left installed -- version, size, USE,
  and how long each package took to merge, read from Portage's own emerge.log.

The run in force is a context variable, so deep code (the container, a phase)
records without being handed the run; outside a run, :func:`current` returns a
recorder that drops everything, which keeps library use and tests unchanged.
"""

import contextlib
import contextvars
import datetime
import hashlib
import json
import os
import re
import resource
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from pathlib import Path
from typing import Any

#: The environment variable and flag names whose VALUES never reach a log.
_SECRET_NAME = re.compile(r"(PASS|PASSWD|PASSWORD|TOKEN|SECRET|CREDENTIAL|PRIVATE|_KEY$)", re.I)

#: ``emerge.log`` lines: ``<epoch>:  >>> emerge (3 of 9) cat/pkg-1.0 to /`` and
#: ``<epoch>:  ::: completed emerge (3 of 9) cat/pkg-1.0 to /``.
_EMERGE_START = re.compile(r"^(\d+):\s+>>> emerge \(\d+ of \d+\) (\S+) to (\S+)")
_EMERGE_DONE = re.compile(r"^(\d+):\s+::: completed emerge \(\d+ of \d+\) (\S+) to (\S+)")

_REPO = Path(__file__).resolve().parent.parent


def redact_argv(argv: Sequence[str]) -> list[str]:
    """``argv`` with the value of every secret-looking ``NAME=value`` masked. Pure.

    Covers ``env NAME=value`` prefixes and ``--name=value`` flags alike.
    """
    out: list[str] = []
    for arg in argv:
        name, sep, _ = arg.partition("=")
        if sep and _SECRET_NAME.search(name.lstrip("-")):
            out.append(f"{name}=***")
        else:
            out.append(arg)
    return out


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _children_usage() -> tuple[float, float, int]:
    """CPU user/system seconds and peak RSS (KiB) of every waited-for descendant."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime, usage.ru_stime, usage.ru_maxrss


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """The SHA-256 of ``path``, read in 1 MiB chunks. I/O."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def repo_state(repo: Path = _REPO) -> dict[str, Any]:
    """The repository commit the run is built from, and whether the tree was dirty."""

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
            )
        except OSError, subprocess.CalledProcessError:
            return None
        return done.stdout.strip()

    status = git("status", "--porcelain")
    return {
        "commit": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status) if status is not None else None,
        "dirty_files": status.splitlines() if status else [],
    }


class Step:
    """A step in progress; :meth:`add` attaches results to its end event.

    ``path`` is its place in the trail (``stage:gnome/stale-binpkgs``): what
    :meth:`Recorder.amend` names to add results once the step has ended.
    """

    def __init__(self, name: str, path: str = "") -> None:
        self.name = name
        self.path = path
        self.fields: dict[str, Any] = {}

    def add(self, **fields: Any) -> None:
        self.fields.update(fields)


class Recorder:
    """Records nothing: what :func:`current` returns outside a run."""

    run_id = ""
    root: Path | None = None

    def event(self, kind: str, **fields: Any) -> None:
        del kind, fields

    @contextlib.contextmanager
    def step(self, name: str, **fields: Any) -> Generator[Step]:
        del fields
        yield Step(name)

    def amend(self, path: str, **fields: Any) -> None:
        """Add results to a step that has already ended (``path`` of its :class:`Step`).

        For what a step decides and a later one carries out: the factory's
        ``stale-binpkgs`` quarantines its binpkgs after the stage's emerge.
        """
        self.event("step.amend", target=path, **fields)

    def command(self, argv: Sequence[str], **fields: Any) -> None:
        del argv, fields

    def metric(self, name: str, value: float | int, unit: str = "", **fields: Any) -> None:
        del name, value, unit, fields

    def input(self, name: str, value: Any) -> None:
        del name, value

    def artifact(self, path: Path, *, role: str, digest: bool = True) -> None:
        del path, role, digest

    def attach(self, name: str, data: Any) -> Path | None:
        del name, data
        return None


class Run(Recorder):
    """One audited run, writing ``events.jsonl`` under ``root`` as it goes."""

    def __init__(
        self,
        root: Path,
        *,
        command: str,
        argv: Sequence[str],
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime.datetime] = _utc_now,
        usage: Callable[[], tuple[float, float, int]] = _children_usage,
        disk: Path | None = None,
    ) -> None:
        self.root = root
        self._dir = root
        self.run_id = root.name
        self.command_name = command
        self._clock = clock
        self._wall = wall
        self._usage = usage
        self._disk = disk
        self._t0 = clock()
        self._stack: list[str] = []
        root.mkdir(parents=True, exist_ok=True)
        self._events = (root / "events.jsonl").open("a", encoding="utf-8")
        self.event("run.start", command=command, argv=redact_argv(argv), pid=os.getpid())

    @property
    def path(self) -> Path:
        """The run's directory."""
        return self._dir

    # --- writing -----------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        record = {
            "ts": self._wall().isoformat(timespec="milliseconds"),
            "t": round(self._clock() - self._t0, 3),
            "run": self.run_id,
            "kind": kind,
            "step": "/".join(self._stack),
            **fields,
        }
        self._events.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        self._events.flush()

    @contextlib.contextmanager
    def step(self, name: str, **fields: Any) -> Generator[Step]:
        """Time a step; nested steps are recorded under their parent's path."""
        self._stack.append(name)
        handle = Step(name, "/".join(self._stack))
        start = self._clock()
        cpu_u, cpu_s, _ = self._usage()
        free = _free_bytes(self._disk) if self._disk is not None else None
        self.event("step.start", **fields)
        status, error = "ok", None
        try:
            yield handle
        except BaseException as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            end_u, end_s, rss = self._usage()
            end: dict[str, Any] = {
                "status": status,
                "duration_s": round(self._clock() - start, 3),
                "cpu_user_s": round(end_u - cpu_u, 3),
                "cpu_sys_s": round(end_s - cpu_s, 3),
                "children_max_rss_kib": rss,
            }
            if free is not None and self._disk is not None:
                after = _free_bytes(self._disk)
                if after is not None:
                    end["disk_used_bytes"] = free - after
            if error is not None:
                end["error"] = error[:2000]
            self.event("step.end", **end, **handle.fields)
            self._stack.pop()

    def command(self, argv: Sequence[str], **fields: Any) -> None:
        self.event("command", argv=redact_argv(argv), **fields)

    def metric(self, name: str, value: float | int, unit: str = "", **fields: Any) -> None:
        self.event("metric", name=name, value=value, unit=unit, **fields)

    def input(self, name: str, value: Any) -> None:
        self.event("input", name=name, value=value)

    def artifact(self, path: Path, *, role: str, digest: bool = True) -> None:
        fields: dict[str, Any] = {"path": str(path), "role": role}
        if path.is_file():
            fields["size_bytes"] = path.stat().st_size
            if digest:
                fields["sha256"] = sha256_file(path)
        self.event("artifact", **fields)

    def attach(self, name: str, data: Any) -> Path:
        """Write ``data`` as ``<name>.json`` beside the events; record where."""
        path = self._dir / f"{name}.json"
        path.write_text(json.dumps(data, indent=1, default=str, ensure_ascii=False) + "\n")
        self.event("attachment", name=name, path=path.name)
        return path

    # --- closing -----------------------------------------------------------------

    def close(self, status: str, error: str | None = None) -> None:
        end: dict[str, Any] = {"status": status, "duration_s": round(self._clock() - self._t0, 3)}
        if error is not None:
            end["error"] = error[:2000]
        self.event("run.end", **end)
        self._events.close()
        manifest = build_manifest(read_events(self._dir / "events.jsonl"))
        (self._dir / "manifest.json").write_text(
            json.dumps(manifest, indent=1, default=str, ensure_ascii=False) + "\n"
        )
        (self._dir / "report.md").write_text(render_report(manifest))


_CURRENT: contextvars.ContextVar[Recorder | None] = contextvars.ContextVar(
    "shidashi_audit_run", default=None
)
_NULL = Recorder()


def current() -> Recorder:
    """The run in force, or a recorder that drops everything."""
    return _CURRENT.get() or _NULL


@contextlib.contextmanager
def recording(recorder: Recorder) -> Generator[Recorder]:
    """Make ``recorder`` :func:`current` for the block.

    For a recorder that wraps the run in force -- :class:`shidashi.progress.Observed`
    shows the steps on the terminal and forwards everything to the run.
    """
    token = _CURRENT.set(recorder)
    try:
        yield recorder
    finally:
        _CURRENT.reset(token)


def new_run_id(now: datetime.datetime | None = None) -> str:
    """``20260930T014500Z-<6 hex>``: sortable, and unique within a second."""
    stamp = (now or _utc_now()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


@contextlib.contextmanager
def run(
    runs_dir: Path,
    *,
    command: str,
    argv: Sequence[str],
    inputs: Mapping[str, Any] | None = None,
    disk: Path | None = None,
) -> Generator[Run]:
    """Open a run for the duration of the block and make it :func:`current`.

    Records the repository state and ``inputs`` first. The run closes with the
    status of the block -- ``ok``, ``error`` (the exception is recorded and
    re-raised) or ``interrupted`` (Ctrl+C) -- and writes the manifest and report.
    """
    recorder = Run(runs_dir / new_run_id(), command=command, argv=argv, disk=disk)
    token = _CURRENT.set(recorder)
    recorder.input("repository", repo_state())
    for name, value in (inputs or {}).items():
        recorder.input(name, value)
    try:
        yield recorder
    except KeyboardInterrupt:
        recorder.close("interrupted")
        raise
    except BaseException as exc:
        recorder.close("error", f"{type(exc).__name__}: {exc}")
        raise
    else:
        recorder.close("ok")
    finally:
        _CURRENT.reset(token)


# --- reading and summarising ---------------------------------------------------------


def read_events(path: Path) -> list[dict[str, Any]]:
    """The events of a run, skipping a truncated last line (a killed run). I/O."""
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def build_manifest(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The run's summary: inputs, steps with timings, artifacts, metrics. Pure."""
    manifest: dict[str, Any] = {
        "run": None,
        "command": None,
        "argv": None,
        "started": None,
        "ended": None,
        "status": "unfinished",
        "duration_s": None,
        "inputs": {},
        "steps": [],
        "commands": {"count": 0, "failed": 0, "duration_s": 0.0},
        "artifacts": [],
        "metrics": [],
        "attachments": [],
    }
    starts: dict[str, Mapping[str, Any]] = {}
    for event in events:
        kind = event.get("kind")
        if kind == "run.start":
            manifest.update(
                run=event.get("run"),
                command=event.get("command"),
                argv=event.get("argv"),
                started=event.get("ts"),
            )
        elif kind == "run.end":
            manifest.update(
                ended=event.get("ts"),
                status=event.get("status"),
                duration_s=event.get("duration_s"),
            )
            if "error" in event:
                manifest["error"] = event["error"]
        elif kind == "input":
            manifest["inputs"][event.get("name")] = event.get("value")
        elif kind == "step.start":
            starts[str(event.get("step"))] = event
        elif kind == "step.end":
            path = str(event.get("step"))
            begun = starts.pop(path, {})
            entry = {
                "step": path,
                "started": begun.get("ts"),
                "ended": event.get("ts"),
                **{k: v for k, v in event.items() if k not in _ENVELOPE},
            }
            manifest["steps"].append(entry)
        elif kind == "step.amend":
            target = event.get("target")
            ended = [s for s in manifest["steps"] if s["step"] == target]
            if not ended:  # kept, not dropped: the step it names never ended
                orphan = {k: v for k, v in event.items() if k not in _ENVELOPE}
                manifest.setdefault("amendments", []).append(orphan)
            else:
                for key, value in event.items():
                    if key in _ENVELOPE or key == "target":
                        continue
                    previous = ended[-1].get(key)
                    if isinstance(previous, list) and isinstance(value, list):
                        ended[-1][key] = previous + value
                    else:
                        ended[-1][key] = value
        elif kind == "command":
            manifest["commands"]["count"] += 1
            if event.get("exit_code") not in (0, None):
                manifest["commands"]["failed"] += 1
            manifest["commands"]["duration_s"] += float(event.get("duration_s") or 0)
        elif kind == "artifact":
            manifest["artifacts"].append({k: v for k, v in event.items() if k not in _ENVELOPE})
        elif kind == "metric":
            fields = {k: v for k, v in event.items() if k not in _ENVELOPE}
            manifest["metrics"].append({"step": event.get("step"), **fields})
        elif kind == "attachment":
            manifest["attachments"].append(event.get("path"))
    manifest["commands"]["duration_s"] = round(manifest["commands"]["duration_s"], 3)
    # a step that started and never ended: the run died inside it
    for path, begun in starts.items():
        manifest["steps"].append({"step": path, "started": begun.get("ts"), "status": "unfinished"})
    return manifest


_ENVELOPE = frozenset({"ts", "t", "run", "kind", "step"})


def _duration(seconds: object) -> str:
    if not isinstance(seconds, int | float):
        return "-"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"


def _size(value: object) -> str:
    if not isinstance(value, int | float):
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def render_report(manifest: Mapping[str, Any]) -> str:
    """The manifest as Markdown: what ran, how long each step took, what came out."""
    lines = [
        f"# shidashi {manifest.get('command')} -- run {manifest.get('run')}",
        "",
        f"- **Status:** {manifest.get('status')}",
        f"- **Started:** {manifest.get('started')}",
        f"- **Ended:** {manifest.get('ended')}",
        f"- **Duration:** {_duration(manifest.get('duration_s'))}",
        f"- **Command line:** `{' '.join(manifest.get('argv') or [])}`",
    ]
    if manifest.get("error"):
        lines.append(f"- **Error:** `{manifest['error']}`")
    commands = manifest.get("commands", {})
    lines.append(
        f"- **Container commands:** {commands.get('count', 0)} "
        f"({commands.get('failed', 0)} failed), {_duration(commands.get('duration_s'))}"
    )
    lines += ["", "## Inputs", ""]
    for name, value in manifest.get("inputs", {}).items():
        lines.append(f"- **{name}:** `{json.dumps(value, default=str, ensure_ascii=False)}`")
    lines += [
        "",
        "## Steps",
        "",
        "| step | status | duration | CPU user | CPU sys | disk used |",
        "|---|---|---|---|---|---|",
    ]
    for step in manifest.get("steps", []):
        lines.append(
            f"| `{step.get('step')}` | {step.get('status')} | {_duration(step.get('duration_s'))}"
            f" | {_duration(step.get('cpu_user_s'))} | {_duration(step.get('cpu_sys_s'))}"
            f" | {_size(step.get('disk_used_bytes'))} |"
        )
    if manifest.get("artifacts"):
        lines += ["", "## Artifacts", "", "| role | path | size | sha256 |", "|---|---|---|---|"]
        for art in manifest["artifacts"]:
            lines.append(
                f"| {art.get('role')} | `{art.get('path')}` | {_size(art.get('size_bytes'))}"
                f" | `{art.get('sha256', '-')}` |"
            )
    if manifest.get("metrics"):
        lines += ["", "## Metrics", "", "| step | metric | value |", "|---|---|---|"]
        for metric in manifest["metrics"]:
            lines.append(
                f"| `{metric.get('step')}` | {metric.get('name')}"
                f" | {metric.get('value')} {metric.get('unit', '')} |"
            )
    if manifest.get("attachments"):
        lines += ["", "## Attachments", ""]
        lines += [f"- `{name}`" for name in manifest["attachments"]]
    return "\n".join(lines) + "\n"


# --- what an emerge left behind ------------------------------------------------------


def parse_emerge_log(
    text: str, *, since: int | None = None, until: int | None = None
) -> dict[str, dict[str, int]]:
    """Per package, when its merge started and ended (epoch s) and how long it took.

    Only merges that STARTED inside ``[since, until]`` count, so one emerge.log
    that grows across stages is read one stage at a time. A merge that never
    completed has no ``duration_s``. Pure.
    """
    merges: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        if match := _EMERGE_START.match(line):
            when = int(match.group(1))
            if (since is not None and when < since) or (until is not None and when > until):
                continue
            merges[match.group(2)] = {"started": when}
        elif (match := _EMERGE_DONE.match(line)) and match.group(2) in merges:
            entry = merges[match.group(2)]
            entry["ended"] = int(match.group(1))
            entry["duration_s"] = entry["ended"] - entry["started"]
    return merges


def _vdb_field(entry: Path, name: str) -> str | None:
    try:
        return (entry / name).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def harvest_packages(
    rootfs: Path,
    *,
    merges: Mapping[str, Mapping[str, int]] | None = None,
    built: Sequence[str] = (),
    reused: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Every installed package of ``rootfs``: version, slot, size, USE, repository,
    and -- when this run merged it -- whether it was built or taken from a binpkg
    and how long the merge took. I/O (reads the vdb)."""
    vdb = rootfs / "var" / "db" / "pkg"
    merges = merges or {}
    built_set, reused_set = set(built), set(reused)
    packages: list[dict[str, Any]] = []
    if not vdb.is_dir():
        return packages
    for category in sorted(p for p in vdb.iterdir() if p.is_dir()):
        for entry in sorted(p for p in category.iterdir() if p.is_dir()):
            atom = f"{category.name}/{entry.name}"
            size = _vdb_field(entry, "SIZE")
            # USE also carries the profile's implicit flags (amd64, elibc_glibc...):
            # keep the package's own, those in its IUSE
            iuse = {f.lstrip("+-") for f in (_vdb_field(entry, "IUSE") or "").split()}
            enabled = set((_vdb_field(entry, "USE") or "").split())
            item: dict[str, Any] = {
                "atom": atom,
                "slot": _vdb_field(entry, "SLOT"),
                "repository": _vdb_field(entry, "repository"),
                "size_bytes": int(size) if size and size.isdigit() else None,
                "use": sorted(enabled & iuse),
            }
            if atom in built_set:
                item["source"] = "built"
            elif atom in reused_set:
                item["source"] = "binpkg"
            if atom in merges:
                item["merge"] = dict(merges[atom])
            packages.append(item)
    return packages
