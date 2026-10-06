"""Shidashi's CLI (Typer) -- a Portage-free ``recipe`` path (R7.3).

Exposes the root ``shidashi`` app with the ``recipe`` subgroup (``show``/``validate``/
``list``), the real commands ``pretend`` (resolution), ``factory`` (binpkg
build) and ``assemble`` (ISO assembly) and the ``release`` stub (Phase 4). The
``recipe`` commands only resolve paths (``shidashi.config``), load and merge
fragments (``shidashi.recipe``) and render.

Error mapping (R5.2/R6.3): ``UnknownAxisError`` (from ``config``) and
``RecipeChainError`` (from ``merge``/``load_chain``) are caught, shown as a friendly
message and turned into ``typer.Exit(1)`` -- no traceback ever reaches the
user.
"""

import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Generator
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt
from rich.table import Table

from shidashi import audit, config, doctor, progress, publish
from shidashi.assembler import Assembler, AssemblerError, AssembleResult
from shidashi.factory import (
    CheckpointDecision,
    Factory,
    FactoryError,
    FactoryResult,
    FailureDecision,
    StaleStateError,
)
from shidashi.image import ImageError
from shidashi.phases import phase_target
from shidashi.recipe import (
    INCLUDE_SET_PREFIX,
    TOOLBOX_STAGE,
    RecipeChainError,
    ResolvedRecipe,
)
from shidashi.resolve import KitView, PretendReport, ResolveError, pretend_resolve
from shidashi.seed import SeedError, load_pointer
from shidashi.state import PhaseDiff
from shidashi.system import ConfigurationError
from shidashi.world import StaleWorldError

app = typer.Typer(no_args_is_help=True, help="Shidashi — catering of bentoo builds and ISOs.")
recipe_app = typer.Typer(no_args_is_help=True, help="Inspect and validate resolved recipes.")
app.add_typer(recipe_app, name="recipe")
vm_app = typer.Typer(
    no_args_is_help=True,
    help="Boot an ISO in a VM and drive it over SSH on vsock (no network, no screen).",
)
app.add_typer(vm_app, name="vm")
kits_app = typer.Typer(no_args_is_help=True, help="The kit library: every package Bentoo builds.")
app.add_typer(kits_app, name="kits")

_err_console = Console(stderr=True)

# Reference to the process's ``sys.stdin``, captured at module import. The
# ``--step`` TTY guard (R2.6) checks ``_PROCESS_STDIN.isatty()`` instead of
# ``sys.stdin.isatty()`` directly because Typer/Click's ``CliRunner`` *replaces*
# ``sys.stdin`` with a non-tty wrapper during ``invoke`` -- a direct read would never
# reflect the real ``isatty`` (nor the tests' monkeypatch). The process's true stdin
# is not swapped, so this reference keeps the observable TTY state.
_PROCESS_STDIN = sys.stdin


def _stdin_isatty() -> bool:
    """Tell whether the process's stdin is an interactive terminal (R2.6).

    Checks the stdin reference captured at import (:data:`_PROCESS_STDIN`),
    sidestepping the ``sys.stdin`` swap that ``CliRunner`` does during ``invoke``;
    outside a test runner it is exactly the process's ``sys.stdin``.
    """
    return _PROCESS_STDIN.isatty()


class OutputFormat(StrEnum):
    """Output formats of ``recipe show``.

    Note: design.md §8 sketches ``class OutputFormat(str, Enum)``; we use
    :class:`enum.StrEnum` (equivalent: members are ``str``) to satisfy the
    ``UP042`` lint of the project's ruff config. Identical behavior.
    """

    yaml = "yaml"
    json = "json"
    pretty = "pretty"


#: ``-v``: the raw output of every command on the terminal too. The progress
#: (:mod:`shidashi.progress`) shows steps, downloads and packages either way, and
#: the raw output always goes to the run's log file.
Verbose = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Also print every line the build prints, as it runs."),
]


def _require_build_host() -> None:
    """Refuse to start a build on a host that lacks what it needs (:mod:`shidashi.doctor`)."""
    try:
        doctor.require_build_host(config.scratch_dir())
    except doctor.DoctorError as err:
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err


def _apply_work_dir(work_dir: Path | None) -> None:
    """Point cache and scratch under a single ``--work-dir`` (takes precedence over env).

    When ``work_dir`` is given, derive ``cache → <work_dir>/cache``,
    ``scratch → <work_dir>/scratch`` and ``runs → <work_dir>/runs`` by setting
    ``SHIDASHI_CACHE``/``SHIDASHI_SCRATCH``/``SHIDASHI_RUNS`` in the
    process environment. :mod:`shidashi.config` reads these variables on every call,
    so the whole path tree (binpkgs, stage3, state, fork points, build rootfs)
    then lives under ``work_dir`` -- without changing the path logic. The flag
    beats the user's env var (overrides it); ``None`` is a no-op (keeps
    env/default). ``--pkgdir`` still takes precedence over the ``cache`` set here.
    """
    if work_dir is None:
        return
    os.environ["SHIDASHI_CACHE"] = str(work_dir / "cache")
    os.environ["SHIDASHI_SCRATCH"] = str(work_dir / "scratch")
    os.environ["SHIDASHI_RUNS"] = str(work_dir / "runs")


def _resolve(arch: str, flavor: str, init: str) -> ResolvedRecipe:
    """Load the target's chain of stages and merge it (D24).

    ``flavor`` is the TARGET: ``minimal`` or a flavor. Delegates to
    :func:`shidashi.config.load_recipe`, which raises
    :class:`~shidashi.config.UnknownAxisError` for unknown names and
    :class:`~shidashi.recipe.RecipeChainError` for a broken chain. It catches
    nothing: the callers map both.
    """
    return config.load_recipe(arch, flavor, init)


def _render_pretty(resolved: ResolvedRecipe) -> None:
    """Render the resolved recipe as ``rich`` tables (meaningful on a TTY)."""
    console = Console()
    summary = Table(title=f"recipe {resolved.arch} × {resolved.flavor} × {resolved.init}")
    summary.add_column("field", style="bold cyan")
    summary.add_column("value")
    summary.add_row("profile", resolved.profile)
    summary.add_row("tier", str(resolved.tier))
    summary.add_row("runnable_on_build_host", str(resolved.runnable_on_build_host))
    summary.add_row("common_flags", resolved.common_flags)
    summary.add_row("goamd64", resolved.goamd64)
    summary.add_row("rustflags", resolved.rustflags)
    summary.add_row("cpu_flags_x86", " ".join(resolved.cpu_flags_x86))
    summary.add_row("sets", " ".join(resolved.sets))
    summary.add_row("portage_layers", " → ".join(resolved.portage_layers))
    summary.add_row("stages", " → ".join(resolved.stages))
    console.print(summary)

    phases_table = Table(title="phases")
    phases_table.add_column("name", style="bold")
    phases_table.add_column("stage")
    phases_table.add_column("targets")
    phases_table.add_column("layers in effect")
    phases_table.add_column("use_break")
    for phase in resolved.phases:
        phases_table.add_row(
            phase.name + (" (ships)" if phase.ships else ""),
            phase.stage or "—",
            " ".join(phase_target(phase, resolved)) or "—",
            " ".join(phase.layers) or "—",
            ", ".join(f"{b.atom} {'' if b.enable else '-'}{b.flag}" for b in phase.use_break)
            or "—",
        )
    console.print(phases_table)


