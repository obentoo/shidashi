"""Modelos de receita do Shidashi e loaders YAML→modelo.

A receita de uma imagem é uma CADEIA DE ESTÁGIOS (D24)::

    base ─► minimal ─► desktop ─► <flavor>

cada estágio declarando quem vem antes dele (``after:``), mais dois eixos
ortogonais: ``arch`` (knobs de CPU) e ``init`` (profile e seat). Todos os
fragmentos são modelos pydantic *frozen* com ``extra="forbid"``. O módulo
mantém-se livre de acoplamento com Portage e com ``config``: quem sabe onde os
arquivos moram passa um ``locate`` para :func:`load_chain`.

O USE não vive aqui. Até 2026-09-26 os fragmentos traziam ``use_prefer``, que
só era EXIBIDO (``recipe show``) e nunca chegava ao build; o USE de verdade
sempre veio do ``make.conf`` de cada camada, e agora essa é a única fonte.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

type SeedSource = Literal["download", "catalyst"]  # fonte do stage3 semente (story 005)
type UpdateMode = Literal["emptytree", "newuse"]

_STRICT = ConfigDict(frozen=True, extra="forbid")

#: The stage every chain starts from, and the only one without ``after``.
BASE_STAGE = "base"

#: Stages that live at ``variants/<name>/`` and give their phase their own
#: name. Every other stage is a flavor, at ``variants/flavor/<name>/``, and its
#: phase is called ``flavor`` -- one phase name however many flavors exist.
CORE_STAGES = ("base", "minimal", "desktop")


def stage_layer(name: str) -> str:
    """The portage-layer identity of a stage: ``minimal`` or ``flavor/kde``. Pure."""
    return name if name in CORE_STAGES else f"flavor/{name}"


def stage_phase_name(name: str) -> str:
    """The phase a stage runs as: its own name, or ``flavor`` for a flavor. Pure."""
    return name if name in CORE_STAGES else "flavor"


class UseBreak(BaseModel):
    """Quebra de ciclo curada: força uma USE flag durante o break-pass (R4.1).

    Frozen pydantic com ``extra="forbid"``. ``atom`` é o pacote, ``flag`` a USE
    flag e ``enable`` o sinal: ``enable=False`` (padrão) força a flag OFF durante
    o break-pass; ``enable=True`` força-a ON. Espelha :class:`resolve.CycleBreak`
    porém sem ``raw_line`` — aqui é dado curado, não extraído de saída.
    """

    model_config = _STRICT
    atom: str
    flag: str
    enable: bool = False


class Phase(BaseModel):
    """Uma fase de build: o que ela instala e sob qual configuração.

    Uma fase por estágio da cadeia (mais as que o ``init`` antepõe, como
    ``seat``). ``sets`` é a relação fase→set, declarada pelo estágio.

    - ``emptytree``: só a base -- a única reconstrução completa.
    - ``ships``: uma imagem entregue termina aqui (``minimal`` e cada flavor);
      o pipeline assenta os cortes de ciclo e grava o fork-point já assentado.
    - ``layers``: as camadas de portage EM VIGOR nesta fase, acumuladas do
      início da cadeia até o seu estágio (mais ``arch`` e ``init``). É isto
      que deixa o USE gráfico entrar no meio do caminho, no estágio desktop.
    """

    model_config = _STRICT
    name: str
    stage: str = ""
    packages: tuple[str, ...] = ()
    sets: tuple[str, ...] = ()
    use_break: tuple[UseBreak, ...] = ()
    emptytree: bool = False
    ships: bool = False
    layers: tuple[str, ...] = ()


class StageFragment(BaseModel):
    """Um estágio da cadeia: configuração (o ``portage/`` ao lado) + escolha.

    ``after`` nomeia o estágio anterior -- a cadeia é explícita. ``sets`` são os
    sets que ESTE estágio instala (o conteúdo mora em ``variants/kits/``, D25).
    ``exclude`` são átomos REMOVIDOS desses sets quando materializados no rootfs;
    note o que ele NÃO faz: não impede o átomo de entrar como DEPENDÊNCIA de
    outro pacote -- é "não peço", não "proíbo". ``use_break`` são os cortes de
    ciclo que valem a partir deste estágio, até o settle da imagem.

    ``init_sets`` maps an init to extra sets this stage installs only under it
    (kde's display manager: plasma-login-manager needs systemd, openrc gets
    sddm). They join ``sets`` on this stage's phase; other inits' are ignored.

    ``include`` puts back, from this stage on, atoms an EARLIER stage excluded or
    a kit marks catalog-only (``#atom``). They become the stage's own set,
    ``@include-<stage>`` (:func:`include_set`), installed by this stage's phase
    only: the kit stays filtered, so the shared fork points of the earlier
    stages never see the atom.
    """

    model_config = _STRICT
    stage: str
    after: str | None = None
    sets: tuple[str, ...] = ()
    init_sets: dict[str, tuple[str, ...]] = {}
    exclude: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    update: UpdateMode = "newuse"
    ships: bool = False
    use_break: tuple[UseBreak, ...] = ()


class BaseFragment(StageFragment):
    """O estágio ``base`` -- o núcleo, e a âncora do profile."""

    stage: str = BASE_STAGE
    update: UpdateMode = "emptytree"
    profile_base: str


class ArchFragment(BaseModel):
    model_config = _STRICT
    arch: str
    common_flags: str
    goamd64: str
    rustflags: str
    cpu_flags_x86: tuple[str, ...]
    runnable_on_build_host: bool = False
    tier: int = 2
    # fonte do stage3 semente (story 005): "download" baixa o genérico (default,
    # retrocompatível); "catalyst" gera um stage3 com -march do alvo via Catalyst.
    seed_source: SeedSource = "download"


class InitFragment(BaseModel):
    model_config = _STRICT
    init: str
    profile_suffix: str = ""  # token puro, SEM barra inicial
    phases_prepend: tuple[Phase, ...] = ()
    #: Atoms left out of every image built with this init, on top of the stages'
    #: own ``exclude``: what the other init does instead (metalog and ntp are
    #: OpenRC's logger and clock; systemd has journald and timesyncd). Per-init
    #: exclusion lives here, in the init layer -- the stages stay init-neutral
    #: (author's decision, 2026-10-01). Validated like any exclude.
    exclude: tuple[str, ...] = ()


class ResolvedRecipe(BaseModel):
    model_config = _STRICT
    arch: str
    #: The TARGET: the last stage of the chain -- ``minimal``, ``kde``, … The
    #: field keeps its historical name because the whole pipeline keys on it.
    flavor: str
    init: str
    profile: str
    common_flags: str
    goamd64: str
    rustflags: str
    cpu_flags_x86: tuple[str, ...]
    tier: int
    runnable_on_build_host: bool
    sets: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    #: Excluded atom -> the layer whose ``exclude:`` took it out first: a stage
    #: (``minimal``) or the init (``init/systemd``).
    exclude_origin: dict[str, str] = {}
    #: ``@include-<stage>`` set name -> the atoms that stage's ``include:`` puts back.
    includes: dict[str, tuple[str, ...]] = {}
    phases: tuple[Phase, ...]
    portage_layers: tuple[str, ...]
    #: The chain, base first: ``("base", "minimal", "desktop", "kde")``.
    stages: tuple[str, ...] = ()
    # default "download" mantém retrocompatível quem constrói ResolvedRecipe
    # diretamente; merge() sempre o preenche explicitamente a partir do arch.
    seed_source: SeedSource = "download"


class RecipeSourceError(Exception):
    """An arch fragment declares a knob that belongs to its ``make.conf``.

    ``COMMON_FLAGS``, ``GOAMD64``, ``RUSTFLAGS`` and ``CPU_FLAGS_X86`` have a
    single source of truth: ``variants/arch/<name>/portage/make.conf``, the file
    the build actually reads. They used to be repeated in ``recipe.yaml`` as
    well, and the two drifted apart silently -- the YAML still advertised
    ``-C link-arg=-fuse-ld=mold`` months after make.conf dropped it on purpose,
    so ``recipe show`` described a build that never happened.
    """


class RecipeFileError(ValueError):
    """A recipe.yaml that does not fit its model; the message names the file
    and the field (``exclude.0``: an empty ``-`` item, say)."""


def _model[M: BaseModel](model: type[M], path: Path) -> M:
    """``model`` built from the YAML at ``path``, a failure naming the file. I/O."""
    try:
        return model(**_read_yaml(path))
    except ValidationError as err:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in err.errors()
        )
        raise RecipeFileError(f"{path}: {problems}") from err


class RecipeChainError(Exception):
    """A broken stage chain: an ``after:`` that loops, or a stage that starts
    one without being the base, or a file that declares another stage's name."""


def _read_yaml(path: Path) -> dict[str, Any]:
    """Lê um YAML de mapeamento e devolve um dict.

    Levanta ``TypeError`` se o documento não for um mapeamento (ex.: lista ou
    escalar), para que loaders construam modelos a partir de dicts apenas.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"esperado mapeamento YAML em {path}, obtido {type(data).__name__}")
    return data


def _ordered_unique(items: tuple[str, ...]) -> tuple[str, ...]:
    """Une preservando a ordem da primeira ocorrência e descartando duplicatas."""
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return tuple(seen)


#: The set a stage's ``include:`` becomes. No kit may use the prefix.
INCLUDE_SET_PREFIX = "include-"


def _exclude_origin(chain: tuple[StageFragment, ...], init: InitFragment) -> dict[str, str]:
    """Each excluded atom -> the first layer that excludes it, chain order then
    the init. Pure."""
    origin: dict[str, str] = {}
    for layer, atoms in (*((st.stage, st.exclude) for st in chain),
                         (f"init/{init.init}", init.exclude)):
        for atom in atoms:
            origin.setdefault(atom, layer)
    return origin


def include_set(stage: str) -> str:
    """``@include-<stage>``: the set holding ``stage``'s ``include:``. Pure."""
    return f"{INCLUDE_SET_PREFIX}{stage}"


def merge(
    base: BaseFragment,
    arch: ArchFragment,
    stages: tuple[StageFragment, ...],
    init: InitFragment,
) -> ResolvedRecipe:
    """Funde a cadeia de estágios com ``arch`` e ``init`` numa receita (D24). Puro.

    ``stages`` é o que :func:`load_chain` devolve: os estágios DEPOIS da base,
    na ordem da cadeia. O último é o alvo (``flavor`` da receita); cadeia vazia
    é a própria base.

    - **Profile:** ``base.profile_base`` + ``/<init.profile_suffix>`` quando há
      sufixo. Só ``init`` mexe no profile.
    - **Camadas:** ``base``, ``arch/<a>``, cada estágio na ordem da cadeia
      (:func:`stage_layer`), ``init/<i>``. ``portage_layers`` é a lista FINAL;
      cada fase carrega em ``layers`` as camadas até o seu estágio -- o
      ``init`` vale em todas, porque o profile e o seat valem desde o seed.
    - **Fases:** ``init.phases_prepend`` e depois uma por estágio
      (:func:`stage_phase_name`), com os sets, os cortes e o modo do estágio.
    - **sets / exclude:** união ordenada-única sobre toda a cadeia; os sets de
      cada estágio incluem os do seu ``init_sets`` para ``init.init``.
    - **include:** each stage's becomes ``@include-<stage>`` on its own phase.
      An atom the same stage, a later one or the init excludes is a contradiction
      and raises :class:`RecipeChainError`.
    """
    chain: tuple[StageFragment, ...] = (base, *stages)
    for prev, stage in zip(chain, chain[1:], strict=False):
        if stage.after != prev.stage:
            raise RecipeChainError(
                f"stage {stage.stage!r} follows {stage.after!r}, but the chain has {prev.stage!r}"
            )
    profile = (
        f"{base.profile_base}/{init.profile_suffix}" if init.profile_suffix else base.profile_base
    )
    head_layers = (BASE_STAGE, f"arch/{arch.arch}")
    init_layer = f"init/{init.init}"

    phases: list[Phase] = [
        p.model_copy(update={"layers": (*head_layers, init_layer)}) for p in init.phases_prepend
    ]
    stage_layers: list[str] = []
    stage_sets = {st.stage: (*st.sets, *st.init_sets.get(init.init, ())) for st in chain}
    includes: dict[str, tuple[str, ...]] = {}
    for i, st in enumerate(chain):
        if not st.include:
            continue
        later_excludes = {a for later in chain[i:] for a in later.exclude} | set(init.exclude)
        clash = sorted(set(st.include) & later_excludes)
        if clash:
            raise RecipeChainError(
                f"stage {st.stage!r} both includes and excludes {', '.join(clash)} "
                "(its own exclude:, a later stage's, or the init's)"
            )
        includes[include_set(st.stage)] = _ordered_unique(st.include)
        stage_sets[st.stage] = (*stage_sets[st.stage], include_set(st.stage))
    for stage in chain:
        if stage.stage != BASE_STAGE:
            stage_layers.append(stage_layer(stage.stage))
        phases.append(
            Phase(
                name=stage_phase_name(stage.stage),
                stage=stage.stage,
                sets=stage_sets[stage.stage],
                use_break=stage.use_break,
                emptytree=stage.update == "emptytree",
                ships=stage.ships,
                layers=(*head_layers, *stage_layers, init_layer),
            )
        )

    return ResolvedRecipe(
        arch=arch.arch,
        flavor=chain[-1].stage,
        init=init.init,
        profile=profile,
        common_flags=arch.common_flags,
        goamd64=arch.goamd64,
        rustflags=arch.rustflags,
        cpu_flags_x86=arch.cpu_flags_x86,
        tier=arch.tier,
        runnable_on_build_host=arch.runnable_on_build_host,
        sets=_ordered_unique(tuple(s for st in chain for s in stage_sets[st.stage])),
        exclude=_ordered_unique(
            (*(a for st in chain for a in st.exclude), *init.exclude)
        ),
        exclude_origin=_exclude_origin(chain, init),
        includes=includes,
        phases=tuple(phases),
        portage_layers=(*head_layers, *stage_layers, init_layer),
        stages=tuple(st.stage for st in chain),
        seed_source=arch.seed_source,
    )


def load_base(path: Path) -> BaseFragment:
    return _model(BaseFragment, path)


#: recipe.yaml field -> make.conf variable. These four live in make.conf only.
_ARCH_KNOBS_FROM_MAKE_CONF = {
    "common_flags": "COMMON_FLAGS",
    "goamd64": "GOAMD64",
    "rustflags": "RUSTFLAGS",
    "cpu_flags_x86": "CPU_FLAGS_X86",
}

#: A single-line ``VAR="value"`` assignment. Comment lines never match, because
#: they cannot start with an uppercase identifier.
_MAKE_CONF_ASSIGNMENT = re.compile(
    r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]*)"\s*(?:#.*)?$', re.MULTILINE
)


