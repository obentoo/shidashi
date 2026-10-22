"""``shidashi release``: build, publish and sign one dated release (story 030).

``cli.py`` only registers :func:`release`, so ``cli`` is imported inside the command: a
module-level import would be circular (design.md GOTCHA). Every seam the tests stand in
for is reached through its module attribute at call time -- ``release.run_build``,
``publish_pool.publish``, ``gpgsign.*``, ``upstream.check``, ``cli._cache_owner`` and
``cli._generation_pkgdir``.
"""

import contextlib
import datetime
import os
import re
import signal
import sys
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import FrameType, ModuleType
from typing import Annotated, NoReturn

import typer
from rich.markup import escape

from shidashi import (
    audit,
    binhost_cli,
    build_outcome,
    config,
    generation,
    gpgsign,
    parallel,
    publish,
    publish_pool,
    release_sign,
    seed,
    toolbox,
    tree,
    upstream,
)
from shidashi import release as release_core
from shidashi.binhost import BinhostError
from shidashi.build_outcome import BuildOutcome, ReleaseError
from shidashi.phases import FactoryError

#: A fingerprint (v4: 40 hex, v5/v6: 64 hex) or a long key ID (16 hex): what gpg's
#: ``VALIDSIG`` can be matched against (release.verify_release). An e-mail or a user ID
#: would build for hours and then fail every signature check.
_KEY_FORM = re.compile(r"[0-9A-F]{16}|[0-9A-F]{40}|[0-9A-F]{64}")
#: 128 + SIGINT: the shell's code for a run stopped by Ctrl-C (R9.1).
_INTERRUPTED_EXIT = 130


class Compression(StrEnum):
    """``build --compression``: checked here, so a typo fails before the first build."""

    zstd = "zstd"
    xz = "xz"
    both = "both"


@dataclass
class _Progress:
    """What the release has done so far: printed as it stands when it is interrupted."""

    dest: Path
    results: list[release_core.VariantResult] = field(default_factory=list)
    running: release_core.Variant | None = None
    signing: bool = False


def _die(cli: ModuleType, message: object) -> NoReturn:
    """Print ``message`` as an error and exit 1, before any build (R1)."""
    cli._err_console.print(f"[bold red]error:[/bold red] {escape(str(message))}", soft_wrap=True)
    raise typer.Exit(1)


def _signing_key(key: str) -> str:
    """``key`` as gpg's status lines spell it: upper-case hex, no spaces, no ``0x``.

    Raises ``ValueError`` for anything that is not a fingerprint or a long key ID.
    """
    compact = "".join(key.split()).upper().removeprefix("0X")
    if not _KEY_FORM.fullmatch(compact):
        raise ValueError(
            f"--key {key!r} is not a key fingerprint (40 or 64 hex digits) or a long key "
            "ID (16): the signatures are checked by fingerprint; `gpg --list-secret-keys "
            "--with-colons` shows it on its fpr line"
        )
    return compact


def _usage(arch: str | None, init: str | None, all_variants: bool) -> None:
    """R1.3: ``--all`` alone, or both axes without it."""
    if all_variants and (arch is not None or init is not None):
        raise typer.BadParameter("--all releases every variant: give no ARCH or INIT with it")
    if not all_variants and (arch is None or init is None):
        raise typer.BadParameter("give ARCH and INIT, or --all")


def _load_pins(inits: Sequence[str]) -> release_core.ReleasePins:
    """The pins the children build from, for ``release.json`` (R5.1). Raises ``SeedError``
    for a missing or broken pin: the build would stop on it, so the release does first."""
    seeds = config.seeds_dir()
    stage3 = {i: seed.load_pointer(i, seeds_dir=seeds) for i in inits}
    tree_pin = tree.load_tree_pin(seeds)
    return release_core.ReleasePins(
        stage3_snapshot=stage3[inits[0]].snapshot,
        stage3={i: (p.filename, p.sha512) for i, p in stage3.items()},
        gentoo={"snapshot": tree_pin.date, "sha512": tree_pin.sha512},
        overlays={o.name: o.commit for o in tree.load_overlay_pins(seeds)},
    )


def _passthrough(flags: Mapping[str, bool], valued: Mapping[str, object | None]) -> list[str]:
    """The options every child build gets unchanged (R2.2) -- never ``--key``."""
    argv = [flag for flag, wanted in flags.items() if wanted]
    for option, value in valued.items():
        if value is not None:
            argv += [option, str(value)]
    return argv