@recipe_app.command("show")
def recipe_show(
    arch: str,
    flavor: str,
    init: str,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format."),
    ] = OutputFormat.yaml,
) -> None:
    """Resolve, merge and render the recipe (R4.1–R4.3)."""
    try:
        resolved = _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    if output_format is OutputFormat.yaml:
        typer.echo(yaml.safe_dump(resolved.model_dump(), sort_keys=False).rstrip("\n"))
    elif output_format is OutputFormat.json:
        typer.echo(resolved.model_dump_json(indent=2))
    else:
        _render_pretty(resolved)


@recipe_app.command("validate")
def recipe_validate(arch: str, flavor: str, init: str) -> None:
    """Validate load+merge: success → exit 0; conflict/unknown axis → exit 1 (R5.1/R5.2)."""
    try:
        _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]invalid:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err
    typer.echo(f"valid: {arch} × {flavor} × {init}")


@recipe_app.command("list")
def recipe_list() -> None:
    """List what can be asked for: arches, TARGETS (delivered images) and inits (R6.1).

    The target is ``minimal`` or a flavor (D24) -- not the ``flavor`` axis, where
    ``minimal`` no longer lives. The base is implicit: every chain starts there.
    """
    rows = (
        ("arch", config.available_names("arch")),
        ("target", config.target_names()),
        ("init", config.available_names("init")),
    )
    for label, names in rows:
        typer.echo(f"{label}: {', '.join(names) if names else '(none)'}")


def _render_report_pretty(report: PretendReport) -> None:
    """Render the :class:`PretendReport` as ``rich`` tables (R1.1)."""
    console = Console()
    pkgs = Table(title=f"pretend {report.arch} × {report.flavor} × {report.init}")
    pkgs.add_column("resolved packages", style="bold cyan")
    for atom in report.packages:
        pkgs.add_row(atom)
    if not report.packages:
        pkgs.add_row("—")
    console.print(pkgs)

    cycles = Table(title="cycle-break suggestions (use_break)")
    cycles.add_column("atom", style="bold")
    cycles.add_column("USE")
    for cb in report.cycle_breaks:
        sign = "+" if cb.enable else "-"
        cycles.add_row(cb.atom, f"{sign}{cb.flag}")
    if not report.cycle_breaks:
        cycles.add_row("—", "(no cycles)")
    console.print(cycles)


@app.command("pretend")
def pretend(
    arch: str,
    flavor: str,
    init: str,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format: pretty (default) or json."),
    ] = OutputFormat.pretty,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Use the cache only; never touch the network.")
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Keep the scratch rootfs after the run.")
    ] = False,
    work_dir: Annotated[
        Path | None,
        typer.Option("--work-dir", help="Work root (cache+scratch under <DIR>)."),
    ] = None,
    verbose: Verbose = False,
) -> None:
    """Resolve the recipe against the real tree via ``emerge --pretend`` (R1.1–R1.4).

    Success (even with reported cycles) → package list + suggestions and exit 0.
    Known errors → friendly message + exit 1, no traceback. On a hard-conflict
    (``ResolveError`` with ``raw_output``) the raw emerge output goes to stderr.
    """
    _apply_work_dir(work_dir)
    _require_build_host()
    try:
        with progress.reporting(_err_console, verbose=verbose):
            report = pretend_resolve(arch, flavor, init, download=not no_download, keep=keep)
    except (SeedError, ResolveError, config.UnknownAxisError, RecipeChainError) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output, markup=False, highlight=False)
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    if output_format is OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        _render_report_pretty(report)


def _pin_inputs(init: str) -> dict[str, object]:
    """The pins a run builds from: stage3, ::gentoo snapshot, overlays (never raises)."""
    from shidashi.tree import load_overlay_pins, load_tree_pin

    inputs: dict[str, object] = {}
    seeds = config.seeds_dir()
    for name, load in (
        ("stage3", lambda: load_pointer(init, seeds_dir=seeds).model_dump(mode="json")),
        ("gentoo_tree", lambda: load_tree_pin(seeds).model_dump(mode="json")),
        ("overlays", lambda: [p.model_dump(mode="json") for p in load_overlay_pins(seeds)]),
    ):
        try:
            inputs[name] = load()
        except Exception as err:  # a missing pin is itself worth recording
            inputs[name] = {"error": f"{type(err).__name__}: {err}"}
    return inputs


@contextlib.contextmanager
def _audited(
    command: str, resolved: ResolvedRecipe, *, verbose: bool = False, **inputs: object
) -> Generator[audit.Recorder]:
    """Run the block as an audited run (:mod:`shidashi.audit`) and say where it went.

    The trail records the whole recipe, the pins and ``inputs``, then every step
    and command. If the runs directory cannot be written (not root, read-only),
    the run goes on unaudited with a visible warning instead of failing before
    the real checks (such as the root guard) could explain why. Either way the
    block's progress is shown on stderr (:mod:`shidashi.progress`; ``verbose``
    adds the raw output).
    """
    all_inputs = {"recipe": resolved.model_dump(mode="json"), **_pin_inputs(resolved.init)}
    all_inputs.update(inputs)
    try:
        cm = audit.run(
            config.runs_dir(),
            command=command,
            argv=sys.argv,
            inputs=all_inputs,
            disk=config.scratch_dir(),
        )
        recorder = cm.__enter__()
    except OSError as err:
        _err_console.print(f"[yellow]warning:[/yellow] audit trail disabled: {escape(str(err))}")
        with progress.reporting(_err_console, verbose=verbose):
            yield audit.current()
        return
    try:
        with progress.reporting(_err_console, verbose=verbose):
            yield recorder
    except BaseException:
        if not cm.__exit__(*sys.exc_info()):
            raise
    else:
        cm.__exit__(None, None, None)
    finally:
        _err_console.print(f"audit: {recorder.path}")


def _generation_pkgdir(arch: str, init: str) -> Path:
    """The default PKGDIR: this arch's, for the generation of the pinned stage3 (D26).

    The factory writes there and the assembler reads from there, so an ISO is
    always assembled from the binpkgs of the stage3 it names.
    """
    return config.pkgdir(arch, load_pointer(init, seeds_dir=config.seeds_dir()).snapshot)


def _render_factory_pretty(result: FactoryResult, arch: str, flavor: str, init: str) -> None:
    """Render the :class:`FactoryResult` as ``rich`` tables (R1.1)."""
    console = Console()
    summary = Table(title=f"factory {arch} × {flavor} × {init}")
    summary.add_column("field", style="bold cyan")
    summary.add_column("value")
    summary.add_row("pkgdir", str(result.pkgdir))
    summary.add_row("phases", " → ".join(result.phases) or "—")
    fork = str(result.fork_point) if result.fork_point is not None else "—"
    reuse = "reused" if result.fork_point_reused else "created"
    summary.add_row("fork_point", f"{fork} ({reuse})")
    if result.bootstrap is not None:
        b = result.bootstrap
        summary.add_row(
            "bootstrap",
            f"binutils {b.binutils} · gcc {b.gcc} · locales {b.locales_before}→{b.locales_after}",
        )
    else:
        summary.add_row("bootstrap", "— (resumed from a checkpoint)")
    console.print(summary)

    summary_reuse = len(result.reused_atoms)
    if summary_reuse:
        console.print(
            f"[green]{summary_reuse}[/green] packages installed from the generation's binpkgs"
        )
    atoms = Table(title="built atoms")
    atoms.add_column("built_atoms", style="green")
    for atom in result.built_atoms:
        atoms.add_row(atom)
    if not result.built_atoms:
        atoms.add_row("—")
    console.print(atoms)

    settle = Table(title="settle-pass")
    settle.add_column("settle_atoms", style="bold")
    for atom in result.settle_atoms:
        settle.add_row(atom)
    if not result.settle_atoms:
        settle.add_row("—")
    console.print(settle)


