"""Modelos de receita do Kaji e loaders YAML→modelo.

Este módulo define os fragmentos de receita (base, arch, flavor, init) e os
tipos resolvidos, todos como modelos pydantic *frozen* com ``extra="forbid"``.
Mantém-se livre de qualquer acoplamento com Portage: não importa
``portage_api`` nem ``config`` (resolução de caminhos é de outro módulo).
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

type UseToken = str  # uma USE flag, opcionalmente negada: "qt6", "-gtk"

_STRICT = ConfigDict(frozen=True, extra="forbid")


class UsePrefer(BaseModel):
    model_config = _STRICT
    add: tuple[UseToken, ...] = ()
    drop: tuple[UseToken, ...] = ()


class Phase(BaseModel):
    model_config = _STRICT
    name: str
    packages: tuple[str, ...] = ()
    use_break: tuple[UseToken, ...] = ()


class BaseFragment(BaseModel):
    model_config = _STRICT
    profile_base: str
    sets: tuple[str, ...] = ()
    phases: tuple[Phase, ...] = ()


class ArchFragment(BaseModel):
    model_config = _STRICT
    arch: str
    common_flags: str
    goamd64: str
    rustflags: str
    cpu_flags_x86: tuple[str, ...]
    runnable_on_build_host: bool = False
    tier: int = 2


class FlavorFragment(BaseModel):
    model_config = _STRICT
    flavor: str
    use_prefer: UsePrefer = UsePrefer()
    sets: tuple[str, ...] = ()
    override_ok: bool = False


class InitFragment(BaseModel):
    model_config = _STRICT
    init: str
    profile_suffix: str = ""  # token puro, SEM barra inicial
    use_prefer: UsePrefer = UsePrefer()
    phases_prepend: tuple[Phase, ...] = ()


class ResolvedUse(BaseModel):
    model_config = _STRICT
    enabled: tuple[UseToken, ...]
    disabled: tuple[UseToken, ...]


class ResolvedRecipe(BaseModel):
    model_config = _STRICT
    arch: str
    flavor: str
    init: str
    profile: str
    common_flags: str
    goamd64: str
    rustflags: str
    cpu_flags_x86: tuple[str, ...]
    tier: int
    runnable_on_build_host: bool
    use: ResolvedUse
    sets: tuple[str, ...]
    phases: tuple[Phase, ...]
    portage_layers: tuple[str, ...]


class RecipeConflictError(Exception):
    """Conflito de USE flag entre duas camadas (layers) da receita.

    Levantada pelo motor de merge (tarefa posterior). Aqui apenas definimos a
    exceção, carregando a flag em conflito e os nomes das duas camadas.
    """

    def __init__(self, flag: str, layer_a: str, layer_b: str) -> None:
        self.flag = flag
        self.layer_a = layer_a
        self.layer_b = layer_b
        super().__init__(
            f"conflito na USE flag {flag!r} entre as camadas {layer_a!r} e {layer_b!r}"
        )


def _read_yaml(path: Path) -> dict[str, Any]:
    """Lê um YAML de mapeamento e devolve um dict.

    Levanta ``TypeError`` se o documento não for um mapeamento (ex.: lista ou
    escalar), para que loaders construam modelos a partir de dicts apenas.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"esperado mapeamento YAML em {path}, obtido {type(data).__name__}")
    return data


def _accumulate_use(flavor: FlavorFragment, init: InitFragment) -> dict[str, tuple[str, str]]:
    """Acumula a intenção de USE das camadas ``flavor`` e ``init`` (R2.3).

    ``arch`` e ``base`` não contribuem com USE. Processa as camadas na ordem
    flavor → init. O estado mapeia ``flag -> (sign, layer_name)`` onde ``sign``
    é ``"+"`` (habilitar) ou ``"-"`` (desabilitar) e ``layer_name`` é a camada
    que fixou o sinal atual (``"flavor"`` ou ``"init"``).

    Um token ``"-gtk"`` significa ``flag="gtk", sign="-"``; ``"qt6"`` significa
    ``flag="qt6", sign="+"``. ``use_prefer.add`` entra como ``"+"`` e
    ``use_prefer.drop`` como ``"-"``.

    Regra de conflito (R3.1/R3.2, design §5.1): num conflito de sinal oposto, se
    a camada anterior for ``"flavor"`` e ``flavor.override_ok`` for ``False``, a
    flavor é autoritativa e levanta-se :class:`RecipeConflictError`. Caso
    contrário a camada posterior vence (override permitido). Repetições de mesmo
    sinal são idempotentes (dedup, sem erro).
    """
    state: dict[str, tuple[str, str]] = {}

    def apply(flag: str, sign: str, layer_name: str) -> None:
        prev = state.get(flag)
        if prev is not None and prev[0] != sign:
            prev_sign, prev_layer = prev
            if prev_layer == "flavor" and flavor.override_ok is False:
                raise RecipeConflictError(flag, prev_layer, layer_name)
            # senão: camada posterior vence (override permitido)
        state[flag] = (sign, layer_name)

    def apply_layer(prefer: UsePrefer, layer_name: str) -> None:
        for token in prefer.add:
            apply(token.lstrip("-"), "+", layer_name)
        for token in prefer.drop:
            apply(token.lstrip("-"), "-", layer_name)

    apply_layer(flavor.use_prefer, "flavor")
    apply_layer(init.use_prefer, "init")
    return state


