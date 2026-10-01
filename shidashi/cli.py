"""CLI do Shidashi (Typer) — caminho ``recipe`` livre de Portage (R7.3).

Expõe o app raiz ``shidashi`` com o subgrupo ``recipe`` (``show``/``validate``/
``list``), os comandos reais ``pretend`` (resolução), ``factory`` (build de
binpkgs) e ``assemble`` (montagem de ISO) e o stub ``release`` (Fase 4). Os
comandos ``recipe`` apenas resolvem caminhos (``shidashi.config``), carregam e
fundem fragmentos (``shidashi.recipe``) e renderizam — sem jamais importar ou
acionar ``shidashi.portage_api``.

Mapeamento de erros (R5.2/R6.3): ``UnknownAxisError`` (de ``config``) e
``RecipeChainError`` (de ``merge``/``load_chain``) são capturados, exibidos como mensagem
amigável e convertidos em ``typer.Exit(1)`` — nenhum traceback escapa ao
usuário.
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

from shidashi import audit, config, publish
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
from shidashi.recipe import RecipeChainError, ResolvedRecipe
from shidashi.resolve import PretendReport, ResolveError, pretend_resolve
from shidashi.seed import SeedError, load_pointer
from shidashi.state import PhaseDiff
from shidashi.system import ConfigurationError
from shidashi.world import StaleWorldError

app = typer.Typer(no_args_is_help=True, help="Shidashi — catering de builds e ISOs do bentoo.")
recipe_app = typer.Typer(no_args_is_help=True, help="Inspeciona e valida receitas resolvidas.")
app.add_typer(recipe_app, name="recipe")
vm_app = typer.Typer(
    no_args_is_help=True,
    help="Boot an ISO in a VM and drive it over SSH on vsock (no network, no screen).",
)
app.add_typer(vm_app, name="vm")
kits_app = typer.Typer(no_args_is_help=True, help="The kit library: every package Bentoo builds.")
app.add_typer(kits_app, name="kits")

_err_console = Console(stderr=True)

# Referência ao ``sys.stdin`` do processo, capturada na importação do módulo. A
# guarda de TTY do ``--step`` (R2.6) consulta ``_PROCESS_STDIN.isatty()`` em vez de
# ``sys.stdin.isatty()`` direto porque o ``CliRunner`` do Typer/Click *substitui*
# ``sys.stdin`` por um wrapper não-tty durante ``invoke`` — uma leitura direta nunca
# refletiria o ``isatty`` real (nem o monkeypatch dos testes). O stdin verdadeiro do
# processo não é trocado, então esta referência preserva o estado de TTY observável.
_PROCESS_STDIN = sys.stdin


def _stdin_isatty() -> bool:
    """Diz se o stdin do processo é um terminal interativo (R2.6).

    Consulta a referência de stdin capturada na importação (:data:`_PROCESS_STDIN`),
    contornando a troca de ``sys.stdin`` que o ``CliRunner`` faz durante ``invoke``;
    fora de um runner de teste é exatamente o ``sys.stdin`` do processo.
    """
    return _PROCESS_STDIN.isatty()


class OutputFormat(StrEnum):
    """Formatos de saída de ``recipe show``.

    Nota: design.md §8 esboça ``class OutputFormat(str, Enum)``; usamos
    :class:`enum.StrEnum` (equivalente: membros são ``str``) para satisfazer o
    lint ``UP042`` da config ruff do projeto. Comportamento idêntico.
    """

    yaml = "yaml"
    json = "json"
    pretty = "pretty"


def _apply_work_dir(work_dir: Path | None) -> None:
    """Aponta cache e scratch sob um único ``--work-dir`` (precedência sobre env).

    Quando ``work_dir`` é dado, deriva ``cache → <work_dir>/cache`` e
    ``scratch → <work_dir>/scratch`` e ``runs → <work_dir>/runs`` setando
    ``SHIDASHI_CACHE``/``SHIDASHI_SCRATCH``/``SHIDASHI_RUNS`` no
    ambiente do processo. :mod:`shidashi.config` lê essas variáveis a cada chamada,
    então toda a árvore de caminhos (binpkgs, stage3, state, fork-points, rootfs
    de build) passa a viver sob ``work_dir`` — sem alterar a lógica de paths. A
    flag vence a env var do usuário (sobrescreve-a); ``None`` é um no-op (mantém
    env/default). ``--pkgdir`` continua tendo precedência sobre o ``cache`` daqui.
    """
    if work_dir is None:
        return
    os.environ["SHIDASHI_CACHE"] = str(work_dir / "cache")
    os.environ["SHIDASHI_SCRATCH"] = str(work_dir / "scratch")
    os.environ["SHIDASHI_RUNS"] = str(work_dir / "runs")


def _resolve(arch: str, flavor: str, init: str) -> ResolvedRecipe:
    """Carrega a cadeia de estágios do alvo e funde-a (D24).

    ``flavor`` é o ALVO: ``minimal`` ou um flavor. Delega a
    :func:`shidashi.config.load_recipe`, que levanta
    :class:`~shidashi.config.UnknownAxisError` para nomes desconhecidos e
    :class:`~shidashi.recipe.RecipeChainError` para uma cadeia quebrada. Não
    captura nada: os chamadores mapeiam as duas.
    """
    return config.load_recipe(arch, flavor, init)


def _render_pretty(resolved: ResolvedRecipe) -> None:
    """Renderiza a receita resolvida como tabelas ``rich`` (significativo num TTY)."""
    console = Console()
    summary = Table(title=f"recipe {resolved.arch} × {resolved.flavor} × {resolved.init}")
    summary.add_column("campo", style="bold cyan")
    summary.add_column("valor")
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
        typer.Option("--format", help="Formato de saída."),
    ] = OutputFormat.yaml,
) -> None:
    """Resolve, funde e renderiza a receita (R4.1–R4.3)."""
    try:
        resolved = _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    if output_format is OutputFormat.yaml:
        typer.echo(yaml.safe_dump(resolved.model_dump(), sort_keys=False).rstrip("\n"))
    elif output_format is OutputFormat.json:
        typer.echo(resolved.model_dump_json(indent=2))
    else:
        _render_pretty(resolved)


@recipe_app.command("validate")
def recipe_validate(arch: str, flavor: str, init: str) -> None:
    """Valida load+merge: sucesso → exit 0; conflito/eixo desconhecido → exit 1 (R5.1/R5.2)."""
    try:
        _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]inválida:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err
    typer.echo(f"válida: {arch} × {flavor} × {init}")


@recipe_app.command("list")
def recipe_list() -> None:
    """Lista o que se pode pedir: arches, ALVOS (imagens entregues) e inits (R6.1).

    O alvo é ``minimal`` ou um flavor (D24) -- não o eixo ``flavor``, onde o
    ``minimal`` já não mora. A base é implícita: toda cadeia começa nela.
    """
    rows = (
        ("arch", config.available_names("arch")),
        ("target", config.target_names()),
        ("init", config.available_names("init")),
    )
    for label, names in rows:
        typer.echo(f"{label}: {', '.join(names) if names else '(nenhum)'}")


def _render_report_pretty(report: PretendReport) -> None:
    """Renderiza o :class:`PretendReport` como tabelas ``rich`` (R1.1)."""
    console = Console()
    pkgs = Table(title=f"pretend {report.arch} × {report.flavor} × {report.init}")
    pkgs.add_column("pacotes resolvidos", style="bold cyan")
    for atom in report.packages:
        pkgs.add_row(atom)
    if not report.packages:
        pkgs.add_row("—")
    console.print(pkgs)

    cycles = Table(title="sugestões de quebra de ciclo (use_break)")
    cycles.add_column("atom", style="bold")
    cycles.add_column("USE")
    for cb in report.cycle_breaks:
        sign = "+" if cb.enable else "-"
        cycles.add_row(cb.atom, f"{sign}{cb.flag}")
    if not report.cycle_breaks:
        cycles.add_row("—", "(sem ciclos)")
    console.print(cycles)


@app.command("pretend")
def pretend(
    arch: str,
    flavor: str,
    init: str,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", help="Formato de saída: pretty (default) ou json."),
    ] = OutputFormat.pretty,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Usar só o cache; nunca tocar a rede.")
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Preservar o rootfs de scratch após o run.")
    ] = False,
    work_dir: Annotated[
        Path | None,
        typer.Option("--work-dir", help="Raiz de trabalho (cache+scratch sob <DIR>)."),
    ] = None,
) -> None:
    """Resolve a receita contra a árvore real via ``emerge --pretend`` (R1.1–R1.4).

    Sucesso (mesmo com ciclos reportados) → lista de pacotes + sugestões e exit 0.
    Erros conhecidos → mensagem amigável + exit 1, sem traceback. Num hard-conflict
    (``ResolveError`` com ``raw_output``) a saída crua do emerge vai para stderr.
    """
    _apply_work_dir(work_dir)
    try:
        report = pretend_resolve(arch, flavor, init, download=not no_download, keep=keep)
    except (SeedError, ResolveError, config.UnknownAxisError, RecipeChainError) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output, markup=False, highlight=False)
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
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
    command: str, resolved: ResolvedRecipe, **inputs: object
) -> Generator[audit.Recorder]:
    """Run the block as an audited run (:mod:`shidashi.audit`) and say where it went.

    The trail records the whole recipe, the pins and ``inputs``, then every step
    and command. If the runs directory cannot be written (not root, read-only),
    the run goes on unaudited with a visible warning instead of failing before
    the real checks (such as the root guard) could explain why.
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
        _err_console.print(f"[yellow]aviso:[/yellow] audit trail disabled: {escape(str(err))}")
        yield audit.current()
        return
    try:
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
    """Renderiza o :class:`FactoryResult` como tabelas ``rich`` (R1.1)."""
    console = Console()
    summary = Table(title=f"factory {arch} × {flavor} × {init}")
    summary.add_column("campo", style="bold cyan")
    summary.add_column("valor")
    summary.add_row("pkgdir", str(result.pkgdir))
    summary.add_row("phases", " → ".join(result.phases) or "—")
    fork = str(result.fork_point) if result.fork_point is not None else "—"
    reuse = "reusado" if result.fork_point_reused else "criado"
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
        console.print(f"[green]{summary_reuse}[/green] pacotes instalados dos binpkgs da geração")
    atoms = Table(title="átomos construídos")
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
    """Renderiza um :class:`~shidashi.state.PhaseDiff` como tabela ``rich`` (R2.2/R4.1).

    Mostra o nome da fase, a contagem de átomos construídos e — quando não vazios
    — os ``unexpected_rebuilds``, as ``use_changes`` e os ``blockers``. Usada tanto
    no checkpoint interativo (Task 7) quanto no relatório final por-fase.
    """
    table = Table(title=f"fase {diff.phase}")
    table.add_column("campo", style="bold cyan")
    table.add_column("valor")
    table.add_row("built", str(len(diff.built)))
    table.add_row("unexpected_rebuilds", "\n".join(diff.unexpected_rebuilds) or "—")
    table.add_row("use_changes", " ".join(diff.use_changes) or "—")
    table.add_row("blockers", "\n".join(diff.blockers) or "—")
    console.print(table)