def _render_phase_diff(diff: PhaseDiff, console: Console) -> None:
    """Render a :class:`~shidashi.state.PhaseDiff` as a ``rich`` table (R2.2/R4.1).

    Shows the phase name, the count of built atoms and -- when not empty --
    the ``unexpected_rebuilds``, the ``use_changes`` and the ``blockers``. Used both
    in the interactive checkpoint (Task 7) and in the final per-phase report.
    """
    table = Table(title=f"phase {diff.phase}")
    table.add_column("field", style="bold cyan")
    table.add_column("value")
    table.add_row("built", str(len(diff.built)))
    table.add_row("unexpected_rebuilds", "\n".join(diff.unexpected_rebuilds) or "—")
    table.add_row("use_changes", " ".join(diff.use_changes) or "—")
    table.add_row("blockers", "\n".join(diff.blockers) or "—")
    console.print(table)


def _render_factory_stepwise_pretty(
    result: FactoryResult, arch: str, flavor: str, init: str
) -> None:
    """Render the final report of the stepwise build (R1.x/R2.x).

    Reuses :func:`_render_factory_pretty` (pkgdir/phases/fork point/atoms/settle) and
    adds the ``stopped_at`` (the label where it stopped, or ``completed`` when
    ``None``) and the per-phase diffs from ``phase_diffs``. The clean stop path
    prints the value of ``stopped_at`` (e.g. ``minimal``) to stdout.
    """
    _render_factory_pretty(result, arch, flavor, init)
    console = Console()
    label = result.stopped_at if result.stopped_at is not None else "completed"
    status = Table(title="stepwise")
    status.add_column("field", style="bold cyan")
    status.add_column("value")
    status.add_row("stopped_at", label)
    status.add_row("completed_phases", " → ".join(result.completed_phases) or "—")
    console.print(status)
    for diff in result.phase_diffs:
        _render_phase_diff(diff, console)


#: Lines of a failed command's output shown on the terminal; the rest is in its log.
_OUTPUT_TAIL = 50


def _print_output(output: object, log: Path | None = None) -> None:
    """The end of a failed command's output, where emerge says what broke and where.

    A multi-hour emerge prints far more than a terminal keeps, and the error is
    at the end. The whole output is in the run's log: ``log``, or the one the
    progress named when the first command started.
    """
    text = output.decode(errors="replace") if isinstance(output, bytes) else str(output or "")
    lines = text.rstrip("\n").splitlines()
    if not lines:
        return
    if len(lines) > _OUTPUT_TAIL:
        where = str(log) if log is not None else "the log named above"
        _err_console.print(
            f"[dim]… {len(lines) - _OUTPUT_TAIL} earlier lines; the whole output is in "
            f"{escape(where)}[/dim]",
            soft_wrap=True,
        )
        lines = lines[-_OUTPUT_TAIL:]
    _err_console.print("\n".join(lines), markup=False, highlight=False)


def _prompt_choice(prompt: str, choices: list[str], default: str) -> str:
    """Ask for a choice among ``choices`` via ``rich`` (the default on no answer).

    Thin wrapper over :meth:`rich.prompt.Prompt.ask` -- restricts the input to
    ``choices`` and returns the default when the user just presses Enter. Kept apart
    so that the interactive callbacks (checkpoint/failure) stay short and testable.
    The progress footer is paused while it asks.
    """
    with progress.current().paused():
        return Prompt.ask(prompt, choices=choices, default=default)


def _on_checkpoint(phase: str, diff: PhaseDiff) -> CheckpointDecision:
    """Post-phase checkpoint: render the diff and ask continue/stop/shell (R2.2/R2.3).

    Shows the phase's :class:`~shidashi.state.PhaseDiff` and maps the user's choice
    to :class:`~shidashi.factory.CheckpointDecision`: ``c`` → ``CONTINUE`` (go on),
    ``s`` → ``STOP`` (stop without settle), ``sh`` → ``SHELL`` (the build layer
    opens the shell and presents the SAME checkpoint again).
    """
    console = Console()
    console.print(f"[bold green]checkpoint[/bold green] after phase {phase}")
    _render_phase_diff(diff, console)
    choice = _prompt_choice("continue/stop/shell", ["c", "s", "sh"], "c")
    if choice == "s":
        return CheckpointDecision.STOP
    if choice == "sh":
        return CheckpointDecision.SHELL
    return CheckpointDecision.CONTINUE


def _on_failure(phase: str, err: Exception) -> FailureDecision:
    """Phase failure callback: print the emerge output and ask retry/abort (R3.1–R3.3).

    ``err`` is the phase's :class:`~shidashi.factory.FactoryError`; prints ``err.phase`` and
    the end of ``err.output`` (the raw ``emerge`` output) to stderr and maps the choice to
    :class:`~shidashi.factory.FailureDecision`: ``r`` → ``RETRY`` (re-runs the same phase),
    anything else → ``ABORT``. Opening the failure shell belongs to the build/driver
    layer -- the callback only prints and asks (R3.4: it never skips the phase).
    """
    failing = getattr(err, "phase", None) or phase
    output = getattr(err, "output", "")
    _err_console.print(f"[bold red]phase failed[/bold red] {failing}: {escape(str(err))}")
    _print_output(output)
    choice = _prompt_choice("retry/abort", ["r", "a"], "a")
    if choice == "r":
        return FailureDecision.RETRY
    return FailureDecision.ABORT


