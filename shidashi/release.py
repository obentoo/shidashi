"""``shidashi release`` -- plan, run and finish one dated release (story 030).

The core is pure (which variants, where, which child argv); the I/O around it is thin.
Each variant is built by the existing ``shidashi build`` as a child process, one at a time
(R2.1, R2.3), so the release reuses the plan across workers, the checkpoints and the boot
test without a second orchestrator.

:func:`run_build` must run on the MAIN thread for a stop signal to reach the child: only
there may it install its SIGINT, SIGTERM and SIGHUP handlers (``signal.signal``). The
library raises, it does not log -- the command reports. Signing lives in ``release_sign``;
its entry points are re-exported here.
"""

import contextlib
import datetime
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from shidashi import config, parallel, publish
from shidashi.build_outcome import BuildOutcome, ReleaseError
from shidashi.release_sign import RELEASE_KEY_FILE as RELEASE_KEY_FILE
from shidashi.release_sign import SUMS_FILE as SUMS_FILE
from shidashi.release_sign import sign_release as sign_release
from shidashi.release_sign import verify_release as verify_release
from shidashi.upstream import UpstreamStage3

#: The signals that stop a release while a child build runs (R9.1; SIGHUP: a closed
#: terminal). Each goes on to the child unless it already has its own copy.
_ROUTED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

#: The release manifest's file name in the release directory (R5.1).
MANIFEST_FILE = "release.json"


@dataclass(frozen=True)
class Variant:
    """One ``arch x init`` pair of the recipe axes."""

    arch: str
    init: str


def variants(arch: str | None, init: str | None, *, all_variants: bool) -> list[Variant]:
    """The variants to release: every ``arch x init`` of the axes on disk, sorted (R1.2),
    or the one pair (R1.1).

    Each axis is checked on its own, so a valid arch never carries an unknown init through
    and an init's name is never taken for an arch (R1.4). The usage combinations (R1.3) are
    the CLI's to refuse.
    """
    if all_variants:
        return [
            Variant(a, i)
            for a in sorted(config.available_names("arch"))
            for i in sorted(config.available_names("init"))
        ]
    if arch is None or init is None:
        raise ValueError("variants() needs both an arch and an init without all_variants")
    for axis, name in (("arch", arch), ("init", init)):
        available = config.available_names(axis)
        if name not in available:
            raise config.UnknownAxisError(axis, name, available)
    return [Variant(arch, init)]


def release_dir(root: Path, when: datetime.date) -> Path:
    """The release directory: ``root/YYYY.MM.DD`` of the start date (R1.8)."""
    return root / f"{when:%Y.%m.%d}"


def build_argv(v: Variant, out: Path, outcome: Path, passthrough: Sequence[str]) -> list[str]:
    """The child ``shidashi build`` of one variant: every image, the release directory as
    output, its outcome file (R2.1); ``passthrough`` follows in the order given (R2.2)."""
    return [
        sys.executable,
        *parallel.CHILD_COMMAND,
        "build",
        v.arch,
        v.init,
        "--images",
        "all",
        "-o",
        str(out),
        "--outcome-file",
        str(outcome),
        *passthrough,
    ]


SignalHandler = Callable[[int, FrameType | None], None]


@contextlib.contextmanager
def _routing_stop_signals(handler: SignalHandler) -> Generator[None]:
    """SIGINT, SIGTERM and SIGHUP routed to ``handler`` for the block, the previous
    handlers restored after it.

    Only the main thread may install a handler: elsewhere nothing is installed. A signal
    ignored on purpose (``nohup``) stays ignored, as ``parallel`` leaves it.
    """
    wanted: tuple[signal.Signals, ...] = ()
    if threading.current_thread() is threading.main_thread():
        wanted = tuple(s for s in _ROUTED_SIGNALS if signal.getsignal(s) is not signal.SIG_IGN)
    previous = []
    try:
        for signum in wanted:
            previous.append((signum, signal.signal(signum, handler)))
        yield
    finally:
        for caught, old in reversed(previous):
            # None: a handler not installed from Python; the default is the closest one
            signal.signal(caught, old if old is not None else signal.SIG_DFL)


