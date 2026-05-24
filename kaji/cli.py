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
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from kaji import config
from kaji.recipe import (
    RecipeConflictError,
    ResolvedRecipe,
    load_arch,
    load_base,
    load_flavor,
    load_init,
    merge,
)

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
            " ".join(phase.use_break) or "—",
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


_STUB_MSG = "não implementado na Fase 0"


@app.command("factory")
def factory(arch: str, flavor: str, init: str) -> None:
    """Stub: construção de stage4 (não implementado na Fase 0) (R6.2)."""
    typer.echo(f"factory: {_STUB_MSG}")
    raise typer.Exit(2)


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