@app.command("factory")
def factory(
    arch: str,
    flavor: str,
    init: str,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format: pretty (default) or json."),
    ] = OutputFormat.pretty,
    emptytree: Annotated[
        bool,
        typer.Option(
            "--emptytree/--no-emptytree",
            help="Rebuild the whole tree (--emptytree, default) or reuse binpkgs.",
        ),
    ] = True,
    pkgdir_opt: Annotated[
        Path | None,
        typer.Option("--pkgdir", help="Host-side output PKGDIR (default: per arch)."),
    ] = None,
    keep: Annotated[
        bool, typer.Option("--keep", help="Keep the scratch rootfs after the build.")
    ] = False,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Use the cache only; never touch the network.")
    ] = False,
    step: Annotated[
        bool,
        typer.Option("--step", help="Interactive build: pause at a checkpoint after each phase."),
    ] = False,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Stop after the named phase (seed + the recipe's phases)."),
    ] = None,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset", help="Discard the persisted state/rootfs and start over from scratch."
        ),
    ] = False,
    force_resume: Annotated[
        bool,
        typer.Option("--force-resume", help="Resume a stale state without starting over."),
    ] = False,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Work root: cache+scratch under <DIR> (beats SHIDASHI_CACHE/_SCRATCH).",
        ),
    ] = None,
    jobs: Annotated[
        int | None,
        typer.Option(
            "--jobs",
            min=1,
            help="This host's jobs, over the recipe's, in every phase: MAKEOPTS=-jN -lN "
            "and emerge --jobs=N --load-average=N.",
        ),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option(
            "--stop-after",
            help="Stop after this STAGE (its fork point written); the next run resumes there.",
        ),
    ] = None,
    update: Annotated[
        bool,
        typer.Option(
            "--update",
            help="Weekly update of the built image within its generation (D26): "
            "-uDN --changed-deps over the pinned tree; refuses a toolchain change.",
        ),
    ] = False,
    verbose: Verbose = False,
) -> None:
    """Build the recipe's binpkgs (stage4) in an nspawn container (R1.1–R1.5/R8.x).

    Without ``--step``/``--until``/``--reset`` it runs story 003's one-shot (R8.2);
    any of them routes to the resumable step-by-step build (``--step`` makes it
    interactive, with checkpoints and failure prompts). Success → report of
    phases/atoms/fork point (+ ``stopped_at``/diffs in stepwise) + exit 0. Known
    errors (``FactoryError``/``StaleStateError``/``SeedError``/``ResolveError``/
    unknown axis/recipe conflict/invalid ``--until``), including the root guard,
    become a friendly message + exit 1, no traceback; a ``FactoryError`` prints
    the phase that failed and the emerge ``output``.
    """
    _apply_work_dir(work_dir)
    _require_build_host()
    if jobs is not None:
        os.environ["SHIDASHI_JOBS"] = str(jobs)  # read by resolve.apply_portage
    try:
        resolved = _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    try:
        pkgdir = pkgdir_opt if pkgdir_opt is not None else _generation_pkgdir(arch, init)
    except SeedError as err:
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    if stop_after is not None and stop_after not in resolved.stages:
        _err_console.print(
            f"[bold red]error:[/bold red] --stop-after {stop_after!r} is not a stage of "
            f"{arch}×{flavor}×{init}; stages: {' → '.join(resolved.stages)}"
        )
        raise typer.Exit(1)
    if stop_after is not None and (update or step or until is not None or reset or force_resume):
        _err_console.print(
            "[bold red]error:[/bold red] --stop-after belongs to a normal build; it does not "
            "combine with --update/--step/--until/--reset/--force-resume"
        )
        raise typer.Exit(1)
    if update and (step or until is not None or reset or force_resume):
        _err_console.print(
            "[bold red]error:[/bold red] --update updates a built image in one run; it "
            "does not combine with --step/--until/--reset/--force-resume"
        )
        raise typer.Exit(1)

    if not step and until is None and not reset and not force_resume:
        _run_factory_oneshot(
            resolved,
            pkgdir,
            arch,
            flavor,
            init,
            output_format=output_format,
            emptytree=emptytree,
            download=not no_download,
            keep=keep,
            update=update,
            stop_after=stop_after,
            verbose=verbose,
        )
        return

    if step and not _stdin_isatty():
        _err_console.print(
            "[bold red]error:[/bold red] --step requires an interactive terminal (TTY); "
            "for non-interactive runs use --until <phase> or resume with --reset/--force-resume"
        )
        raise typer.Exit(1)
    if step and output_format is OutputFormat.json:
        _err_console.print(
            "[bold red]error:[/bold red] --step (interactive checkpoints) is incompatible "
            "with --format json; use the pretty format (default)"
        )
        raise typer.Exit(1)

    _run_factory_stepwise(
        resolved,
        pkgdir,
        arch,
        flavor,
        init,
        output_format=output_format,
        emptytree=emptytree,
        download=not no_download,
        until=until,
        step=step,
        reset=reset,
        force_resume=force_resume,
        verbose=verbose,
    )


def _run_factory_oneshot(
    resolved: ResolvedRecipe,
    pkgdir: Path,
    arch: str,
    flavor: str,
    init: str,
    *,
    output_format: OutputFormat,
    emptytree: bool,
    download: bool,
    keep: bool,
    update: bool = False,
    stop_after: str | None = None,
    verbose: bool = False,
) -> None:
    """Story 003's one-shot path -- behavior unchanged byte for byte (R8.2).

    With ``update`` it runs :meth:`Factory.update` instead of a build (D26).
    """
    try:
        with _audited(
            "factory",
            resolved,
            verbose=verbose,
            pkgdir=str(pkgdir),
            update=update,
            emptytree=emptytree,
            stop_after=stop_after,
        ):
            factory_obj = Factory(resolved, pkgdir)
            if update:
                result = factory_obj.update(download=download, keep=keep)
            else:
                result = factory_obj.build(
                    emptytree=emptytree, download=download, keep=keep, stop_after=stop_after
                )
    except FactoryError as err:
        # soft_wrap: a long --pkgdir in the message must not be folded mid-path
        if err.phase:
            _err_console.print(
                f"[bold red]phase failed[/bold red] {escape(str(err.phase))}: {escape(str(err))}",
                soft_wrap=True,
            )
        else:
            _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}", soft_wrap=True)
        _print_output(err.output, config.build_log_path(resolved))
        raise typer.Exit(1) from err
    except (SeedError, ResolveError, config.UnknownAxisError, RecipeChainError) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output, markup=False, highlight=False)
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    if output_format is OutputFormat.json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _render_factory_pretty(result, arch, flavor, init)


def _run_factory_stepwise(
    resolved: ResolvedRecipe,
    pkgdir: Path,
    arch: str,
    flavor: str,
    init: str,
    *,
    output_format: OutputFormat,
    emptytree: bool,
    download: bool,
    until: str | None,
    step: bool,
    reset: bool,
    force_resume: bool,
    verbose: bool = False,
) -> None:
    """Stepwise/resumable path (R1.x/R2.x/R3.x/R6.3).

    Builds the interactive callbacks only with ``--step``; maps
    ``StaleStateError`` (before the generic ``FactoryError``) to the stale-state
    prompt/diagnosis (R6.3), and ``ValueError`` (invalid ``--until``) and
    ``FactoryError`` (abort/build failure) to a friendly exit 1.
    """
    factory_obj = Factory(resolved, pkgdir)
    on_checkpoint = _on_checkpoint if step else None
    on_failure = _on_failure if step else None

    # A loop of at most two iterations: the 1st invocation and, if it raises
    # StaleStateError and the user picks reset/proceed on a TTY, the re-invocation with
    # the resolved flag. Keeping the re-invocation INSIDE the same try ensures that a
    # build failure/invalid `--until` after the reset also maps to a friendly exit 1
    # (R3.3/R1.5) -- never a traceback.
    while True:
        try:
            with _audited(
                "factory-stepwise",
                resolved,
                verbose=verbose,
                pkgdir=str(pkgdir),
                until=until,
                reset=reset,
                force_resume=force_resume,
            ):
                result = _invoke_stepwise(
                    factory_obj,
                    until=until,
                    interactive=step,
                    emptytree=emptytree,
                    download=download,
                    reset=reset,
                    force_resume=force_resume,
                    on_checkpoint=on_checkpoint,
                    on_failure=on_failure,
                )
            break
        except StaleStateError as err:
            reset, force_resume = _resolve_stale_state(err)
            continue
        except ValueError as err:
            _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
            raise typer.Exit(1) from err
        except FactoryError as err:
            # soft_wrap: a long --pkgdir in the message must not be folded mid-path
            if err.phase:
                _err_console.print(
                    f"[bold red]phase failed[/bold red] "
                    f"{escape(str(err.phase))}: {escape(str(err))}",
                    soft_wrap=True,
                )
            else:
                _err_console.print(
                    f"[bold red]error:[/bold red] {escape(str(err))}", soft_wrap=True
                )
            _print_output(err.output, config.build_log_path(resolved))
            raise typer.Exit(1) from err
        except (SeedError, ResolveError, config.UnknownAxisError, RecipeChainError) as err:
            if isinstance(err, ResolveError) and err.raw_output:
                _err_console.print(err.raw_output, markup=False, highlight=False)
            _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
            raise typer.Exit(1) from err

    if output_format is OutputFormat.json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _render_factory_stepwise_pretty(result, arch, flavor, init)