@contextlib.contextmanager
def _stop_signals_stop_the_release() -> Generator[None]:
    """SIGTERM and SIGHUP (a closed terminal) unwind the release like a Ctrl-C (R9.1),
    between child builds and while signing too; while a child runs, ``release.run_build``
    routes them first. Fires once -- a hangup often comes twice, ``sudo``'s relay then the
    pty's own; main thread only; a signal ignored on purpose (``nohup``) stays ignored."""
    wanted: list[signal.Signals] = []
    if threading.current_thread() is threading.main_thread():
        wanted = [
            s for s in (signal.SIGTERM, signal.SIGHUP) if signal.getsignal(s) is not signal.SIG_IGN
        ]

    def stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        for caught in wanted:
            signal.signal(caught, signal.SIG_IGN)
        raise KeyboardInterrupt

    previous = {s: signal.signal(s, stop) for s in wanted}
    try:
        yield
    finally:
        for caught, old in previous.items():
            signal.signal(caught, old if old is not None else signal.SIG_DFL)


def _upstream(
    trail: audit.Recorder, inits: Sequence[str], pinned: str, *, download: bool
) -> list[upstream.UpstreamStage3]:
    """The upstream stage3 of each released init: reported, never built (R7.1, R7.3)."""
    found = []
    for init in inits:
        with trail.step(f"upstream {init}") as step:
            try:
                got = upstream.check(init, download=download)
            except seed.SeedError as err:  # the pin changed since the preflight
                got = upstream.UpstreamStage3(init=init, pinned=pinned, latest=None, note=str(err))
            step.add(latest=got.latest, note=got.note)
        found.append(got)
    return found


def _build(
    trail: audit.Recorder, v: release_core.Variant, dest: Path, passthrough: Sequence[str]
) -> BuildOutcome:
    """One child build and how it ended (R2.1, R3.7). Last run's outcome file is removed
    first: a child killed before writing its own must not be read as a success; one that
    cannot be removed is this variant's error, never the release's end (R2.4)."""
    outcome = dest / ".outcome" / f"{v.arch}-{v.init}.json"
    with trail.step(f"build {v.arch}/{v.init}", arch=v.arch, init=v.init) as step:
        try:
            outcome.parent.mkdir(mode=release_sign.RELEASE_DIR_MODE, exist_ok=True)
            outcome.unlink(missing_ok=True)
            code = release_core.run_build(release_core.build_argv(v, dest, outcome, passthrough))
        except (ReleaseError, OSError) as err:  # the child never started
            got = _error(v, parallel.NOT_STARTED, str(err))
        else:
            try:
                got = build_outcome.load(outcome)
                if (got.arch, got.init) != (v.arch, v.init):
                    raise ReleaseError(f"{outcome}: written by {got.arch}/{got.init}")
            except ReleaseError as err:
                got = _error(v, code, f"build exited {code} without a readable outcome: {err}")
        step.add(outcome=got.status, exit_code=got.exit_code, isos=got.isos)
    return got


def _error(v: release_core.Variant, code: int, message: str) -> BuildOutcome:
    """The ``error`` outcome of a child that left none to read (R3.7)."""
    return BuildOutcome(
        arch=v.arch,
        init=v.init,
        status="error",
        exit_code=code,
        isos=[],
        failed_images=[],
        message=message,
        toolchain=None,
    )


def _publish(cli: ModuleType, trail: audit.Recorder, arch: str, init: str, key: str) -> str:
    """The signed publish pool of one arch that built (R4.1), as its result line; a
    worker-owned arch is skipped (R4.2) and a failure only recorded (R4.4)."""
    try:
        owner = cli._cache_owner(arch)
    except typer.Exit as err:  # the registry is unreadable; cli printed why
        return f"failed: cannot read the worker registry: {err.__cause__}"
    if owner is not None:
        return f"skipped: {arch} is owned by {owner}"
    try:
        with trail.step(f"publish {arch}", arch=arch) as step, ExitStack() as stack:
            pkgdir = cli._generation_pkgdir(arch, init)

            def toolbox_for(
                rw: Mapping[str, Path], file_binds: Sequence[tuple[Path, Path]]
            ) -> toolbox.Toolbox:
                return binhost_cli._toolbox_for(arch, init, stack, rw=rw, file_binds=file_binds)

            done = publish_pool.publish(
                pkgdir,
                binhost_cli._default_publish_dest(arch, pkgdir),
                key=key,
                toolbox_for=toolbox_for,
            )
            step.add(dest=str(done.dest), published=done.published)
    except (BinhostError, seed.SeedError, config.UnknownAxisError, OSError) as err:
        return f"failed: {err}"
    except Exception as err:  # R4.4: any publish failure is recorded; a recipe edited mid-run
        return f"failed: {type(err).__name__}: {' '.join(str(err).split())}"
    return f"published {done.dest} ({done.published} finals)"


