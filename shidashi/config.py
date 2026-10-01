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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shidashi.recipe import ResolvedRecipe

from shidashi.recipe import ResolvedRecipe

_ENV_VAR = "SHIDASHI_VARIANTS_DIR"
_SCRATCH_ENV = "SHIDASHI_SCRATCH"
_CACHE_ENV = "SHIDASHI_CACHE"
_SEEDS_ENV = "SHIDASHI_SEEDS_DIR"
_CATALYST_ENV = "SHIDASHI_CATALYST_DIR"
_RUNS_ENV = "SHIDASHI_RUNS"


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
    """Devolve ``variants_dir()/"base"/"recipe.yaml"`` (R1.3)."""
    return variants_dir() / "base" / "recipe.yaml"


def stage_path(name: str) -> Path:
    """The YAML of a stage (D24): where each kind of stage lives.

    - ``base``, ``minimal``, ``desktop`` → ``variants/<name>/recipe.yaml``;
    - a flavor → ``variants/flavor/<name>/recipe.yaml``.

    Every stage is a ``recipe.yaml``, like every axis value: the place of a
    stage's ``exclude:`` is the same wherever the stage sits in the chain.

    An unknown name raises :class:`UnknownAxisError` listing the targets.
    """
    if name == "base":
        return base_path()
    core = variants_dir() / name / "recipe.yaml"
    if name in ("minimal", "desktop") and core.is_file():
        return core
    flavor = variants_dir() / "flavor" / name / "recipe.yaml"
    if flavor.is_file():
        return flavor
    raise UnknownAxisError("target", name, target_names())


def target_names() -> list[str]:
    """The images one can build: ``minimal`` and every flavor, in chain order."""
    return ["minimal", *available_names("flavor")]


def stage_names() -> list[str]:
    """Every stage of the chains, base first: the images plus ``base`` and
    ``desktop``, the stages they grow from without being images themselves."""
    from shidashi.recipe import CORE_STAGES

    return [*CORE_STAGES, *available_names("flavor")]


def load_recipe(arch: str, target: str, init: str, *, any_stage: bool = False) -> ResolvedRecipe:
    """Load the whole chain for ``target`` and merge it with ``arch`` and ``init``.

    The one entry point for "give me the recipe of this image": CLI, lab sync
    and tests all go through it, so the chain is walked in exactly one place.
    ``any_stage`` also accepts ``base`` and ``desktop``: stages, not images, so
    only read-only views (``shidashi world desktop``) ask for them.
    """
    from shidashi.recipe import load_arch, load_base, load_chain, load_init, merge

    if target not in (stage_names() if any_stage else target_names()):
        raise UnknownAxisError("target", target, target_names())
    return merge(
        load_base(base_path()),
        load_arch(recipe_path("arch", arch)),
        load_chain(target, stage_path),
        load_init(recipe_path("init", init)),
    )


def kits_dir() -> Path:
    """Devolve ``variants_dir()/"kits"``: a biblioteca de TODOS os sets (D25).

    Não é um eixo nem uma camada: não tem ``portage/`` nem receita. As camadas
    (base, flavor, …) só DECLARAM quais sets instalam; o conteúdo mora aqui, em
    ``kits/<categoria>/<set>``. As categorias são só para pessoas.
    """
    return variants_dir() / "kits"


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


def runs_dir() -> Path:
    """Where each run's audit trail lives (:mod:`shidashi.audit`): one directory per run.

    Honors ``SHIDASHI_RUNS`` (read on every call); default ``/var/log/shidashi/runs``.
    Kept apart from scratch and cache, which are wiped or reused: an audit trail
    outlives the builds it describes.
    """
    override = os.environ.get(_RUNS_ENV)
    if override:
        return Path(override)
    return Path("/var/log/shidashi/runs")


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


def pkgdir(arch: str, generation: str | None = None) -> Path:
    """Devolve o ``PKGDIR`` de pacotes binários por arch (R6.2): ``cache_dir()/binpkgs/<arch>``.

    Particionado por ``arch`` para que variantes de microarquitetura (``v3``,
    ``znver5``, …) não compartilhem binpkgs incompatíveis. Herda o override
    ``SHIDASHI_CACHE`` (lido por chamada) de :func:`cache_dir`.

    With ``generation`` -- the pinned stage3's snapshot -- one more level:
    ``binpkgs/<arch>/<generation>``. A new stage3 pin therefore starts an empty
    PKGDIR, and nothing is reused across generations but distfiles and ccache
    (D26). The generation fingerprint guards what this layout cannot see.
    """
    base = cache_dir() / "binpkgs" / arch
    return base / generation if generation is not None else base


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


def build_log_path(recipe: ResolvedRecipe) -> Path:
    """Where a factory run streams its container output: ``scratch_dir()/logs/<chave>.log``.

    Appended to across runs (each command is stamped), and kept outside the
    rootfs so that a discarded or restored rootfs does not take it along.
    """
    return scratch_dir() / "logs" / f"{recipe.arch}-{recipe.flavor}-{recipe.init}.log"