def _render_factory_stepwise_pretty(
    result: FactoryResult, arch: str, flavor: str, init: str
) -> None:
    """Renderiza o relatório final do build stepwise (R1.x/R2.x).

    Reusa :func:`_render_factory_pretty` (pkgdir/fases/fork-point/átomos/settle) e
    complementa com o ``stopped_at`` (o rótulo onde parou, ou ``completed`` quando
    ``None``) e os diffs por fase de ``phase_diffs``. O caminho de stop limpo
    imprime o valor de ``stopped_at`` (ex.: ``minimal``) em stdout.
    """
    _render_factory_pretty(result, arch, flavor, init)
    console = Console()
    label = result.stopped_at if result.stopped_at is not None else "completed"
    status = Table(title="stepwise")
    status.add_column("campo", style="bold cyan")
    status.add_column("valor")
    status.add_row("stopped_at", label)
    status.add_row("completed_phases", " → ".join(result.completed_phases) or "—")
    console.print(status)
    for diff in result.phase_diffs:
        _render_phase_diff(diff, console)


def _prompt_choice(prompt: str, choices: list[str], default: str) -> str:
    """Pergunta uma escolha entre ``choices`` via ``rich`` (default em não-resposta).

    Wrapper fino sobre :meth:`rich.prompt.Prompt.ask` — restringe a entrada a
    ``choices`` e devolve o default quando o usuário só aperta Enter. Isolado para
    manter os callbacks interativos (checkpoint/falha) curtos e testáveis.
    """
    return Prompt.ask(prompt, choices=choices, default=default)