def _invoke_stepwise(
    factory_obj: Factory,
    *,
    until: str | None,
    interactive: bool,
    emptytree: bool,
    download: bool,
    reset: bool,
    force_resume: bool,
    on_checkpoint: object,
    on_failure: object,
) -> FactoryResult:
    """Call :meth:`Factory.build_stepwise` with Task 6's keyword-only signature."""
    return factory_obj.build_stepwise(
        until=until,
        interactive=interactive,
        emptytree=emptytree,
        download=download,
        reset=reset,
        force_resume=force_resume,
        on_checkpoint=on_checkpoint,  # type: ignore[arg-type]
        on_failure=on_failure,  # type: ignore[arg-type]
    )


def _resolve_stale_state(err: StaleStateError) -> tuple[bool, bool]:
    """Resolve a ``StaleStateError`` into the resume flags ``(reset, force_resume)`` (R6.3).

    Without a TTY → exit 1 telling the user to pass ``--reset`` or ``--force-resume``
    (it never proceeds silently over stale state). On a TTY, asks
    ``reset``/``proceed``/``cancel``: ``cancel`` → exit 1; ``reset`` → ``(True, False)``
    (start over from scratch); ``proceed`` → ``(False, True)`` (resume anyway). The
    caller re-invokes :meth:`Factory.build_stepwise` with the returned flags.
    """
    _err_console.print(f"[bold yellow]stale state:[/bold yellow] {escape(str(err))}")
    if not _stdin_isatty():
        _err_console.print(
            "[bold red]error:[/bold red] stale build state; run with --reset "
            "(start over from scratch) or --force-resume (resume anyway)"
        )
        raise typer.Exit(1) from err

    choice = _prompt_choice("reset/proceed/cancel", ["reset", "proceed", "cancel"], "cancel")
    if choice == "cancel":
        raise typer.Exit(1) from err
    return (choice == "reset", choice == "proceed")


_STUB_MSG = "not implemented in Phase 0"


def _render_assemble_pretty(result: AssembleResult, arch: str, flavor: str, init: str) -> None:
    """The ``assemble`` result as a ``rich`` table: the ISOs, then every artifact."""
    console = Console()
    table = Table(title=f"ISO {arch} × {flavor} × {init} -- {result.name}")
    table.add_column("field", style="bold cyan")
    table.add_column("value")
    for iso in result.isos:
        table.add_row("iso", str(iso))
    for artifact in result.artifacts:
        table.add_row("artifact", str(artifact))
    console.print(table)


@app.command("assemble")
def assemble(
    arch: str,
    flavor: str,
    init: str,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Output format: pretty (default) or json."),
    ] = OutputFormat.pretty,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory of the ISO(s) and their artifacts "
            "(bentoo-<date>-<flavor>-<init>-<arch>.iso, .DIGESTS, SHA256SUMS...).",
        ),
    ] = Path("."),
    compression: Annotated[
        str,
        typer.Option(
            "--compression",
            help="zstd (default: the faster live session), xz (the smaller download), "
            "or both (two ISOs).",
        ),
    ] = "zstd",
    stage4: Annotated[
        bool,
        typer.Option(
            "--stage4",
            help="Also publish the configured system as a stage4 "
            "tarball (tar.xz, xz -9e), made before the live user is added.",
        ),
    ] = False,
    no_sbom: Annotated[
        bool, typer.Option("--no-sbom", help="Do not generate the SBOM (syft).")
    ] = False,
    binhost_opt: Annotated[
        Path | None,
        typer.Option(
            "--binhost", help="Host-side output binhost (publish-pool) (default: per arch)."
        ),
    ] = None,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Use the cache only; never touch the network.")
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Keep the scratch rootfs after the assembly.")
    ] = False,
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Restore no checkpoint, not even the trunk: install from the stage3 again.",
        ),
    ] = False,
    no_trunk: Annotated[
        bool,
        typer.Option(
            "--no-trunk",
            help="Install a flavor whole from the stage3, not on top of its trunk (desktop).",
        ),
    ] = False,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Work root: cache+scratch under <DIR> (beats SHIDASHI_CACHE/_SCRATCH).",
        ),
    ] = None,
    jobs: Annotated[
        int | None,
        typer.Option(
            "--jobs",
            min=1,
            help="emerge --jobs N (binpkgs merged in parallel), mksquashfs/unsquashfs "
            "-processors N and the stage4's xz -TN. Default: emerge one package at a "
            "time, the others on every CPU.",
        ),
    ] = None,
    verbose: Verbose = False,
) -> None:
    """Assemble the recipe's live ISO from the binhost (OVERVIEW §7).

    Seeds a stage3, overlays the recipe's layers (final USE = that of the binpkgs,
    §18.6), pulls the flavor's slice with ``emerge --usepkgonly`` and produces the
    hybrid ISO (squashfs + dracut ``dmsquash-live`` + grub-mkrescue). Success →
    ISO path + exit 0. Known errors (root guard, missing kernel, unknown
    axis/recipe conflict, seed/resolve, emerge/dracut failure, squashfs/ISO
    failure) become a friendly message + exit 1, no traceback.
    """
    _apply_work_dir(work_dir)
    _require_build_host()
    try:
        resolved = _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    try:
        binhost = binhost_opt if binhost_opt is not None else _generation_pkgdir(arch, init)
    except SeedError as err:
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err
    if compression not in ("zstd", "xz", "both"):
        _err_console.print("[bold red]error:[/bold red] --compression is zstd, xz or both")
        raise typer.Exit(1)
    compressions = ("zstd", "xz") if compression == "both" else (compression,)

    try:
        with _audited(
            "assemble",
            resolved,
            verbose=verbose,
            binhost=str(binhost),
            output_dir=str(output_dir),
            jobs=jobs,
            keep=keep,
            compressions=list(compressions),
            stage4=stage4,
            sbom=not no_sbom,
            fresh=fresh,
            trunk=not no_trunk,
        ) as trail:
            produced = Assembler(resolved, binhost, jobs=jobs).assemble(
                output_dir,
                download=not no_download,
                keep=keep,
                compressions=compressions,
                stage4=stage4,
                sbom=not no_sbom,
                fresh=fresh,
                trunk=not no_trunk,
            )
        # the trail is complete only once the run closed: publish it beside the ISO
        if trail.root is not None:
            bundle = publish.bundle_run(trail.root, output_dir / f"{produced.name}.build.tar.zst")
            publish.update_sha256sums(output_dir, {bundle.name: publish.sha256_of(bundle)})
            produced = produced.model_copy(update={"artifacts": (*produced.artifacts, bundle)})
    except (
        AssemblerError,
        ImageError,
        SeedError,
        ResolveError,
        ConfigurationError,
        StaleWorldError,
    ) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output, markup=False, highlight=False)
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err
    except subprocess.CalledProcessError as err:
        _err_console.print(
            f"[bold red]emerge/dracut failure in the ISO[/bold red] (exit {err.returncode})"
        )
        # a logged container merges stderr into the output
        _print_output(err.output or err.stderr)
        raise typer.Exit(1) from err

    if output_format is OutputFormat.json:
        typer.echo(
            json.dumps(
                {
                    "name": produced.name,
                    "isos": [str(p) for p in produced.isos],
                    "artifacts": [str(p) for p in produced.artifacts],
                    "arch": arch,
                    "flavor": flavor,
                    "init": init,
                    "binhost": str(binhost),
                },
                indent=2,
            )
        )
    else:
        _render_assemble_pretty(produced, arch, flavor, init)


