"""``shidashi release``: build, publish and sign one dated release (story 030).

``cli.py`` only registers :func:`release`, so ``cli`` is imported inside the command: a
module-level import would be circular (design.md GOTCHA). Every seam the tests stand in
for is reached through its module attribute at call time -- ``release.run_build``,
``publish_pool.publish``, ``gpgsign.*``, ``upstream.check``, ``cli._cache_owner`` and
``cli._generation_pkgdir``.
"""

import contextlib
import datetime
import json
import re
import signal
import sys
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack
from enum import StrEnum
from pathlib import Path
from types import FrameType, ModuleType
from typing import Annotated, Any, NoReturn

import typer
from rich.markup import escape
from typer.core import TyperGroup

from shidashi import (
    audit,
    config,
    gpgsign,
    release_prune_cli,
    release_report,
    release_run,
    release_sign,
    seed,
    tree,
)
from shidashi import release as release_core
from shidashi.build_outcome import ReleaseError

#: A fingerprint (v4: 40 hex, v5/v6: 64 hex) or a long key ID (16 hex): what gpg's
#: ``VALIDSIG`` can be matched against (release.verify_release). An e-mail or a user ID
#: would build for hours and then fail every signature check.
_KEY_FORM = re.compile(r"[0-9A-F]{16}|[0-9A-F]{40}|[0-9A-F]{64}")
#: 128 + SIGINT: the shell's code for a run stopped by Ctrl-C (R9.1).
_INTERRUPTED_EXIT = 130


#: The command a bare ``shidashi release v3 systemd`` runs: ``release`` became a group (story 032).
_RUN_COMMAND = "run"


class ReleaseGroup(TyperGroup):
    """``shidashi release``: ``report`` and ``prune`` are subcommands, and anything else
    -- ``v3 systemd``, ``--all`` -- is the release itself, so story 030's form keeps working."""

    # ``ctx`` is Any: typer 0.27 keeps click's Context in a private module, and typer.Context
    # is a subclass, which an override may not narrow to.
    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if not args or (args[0] not in self.commands and args[0] not in ctx.help_option_names):
            args = [_RUN_COMMAND, *args]
        return super().parse_args(ctx, args)


app = typer.Typer(cls=ReleaseGroup, help="Build, sign and report one dated release.")


class Compression(StrEnum):
    """``build --compression``: checked here, so a typo fails before the first build."""

    zstd = "zstd"
    xz = "xz"
    both = "both"


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


def _usage(
    arch: str | None, init: str | None, all_variants: bool, key: str | None, unsigned: bool
) -> None:
    """R1.3: ``--all`` alone, or both axes without it; story 032 R1.2: ``--key`` or
    ``--unsigned``, never both and never neither."""
    if all_variants and (arch is not None or init is not None):
        raise typer.BadParameter("--all releases every variant: give no ARCH or INIT with it")
    if not all_variants and (arch is None or init is None):
        raise typer.BadParameter("give ARCH and INIT, or --all")
    if unsigned and key is not None:
        raise typer.BadParameter("--unsigned makes no signature: give it or --key, not both")
    if not unsigned and key is None:
        raise typer.BadParameter("give --key FPR to sign the release, or --unsigned to skip it")


def _images(wanted: str) -> str:
    """``--images`` as the children get it: ``all``, or a comma list of image targets.

    Raises ``ValueError`` naming every entry that is not a ``config.target_names()`` --
    ``desktop`` and ``base`` are stages, not images, and ``all`` does not mix with names.
    """
    names = [name.strip() for name in wanted.split(",")]
    if names == ["all"]:
        return "all"
    targets = config.target_names()
    unknown = [name or "''" for name in names if name not in targets]
    if unknown:
        raise ValueError(
            f"--images: unknown image {', '.join(unknown)}; choose from all or {', '.join(targets)}"
        )
    return ",".join(names)


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


def release(
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
    images: Annotated[
        str,
        typer.Option("--images", help="Comma list of images every build makes (default all)."),
    ] = "all",
    key: Annotated[
        str | None,
        typer.Option("--key", help="Fingerprint of the maintainer's key that signs the release."),
    ] = None,
    unsigned: Annotated[
        bool,
        typer.Option(
            "--unsigned",
            help="No key: skip the binhost publish and the signature; sign later on the host.",
        ),
    ] = False,
) -> None:
    """Build every variant asked for, publish each arch's binhost, and sign one dated release.

    ``sudo shidashi release v3 systemd --key FPR`` (or ``--unsigned``) runs one
    ``shidashi build --images all`` per variant into ``<cache>/releases/<YYYY.MM.DD>`` (the
    start date in UTC), publishes the signed binhost of each arch that built, writes
    ``release.json`` and signs ``SHA256SUMS`` and every ``DIGESTS`` with the maintainer's
    key. Exit 0 only when every variant built, published and the signatures verified;
    ``--unsigned`` publishes and signs
    nothing and exits 0 when every variant built.
    """
    from shidashi import cli  # call time: cli imports this module to register the command

    _usage(arch, init, all_variants, key, unsigned)
    fingerprint: str | None = None
    if key is not None:
        try:
            fingerprint = _signing_key(key)
        except ValueError as err:
            _die(cli, err)
    try:
        wanted_images = _images(images)
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
    if fingerprint is not None:
        try:
            gpgsign.check_key(fingerprint)
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
    state = release_run.Progress(dest)
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
                            "key": fingerprint,
                            "unsigned": unsigned,
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
            lines, code = release_run.run(
                cli,
                trail,
                state,
                variants,
                key=fingerprint,
                pins=pins,
                started=started,
                passthrough=passthrough,
                images=wanted_images,
                download=not no_download,
            )
    except KeyboardInterrupt:
        release_run.interrupted(state)
        raise typer.Exit(_INTERRUPTED_EXIT) from None
    for line in lines:
        typer.echo(line)
    typer.echo(f"release: {dest}")
    raise typer.Exit(code)


app.command(_RUN_COMMAND)(release)
app.command("prune")(release_prune_cli.prune)


@app.command("report")
def report(
    directory: Annotated[Path, typer.Argument(help="A release directory holding release.json.")],
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Print a Markdown table (a job summary).")
    ] = False,
) -> None:
    """Print one release's variants, ISOs and why any failed; reads release.json, writes nothing."""
    from shidashi import cli  # call time: cli imports this module to register the command

    path = directory / release_core.MANIFEST_FILE
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            raise ValueError("not a JSON object")
        text = release_report.render(body, markdown=markdown)
    except (OSError, ValueError, release_report.ReleaseReportError) as err:
        _die(cli, f"{path}: {err}")
    typer.echo(text, nl=False)