def _on_checkpoint(phase: str, diff: PhaseDiff) -> CheckpointDecision:
    """Checkpoint pós-fase: renderiza o diff e pergunta continuar/parar/shell (R2.2/R2.3).

    Mostra o :class:`~shidashi.state.PhaseDiff` da fase e mapeia a escolha do usuário
    em :class:`~shidashi.factory.CheckpointDecision`: ``c`` → ``CONTINUE`` (segue),
    ``s`` → ``STOP`` (interrompe sem settle), ``sh`` → ``SHELL`` (a camada de build
    abre o shell e re-apresenta o MESMO checkpoint).
    """
    console = Console()
    console.print(f"[bold green]checkpoint[/bold green] após a fase {phase}")
    _render_phase_diff(diff, console)
    choice = _prompt_choice("continuar/parar/shell", ["c", "s", "sh"], "c")
    if choice == "s":
        return CheckpointDecision.STOP
    if choice == "sh":
        return CheckpointDecision.SHELL
    return CheckpointDecision.CONTINUE


def _on_failure(phase: str, err: Exception) -> FailureDecision:
    """Callback de falha de fase: imprime a saída do emerge e pergunta retry/abort (R3.1–R3.3).

    ``err`` é a :class:`~shidashi.factory.FactoryError` da fase; imprime ``err.phase`` e
    ``err.output`` (a saída crua do ``emerge``) em stderr e mapeia a escolha em
    :class:`~shidashi.factory.FailureDecision`: ``r`` → ``RETRY`` (re-roda a mesma fase),
    qualquer outra → ``ABORT``. A abertura do shell de falha é da camada de
    build/driver — o callback apenas imprime e pergunta (R3.4: nunca pula a fase).
    """
    failing = getattr(err, "phase", None) or phase
    output = getattr(err, "output", "")
    _err_console.print(f"[bold red]falha na fase[/bold red] {failing}: {escape(str(err))}")
    if output:
        _err_console.print(output, markup=False, highlight=False)
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
        typer.Option("--format", help="Formato de saída: pretty (default) ou json."),
    ] = OutputFormat.pretty,
    emptytree: Annotated[
        bool,
        typer.Option(
            "--emptytree/--no-emptytree",
            help="Reconstruir a árvore inteira (--emptytree, default) ou reaproveitar binpkgs.",
        ),
    ] = True,
    pkgdir_opt: Annotated[
        Path | None,
        typer.Option("--pkgdir", help="PKGDIR host-side de saída (default: por arch)."),
    ] = None,
    keep: Annotated[
        bool, typer.Option("--keep", help="Preservar o rootfs de scratch após o build.")
    ] = False,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Usar só o cache; nunca tocar a rede.")
    ] = False,
    step: Annotated[
        bool,
        typer.Option("--step", help="Build interativo: pausa num checkpoint após cada fase."),
    ] = False,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Para após a fase nomeada (seed + fases da receita)."),
    ] = None,
    reset: Annotated[
        bool,
        typer.Option("--reset", help="Descarta o estado/rootfs persistido e recomeça do zero."),
    ] = False,
    force_resume: Annotated[
        bool,
        typer.Option("--force-resume", help="Retoma um estado obsoleto sem recomeçar."),
    ] = False,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Raiz de trabalho: cache+scratch sob <DIR> (vence SHIDASHI_CACHE/_SCRATCH).",
        ),
    ] = None,
    jobs: Annotated[
        int | None,
        typer.Option(
            "--jobs",
            min=1,
            help="MAKEOPTS=-jN -lN for this host, over the recipe's (every phase).",
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
) -> None:
    """Constrói os binpkgs (stage4) da receita num container nspawn (R1.1–R1.5/R8.x).

    Sem ``--step``/``--until``/``--reset`` roda o one-shot da story 003 (R8.2);
    qualquer um deles roteia para o build passo-a-passo resumível (``--step`` o
    torna interativo, com checkpoints e prompts de falha). Sucesso → relatório de
    fases/átomos/fork-point (+ ``stopped_at``/diffs no stepwise) + exit 0. Erros
    conhecidos (``FactoryError``/``StaleStateError``/``SeedError``/``ResolveError``/
    eixo desconhecido/conflito de receita/``--until`` inválido), incluindo a guarda
    de root, viram mensagem amigável + exit 1, sem traceback; uma ``FactoryError``
    imprime a fase que falhou e a ``output`` do emerge.
    """
    _apply_work_dir(work_dir)
    if jobs is not None:
        os.environ["SHIDASHI_JOBS"] = str(jobs)  # read by resolve.apply_portage
    try:
        resolved = _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    try:
        pkgdir = pkgdir_opt if pkgdir_opt is not None else _generation_pkgdir(arch, init)
    except SeedError as err:
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    if stop_after is not None and stop_after not in resolved.stages:
        _err_console.print(
            f"[bold red]erro:[/bold red] --stop-after {stop_after!r} is not a stage of "
            f"{arch}×{flavor}×{init}; stages: {' → '.join(resolved.stages)}"
        )
        raise typer.Exit(1)
    if stop_after is not None and (update or step or until is not None or reset or force_resume):
        _err_console.print(
            "[bold red]erro:[/bold red] --stop-after belongs to a normal build; it does not "
            "combine with --update/--step/--until/--reset/--force-resume"
        )
        raise typer.Exit(1)
    if update and (step or until is not None or reset or force_resume):
        _err_console.print(
            "[bold red]erro:[/bold red] --update updates a built image in one run; it "
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
        )
        return

    if step and not _stdin_isatty():
        _err_console.print(
            "[bold red]erro:[/bold red] --step exige um terminal interativo (TTY); "
            "para runs não-interativos use --until <fase> ou retome com --reset/--force-resume"
        )
        raise typer.Exit(1)
    if step and output_format is OutputFormat.json:
        _err_console.print(
            "[bold red]erro:[/bold red] --step (checkpoints interativos) é incompatível "
            "com --format json; use o formato pretty (default)"
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
) -> None:
    """Caminho one-shot da story 003 — comportamento byte-a-byte inalterado (R8.2).

    With ``update`` it runs :meth:`Factory.update` instead of a build (D26).
    """
    try:
        with _audited("factory", resolved, pkgdir=str(pkgdir), update=update,
                      emptytree=emptytree, stop_after=stop_after):
            factory_obj = Factory(resolved, pkgdir)
            if update:
                result = factory_obj.update(download=download, keep=keep)
            else:
                result = factory_obj.build(
                    emptytree=emptytree, download=download, keep=keep, stop_after=stop_after
                )
    except FactoryError as err:
        if err.phase:
            _err_console.print(
                f"[bold red]falha na fase[/bold red] "
                f"{escape(str(err.phase))}: {escape(str(err))}"
            )
        else:
            _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        if err.output:
            _err_console.print(err.output, markup=False, highlight=False)
        raise typer.Exit(1) from err
    except (SeedError, ResolveError, config.UnknownAxisError, RecipeChainError) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output, markup=False, highlight=False)
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
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
) -> None:
    """Caminho stepwise/resumível (R1.x/R2.x/R3.x/R6.3).

    Constrói os callbacks interativos só quando ``--step``; mapeia
    ``StaleStateError`` (antes do ``FactoryError`` genérico) para o prompt/diagnóstico
    de estado obsoleto (R6.3), ``ValueError`` (``--until`` inválido) e
    ``FactoryError`` (abort/falha de build) para exit 1 amigável.
    """
    factory_obj = Factory(resolved, pkgdir)
    on_checkpoint = _on_checkpoint if step else None
    on_failure = _on_failure if step else None

    # Laço de no máximo duas iterações: a 1ª invocação e, se ela levantar
    # StaleStateError e o usuário escolher reset/proceed num TTY, a re-invocação com
    # a flag resolvida. Manter a re-invocação DENTRO do mesmo try garante que uma
    # falha de build/`--until` inválido depois do reset também mapeie para exit 1
    # amigável (R3.3/R1.5) — nunca um traceback.
    while True:
        try:
            with _audited("factory-stepwise", resolved, pkgdir=str(pkgdir), until=until,
                          reset=reset, force_resume=force_resume):
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
            _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
            raise typer.Exit(1) from err
        except FactoryError as err:
            if err.phase:
                _err_console.print(
                    f"[bold red]falha na fase[/bold red] "
                    f"{escape(str(err.phase))}: {escape(str(err))}"
                )
            else:
                _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
            if err.output:
                _err_console.print(err.output, markup=False, highlight=False)
            raise typer.Exit(1) from err
        except (SeedError, ResolveError, config.UnknownAxisError, RecipeChainError) as err:
            if isinstance(err, ResolveError) and err.raw_output:
                _err_console.print(err.raw_output, markup=False, highlight=False)
            _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
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
    """Chama :meth:`Factory.build_stepwise` com a assinatura keyword-only da Task 6."""
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
    """Resolve um ``StaleStateError`` nas flags ``(reset, force_resume)`` de retomada (R6.3).

    Sem TTY → exit 1 instruindo a passar ``--reset`` ou ``--force-resume`` (nunca
    prossegue silenciosamente sobre estado obsoleto). Num TTY, pergunta
    ``reset``/``proceed``/``cancel``: ``cancel`` → exit 1; ``reset`` → ``(True, False)``
    (recomeça do zero); ``proceed`` → ``(False, True)`` (retoma assim mesmo). O
    chamador re-invoca :meth:`Factory.build_stepwise` com as flags devolvidas.
    """
    _err_console.print(f"[bold yellow]estado obsoleto:[/bold yellow] {escape(str(err))}")
    if not _stdin_isatty():
        _err_console.print(
            "[bold red]erro:[/bold red] estado de build obsoleto; rode com --reset "
            "(recomeça do zero) ou --force-resume (retoma assim mesmo)"
        )
        raise typer.Exit(1) from err

    choice = _prompt_choice("reset/proceed/cancel", ["reset", "proceed", "cancel"], "cancel")
    if choice == "cancel":
        raise typer.Exit(1) from err
    return (choice == "reset", choice == "proceed")


