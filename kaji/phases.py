"""Phases — execução do build em fases + cache de camadas (OVERVIEW §6.4 / §6.5).

Camada de planejamento PURA da story 003 (grupos 3 + 4.1):

* **3.1** :func:`phase_target` / :func:`phase_emerge_argv` — alvo emerge de cada
  fase e o argv completo (``--emptytree`` só no ``rebuild``).
* **3.2** :func:`use_break_lines` / :func:`write_use_break` / :func:`clear_use_break`
  — ``package.use`` transitório do break-pass (I/O só contra um rootfs em disco,
  sem root).
* **3.3** :func:`parse_built_atoms` — átomos construídos a partir da saída
  ``emerge --verbose`` (reusa o matcher de :mod:`kaji.resolve`).
* **3.4** :func:`fork_point` / :func:`trunk_phase_names` — decisão de reuso do
  tronco (só sonda o filesystem) e nomes das fases do tronco.
* **4.1** :func:`snapshot_fork_point` / :func:`restore_fork_point` — captura e
  restauração do tronco como tarball (escrita atômica via temp + ``os.replace``;
  I/O contra uma árvore em disco, sem nspawn).

A orquestração privilegiada (``run_phase``/``run_phases``) da story 003 tarefa 5
ainda é esqueleto: cada corpo levanta ``NotImplementedError``.
"""

import os
import tarfile
from pathlib import Path

from kaji.container import Container
from kaji.recipe import Phase, ResolvedRecipe
from kaji.resolve import _iter_atom_lines

_USE_BREAK_FILE = ("etc", "portage", "package.use", "zz-kaji-use-break")


class PhaseResult:
    """Resultado da execução de uma única fase (OVERVIEW §6.4).

    Carrega a fase executada e o caminho do snapshot do fork-point quando houver
    (OVERVIEW §6.5). Esqueleto: o construtor ainda não é implementado.
    """

    def __init__(self, phase: Phase, snapshot: Path | None) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §6.4")


# --- 3.1 phase_target / phase_emerge_argv (PURO) -----------------------------