def _ordered_unique(items: tuple[str, ...]) -> tuple[str, ...]:
    """Une preservando a ordem da primeira ocorrência e descartando duplicatas."""
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return tuple(seen)


def merge(
    base: BaseFragment,
    arch: ArchFragment,
    flavor: FlavorFragment,
    init: InitFragment,
) -> ResolvedRecipe:
    """Funde os quatro fragmentos numa :class:`ResolvedRecipe` (design §5–§7).

    - **Profile (R2.2):** ``base.profile_base`` acrescido de ``/<suffix>`` quando
      ``init.profile_suffix`` não é vazio (o sufixo é um token puro, sem barra
      inicial). ``arch`` e ``flavor`` nunca tocam no profile.
    - **Knobs de arch (R2.6):** ``common_flags, goamd64, rustflags,
      cpu_flags_x86, tier, runnable_on_build_host`` são copiados de ``arch``.
    - **portage_layers (R2.7):** tupla ordenada registrando a ordem das camadas
      base → arch → flavor → init. Como os caminhos de diretório de cada eixo
      são resolvidos noutro módulo (``config.py``, indisponível aqui), registra-se
      a *identidade* de cada eixo na ordem, pela convenção estável
      ``("base", f"arch/{arch.arch}", f"flavor/{flavor.flavor}", f"init/{init.init}")``.
      Não há I/O de filesystem.
    - **USE (R2.3):** ver :func:`_accumulate_use`. ``enabled`` = flags com sinal
      ``"+"`` ordenadas; ``disabled`` = flags com sinal ``"-"`` ordenadas
      (armazenadas SEM o ``"-"`` inicial).
    - **sets (R2.4):** união ordenada-única de ``base.sets + flavor.sets``.
    - **phases (R2.5):** ``init.phases_prepend + base.phases``; quando
      ``flavor.sets == ()`` omite-se a phase de nome ``"desktop"``.
    """
    profile = (
        f"{base.profile_base}/{init.profile_suffix}" if init.profile_suffix else base.profile_base
    )

    use_state = _accumulate_use(flavor, init)
    enabled = tuple(sorted(flag for flag, (sign, _) in use_state.items() if sign == "+"))
    disabled = tuple(sorted(flag for flag, (sign, _) in use_state.items() if sign == "-"))

    sets = _ordered_unique(base.sets + flavor.sets)

    base_phases: tuple[Phase, ...] = base.phases
    if flavor.sets == ():
        base_phases = tuple(p for p in base_phases if p.name != "desktop")
    phases = init.phases_prepend + base_phases

    return ResolvedRecipe(
        arch=arch.arch,
        flavor=flavor.flavor,
        init=init.init,
        profile=profile,
        common_flags=arch.common_flags,
        goamd64=arch.goamd64,
        rustflags=arch.rustflags,
        cpu_flags_x86=arch.cpu_flags_x86,
        tier=arch.tier,
        runnable_on_build_host=arch.runnable_on_build_host,
        use=ResolvedUse(enabled=enabled, disabled=disabled),
        sets=sets,
        phases=phases,
        portage_layers=(
            "base",
            f"arch/{arch.arch}",
            f"flavor/{flavor.flavor}",
            f"init/{init.init}",
        ),
    )


def load_base(path: Path) -> BaseFragment:
    return BaseFragment(**_read_yaml(path))


def load_arch(path: Path) -> ArchFragment:
    return ArchFragment(**_read_yaml(path))


def load_flavor(path: Path) -> FlavorFragment:
    return FlavorFragment(**_read_yaml(path))


def load_init(path: Path) -> InitFragment:
    return InitFragment(**_read_yaml(path))