_STUB_MSG = "não implementado na Fase 0"


def _render_assemble_pretty(result: AssembleResult, arch: str, flavor: str, init: str) -> None:
    """The ``assemble`` result as a ``rich`` table: the ISOs, then every artifact."""
    console = Console()
    table = Table(title=f"ISO {arch} × {flavor} × {init} -- {result.name}")
    table.add_column("campo", style="bold cyan")
    table.add_column("valor")
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
        typer.Option("--format", help="Formato de saída: pretty (default) ou json."),
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
        typer.Option("--stage4", help="Also publish the configured system as a stage4 "
                     "tarball (tar.xz, xz -9e), made before the live user is added."),
    ] = False,
    no_sbom: Annotated[
        bool, typer.Option("--no-sbom", help="Do not generate the SBOM (syft).")
    ] = False,
    binhost_opt: Annotated[
        Path | None,
        typer.Option(
            "--binhost", help="Binhost (publish-pool) host-side de saída (default: por arch)."
        ),
    ] = None,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Usar só o cache; nunca tocar a rede.")
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Preservar o rootfs de scratch após a montagem.")
    ] = False,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            help="Raiz de trabalho: cache+scratch sob <DIR> (vence SHIDASHI_CACHE/_SCRATCH).",
        ),
    ] = None,
    jobs: Annotated[
        int | None,
        typer.Option(
            "--jobs",
            min=1,
            help="emerge --jobs N (binpkgs merged in parallel) and mksquashfs -processors N. "
            "Default: emerge one package at a time, mksquashfs on every CPU.",
        ),
    ] = None,
) -> None:
    """Monta a ISO live da receita a partir do binhost (OVERVIEW §7).

    Semeia um stage3, sobrepõe os layers da receita (USE final = a dos binpkgs,
    §18.6), puxa a fatia do flavor com ``emerge --usepkgonly`` e produz a ISO
    híbrida (squashfs + dracut ``dmsquash-live`` + grub-mkrescue). Sucesso →
    caminho da ISO + exit 0. Erros conhecidos (guarda de root, kernel ausente,
    eixo desconhecido/conflito de receita, seed/resolve, falha de emerge/dracut,
    falha de squashfs/ISO) viram mensagem amigável + exit 1, sem traceback.
    """
    _apply_work_dir(work_dir)
    try:
        resolved = _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    try:
        binhost = binhost_opt if binhost_opt is not None else _generation_pkgdir(arch, init)
    except SeedError as err:
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err
    if compression not in ("zstd", "xz", "both"):
        _err_console.print("[bold red]erro:[/bold red] --compression is zstd, xz or both")
        raise typer.Exit(1)
    compressions = ("zstd", "xz") if compression == "both" else (compression,)

    try:
        with _audited("assemble", resolved, binhost=str(binhost), output_dir=str(output_dir),
                      jobs=jobs, keep=keep, compressions=list(compressions), stage4=stage4,
                      sbom=not no_sbom) as trail:
            produced = Assembler(resolved, binhost, jobs=jobs).assemble(
                output_dir, download=not no_download, keep=keep, compressions=compressions,
                stage4=stage4, sbom=not no_sbom,
            )
        # the trail is complete only once the run closed: publish it beside the ISO
        if trail.root is not None:
            bundle = publish.bundle_run(trail.root, output_dir / f"{produced.name}.build.tar.zst")
            publish.update_sha256sums(output_dir, {bundle.name: publish.sha256_of(bundle)})
            produced = produced.model_copy(update={"artifacts": (*produced.artifacts, bundle)})
    except (
        AssemblerError, ImageError, SeedError, ResolveError, ConfigurationError, StaleWorldError
    ) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output, markup=False, highlight=False)
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err
    except subprocess.CalledProcessError as err:
        _err_console.print(
            f"[bold red]falha de emerge/dracut na ISO[/bold red] (exit {err.returncode})"
        )
        if err.stderr:
            _err_console.print(err.stderr, markup=False, highlight=False)
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
    first one's desktop fork point.
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
    return (flavors or ["minimal"]), isos


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
    skip_factory: Annotated[
        bool,
        typer.Option("--skip-factory", help="Assemble from the binpkgs already built."),
    ] = False,
    jobs: Annotated[
        int | None,
        typer.Option("--jobs", min=1, help="MAKEOPTS -jN in the factory; emerge --jobs N and "
                     "mksquashfs -processors N in the assemble."),
    ] = None,
    keep: Annotated[bool, typer.Option("--keep", help="Keep the build rootfs.")] = False,
    no_download: Annotated[
        bool, typer.Option("--no-download", help="Cache only; never touch the network.")
    ] = False,
    boot_test: Annotated[
        bool,
        typer.Option("--boot-test", help="Boot every ISO on BIOS and UEFI and check it "
                     "(shidashi vm test); a failed check fails the build."),
    ] = False,
    work_dir: Annotated[
        Path | None,
        typer.Option("--work-dir", help="Root of cache, scratch and runs."),
    ] = None,
) -> None:
    """Build a tree of images in one audited run: the factory, then every ISO.

    ``shidashi build v3 systemd --images minimal,kde,gnome`` builds the trunk once,
    each flavor from the desktop fork point, then assembles the three ISOs --
    with one audit trail covering all of it (steps factory:<image>, assemble:<image>).
    """
    _apply_work_dir(work_dir)
    if jobs is not None:
        os.environ["SHIDASHI_JOBS"] = str(jobs)
    if compression not in ("zstd", "xz", "both"):
        _err_console.print("[bold red]erro:[/bold red] --compression is zstd, xz or both")
        raise typer.Exit(1)
    compressions = ("zstd", "xz") if compression == "both" else (compression,)
    wanted = config.target_names() if images == "all" else [
        i.strip() for i in images.split(",") if i.strip()
    ]
    try:
        factory_targets, iso_targets = plan_tree(wanted)
        recipes = {t: _resolve(arch, t, init) for t in {*factory_targets, *iso_targets}}
        pkgdir = _generation_pkgdir(arch, init)
    except (typer.BadParameter, config.UnknownAxisError, RecipeChainError, SeedError) as err:
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err

    results: list[AssembleResult] = []
    try:
        with _audited(
            "build", recipes[iso_targets[-1]], images=iso_targets,
            factory=[] if skip_factory else factory_targets, pkgdir=str(pkgdir),
            output_dir=str(output_dir), compressions=list(compressions), stage4=stage4,
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
                            output_dir, download=not no_download, keep=keep,
                            compressions=compressions, stage4=stage4, sbom=not no_sbom,
                        )
                    )
            if boot_test:
                from shidashi import vm

                for target, result in zip(iso_targets, results, strict=True):
                    with run.step(f"boot-test:{target}") as step:
                        report = vm.boot_test(result.isos[0])
                        step.add(passed=report["passed"])
                        if not report["passed"]:
                            failed = [f"{fw}: {c['check']}"
                                      for fw, r in report["firmwares"].items()
                                      for c in r["checks"] if not c["passed"]]
                            raise AssemblerError(
                                f"{result.isos[0].name} failed its boot test: "
                                + "; ".join(failed)
                            )
    except FactoryError as err:
        where = f" {escape(str(err.phase))}" if err.phase else ""
        _err_console.print(f"[bold red]falha na fase[/bold red]{where}: {escape(str(err))}")
        if err.output:
            _err_console.print(err.output, markup=False, highlight=False)
        raise typer.Exit(1) from err
    except (
        AssemblerError, ImageError, SeedError, ResolveError, ConfigurationError, StaleWorldError
    ) as err:
        _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
        raise typer.Exit(1) from err
    except subprocess.CalledProcessError as err:
        _err_console.print(f"[bold red]falha de emerge/dracut[/bold red] (exit {err.returncode})")
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