def _read_make_conf_scalars(path: Path) -> dict[str, str]:
    """Collect the plain ``VAR="value"`` assignments from a make.conf.

    Deliberately not a shell parser: it reads literal one-line assignments and
    nothing else. Values that reference other variables (``CFLAGS="${COMMON_FLAGS}"``)
    are returned unexpanded, which is fine because every knob read through here
    is a literal.
    """
    text = path.read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for m in _MAKE_CONF_ASSIGNMENT.finditer(text)}


def load_arch(path: Path) -> ArchFragment:
    """Load an arch fragment, taking the build knobs from its make.conf.

    ``recipe.yaml`` carries identity and policy (``arch``, ``tier``,
    ``runnable_on_build_host``, ``seed_source``); the flags that decide how code
    is compiled come from ``portage/make.conf`` next to it, which is the file
    the build itself reads. Declaring either of the four in the YAML is an error
    rather than an override -- see :class:`RecipeSourceError`.
    """
    data = _read_yaml(path)

    duplicated = sorted(key for key in _ARCH_KNOBS_FROM_MAKE_CONF if key in data)
    if duplicated:
        raise RecipeSourceError(
            f"{path}: {', '.join(duplicated)} must not be declared here; "
            f"they are read from {path.parent / 'portage' / 'make.conf'}"
        )

    make_conf = path.parent / "portage" / "make.conf"
    if not make_conf.is_file():
        raise RecipeSourceError(f"{path}: missing {make_conf}, which carries the build knobs")
    scalars = _read_make_conf_scalars(make_conf)

    missing = sorted(var for var in _ARCH_KNOBS_FROM_MAKE_CONF.values() if var not in scalars)
    if missing:
        raise RecipeSourceError(f"{make_conf}: missing required {', '.join(missing)}")

    data["common_flags"] = scalars["COMMON_FLAGS"]
    data["goamd64"] = scalars["GOAMD64"]
    data["rustflags"] = scalars["RUSTFLAGS"]
    data["cpu_flags_x86"] = tuple(scalars["CPU_FLAGS_X86"].split())
    return ArchFragment(**data)