def _terminal_sent_it_to_the_child(signum: int) -> bool:
    """Whether the child already has its own copy of ``signum``: a terminal's Ctrl-C goes
    to its whole foreground process group, which the child shares with the release.

    A ``kill -INT`` of a foreground release is taken for a Ctrl-C too: nothing in a Python
    handler tells the two apart. SIGTERM never comes from a terminal. A SIGHUP always
    does, as far as the release can tell: the hung-up pty signals its foreground group and
    the shell its jobs' groups, the child's included -- and ``sudo`` relays its own to the
    release. A second copy would end the child's grace period (``parallel._stop``).
    """
    if signum == signal.SIGHUP:
        return True
    if signum != signal.SIGINT:
        return False
    try:
        tty = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
    except OSError:  # no controlling terminal: the SIGINT came from a kill
        return False
    try:
        return os.tcgetpgrp(tty) == os.getpgrp()
    except OSError:
        return False
    finally:
        os.close(tty)


def run_build(argv: Sequence[str]) -> int:
    """Run one child build with the terminal's output and no timeout (a build takes hours);
    return its exit code.

    A SIGINT, SIGTERM or SIGHUP received meanwhile is sent to the child -- a ``kill`` of
    the release pid reaches no child through the process group -- and, once the child has
    exited, raises ``KeyboardInterrupt`` (R9.1). One signal at most is forwarded: a second
    must not cut the child's own cleanup short. One the child already has is not
    (:func:`_terminal_sent_it_to_the_child`), nor does it use up the forward: a SIGTERM
    after it still goes on. The handler never waits itself: ``Popen.wait`` holds a
    non-reentrant lock while it blocks, so a wait from the handler would deadlock; the
    wait below resumes after the handler returns.
    """
    child: subprocess.Popen[bytes] | None = None
    received: list[int] = []
    forwarded: list[int] = []

    def forward(signum: int, frame: FrameType | None) -> None:
        del frame
        received.append(signum)
        if child is not None and not forwarded and not _terminal_sent_it_to_the_child(signum):
            forwarded.append(signum)
            child.send_signal(signum)

    with _routing_stop_signals(forward):
        try:
            child = subprocess.Popen(list(argv))
        except OSError as err:
            raise ReleaseError(f"cannot start the child build {argv[0]}: {err}") from err
        if received and not forwarded:
            # A stop that arrived while the child was being started: it had no copy then
            forwarded.append(received[0])
            child.send_signal(received[0])
        code = child.wait()
    if received:
        raise KeyboardInterrupt
    return code


@dataclass(frozen=True)
class VariantResult:
    """One variant's end: its build outcome, its arch's publish result (``published <dest>
    (<n> finals)``, ``skipped: ...``, ``failed: ...`` or ``not run: build failed``), the
    generation (the PKGDIR's name) and the ``{gcc, glibc, binutils}`` versions of its
    fingerprint file -- ``None`` when missing or unreadable."""

    variant: Variant
    outcome: BuildOutcome
    publish: str
    generation: str | None = None
    toolchain: dict[str, str] | None = None


@dataclass(frozen=True)
class ReleasePins:
    """The pins the release builds from: ``stage3`` is init -> ``(filename, sha512)``,
    ``gentoo`` the ``::gentoo`` pin's ``{snapshot, sha512}``, ``overlays`` name -> commit."""

    stage3_snapshot: str
    stage3: dict[str, tuple[str, str]]
    gentoo: dict[str, str]
    overlays: dict[str, str]


def _utc_stamp(when: datetime.datetime) -> str:
    """``2026-10-25T00:00:03Z``; a naive ``when`` is taken as local time."""
    return when.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _variant_entry(r: VariantResult) -> dict[str, object]:
    """One ``variants`` entry of ``release.json`` (Data Models). The build's toolchain
    *refusal* goes under ``toolchain_refusal``; ``toolchain`` holds the versions."""
    entry: dict[str, object] = {
        "arch": r.variant.arch,
        "init": r.variant.init,
        "status": r.outcome.status,
        "isos": list(r.outcome.isos),
        "generation": r.generation,
        "toolchain": dict(r.toolchain) if r.toolchain is not None else None,
        "publish": r.publish,
    }
    if r.outcome.toolchain is not None:
        entry["toolchain_refusal"] = r.outcome.toolchain.model_dump(mode="json")
    if r.outcome.status != "ok":
        # why it failed: a child killed before its outcome file has nothing else (R3.7)
        entry["exit_code"] = r.outcome.exit_code
        entry["message"] = r.outcome.message
    return entry