def _generation(
    cli: ModuleType, v: release_core.Variant
) -> tuple[str | None, dict[str, str] | None, str | None]:
    """The generation a variant built into, its toolchain versions, and why they could
    not be read, if so; a fingerprint missing is no error (R5.1)."""
    try:
        pkgdir: Path = cli._generation_pkgdir(v.arch, v.init)
    except seed.SeedError as err:
        return None, None, f"generation of {v.arch}/{v.init}: {err}"
    try:
        return pkgdir.name, generation.recorded_toolchain(pkgdir), None
    except FactoryError as err:
        return pkgdir.name, None, str(err)


def _run(
    cli: ModuleType,
    trail: audit.Recorder,
    state: _Progress,
    variants: Sequence[release_core.Variant],
    *,
    key: str,
    pins: release_core.ReleasePins,
    started: datetime.datetime,
    passthrough: Sequence[str],
    download: bool,
) -> tuple[list[str], int]:
    """Everything after the preflight; the summary lines and the exit code."""
    dest = state.dest
    inits = sorted({v.init for v in variants})
    found = _upstream(trail, inits, pins.stage3_snapshot, download=download)
    outcomes: list[tuple[release_core.Variant, BuildOutcome]] = []
    for v in variants:  # one at a time (R2.3); a failure goes on to the next (R2.4)
        state.running = v
        outcomes.append((v, _build(trail, v, dest, passthrough)))
        state.results.append(release_core.VariantResult(v, outcomes[-1][1], "not run: interrupted"))
    state.running = None

    published: dict[str, str] = {}
    for v, outcome in outcomes:  # one publish per arch with an ok variant (R4.1, R4.3)
        if outcome.status == "ok" and v.arch not in published:
            published[v.arch] = _publish(cli, trail, v.arch, v.init, key)
    notes: list[str] = []
    results = []
    for v, outcome in outcomes:
        if outcome.status != "ok":
            results.append(release_core.VariantResult(v, outcome, "not run: build failed"))
            continue
        made_in, versions, problem = _generation(cli, v)
        if problem is not None:
            notes.append(problem)
        results.append(release_core.VariantResult(v, outcome, published[v.arch], made_in, versions))
    state.results = results

    failures: list[str] = []
    try:
        with trail.step("manifest release.json"):
            body = release_core.manifest(
                results,
                pins=pins,
                upstream=found,
                repo=audit.repo_state(),
                started=started,
                finished=datetime.datetime.now(datetime.UTC),
                earlier=release_core.read_earlier(dest),
            )
            release_core.write_manifest(dest, body)
    except (ReleaseError, publish.PublishError, OSError) as err:
        notes.append(f"{release_core.MANIFEST_FILE}: {err}; nothing signed")
        signed = False
    else:
        state.signing = True
        failures = release_sign.sign_and_verify(trail, dest, key)
        signed = not failures
    lines, code = release_core.summary(results, found, signed=signed, verify_failures=failures)
    if notes:  # a corrupt fingerprint or manifest is the release's failure too
        lines += notes
        code = max(code, 1)
    with trail.step("summary") as step:
        step.add(exit_code=code, lines=lines)
    return lines, code


def _interrupted(state: _Progress) -> None:
    """R9.1: say what was built, and when a rerun continues (R9.2): on the same UTC day
    only, since a later start names a new directory (R1.8). A stop after the signature was
    moved in finds the release signed: ``SHA256SUMS.asc`` exists only once verified (R6.6)."""
    lines, _code = release_core.summary(state.results, [], signed=False, verify_failures=[])
    for line in lines[: len(state.results)]:
        typer.echo(line)
    if state.signing and os.path.lexists(state.dest / f"{release_core.SUMS_FILE}.asc"):
        typer.echo(f"interrupted after signing: {state.dest} is signed and verified")
        return
    stopped = f" while building {state.running.arch}/{state.running.init}" if state.running else ""
    typer.echo(
        f"interrupted{stopped}: nothing signed; a rerun on {state.dest.name} (UTC) continues "
        f"{state.dest}, a later one starts a new directory"
    )