def plan_tree(images: list[str]) -> tuple[list[str], list[str]]:
    """Which images the factory builds, and which become ISOs, in chain order. Pure.

    The build is a tree rooted at (arch, init, generation): bootstrap → base →
    minimal, and minimal → desktop → each flavor. A flavor's chain builds and
    settles minimal on its way (minimal ships), so minimal needs a factory run of
    its own only when no flavor is asked for; the second flavor starts from the
    first one's desktop fork point. ``worker`` (minimal → worker) is planned like
    a flavor: its chain settles minimal too. The toolbox (base → toolbox) comes
    first: every ISO is made in it.
    """
    order = config.target_names()
    unknown = [i for i in images if i not in order]
    if unknown:
        hint = ""
        if "desktop" in unknown:
            hint = " (desktop is the flavors' shared trunk, not an image)"
        raise typer.BadParameter(
            f"unknown image(s) {', '.join(unknown)}{hint}; images: {', '.join(order)}"
        )
    isos = [i for i in order if i in images]
    flavors = [i for i in isos if i != "minimal"]
    return [TOOLBOX_STAGE, *(flavors or ["minimal"])], isos


@app.command("build")
def build(
    arch: str,
    init: str,
    images: Annotated[
        str,
        typer.Option("--images", help="Comma-separated images (minimal, kde, gnome...) or all."),
    ] = "all",
    output_dir: Annotated[
        Path, typer.Option("--output-dir", "-o", help="Directory of the ISOs and artifacts.")
    ] = Path("."),
    compression: Annotated[
        str, typer.Option("--compression", help="zstd, xz or both (two ISOs per image).")
    ] = "zstd",
    stage4: Annotated[bool, typer.Option("--stage4", help="Also a stage4 per image.")] = False,
    no_sbom: Annotated[bool, typer.Option("--no-sbom", help="No SBOM (syft).")] = False,
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Restore no checkpoint, not even the trunk: install from the stage3 again.",
        ),
    ] = False,
    no_trunk: Annotated[
        bool,
        typer.Option(
            "--no-trunk",
            help="Install a flavor whole from the stage3, not on top of its trunk (desktop).",
        ),
    ] = False,
    skip_factory: Annotated[
        bool,
        typer.Option("--skip-factory", help="Assemble from the binpkgs already built."),
    ] = False,
    jobs: Annotated[
        int | None,
        typer.Option(
            "--jobs",
            min=1,
            help="In the factory MAKEOPTS -jN -lN and emerge --jobs=N --load-average=N; "
            "in the assemble emerge --jobs N, mksquashfs/unsquashfs -processors N and "
            "the stage4's xz -TN.",
        ),
    ] = None,
    keep: Annotated[bool, typer.Option("--keep", help="Keep the build rootfs.")] = False,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Cache only; never touch the network.")
    ] = False,
    boot_test: Annotated[
        bool,
        typer.Option(
            "--boot-test",
            help="Boot every ISO on BIOS and UEFI and check it "
            "(shidashi vm test); a failed check fails the build.",
        ),
    ] = False,
    work_dir: Annotated[
        Path | None,
        typer.Option("--work-dir", help="Root of cache, scratch and runs."),
    ] = None,
    verbose: Verbose = False,
) -> None:
    """Build a tree of images in one audited run: the factory, then every ISO.

    ``shidashi build v3 systemd --images minimal,kde,gnome`` builds the trunk once,
    each flavor from the desktop fork point, then assembles the three ISOs --
    with one audit trail covering all of it (steps factory:<image>, assemble:<image>).
    """
    _apply_work_dir(work_dir)
    _require_build_host()
    if jobs is not None:
        os.environ["SHIDASHI_JOBS"] = str(jobs)
    if compression not in ("zstd", "xz", "both"):
        _err_console.print("[bold red]error:[/bold red] --compression is zstd, xz or both")
        raise typer.Exit(1)
    compressions = ("zstd", "xz") if compression == "both" else (compression,)
    wanted = (
        config.target_names()
        if images == "all"
        else [i.strip() for i in images.split(",") if i.strip()]
    )
    try:
        factory_targets, iso_targets = plan_tree(wanted)
        recipes = {t: _resolve(arch, t, init) for t in {*factory_targets, *iso_targets}}
        pkgdir = _generation_pkgdir(arch, init)
    except (typer.BadParameter, config.UnknownAxisError, RecipeChainError, SeedError) as err:
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    results: list[AssembleResult] = []
    try:
        with _audited(
            "build",
            recipes[iso_targets[-1]],
            verbose=verbose,
            images=iso_targets,
            factory=[] if skip_factory else factory_targets,
            pkgdir=str(pkgdir),
            output_dir=str(output_dir),
            compressions=list(compressions),
            stage4=stage4,
            jobs=jobs,
        ) as trail:
            run = audit.current()
            if not skip_factory:
                for target in factory_targets:
                    with run.step(f"factory:{target}"):
                        Factory(recipes[target], pkgdir).build(
                            emptytree=True, download=not no_download, keep=keep
                        )
            for target in iso_targets:
                with run.step(f"assemble:{target}"):
                    results.append(
                        Assembler(recipes[target], pkgdir, jobs=jobs).assemble(
                            output_dir,
                            download=not no_download,
                            keep=keep,
                            compressions=compressions,
                            stage4=stage4,
                            sbom=not no_sbom,
                            fresh=fresh,
                            trunk=not no_trunk,
                        )
                    )
            if boot_test:
                from shidashi import vm

                for target, result in zip(iso_targets, results, strict=True):
                    with run.step(f"boot-test:{target}") as step:
                        report = vm.boot_test(result.isos[0])
                        step.add(passed=report["passed"])
                        if not report["passed"]:
                            failed = [
                                f"{fw}: {c['check']}"
                                for fw, r in report["firmwares"].items()
                                for c in r["checks"]
                                if not c["passed"]
                            ]
                            raise AssemblerError(
                                f"{result.isos[0].name} failed its boot test: " + "; ".join(failed)
                            )
    except FactoryError as err:
        where = f" {escape(str(err.phase))}" if err.phase else ""
        _err_console.print(f"[bold red]phase failed[/bold red]{where}: {escape(str(err))}")
        _print_output(err.output)
        raise typer.Exit(1) from err
    except (
        AssemblerError,
        ImageError,
        SeedError,
        ResolveError,
        ConfigurationError,
        StaleWorldError,
    ) as err:
        _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err
    except subprocess.CalledProcessError as err:
        _err_console.print(f"[bold red]emerge/dracut failure[/bold red] (exit {err.returncode})")
        _print_output(err.output or err.stderr)
        raise typer.Exit(1) from err

    if trail.root is not None:
        bundle = publish.bundle_run(trail.root, output_dir / f"{trail.run_id}.build.tar.zst")
        publish.update_sha256sums(output_dir, {bundle.name: publish.sha256_of(bundle)})
    for target, result in zip(iso_targets, results, strict=True):
        _render_assemble_pretty(result, arch, target, init)