def load_stage(path: Path) -> StageFragment:
    return _model(StageFragment, path)


def load_chain(target: str, locate: Callable[[str], Path]) -> tuple[StageFragment, ...]:
    """Walk ``after:`` from ``target`` down to the base; return the stages after it.

    ``locate`` maps a stage name to its YAML (the caller owns the filesystem
    layout -- this module stays free of ``config``). The result is ordered from
    the base outwards and EXCLUDES the base itself, so ``load_chain("kde", …)``
    is ``(minimal, desktop, kde)`` and ``load_chain("base", …)`` is ``()``.
    A stage without ``after``, other than the base, or a loop, is a
    :class:`RecipeChainError`.
    """
    chain: list[StageFragment] = []
    seen: set[str] = set()
    name = target
    while name != BASE_STAGE:
        if name in seen:
            raise RecipeChainError(f"stage chain loops back to {name!r}: {' -> '.join(seen)}")
        seen.add(name)
        stage = load_stage(locate(name))
        if stage.stage != name:
            raise RecipeChainError(
                f"{locate(name)} declares stage {stage.stage!r}, expected {name!r}"
            )
        if stage.after is None:
            raise RecipeChainError(f"stage {name!r} has no `after:`; only the base starts a chain")
        chain.append(stage)
        name = stage.after
    return tuple(reversed(chain))


def load_init(path: Path) -> InitFragment:
    return _model(InitFragment, path)