def phase_target(phase: Phase, recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Devolve o alvo ``emerge`` de uma fase (R3.1/R3.2/R3.3). Puro.

    Convenções por nome de fase: ``rebuild`` → ``@world``; ``seat`` →
    ``phase.packages`` (átomos explícitos); ``desktop`` → ``@<flavor>`` (o set do
    flavor); ``apps`` → ``@bentoo-apps``. Para qualquer outro nome, se ele é um
    set declarado em ``recipe.sets`` usa-se ``@<nome>``; senão recai-se nos
    ``phase.packages``.
    """
    if phase.name == "rebuild":
        return ("@world",)
    if phase.name == "seat":
        return phase.packages
    if phase.name == "desktop":
        return ("@" + recipe.flavor,)
    if phase.name == "apps":
        return ("@bentoo-apps",)
    if phase.name in recipe.sets:
        return ("@" + phase.name,)
    return phase.packages


def phase_emerge_argv(phase: Phase, recipe: ResolvedRecipe, *, emptytree: bool) -> list[str]:
    """Monta o argv de ``emerge`` para uma fase (R3.1). Puro.

    Sempre ``emerge --verbose`` seguido do(s) alvo(s) de :func:`phase_target`.
    ``--emptytree`` é emitido apenas na fase ``rebuild`` e somente quando
    ``emptytree`` é verdadeiro (reconstrução total do tronco).
    """
    return [
        "emerge",
        "--verbose",
        *(("--emptytree",) if (emptytree and phase.name == "rebuild") else ()),
        *phase_target(phase, recipe),
    ]


# --- 3.2 package.use transitório do break-pass -------------------------------


def use_break_lines(phase: Phase) -> tuple[str, ...]:
    """Renderiza as linhas ``package.use`` das quebras de ciclo da fase (R4.1). Puro.

    Uma linha por :class:`~kaji.recipe.UseBreak`: ``"<atom> <±flag>"`` onde o
    sinal é ``""`` (habilita) quando ``enable`` é verdadeiro e ``"-"``
    (desabilita) caso contrário. Fase sem quebras → tupla vazia.
    """
    return tuple(
        f"{ub.atom} {'' if ub.enable else '-'}{ub.flag}" for ub in phase.use_break
    )


def write_use_break(rootfs: Path, phase: Phase) -> Path | None:
    """Escreve o ``package.use`` transitório do break-pass (R4.1/R4.4).

    Grava as linhas de :func:`use_break_lines` em
    ``${rootfs}/etc/portage/package.use/zz-kaji-use-break`` (criando os
    diretórios-pai) e devolve o caminho escrito. Quando a fase não tem quebras,
    nada é escrito e devolve-se ``None``. Apenas I/O de filesystem — sem root.
    """
    lines = use_break_lines(phase)
    if not lines:
        return None
    target = rootfs.joinpath(*_USE_BREAK_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def clear_use_break(rootfs: Path) -> None:
    """Remove o ``package.use`` transitório do break-pass se presente (R4.4).

    Idempotente: limpar quando o arquivo já não existe é um no-op (não levanta).
    """
    rootfs.joinpath(*_USE_BREAK_FILE).unlink(missing_ok=True)


# --- 3.3 parse de átomos construídos -----------------------------------------


def parse_built_atoms(emerge_output: str) -> tuple[str, ...]:
    """Extrai os átomos ``cat/pkg-version`` de uma saída ``emerge --verbose`` (R3.4). Puro.

    Reusa o matcher compartilhado :func:`kaji.resolve._iter_atom_lines` (mesmo
    casamento de linha ``[ebuild ...]`` de :func:`kaji.resolve.parse_packages`).
    Saída sem linhas ``[ebuild ...]`` → tupla vazia.
    """
    return tuple(_iter_atom_lines(emerge_output))


# --- 3.4 decisão de fork-point (PURO, só sonda o filesystem) -----------------


def fork_point(
    recipe: ResolvedRecipe, *, snapshot: str, fork_points_dir: Path
) -> Path | None:
    """Devolve o snapshot do tronco a reusar antes da fase de desktop (R5.1/R5.2).

    A chave do tarball é ``<arch>-<flavor>-<init>-<snapshot>.tar`` sob
    ``fork_points_dir``. Devolve o caminho se o arquivo existir, senão ``None``.
    Apenas sonda o filesystem — não cria, extrai nem escreve nada.
    """
    candidate = fork_points_dir / f"{recipe.arch}-{recipe.flavor}-{recipe.init}-{snapshot}.tar"
    return candidate if candidate.exists() else None


def trunk_phase_names(recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Nomes das fases do *tronco*: as que precedem ``desktop`` (R5.4). Puro.

    O tronco é a parte das fases comum entre flavors (depende só do init), antes
    do fork na fase ``desktop``. Para um flavor sem fase ``desktop`` (ex.:
    ``minimal``) o tronco é toda a sequência de fases.
    """
    names: list[str] = []
    for phase in recipe.phases:
        if phase.name == "desktop":
            break
        names.append(phase.name)
    return tuple(names)


# --- 4.1 snapshot / restore do fork-point (tarball, I/O em disco) ------------


def snapshot_fork_point(rootfs: Path, dest: Path) -> Path:
    """Captura ``rootfs`` num tarball em ``dest`` e devolve ``dest`` (R5.1/R5.2).

    Escrita atômica: o tar é gravado primeiro num arquivo temporário irmão de
    ``dest`` (mesmo diretório, logo mesmo filesystem) e só então promovido via
    :func:`os.replace`, que consome o nome temporário — em caso de sucesso não
    fica nenhum temp pendente ao lado de ``dest``. Falha durante a escrita remove
    o temp parcial. O ``arcname=""`` mantém o conteúdo do rootfs na raiz do tar,
    de modo que :func:`restore_fork_point` o reconstrua diretamente sob outro
    diretório (layout relativo preservado). Ownership/devices fiéis de um rootfs
    real exigem root (coberto pelo teste de integração host-gated); a árvore em
    tmp faz round-trip de conteúdo + layout sem root.
    """
    tmp = dest.with_name(f".{dest.name}.tmp")
    try:
        with tarfile.open(tmp, "w") as tar:
            tar.add(rootfs, arcname="")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def restore_fork_point(tarball: Path, rootfs: Path) -> None:
    """Extrai ``tarball`` dentro de ``rootfs`` (R5.1/R5.2).

    Usa o filtro ``"tar"`` na extração para preservar ownership/permissões quando
    rodando como root (o teste de integração host-gated verifica ``st_uid == 0``);
    sob a árvore tmp sem root isto degrada para conteúdo + layout relativo, que é
    o que o round-trip unitário exige.
    """
    with tarfile.open(tarball, "r") as tar:
        tar.extractall(rootfs, filter="tar")


# --- orquestração (story 003 tarefa 5 — esqueleto) ---------------------------


def run_phase(container: Container, recipe: ResolvedRecipe, phase: Phase) -> PhaseResult:
    """Executa uma fase (um ``emerge`` ordenado) dentro do container (OVERVIEW §6.4)."""
    raise NotImplementedError("Fase 0 — ver OVERVIEW §6.4")


def run_phases(container: Container, recipe: ResolvedRecipe) -> tuple[PhaseResult, ...]:
    """Executa todas as fases da receita na ordem definida (OVERVIEW §6.4)."""
    raise NotImplementedError("Fase 0 — ver OVERVIEW §6.4")
