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

import json
import os
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from shidashi import config
from shidashi.assembler import Assembler, AssemblerError
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
from shidashi.seed import SeedError
from shidashi.state import PhaseDiff

app = typer.Typer(no_args_is_help=True, help="Shidashi — catering de builds e ISOs do bentoo.")
recipe_app = typer.Typer(no_args_is_help=True, help="Inspeciona e valida receitas resolvidas.")
app.add_typer(recipe_app, name="recipe")

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
    ``scratch → <work_dir>/scratch`` setando ``SHIDASHI_CACHE``/``SHIDASHI_SCRATCH`` no
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
        _err_console.print(f"[bold red]erro:[/bold red] {err}")
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
        _err_console.print(f"[bold red]inválida:[/bold red] {err}")
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
            _err_console.print(err.raw_output)
        _err_console.print(f"[bold red]erro:[/bold red] {err}")
        raise typer.Exit(1) from err

    if output_format is OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        _render_report_pretty(report)


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
    console.print(summary)

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
    imprime o valor de ``stopped_at`` (ex.: ``rebuild``) em stdout.
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
    _err_console.print(f"[bold red]falha na fase[/bold red] {failing}: {err}")
    if output:
        _err_console.print(output)
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
    try:
        resolved = _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeChainError) as err:
        _err_console.print(f"[bold red]erro:[/bold red] {err}")
        raise typer.Exit(1) from err

    pkgdir = pkgdir_opt if pkgdir_opt is not None else config.pkgdir(arch)

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
) -> None:
    """Caminho one-shot da story 003 — comportamento byte-a-byte inalterado (R8.2)."""
    try:
        result = Factory(resolved, pkgdir).build(emptytree=emptytree, download=download, keep=keep)
    except FactoryError as err:
        if err.phase:
            _err_console.print(f"[bold red]falha na fase[/bold red] {err.phase}: {err}")
        else:
            _err_console.print(f"[bold red]erro:[/bold red] {err}")
        if err.output:
            _err_console.print(err.output)
        raise typer.Exit(1) from err
    except (SeedError, ResolveError, config.UnknownAxisError, RecipeChainError) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output)
        _err_console.print(f"[bold red]erro:[/bold red] {err}")
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
            _err_console.print(f"[bold red]erro:[/bold red] {err}")
            raise typer.Exit(1) from err
        except FactoryError as err:
            if err.phase:
                _err_console.print(f"[bold red]falha na fase[/bold red] {err.phase}: {err}")
            else:
                _err_console.print(f"[bold red]erro:[/bold red] {err}")
            if err.output:
                _err_console.print(err.output)
            raise typer.Exit(1) from err
        except (SeedError, ResolveError, config.UnknownAxisError, RecipeChainError) as err:
            if isinstance(err, ResolveError) and err.raw_output:
                _err_console.print(err.raw_output)
            _err_console.print(f"[bold red]erro:[/bold red] {err}")
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
    _err_console.print(f"[bold yellow]estado obsoleto:[/bold yellow] {err}")
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


def _render_assemble_pretty(iso: Path, arch: str, flavor: str, init: str) -> None:
    """Renderiza o resultado do ``assemble`` como tabela ``rich`` (significativo num TTY)."""
    console = Console()
    table = Table(title=f"ISO {arch} × {flavor} × {init}")
    table.add_column("campo", style="bold cyan")
    table.add_column("valor")
    table.add_row("iso", str(iso))
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
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Caminho da ISO de saída (default: bentoo-<flavor>-<init>-<arch>.iso).",
        ),
    ] = None,
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
        _err_console.print(f"[bold red]erro:[/bold red] {err}")
        raise typer.Exit(1) from err

    binhost = binhost_opt if binhost_opt is not None else config.pkgdir(arch)
    iso_path = output if output is not None else Path(f"bentoo-{flavor}-{init}-{arch}.iso")

    try:
        produced = Assembler(resolved, binhost).assemble(
            iso_path, download=not no_download, keep=keep
        )
    except (AssemblerError, ImageError, SeedError, ResolveError) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output)
        _err_console.print(f"[bold red]erro:[/bold red] {err}")
        raise typer.Exit(1) from err
    except subprocess.CalledProcessError as err:
        _err_console.print(
            f"[bold red]falha de emerge/dracut na ISO[/bold red] (exit {err.returncode})"
        )
        if err.stderr:
            _err_console.print(err.stderr)
        raise typer.Exit(1) from err

    if output_format is OutputFormat.json:
        typer.echo(
            json.dumps(
                {
                    "iso": str(produced),
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


if __name__ == "__main__":
    app()
