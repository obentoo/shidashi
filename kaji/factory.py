"""Factory — Package Factory: constrói binpkgs a partir de uma receita (OVERVIEW §6).

Esqueleto da Fase 0: apenas as assinaturas públicas tipadas. A Factory compila,
para uma :class:`~kaji.recipe.ResolvedRecipe`, os binpkgs multi-instance em
ambientes limpos por flavor (OVERVIEW §6.1–§6.2), executando o build em fases
(OVERVIEW §6.4) sobre o ``/etc/portage`` resolvido. Nada aqui compila ainda:
cada corpo levanta ``NotImplementedError``.
"""

from pathlib import Path

from kaji.recipe import ResolvedRecipe


class FactoryResult:
    """Resultado de uma execução da Factory (OVERVIEW §6).

    Carrega o diretório de binpkgs produzido e os átomos compilados. Esqueleto:
    o construtor ainda não é implementado.
    """

    def __init__(self, pkgdir: Path, built_atoms: tuple[str, ...]) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §6")


class Factory:
    """Constrói o stage4 (binpkgs) de uma receita resolvida (OVERVIEW §6).

    Recebe a receita resolvida e o diretório de saída de binpkgs (PKGDIR) e
    orquestra o build em fases num container limpo. Esqueleto: nenhum método é
    implementado.
    """

    def __init__(self, recipe: ResolvedRecipe, pkgdir: Path) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §6")

    def build(self, *, emptytree: bool = False) -> FactoryResult:
        """Compila os binpkgs da receita; ``emptytree`` força recompilação total."""
        raise NotImplementedError("Fase 0 — ver OVERVIEW §6")
