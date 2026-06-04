"""Binhost — gestão multi-instance, índice e assinatura (OVERVIEW §6.3 / §16 / §18.4).

Esqueleto da Fase 0: apenas as assinaturas públicas tipadas. O binhost é
particionado por arch (OVERVIEW §6.3) e é um *grafo* de nós ``(CPV, USE)``, não
um array (OVERVIEW §18.5); o GC é ciente da USE para distinguir variantes de
flavor de transientes de bootstrap (OVERVIEW §18.4). A distribuição pública
exige índice por arch e assinatura GPG (OVERVIEW §16). Nada aqui executa ainda:
cada corpo levanta ``NotImplementedError``.
"""

from pathlib import Path


class BinpkgRef:
    """Referência a uma instância de binpkg no pool multi-instance (OVERVIEW §6.2).

    Identifica um nó ``(CPV, USE)`` do grafo do binhost, distinguido por
    ``build_id`` (OVERVIEW §18.5). Esqueleto: o construtor ainda não é
    implementado.
    """

    def __init__(self, cpv: str, use: tuple[str, ...], build_id: int) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §18.4")


class Binhost:
    """Host de binpkgs de uma arch: índice, consulta e GC ciente de USE (OVERVIEW §6.3).

    Encapsula o tree de um arch (``binhost/<arch>/``), mantendo o índice
    ``Packages`` e o pool multi-instance. Esqueleto: nenhum método é
    implementado.
    """

    def __init__(self, root: Path, arch: str) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §6.3")

    def reindex(self) -> Path:
        """Regenera o índice ``Packages`` da arch e devolve o caminho (OVERVIEW §16)."""
        raise NotImplementedError("Fase 0 — ver OVERVIEW §16")

    def sign(self) -> None:
        """Assina o índice/binpkgs com GPG para distribuição pública (OVERVIEW §16)."""
        raise NotImplementedError("Fase 0 — ver OVERVIEW §16")

    def gc(self, *, keep: int = 2) -> tuple[BinpkgRef, ...]:
        """Poda transientes/versões antigas (ciente de USE) e devolve o podado (OVERVIEW §18.4)."""
        raise NotImplementedError("Fase 0 — ver OVERVIEW §18.4")
