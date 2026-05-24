"""Phases — execução do build em fases + cache de camadas (OVERVIEW §6.4 / §6.5).

Esqueleto da Fase 0: apenas as assinaturas públicas tipadas. Cada fase é um
``emerge`` próprio, ordenado, com o ``/etc/portage`` do flavor já aplicado
(OVERVIEW §6.4); as fases iniciais dependem só do init e o fork ocorre tarde,
permitindo reusar o tronco via snapshot no fork-point (OVERVIEW §6.5). Nada aqui
executa ainda: cada corpo levanta ``NotImplementedError``.
"""

from pathlib import Path

from kaji.container import Container
from kaji.recipe import Phase, ResolvedRecipe


class PhaseResult:
    """Resultado da execução de uma única fase (OVERVIEW §6.4).

    Carrega a fase executada e o caminho do snapshot do fork-point quando houver
    (OVERVIEW §6.5). Esqueleto: o construtor ainda não é implementado.
    """

    def __init__(self, phase: Phase, snapshot: Path | None) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §6.4")


def run_phase(container: Container, recipe: ResolvedRecipe, phase: Phase) -> PhaseResult:
    """Executa uma fase (um ``emerge`` ordenado) dentro do container (OVERVIEW §6.4)."""
    raise NotImplementedError("Fase 0 — ver OVERVIEW §6.4")


def run_phases(container: Container, recipe: ResolvedRecipe) -> tuple[PhaseResult, ...]:
    """Executa todas as fases da receita na ordem definida (OVERVIEW §6.4)."""
    raise NotImplementedError("Fase 0 — ver OVERVIEW §6.4")


def fork_point(recipe: ResolvedRecipe) -> Path | None:
    """Devolve o snapshot do tronco a reusar antes da fase de desktop (OVERVIEW §6.5)."""
    raise NotImplementedError("Fase 0 — ver OVERVIEW §6.5")
