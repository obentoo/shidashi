"""Binhost — multi-instance management, index and signing (OVERVIEW §6.3 / §16 / §18.4).

Phase 0 skeleton: only the typed public signatures. The binhost is
partitioned by arch (OVERVIEW §6.3) and is a *graph* of ``(CPV, USE)`` nodes, not
an array (OVERVIEW §18.5); the GC is USE-aware so it can tell flavor variants
apart from bootstrap transients (OVERVIEW §18.4). Public distribution
requires a per-arch index and a GPG signature (OVERVIEW §16). Nothing here runs yet:
every body raises ``NotImplementedError``.
"""

from pathlib import Path


class BinpkgRef:
    """Reference to a binpkg instance in the multi-instance pool (OVERVIEW §6.2).

    Identifies a ``(CPV, USE)`` node of the binhost graph, distinguished by
    ``build_id`` (OVERVIEW §18.5). Skeleton: the constructor is not
    implemented yet.
    """

    def __init__(self, cpv: str, use: tuple[str, ...], build_id: int) -> None:
        raise NotImplementedError("Phase 0 — see OVERVIEW §18.4")


class Binhost:
    """Binpkg host for one arch: index, lookup and USE-aware GC (OVERVIEW §6.3).

    Wraps the tree of one arch (``binhost/<arch>/``), maintaining the
    ``Packages`` index and the multi-instance pool. Skeleton: no method is
    implemented.
    """

    def __init__(self, root: Path, arch: str) -> None:
        raise NotImplementedError("Phase 0 — see OVERVIEW §6.3")

    def reindex(self) -> Path:
        """Regenerate the arch's ``Packages`` index and return its path (OVERVIEW §16)."""
        raise NotImplementedError("Phase 0 — see OVERVIEW §16")

    def sign(self) -> None:
        """Sign the index/binpkgs with GPG for public distribution (OVERVIEW §16)."""
        raise NotImplementedError("Phase 0 — see OVERVIEW §16")

    def gc(self, *, keep: int = 2) -> tuple[BinpkgRef, ...]:
        """Prune transients/old versions (USE-aware) and return what was pruned (OVERVIEW §18.4)."""
        raise NotImplementedError("Phase 0 — see OVERVIEW §18.4")