def _world_recipes() -> list[ResolvedRecipe]:
    """One recipe per image and init. Sets do not depend on the arch, so the
    first one stands for all (the test suite checks that they agree)."""
    arch = config.available_names("arch")[0]
    return [
        config.load_recipe(arch, target, init)
        for target in config.target_names()
        for init in config.available_names("init")
    ]


def _echo_kit(
    by_name: dict[str, KitView],
    name: str,
    depth: int,
    printed: set[str],
    notes: dict[str, str] | None = None,
) -> None:
    """One kit as a tree: its atoms, the atoms exclude: took out, then the kits it
    includes, one level deeper. A kit already printed is only named. ``notes``
    says, per atom, which layer excluded it and which stage put it back."""
    notes = notes or {}
    pad = "  " * depth
    if name in printed:
        typer.echo(f"{pad}@{name}  (listed above)")
        return
    printed.add(name)
    kit = by_name[name]
    typer.echo(f"{pad}@{name}{'  (include:)' if kit.included else ''}")
    for atom in kit.atoms:
        typer.echo(f"{pad}  {atom}")
    for atom in kit.dropped:
        typer.echo(f"{pad}  {atom}  ({notes.get(atom, 'excluded')})")
    for entry in kit.catalog:
        typer.echo(f"{pad}  {entry}  ({notes.get(entry, 'catalog only')})")
    for ref in kit.refs:
        _echo_kit(by_name, ref, depth + 1, printed, notes)


def _world_notes(recipe: ResolvedRecipe, kits: list[KitView]) -> dict[str, str]:
    """The legend of each taken-out atom: ``excluded by minimal``, ``catalog only``,
    plus ``; included by desktop`` when a later stage's include: puts it back. Pure."""
    included_by = {
        atom: name.removeprefix(INCLUDE_SET_PREFIX)
        for name, atoms in recipe.includes.items()
        for atom in atoms
    }
    notes: dict[str, str] = {}
    for kit in kits:
        for atom in kit.dropped:
            notes[atom] = f"excluded by {recipe.exclude_origin.get(atom, '?')}"
        for entry in kit.catalog:
            notes[entry] = "catalog only"
    for atom, stage in included_by.items():
        if atom in notes:
            notes[atom] += f"; included by {stage}"
    return notes


def _show_world(target: str, inits: list[str]) -> None:
    """Print each image's packages kit by kit, excluded atoms marked. Writes nothing."""
    from shidashi.recipe import BASE_STAGE, RecipeFileError
    from shidashi.resolve import ResolveError, kit_view

    arch = config.available_names("arch")[0]
    for i, init in enumerate(inits):
        try:
            recipe = config.load_recipe(arch, target, init, any_stage=True)
            kits = kit_view(recipe)
        except (ResolveError, RecipeFileError) as err:
            _err_console.print(f"[bold red]error:[/bold red] {err}")
            raise typer.Exit(1) from err
        if i:
            typer.echo()
        atoms = {a for kit in kits for a in kit.atoms}
        dropped = {a for kit in kits for a in kit.dropped}
        catalog = {a for kit in kits for a in kit.catalog}
        typer.echo(
            f"{target}/{init}: {len(atoms)} packages from {len(kits)} kits, "
            f"{len(dropped)} excluded, {len(catalog)} catalog only "
            f"({' -> '.join(recipe.stages) or BASE_STAGE})"
        )
        by_name = {kit.name: kit for kit in kits}
        printed: set[str] = set()
        notes = _world_notes(recipe, kits)
        for name in recipe.sets:
            if name not in printed:
                typer.echo()
                _echo_kit(by_name, name, 0, printed, notes)


@app.command("world")
def world(
    target: Annotated[
        str | None,
        typer.Argument(help="Print this image's (or stage's) packages instead of writing files."),
    ] = None,
    init: Annotated[
        str | None,
        typer.Argument(help="Only this init (default: every init)."),
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Do not write: exit 1 if any world file is stale."),
    ] = False,
) -> None:
    """Write (or check) every image's world file, variants/<stage>/world.<init>.

    The flat list of packages each image asks for, generated from the kits: the
    other end of the set composition. Run it after changing a kit or a stage's
    sets, and commit the diff with the change.

    With an image (``shidashi world kde systemd``) it writes nothing: it prints
    that image's packages kit by kit, with the atoms ``exclude:`` takes out.
    ``base`` and ``desktop`` are accepted too: the stages images grow from.
    Dependencies are not listed -- only what the image asks for.
    """
    from shidashi import world as world_mod

    if target is not None:
        inits = config.available_names("init")
        images = config.stage_names()
        if target not in images or (init is not None and init not in inits):
            _err_console.print(
                f"[bold red]error:[/bold red] unknown image {target}"
                f"{'/' + init if init else ''}; images: {', '.join(images)}; "
                f"inits: {', '.join(inits)}"
            )
            raise typer.Exit(1)
        _show_world(target, [init] if init else inits)
        return

    from shidashi.recipe import RecipeFileError
    from shidashi.resolve import ResolveError

    stale: list[str] = []
    try:
        recipes = _world_recipes()
    except (RecipeFileError, RecipeChainError) as err:
        _err_console.print(f"[bold red]error:[/bold red] {err}")
        raise typer.Exit(1) from err
    for recipe in recipes:
        path = world_mod.world_file(recipe, config.variants_dir())
        try:
            text = world_mod.render(recipe)
        except ResolveError as err:
            _err_console.print(f"[bold red]error:[/bold red] {recipe.flavor}/{recipe.init}: {err}")
            raise typer.Exit(1) from err
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        rel = path.relative_to(config.variants_dir().parent)
        if current == text:
            typer.echo(f"ok      {rel}")
            continue
        if check:
            stale.append(str(rel))
            typer.echo(f"stale   {rel}")
        else:
            path.write_text(text, encoding="utf-8")
            typer.echo(f"written {rel} ({len(world_mod.read(path))} packages)")
    if stale:
        _err_console.print(
            f"[bold red]error:[/bold red] {len(stale)} world file(s) out of date; "
            "run `shidashi world` and commit the result"
        )
        raise typer.Exit(1)


@app.command("doctor")
def doctor_cmd(
    work_dir: Annotated[
        Path | None,
        typer.Option("--work-dir", help="Work root to check (its filesystem)."),
    ] = None,
) -> None:
    """Check what this host must provide to build images and boot them.

    Needs no root and changes nothing. Exits 1 when something a build needs is
    missing (``build``); what only ``shidashi vm`` needs (``vm``) and the
    ``optional`` tools are reported without failing.
    """
    _apply_work_dir(work_dir)
    found = doctor.checks(config.scratch_dir())
    table = Table(title="shidashi doctor")
    table.add_column("check", style="bold")
    table.add_column("for")
    table.add_column("", justify="center")
    table.add_column("detail")
    for c in found:
        mark = (
            "[green]ok[/green]"
            if c.ok
            else ("[yellow]—[/yellow]" if c.scope in ("optional", "info") else "[red]missing[/red]")
        )
        table.add_row(c.name, c.scope, mark, escape(c.detail))
    Console().print(table)
    lacking = doctor.missing(found, "build")
    if lacking:
        _err_console.print(
            f"[bold red]this host cannot build:[/bold red] {', '.join(c.name for c in lacking)}"
        )
        raise typer.Exit(1)
    vm = doctor.missing(found, "vm")
    if vm:
        typer.echo(f"builds: ok; `shidashi vm` also needs: {', '.join(c.name for c in vm)}")
    else:
        typer.echo("builds and `shidashi vm`: ok")


