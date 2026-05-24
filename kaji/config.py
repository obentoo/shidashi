"""Resolução de caminhos da árvore ``variants/`` do Kaji.

Este módulo descobre o diretório ``variants/`` (com override por variável de
ambiente ``KAJI_VARIANTS_DIR``) e resolve os caminhos de cada eixo
(``arch``/``flavor``/``init``) e do fragmento ``base``. Não parseia YAML — isso
é responsabilidade de :mod:`kaji.recipe`, cujos loaders recebem um ``Path``
explícito. Mantém-se puro: sem estado mutável global; a variável de ambiente é
lida a cada chamada para que testes possam fazer ``monkeypatch``.
"""

import os
from pathlib import Path

_ENV_VAR = "KAJI_VARIANTS_DIR"


class UnknownAxisError(Exception):
    """Eixo/valor desconhecido ao resolver um diretório de ``variants/`` (R1.4).

    Carrega o ``axis`` consultado, o ``name`` inexistente e a lista de
    ``available`` (nomes válidos para esse eixo). A mensagem embute os nomes
    disponíveis para que a CLI (tarefa posterior) os exiba ao usuário.
    """

    def __init__(self, axis: str, name: str, available: list[str]) -> None:
        self.axis = axis
        self.name = name
        self.available = available
        disponiveis = ", ".join(available) if available else "(nenhum)"
        super().__init__(
            f"valor {name!r} desconhecido para o eixo {axis!r}; "
            f"disponíveis: {disponiveis}"
        )


def variants_dir() -> Path:
    """Devolve o diretório ``variants/`` (R6.1).

    Se ``KAJI_VARIANTS_DIR`` estiver definida, usa-a; caso contrário localiza
    ``variants/`` relativo ao pacote: a raiz do projeto é o diretório-pai do
    pacote ``kaji`` e ``variants/`` vive em ``<raiz>/variants``.
    """
    override = os.environ.get(_ENV_VAR)
    if override:
        return Path(override)
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "variants"


def available_names(axis: str) -> list[str]:
    """Lista ordenada dos subdiretórios sob ``variants_dir()/axis``.

    Cada subdiretório representa um valor do eixo. Devolve lista vazia se o
    diretório do eixo não existir.
    """
    axis_root = variants_dir() / axis
    if not axis_root.is_dir():
        return []
    return sorted(entry.name for entry in axis_root.iterdir() if entry.is_dir())


def axis_dir(axis: str, name: str) -> Path:
    """Devolve ``variants_dir()/axis/name`` (R1.3).

    Levanta :class:`UnknownAxisError` (com os nomes disponíveis) se o diretório
    resolvido não existir.
    """
    candidate = variants_dir() / axis / name
    if not candidate.is_dir():
        raise UnknownAxisError(axis, name, available_names(axis))
    return candidate


def recipe_path(axis: str, name: str) -> Path:
    """Devolve ``axis_dir(axis, name)/"recipe.yaml"`` (R1.3).

    Valida primeiro a existência do diretório do eixo via :func:`axis_dir`.
    """
    return axis_dir(axis, name) / "recipe.yaml"


def base_path() -> Path:
    """Devolve ``variants_dir()/"base"/"base.yaml"`` (R1.3)."""
    return variants_dir() / "base" / "base.yaml"
