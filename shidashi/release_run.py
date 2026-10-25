"""The run of ``shidashi release`` after its preflight: build, publish, manifest, sign (story 030).

Split out of ``release_cli`` (story 032) to keep that module under 500 lines. ``cli`` is
imported by the caller and passed in, so this module never imports it (circular). Every seam
the tests stand in for is reached through its module attribute at call time --
``release.run_build``, ``publish_pool.publish``, ``upstream.check``, ``cli._cache_owner`` and
``cli._generation_pkgdir``.
"""

import datetime
import os
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import typer

from shidashi import (
    audit,
    binhost_cli,
    build_outcome,
    config,
    generation,
    parallel,
    publish,
    publish_pool,
    release_sign,
    seed,
    toolbox,
    upstream,
)
from shidashi import release as release_core
from shidashi.binhost import BinhostError
from shidashi.build_outcome import BuildOutcome, ReleaseError
from shidashi.phases import FactoryError


@dataclass
class Progress:
    """What the release has done so far: printed as it stands when it is interrupted."""

    dest: Path
    results: list[release_core.VariantResult] = field(default_factory=list)
    running: release_core.Variant | None = None
    signing: bool = False


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
    trail: audit.Recorder,
    v: release_core.Variant,
    dest: Path,
    passthrough: Sequence[str],
    images: str = "all",
) -> BuildOutcome:
    """One child build and how it ended (R2.1, R3.7). Last run's outcome file is removed
    first: a child killed before writing its own must not be read as a success; one that
    cannot be removed is this variant's error, never the release's end (R2.4)."""
    outcome = dest / ".outcome" / f"{v.arch}-{v.init}.json"
    with trail.step(f"build {v.arch}/{v.init}", arch=v.arch, init=v.init) as step:
        try:
            release_sign.ensure_release_dir(outcome.parent)
            outcome.unlink(missing_ok=True)
            code = release_core.run_build(
                release_core.build_argv(v, dest, outcome, passthrough, images)
            )
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


def run(
    cli: ModuleType,
    trail: audit.Recorder,
    state: Progress,
    variants: Sequence[release_core.Variant],
    *,
    key: str | None,
    pins: release_core.ReleasePins,
    started: datetime.datetime,
    passthrough: Sequence[str],
    images: str,
    download: bool,
) -> tuple[list[str], int]:
    """Everything after the preflight; the summary lines and the exit code.

    ``key`` is ``None`` for an ``--unsigned`` release: nothing is published or signed, each
    built variant's publish reads ``skipped: unsigned``, and ``release.json`` says so (R1)."""
    dest = state.dest
    inits = sorted({v.init for v in variants})
    found = _upstream(trail, inits, pins.stage3_snapshot, download=download)
    outcomes: list[tuple[release_core.Variant, BuildOutcome]] = []
    for v in variants:  # one at a time (R2.3); a failure goes on to the next (R2.4)
        state.running = v
        outcomes.append((v, _build(trail, v, dest, passthrough, images)))
        state.results.append(release_core.VariantResult(v, outcomes[-1][1], "not run: interrupted"))
    state.running = None

    published: dict[str, str] = {}
    for v, outcome in outcomes:  # one publish per arch with an ok variant (R4.1, R4.3)
        if outcome.status == "ok" and v.arch not in published:
            published[v.arch] = (
                "skipped: unsigned" if key is None else _publish(cli, trail, v.arch, v.init, key)
            )
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
                signed=key is not None,
            )
            release_core.write_manifest(dest, body)
    except (ReleaseError, publish.PublishError, OSError) as err:
        notes.append(f"{release_core.MANIFEST_FILE}: {err}; nothing signed")
        attested = False
    else:
        attested = False
        if key is not None:
            state.signing = True
            failures = release_sign.sign_and_verify(trail, dest, key)
            attested = not failures
    lines, code = release_core.summary(
        results, found, signed=attested, verify_failures=failures, unsigned=key is None
    )
    if notes:  # a corrupt fingerprint or manifest is the release's failure too
        lines += notes
        code = max(code, 1)
    with trail.step("summary") as step:
        step.add(exit_code=code, lines=lines)
    return lines, code


def interrupted(state: Progress) -> None:
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
