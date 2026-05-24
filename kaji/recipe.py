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
            f"conflito na USE flag {flag!r} entre as camadas "
            f"{layer_a!r} e {layer_b!r}"
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


def load_base(path: Path) -> BaseFragment:
    return BaseFragment(**_read_yaml(path))


def load_arch(path: Path) -> ArchFragment:
    return ArchFragment(**_read_yaml(path))


def load_flavor(path: Path) -> FlavorFragment:
    return FlavorFragment(**_read_yaml(path))


def load_init(path: Path) -> InitFragment:
    return InitFragment(**_read_yaml(path))
