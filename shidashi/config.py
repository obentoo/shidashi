"""Resolução de caminhos da árvore ``variants/`` do Shidashi.

Este módulo descobre o diretório ``variants/`` (com override por variável de
ambiente ``SHIDASHI_VARIANTS_DIR``) e resolve os caminhos de cada eixo
(``arch``/``flavor``/``init``) e do fragmento ``base``. Não parseia YAML — isso
é responsabilidade de :mod:`shidashi.recipe`, cujos loaders recebem um ``Path``
explícito. Mantém-se puro: sem estado mutável global; a variável de ambiente é
lida a cada chamada para que testes possam fazer ``monkeypatch``.
"""

import os
from pathlib import Path

from shidashi.recipe import ResolvedRecipe

_ENV_VAR = "SHIDASHI_VARIANTS_DIR"
_SCRATCH_ENV = "SHIDASHI_SCRATCH"
_CACHE_ENV = "SHIDASHI_CACHE"
_SEEDS_ENV = "SHIDASHI_SEEDS_DIR"
_CATALYST_ENV = "SHIDASHI_CATALYST_DIR"


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
            f"valor {name!r} desconhecido para o eixo {axis!r}; disponíveis: {disponiveis}"
        )


def variants_dir() -> Path:
    """Devolve o diretório ``variants/`` (R6.1).

    Se ``SHIDASHI_VARIANTS_DIR`` estiver definida, usa-a; caso contrário localiza
    ``variants/`` relativo ao pacote: a raiz do projeto é o diretório-pai do
    pacote ``shidashi`` e ``variants/`` vive em ``<raiz>/variants``.
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


def scratch_dir() -> Path:
    """Devolve o diretório de scratch do fluxo *pretend* (R6.2).

    Honra ``SHIDASHI_SCRATCH`` (lida a cada chamada, como :func:`variants_dir`);
    na ausência usa o default ``/var/tmp/shidashi-pretend``. Todo estado efêmero
    da resolução (rootfs seedado) é confinado aqui.
    """
    override = os.environ.get(_SCRATCH_ENV)
    if override:
        return Path(override)
    return Path("/var/tmp/shidashi-pretend")


def cache_dir() -> Path:
    """Devolve o diretório de cache de stage3 baixados (R2.5).

    Honra ``SHIDASHI_CACHE`` (lida a cada chamada); default ``/var/cache/shidashi``.
    O tarball verificado é guardado aqui para reuso entre execuções.
    """
    override = os.environ.get(_CACHE_ENV)
    if override:
        return Path(override)
    return Path("/var/cache/shidashi")


def seeds_dir() -> Path:
    """Devolve o diretório ``seeds/`` do repo (pointer pinado) (R2.1).

    Honra ``SHIDASHI_SEEDS_DIR`` (lida a cada chamada); na ausência resolve
    ``seeds/`` relativo à raiz do projeto (mesma resolução de
    :func:`variants_dir`: o diretório-pai do pacote ``shidashi``).
    """
    override = os.environ.get(_SEEDS_ENV)
    if override:
        return Path(override)
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "seeds"


def build_root() -> Path:
    """Devolve a raiz dos rootfs de build (R5.1/R8.2): ``scratch_dir()/build``.

    Cada flavor/arch monta seu rootfs efêmero sob este diretório. Herda o
    override ``SHIDASHI_SCRATCH`` (lido por chamada) de :func:`scratch_dir`.
    """
    return scratch_dir() / "build"


def pkgdir(arch: str) -> Path:
    """Devolve o ``PKGDIR`` de pacotes binários por arch (R6.2): ``cache_dir()/binpkgs/<arch>``.

    Particionado por ``arch`` para que variantes de microarquitetura (``v3``,
    ``znver5``, …) não compartilhem binpkgs incompatíveis. Herda o override
    ``SHIDASHI_CACHE`` (lido por chamada) de :func:`cache_dir`.
    """
    return cache_dir() / "binpkgs" / arch


def catalyst_dir(arch: str) -> Path:
    """Devolve o diretório do storedir/saída do Catalyst por arch (story 005).

    Particionado por ``arch`` (como :func:`pkgdir`) para que stage3 de
    microarquiteturas distintas não colidam. Honra o override dedicado
    ``SHIDASHI_CATALYST_DIR`` (lido por chamada); na ausência usa
    ``cache_dir()/catalyst`` — herdando assim o override ``SHIDASHI_CACHE``.
    """
    override = os.environ.get(_CATALYST_ENV)
    base = Path(override) if override else cache_dir() / "catalyst"
    return base / arch


def catalyst_spec_dir(arch: str) -> Path:
    """Devolve o diretório de specs efêmeros do Catalyst por arch (story 005).

    Os specs stage1/2/3 são regeneráveis a cada build, logo vivem sob o scratch:
    ``scratch_dir()/catalyst/<arch>``. Herda o override ``SHIDASHI_SCRATCH``
    (lido por chamada) de :func:`scratch_dir`.
    """
    return scratch_dir() / "catalyst" / arch


def ccache_dir() -> Path:
    """Devolve o diretório ``ccache`` compartilhado (R6.2): ``cache_dir()/ccache``.

    Compartilhado entre flavors/archs (cache de compilação C/C++). Herda o
    override ``SHIDASHI_CACHE`` (lido por chamada).
    """
    return cache_dir() / "ccache"


def sccache_dir() -> Path:
    """Devolve o diretório ``sccache`` compartilhado (R6.2): ``cache_dir()/sccache``.

    Compartilhado entre flavors/archs (cache de compilação Rust). Herda o
    override ``SHIDASHI_CACHE`` (lido por chamada).
    """
    return cache_dir() / "sccache"


def distdir() -> Path:
    """Devolve o ``DISTDIR`` compartilhado (R6.2): ``cache_dir()/distfiles``.

    Compartilhado entre flavors/archs (tarballs de fonte baixados). Herda o
    override ``SHIDASHI_CACHE`` (lido por chamada).
    """
    return cache_dir() / "distfiles"


def fork_points_dir() -> Path:
    """Devolve o diretório de *fork points* (R6.2): ``cache_dir()/fork-points``.

    Guarda os marcos de fork entre estágios de build. Herda o override
    ``SHIDASHI_CACHE`` (lido por chamada).
    """
    return cache_dir() / "fork-points"


def state_dir() -> Path:
    """Devolve o diretório de estado de build persistido (R6.1): ``cache_dir()/state``.

    O estado de progresso de cada build vive aqui, sob o cache, para sobreviver ao
    teardown do rootfs efêmero. Herda o override ``SHIDASHI_CACHE`` (lido por chamada).
    """
    return cache_dir() / "state"


def build_state_path(recipe: ResolvedRecipe) -> Path:
    """Devolve o caminho do estado de uma receita (R6.1): ``state_dir()/<chave>.json``.

    A chave ``<arch>-<flavor>-<init>`` espelha a convenção de rootfs/fork-point,
    isolando o progresso por variante. Herda o override ``SHIDASHI_CACHE`` (lido por
    chamada) de :func:`state_dir`.
    """
    return state_dir() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}.json"
