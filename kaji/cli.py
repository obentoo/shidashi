"""CLI do Kaji (Typer) — caminho ``recipe`` livre de Portage (R7.3).

Expõe o app raiz ``kaji`` com o subgrupo ``recipe`` (``show``/``validate``/
``list``) e os stubs de Fase 0 (``factory``/``assemble``/``release``). Os
comandos ``recipe`` apenas resolvem caminhos (``kaji.config``), carregam e
fundem fragmentos (``kaji.recipe``) e renderizam — sem jamais importar ou
acionar ``kaji.portage_api``.

Mapeamento de erros (R5.2/R6.3): ``UnknownAxisError`` (de ``config``) e
``RecipeConflictError`` (de ``merge``) são capturados, exibidos como mensagem
amigável e convertidos em ``typer.Exit(1)`` — nenhum traceback escapa ao
usuário.
"""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from kaji import config
from kaji.factory import Factory, FactoryError, FactoryResult
from kaji.recipe import (
    RecipeConflictError,
    ResolvedRecipe,
    load_arch,
    load_base,
    load_flavor,
    load_init,
    merge,
)
from kaji.resolve import PretendReport, ResolveError, pretend_resolve
from kaji.seed import SeedError

app = typer.Typer(no_args_is_help=True, help="Kaji — forja de builds e ISOs do bentoo.")
recipe_app = typer.Typer(no_args_is_help=True, help="Inspeciona e valida receitas resolvidas.")
app.add_typer(recipe_app, name="recipe")

_err_console = Console(stderr=True)


class OutputFormat(StrEnum):
    """Formatos de saída de ``recipe show``.

    Nota: design.md §8 esboça ``class OutputFormat(str, Enum)``; usamos
    :class:`enum.StrEnum` (equivalente: membros são ``str``) para satisfazer o
    lint ``UP042`` da config ruff do projeto. Comportamento idêntico.
    """

    yaml = "yaml"
    json = "json"
    pretty = "pretty"


def _resolve(arch: str, flavor: str, init: str) -> ResolvedRecipe:
    """Carrega os quatro fragmentos e funde-os numa :class:`ResolvedRecipe`.

    Resolve caminhos via :mod:`kaji.config` (que levanta
    :class:`~kaji.config.UnknownAxisError` para nomes desconhecidos) e funde via
    :func:`kaji.recipe.merge` (que pode levantar
    :class:`~kaji.recipe.RecipeConflictError`). Não captura nada: deixa as duas
    exceções conhecidas propagarem para os chamadores mapearem.
    """
    base = load_base(config.base_path())
    arch_fragment = load_arch(config.recipe_path("arch", arch))
    flavor_fragment = load_flavor(config.recipe_path("flavor", flavor))
    init_fragment = load_init(config.recipe_path("init", init))
    return merge(base, arch_fragment, flavor_fragment, init_fragment)


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
    console.print(summary)

    use_table = Table(title="USE")
    use_table.add_column("enabled", style="green")
    use_table.add_column("disabled", style="red")
    use_table.add_row(
        "\n".join(resolved.use.enabled) or "—",
        "\n".join(resolved.use.disabled) or "—",
    )
    console.print(use_table)

    phases_table = Table(title="phases")
    phases_table.add_column("name", style="bold")
    phases_table.add_column("packages")
    phases_table.add_column("use_break")
    for phase in resolved.phases:
        phases_table.add_row(
            phase.name,
            " ".join(phase.packages) or "—",
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
    except (config.UnknownAxisError, RecipeConflictError) as err:
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
    except (config.UnknownAxisError, RecipeConflictError) as err:
        _err_console.print(f"[bold red]inválida:[/bold red] {err}")
        raise typer.Exit(1) from err
    typer.echo(f"válida: {arch} × {flavor} × {init}")


@recipe_app.command("list")
def recipe_list() -> None:
    """Lista os nomes disponíveis de cada eixo (base é implícita) (R6.1)."""
    for axis in ("arch", "flavor", "init"):
        names = config.available_names(axis)
        typer.echo(f"{axis}: {', '.join(names) if names else '(nenhum)'}")


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
) -> None:
    """Resolve a receita contra a árvore real via ``emerge --pretend`` (R1.1–R1.4).

    Sucesso (mesmo com ciclos reportados) → lista de pacotes + sugestões e exit 0.
    Erros conhecidos → mensagem amigável + exit 1, sem traceback. Num hard-conflict
    (``ResolveError`` com ``raw_output``) a saída crua do emerge vai para stderr.
    """
    try:
        report = pretend_resolve(arch, flavor, init, download=not no_download, keep=keep)
    except (SeedError, ResolveError, config.UnknownAxisError, RecipeConflictError) as err:
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
) -> None:
    """Constrói os binpkgs (stage4) da receita num container nspawn (R1.1–R1.5/R8.x).

    Sucesso → relatório de fases/átomos/fork-point + exit 0. Erros conhecidos
    (``FactoryError``/``SeedError``/``ResolveError``/eixo desconhecido/conflito de
    receita), incluindo a guarda de root, viram mensagem amigável + exit 1, sem
    traceback; uma ``FactoryError`` imprime a fase que falhou e a ``output`` do emerge.
    """
    try:
        resolved = _resolve(arch, flavor, init)
    except (config.UnknownAxisError, RecipeConflictError) as err:
        _err_console.print(f"[bold red]erro:[/bold red] {err}")
        raise typer.Exit(1) from err

    pkgdir = pkgdir_opt if pkgdir_opt is not None else config.pkgdir(arch)
    try:
        result = Factory(resolved, pkgdir).build(
            emptytree=emptytree, download=not no_download, keep=keep
        )
    except FactoryError as err:
        if err.phase:
            _err_console.print(f"[bold red]falha na fase[/bold red] {err.phase}: {err}")
        else:
            _err_console.print(f"[bold red]erro:[/bold red] {err}")
        if err.output:
            _err_console.print(err.output)
        raise typer.Exit(1) from err
    except (SeedError, ResolveError, config.UnknownAxisError, RecipeConflictError) as err:
        if isinstance(err, ResolveError) and err.raw_output:
            _err_console.print(err.raw_output)
        _err_console.print(f"[bold red]erro:[/bold red] {err}")
        raise typer.Exit(1) from err

    if output_format is OutputFormat.json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _render_factory_pretty(result, arch, flavor, init)


_STUB_MSG = "não implementado na Fase 0"


@app.command("assemble")
def assemble(arch: str, flavor: str, init: str) -> None:
    """Stub: montagem de ISO (não implementado na Fase 0) (R6.2)."""
    typer.echo(f"assemble: {_STUB_MSG}")
    raise typer.Exit(2)


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