@app.command("release")
def release(
    arch: Annotated[str, typer.Argument(help="Arch axis (ignored by the stub).")] = "",
    flavor: Annotated[str, typer.Argument(help="Flavor axis (ignored by the stub).")] = "",
    init: Annotated[str, typer.Argument(help="Init axis (ignored by the stub).")] = "",
    all_variants: Annotated[bool, typer.Option("--all", help="Release every variant.")] = False,
) -> None:
    """Stub: release publishing (not implemented in Phase 0) (R6.2)."""
    typer.echo(f"release: {_STUB_MSG}")
    raise typer.Exit(2)


@kits_app.command("check")
def kits_check(
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Use the cached pinned trees only.")
    ] = False,
) -> None:
    """Every kit atom must exist in the pinned trees; every @ref must name a kit.

    Read-only. Exit 1 with one line per problem (kit:line: atom -- why).
    """
    from shidashi import kits
    from shidashi.tree import pinned_repos

    _apply_work_dir(work_dir)
    try:
        repos = pinned_repos(
            seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=not no_download
        )
    except (SeedError, OSError) as err:
        raise _vm_error(err) from err
    problems = kits.check(config.kits_dir(), repos)
    atoms = sum(1 for *_, token in kits.iter_kits(config.kits_dir()) if not token.startswith("@"))
    for problem in problems:
        typer.echo(problem)
    pins = ", ".join(f"::{name} ({path.name})" for name, path in repos.items())
    typer.echo(f"{atoms} atoms checked against {pins}: {len(problems)} problem(s)")
    if problems:
        raise typer.Exit(1)


# --- vm: control and the automated boot test ------------------------------------------


def _vm_error(err: Exception) -> typer.Exit:
    _err_console.print(f"[bold red]error:[/bold red] {escape(str(err))}")
    return typer.Exit(1)


@vm_app.command("start")
def vm_start(
    iso: Path,
    uefi: Annotated[bool, typer.Option("--uefi", help="Boot with OVMF instead of BIOS.")] = False,
    name: Annotated[str, typer.Option("--name", help="Session name.")] = "bentoo",
    cid: Annotated[int, typer.Option("--cid", min=3, help="The guest's vsock CID.")] = 42,
    memory: Annotated[str, typer.Option("--memory")] = "8G",
    cpus: Annotated[int, typer.Option("--cpus", min=1)] = 8,
    display: Annotated[
        str, typer.Option("--display", help="none (headless) or sdl to watch it.")
    ] = "none",
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for SSH.")] = True,
    disk: Annotated[
        list[Path] | None,
        typer.Option("--disk", help="A qcow2 disk for the guest (/dev/vda, vdb...); repeatable."),
    ] = None,
    disk_size: Annotated[
        str | None,
        typer.Option("--disk-size", help="Create a missing --disk with this size (e.g. 200G)."),
    ] = None,
    share: Annotated[
        list[str] | None,
        typer.Option(
            "--share",
            help="TAG=DIR[:rw]: a host directory the guest mounts with "
            "`mount -t virtiofs TAG <dir>`; read-only unless :rw. Repeatable.",
        ),
    ] = None,
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Start ``iso`` in a VM reachable with `shidashi vm run`."""
    from shidashi import vm

    _apply_work_dir(work_dir)
    try:
        shares = tuple(vm.parse_share(s) for s in share or ())
        disks = tuple(vm.ensure_disk(d.resolve(), disk_size) for d in disk or ())
    except vm.VmError as err:
        raise _vm_error(err) from err
    spec = vm.VmSpec(
        iso=iso.resolve(),
        uefi=uefi,
        cid=cid,
        memory=memory,
        cpus=cpus,
        display=display,
        disks=disks,
        shares=shares,
    )
    session = vm.Session(spec, vm.session_dir(name))
    try:
        session.start()
        if wait:
            typer.echo(f"ssh up after {session.wait_ssh()} s (vsock/{cid})")
    except vm.VmError as err:
        raise _vm_error(err) from err


@vm_app.command("run")
def vm_run(
    command: Annotated[list[str], typer.Argument(help="The command, run by the guest's shell.")],
    name: Annotated[str, typer.Option("--name")] = "bentoo",
    timeout: Annotated[
        int, typer.Option("--timeout", min=1, help="Seconds before giving up on the command.")
    ] = 600,
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Run a command as root in the VM; its exit code becomes ours."""
    from shidashi import vm

    _apply_work_dir(work_dir)
    try:
        result = vm.load_session(name).run_command(" ".join(command), timeout=timeout)
    except vm.VmError as err:
        raise _vm_error(err) from err
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise typer.Exit(result.exit_code)


@vm_app.command("screenshot")
def vm_screenshot(
    dest: Path,
    name: Annotated[str, typer.Option("--name")] = "bentoo",
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Save the VM's screen (PNG) -- for when looking is really needed."""
    from shidashi import vm

    _apply_work_dir(work_dir)
    try:
        typer.echo(str(vm.load_session(name).screenshot(dest)))
    except (vm.VmError, OSError) as err:
        raise _vm_error(err) from err


@vm_app.command("stop")
def vm_stop(
    name: Annotated[str, typer.Option("--name")] = "bentoo",
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Power the VM off."""
    from shidashi import vm

    _apply_work_dir(work_dir)
    try:
        vm.load_session(name).stop()
    except vm.VmError as err:
        raise _vm_error(err) from err


@vm_app.command("test")
def vm_test(
    iso: Path,
    firmware: Annotated[str, typer.Option("--firmware", help="bios, uefi or both.")] = "both",
    cid: Annotated[int, typer.Option("--cid", min=3)] = 42,
    screenshots: Annotated[
        Path | None, typer.Option("--screenshots", help="Also save a screenshot per boot here.")
    ] = None,
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """The automated boot test: boot ``iso`` and check what its system.yaml declared.

    Audited like a build (steps boot:<firmware> and check:<name>, the report
    attached as boot-test.json). Exit 1 when any check fails.
    """
    from shidashi import vm

    _apply_work_dir(work_dir)
    if firmware not in ("bios", "uefi", "both"):
        raise _vm_error(ValueError("--firmware is bios, uefi or both"))
    firmwares = ("bios", "uefi") if firmware == "both" else (firmware,)
    try:
        info = vm.read_build_info(iso.resolve())
        recipe = _resolve(info["arch"], info["flavor"], info["init"])
        with _audited("vm-test", recipe, iso=str(iso.resolve()), firmwares=list(firmwares)):
            report = vm.boot_test(
                iso.resolve(), firmwares=firmwares, cid=cid, screenshots=screenshots
            )
    except (vm.VmError, config.UnknownAxisError, RecipeChainError) as err:
        raise _vm_error(err) from err

    console = Console()
    for name, result in report["firmwares"].items():
        table = Table(
            title=f"boot test {name}: "
            f"{'PASSED' if result['passed'] else 'FAILED'} "
            f"(ssh after {result['ssh_after_s']} s)"
        )
        table.add_column("check")
        table.add_column("result")
        table.add_column("got")
        for check in result["checks"]:
            table.add_row(
                check["check"], "ok" if check["passed"] else "FAIL", escape(str(check["got"]))[:80]
            )
        for metric, value in result["metrics"].items():
            table.add_row(f"[dim]{metric}[/dim]", "", escape(value)[:80])
        console.print(table)
    if not report["passed"]:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