def release(
    key: Annotated[
        str,
        typer.Option("--key", help="Fingerprint of the maintainer's key that signs the release."),
    ],
    arch: Annotated[str | None, typer.Argument(help="Arch axis (e.g. v3); not with --all.")] = None,
    init: Annotated[
        str | None, typer.Argument(help="Init axis (systemd, openrc); not with --all.")
    ] = None,
    all_variants: Annotated[
        bool, typer.Option("--all", help="Release every arch × init of the recipe axes.")
    ] = False,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root",
            help="Parent of the dated release directories; default <cache>/releases.",
        ),
    ] = None,
    boot_test: Annotated[
        bool, typer.Option("--boot-test", help="Boot-test every ISO (build --boot-test).")
    ] = False,
    no_download: Annotated[
        bool,
        typer.Option(
            "--no-download", help="Cache only: no upstream stage3 check, no fetch in the builds."
        ),
    ] = False,
    jobs: Annotated[
        int | None, typer.Option("--jobs", min=1, help="Passed to every build.")
    ] = None,
    compression: Annotated[
        Compression | None, typer.Option("--compression", help="Passed to every build.")
    ] = None,
    no_sbom: Annotated[bool, typer.Option("--no-sbom", help="Passed to every build.")] = False,
    local: Annotated[bool, typer.Option("--local", help="Passed to every build.")] = False,
    allow_dirty: Annotated[
        bool, typer.Option("--allow-dirty", help="Passed to every build.")
    ] = False,
    work_dir: Annotated[
        Path | None, typer.Option("--work-dir", help="Root of cache, scratch and runs.")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Passed to every build.")
    ] = False,
) -> None:
    """Build every variant asked for, publish each arch's binhost, and sign one dated release.

    ``sudo shidashi release v3 systemd --key FPR`` runs one ``shidashi build --images all``
    per variant into ``<cache>/releases/<YYYY.MM.DD>`` (the start date in UTC), publishes
    the signed binhost of each arch that built, writes ``release.json`` and signs
    ``SHA256SUMS`` and every ``DIGESTS`` with the maintainer's key. Exit 0 only when every
    variant built, published and the signatures verified.
    """
    from shidashi import cli  # call time: cli imports this module to register the command

    _usage(arch, init, all_variants)
    try:
        key = _signing_key(key)
    except ValueError as err:
        _die(cli, err)
    cli._apply_work_dir(work_dir)
    cli._require_build_host()
    if boot_test:
        cli._require_boot_test_runner()
    try:
        variants = release_core.variants(arch, init, all_variants=all_variants)
        pins = _load_pins(sorted({v.init for v in variants}))
    except (config.UnknownAxisError, seed.SeedError) as err:
        _die(cli, err)
    try:
        gpgsign.check_key(key)
    except gpgsign.GpgError as err:
        _die(cli, err)
    started = datetime.datetime.now(datetime.UTC)
    dest = release_core.release_dir(output_root or config.cache_dir() / "releases", started.date())
    passthrough = _passthrough(
        {
            "--boot-test": boot_test,
            "--no-sbom": no_sbom,
            "--no-download": no_download,
            "--local": local,
            "--allow-dirty": allow_dirty,
            "--verbose": verbose,
        },
        {"--jobs": jobs, "--compression": compression, "--work-dir": work_dir},
    )
    state = _Progress(dest)
    try:
        with _stop_signals_stop_the_release(), ExitStack() as stack:
            try:  # held until the signing ended; a second release is refused (R1.7)
                stack.enter_context(release_sign.claim_release_dir(dest))
            except ReleaseError as err:
                _die(cli, err)
            try:
                trail: audit.Recorder = stack.enter_context(
                    audit.run(
                        config.runs_dir(),
                        command="release",
                        argv=sys.argv,
                        inputs={
                            "variants": [f"{v.arch}/{v.init}" for v in variants],
                            "key": key,
                            "release_dir": str(dest),
                        },
                        disk=config.scratch_dir(),
                    )
                )
            except OSError as err:
                cli._err_console.print(
                    f"[yellow]warning:[/yellow] audit trail disabled: {escape(str(err))}"
                )
                trail = audit.current()
            lines, code = _run(
                cli,
                trail,
                state,
                variants,
                key=key,
                pins=pins,
                started=started,
                passthrough=passthrough,
                download=not no_download,
            )
    except KeyboardInterrupt:
        _interrupted(state)
        raise typer.Exit(_INTERRUPTED_EXIT) from None
    for line in lines:
        typer.echo(line)
    typer.echo(f"release: {dest}")
    raise typer.Exit(code)
