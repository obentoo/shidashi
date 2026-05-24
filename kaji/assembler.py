"""Assembler — ISO Assembler: monta a ISO live a partir do binhost (OVERVIEW §7).

Esqueleto da Fase 0: apenas as assinaturas públicas tipadas. O Assembler semeia
o rootfs com ``emerge --usepkgonly`` (puxa do binhost, não compila), comprime em
squashfs, gera o live medium com dracut e produz a ISO híbrida (OVERVIEW §7). É
imune a ciclo: navega o grafo multi-instance pela USE final (OVERVIEW §7,
§18.6). Nada aqui executa ainda: cada corpo levanta ``NotImplementedError``.
"""

from pathlib import Path

from kaji.recipe import ResolvedRecipe


class Assembler:
    """Monta a ISO de uma receita resolvida a partir do binhost (OVERVIEW §7).

    Recebe a receita resolvida e o diretório do binhost de onde puxar os binpkgs
    finais. Esqueleto: nenhum método é implementado.
    """

    def __init__(self, recipe: ResolvedRecipe, binhost_dir: Path) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §7")

    def assemble(self, output: Path) -> Path:
        """Produz a ISO em ``output`` e devolve o caminho do artefato gerado."""
        raise NotImplementedError("Fase 0 — ver OVERVIEW §7")