@app.command("world")
def world(
    check: Annotated[
        bool,
        typer.Option("--check", help="Do not write: exit 1 if any world file is stale."),
    ] = False,
) -> None:
    """Write (or check) every image's world file, variants/<stage>/world.<init>.

    The flat list of packages each image asks for, generated from the kits: the
    other end of the set composition. Run it after changing a kit or a stage's
    sets, and commit the diff with the change.
    """
    from shidashi import world as world_mod

    stale: list[str] = []
    for recipe in _world_recipes():
        path = world_mod.world_file(recipe, config.variants_dir())
        text = world_mod.render(recipe)
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
            f"[bold red]erro:[/bold red] {len(stale)} world file(s) out of date; "
            "run `shidashi world` and commit the result"
        )
        raise typer.Exit(1)


@app.command("release")
def release(
    arch: Annotated[str, typer.Argument(help="Eixo arch (ignorado no stub).")] = "",
    flavor: Annotated[str, typer.Argument(help="Eixo flavor (ignorado no stub).")] = "",
    init: Annotated[str, typer.Argument(help="Eixo init (ignorado no stub).")] = "",
    all_variants: Annotated[
        bool, typer.Option("--all", help="Liberar todas as variantes.")
    ] = False,
) -> None:
    """Stub: publicação de release (não implementado na Fase 0) (R6.2)."""
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
    _err_console.print(f"[bold red]erro:[/bold red] {escape(str(err))}")
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
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Start ``iso`` in a VM reachable with `shidashi vm run`."""
    from shidashi import vm

    _apply_work_dir(work_dir)
    spec = vm.VmSpec(iso=iso.resolve(), uefi=uefi, cid=cid, memory=memory, cpus=cpus,
                     display=display)
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
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
) -> None:
    """Run a command as root in the VM; its exit code becomes ours."""
    from shidashi import vm

    _apply_work_dir(work_dir)
    try:
        result = vm.load_session(name).run_command(" ".join(command))
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
    firmware: Annotated[
        str, typer.Option("--firmware", help="bios, uefi or both.")
    ] = "both",
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
            report = vm.boot_test(iso.resolve(), firmwares=firmwares, cid=cid,
                                  screenshots=screenshots)
    except (vm.VmError, config.UnknownAxisError, RecipeChainError) as err:
        raise _vm_error(err) from err

    console = Console()
    for name, result in report["firmwares"].items():
        table = Table(title=f"boot test {name}: "
                      f"{'PASSED' if result['passed'] else 'FAILED'} "
                      f"(ssh after {result['ssh_after_s']} s)")
        table.add_column("check")
        table.add_column("result")
        table.add_column("got")
        for check in result["checks"]:
            table.add_row(check["check"], "ok" if check["passed"] else "FAIL",
                          escape(str(check["got"]))[:80])
        for metric, value in result["metrics"].items():
            table.add_row(f"[dim]{metric}[/dim]", "", escape(value)[:80])
        console.print(table)
    if not report["passed"]:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