def _kept_from(earlier: Mapping[str, object], released: set[tuple[str, str]]) -> list[object]:
    """The earlier manifest's variant entries this run did not release again (R9.3)."""
    entries = earlier.get("variants")
    if not isinstance(entries, list):
        return []
    return [
        e
        for e in entries
        if not (isinstance(e, dict) and (e.get("arch"), e.get("init")) in released)
    ]


def _kept_inits(
    earlier: Mapping[str, object], path: tuple[str, ...], released: set[str]
) -> dict[str, object]:
    """The per-init entries under ``earlier[path]`` of the inits this run did not release (R9.3).

    ``snapshot`` shares ``pins.stage3`` with the init keys but describes this run, so it is
    never kept.
    """
    section: object = earlier
    for key in path:
        section = section.get(key) if isinstance(section, dict) else None
    if not isinstance(section, dict):
        return {}
    return {
        str(init): entry
        for init, entry in section.items()
        if init != "snapshot" and init not in released and isinstance(entry, dict)
    }


def _variant_key(entry: object) -> tuple[str, str]:
    if isinstance(entry, dict):
        return (str(entry.get("arch", "")), str(entry.get("init", "")))
    return ("", "")


def manifest(
    results: Sequence[VariantResult],
    *,
    pins: ReleasePins,
    upstream: Sequence[UpstreamStage3],
    repo: Mapping[str, object],
    started: datetime.datetime,
    finished: datetime.datetime,
    earlier: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """The ``release.json`` body (R5.1, design.md "Data Models"). Pure.

    ``repo`` is ``audit.repo_state()``; only its ``commit`` and ``dirty`` are kept.
    ``earlier`` is the directory's previous ``release.json`` (R9.3): its variants this
    run did not release are kept, the others replaced, and its ``started`` stays -- the
    release began then. ``pins.stage3[init]`` and ``upstream_stage3[init]`` of every init
    this run did not release are kept too; ``snapshot``, ``gentoo`` and ``overlays`` are
    this run's. ``date`` is this run's start date, the directory's name.
    """
    entries: list[object] = [_variant_entry(r) for r in results]
    start = _utc_stamp(started)
    stage3: dict[str, object] = {"snapshot": pins.stage3_snapshot}
    upstream_stage3: dict[str, object] = {}
    if earlier is not None:
        released = {(r.variant.arch, r.variant.init) for r in results}
        entries.extend(_kept_from(earlier, released))
        if isinstance(earlier.get("started"), str):
            start = str(earlier["started"])
        # A rerun's pins and upstream list hold only the inits it released (release_cli
        # builds them per released init): the others keep the earlier run's entries.
        inits = {r.variant.init for r in results}
        stage3.update(_kept_inits(earlier, ("pins", "stage3"), inits))
        upstream_stage3.update(_kept_inits(earlier, ("upstream_stage3",), inits))
    for init, (filename, sha512) in sorted(pins.stage3.items()):
        stage3[init] = {"filename": filename, "sha512": sha512}
    upstream_stage3.update({u.init: {"latest": u.latest, "note": u.note} for u in upstream})
    return {
        "date": started.astimezone(datetime.UTC).date().isoformat(),
        "started": start,
        "finished": _utc_stamp(finished),
        "shidashi": {"commit": repo.get("commit"), "dirty": repo.get("dirty")},
        "pins": {"stage3": stage3, "gentoo": dict(pins.gentoo), "overlays": dict(pins.overlays)},
        "upstream_stage3": upstream_stage3,
        "variants": sorted(entries, key=_variant_key),
    }


def write_manifest(directory: Path, body: Mapping[str, object]) -> Path:
    """Write ``directory/release.json`` atomically (R5.3), then set its line in
    ``SHA256SUMS`` (R5.2). I/O.

    A write or rename that fails raises :class:`ReleaseError` naming the file, leaves no
    temporary file and the previous ``release.json`` whole. A ``SHA256SUMS`` that cannot be
    read raises ``publish.PublishError`` and stays as it was: the release then must not sign.
    """
    path = directory / MANIFEST_FILE
    data = (json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=f".{MANIFEST_FILE}.", suffix=".tmp", delete=False
        ) as handle:
            tmp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # NamedTemporaryFile creates 0600; the manifest is published like every other file
        # of the directory (gpgsign._PUBLISHED_MODE) — a non-root `sha256sum -c` must read it
        tmp.chmod(0o644)
        os.replace(tmp, path)
    except BaseException as err:
        # BaseException: a Ctrl-C or SIGTERM mid-write must not leave the temp file either
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()
        if isinstance(err, OSError):
            raise ReleaseError(f"{path}: cannot write the release manifest: {err}") from err
        raise
    publish.update_sha256sums(directory, {MANIFEST_FILE: publish.sha256_of(path)})
    return path


def read_earlier(directory: Path) -> dict[str, object] | None:
    """The directory's previous ``release.json``; ``None`` when there is none. I/O.

    An unreadable file, one that is not JSON, or JSON that is not an object raises
    :class:`ReleaseError` naming it; the file is left as it is (the release stops before
    signing, so nothing is lost).
    """
    path = directory / MANIFEST_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise ReleaseError(f"{path}: cannot read the earlier release manifest: {err}") from err
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ReleaseError(f"{path}: the earlier release manifest is not JSON: {err}") from err
    if not isinstance(body, dict):
        raise ReleaseError(f"{path}: the earlier release manifest is not a JSON object")
    return body


def _variant_line(r: VariantResult) -> str:
    """``<arch>/<init>: <status> — <ISOs or reason>; publish: <result>`` (R8.1, R8.2)."""
    o = r.outcome
    if o.status == "ok":
        detail = f"{len(o.isos)} ISOs"
    elif o.status == "images-failed":
        detail = ", ".join(o.failed_images)
    elif o.status == "toolchain-refused" and o.toolchain is not None:
        refusal = o.toolchain
        detail = f"{', '.join(refusal.differences)}; successor PKGDIR {refusal.successor}"
    else:
        detail = o.message
    return f"{r.variant.arch}/{r.variant.init}: {o.status} — {detail}; publish: {r.publish}"


def _upstream_line(u: UpstreamStage3) -> str | None:
    """A newer or unknown upstream stage3 for ``u.init``; ``None`` when it is not news."""
    if u.latest is None:
        return f"{u.init}: upstream stage3 unknown: {u.note}"
    if u.newer:
        return f"{u.init}: upstream stage3 {u.latest} is newer than the pin {u.pinned}"
    return None


def summary(
    results: Sequence[VariantResult],
    upstream: Sequence[UpstreamStage3],
    *,
    signed: bool,
    verify_failures: Sequence[str],
) -> tuple[list[str], int]:
    """The release's summary lines and its exit code. Pure.

    One line per variant, one per init whose upstream is newer (R7.2) or unknown (R7.4),
    then the signing line. Exit 0 only when every variant is ``ok``, every publish was
    ``published`` or ``skipped:`` (another host owns that arch), and the release is signed
    with every signature verified (R8.3); 1 otherwise (R8.4). An upstream is only reported.
    """
    lines = [_variant_line(r) for r in results]
    lines.extend(line for u in upstream if (line := _upstream_line(u)) is not None)
    if verify_failures:
        lines.append(f"signing: failed — {'; '.join(verify_failures)}")
    elif not signed:
        lines.append(f"signing: failed — {SUMS_FILE} left unattested")
    else:
        lines.append(f"signing: verified — {SUMS_FILE}.asc")
    good = (
        all(r.outcome.status == "ok" for r in results)
        and all(r.publish.startswith(("published", "skipped:")) for r in results)
        and signed
        and not verify_failures
    )
    return lines, 0 if good else 1
