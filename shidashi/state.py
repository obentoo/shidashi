"""Estado de build do Shidashi: modelos de progresso/diff + persistência (story 004).

Define os modelos *frozen* pydantic v2 (``extra="forbid"``, coleções ``tuple``,
idioma ``_STRICT`` de :mod:`shidashi.recipe`) que registram o progresso de um build
em fases — entradas do plano de emerge, o diff por fase (atoms construídos, USE
changes, rebuilds inesperados, blockers) e o ``BuildState`` agregado (R4.1/R4.4).

A persistência (R6.1/R6.2/R6.4) é pura I/O contra um ``Path`` explícito: o estado
é serializado como JSON sob o cache (sobrevive ao teardown do rootfs), gravado de
forma atômica (temp irmão + ``os.replace``), relido com erro claro quando
corrompido, limpo de forma idempotente e comparado contra ``snapshot``/``hash``
da receita para detectar staleness. Usa apenas stdlib (``hashlib``/``os``/
``tempfile``) mais pydantic.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from shidashi.recipe import ResolvedRecipe, UseBreak

_STRICT = ConfigDict(frozen=True, extra="forbid")


class StateError(Exception):
    """Falha ao carregar um estado de build persistido (R6.1).

    Levantada por :func:`load_state` quando o arquivo existe mas está corrompido
    (JSON inválido ou não conforme ao schema de :class:`BuildState`). Nunca é
    levantada por ausência do arquivo — esse caso devolve ``None``.
    """


class EmergePlanEntry(BaseModel):
    """Uma entrada do plano de emerge (saída de ``--pretend``) (R4.4).

    Frozen pydantic com ``extra="forbid"``. ``atom`` é o pacote/versão, ``op`` o
    código de operação do Portage (``N`` novo, ``R`` rebuild, ``U`` upgrade, …) e
    ``use_changes`` as USE flags alteradas para esse atom (vazio por padrão).
    """

    model_config = _STRICT
    atom: str
    op: str
    use_changes: tuple[str, ...] = ()


class PhaseDiff(BaseModel):
    """Diff observado de uma fase de build (R4.1/R4.4).

    Frozen pydantic com ``extra="forbid"``. Registra, para a fase ``phase``: os
    atoms efetivamente ``built``, os ``unexpected_rebuilds`` (rebuilds fora do
    plano), as ``use_changes`` aplicadas e os ``blockers`` encontrados. Todas as
    coleções são ``tuple`` e vazias por padrão (exceto ``built``).
    """

    model_config = _STRICT
    phase: str
    built: tuple[str, ...]
    unexpected_rebuilds: tuple[str, ...] = ()
    use_changes: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


class BuildState(BaseModel):
    """Estado agregado e persistido de um build em fases (R4.1/R4.4/R6.1).

    Frozen pydantic com ``extra="forbid"``. Identifica o build pela chave
    ``arch``/``flavor``/``init`` mais o ``snapshot`` do stage3 e o ``recipe_hash``
    da receita resolvida (ambos comparados em :func:`is_stale`). ``seed_done``
    marca a extração concluída; ``completed_phases`` as fases já encerradas;
    ``accumulated_breaks`` os :class:`~shidashi.recipe.UseBreak` acumulados; e
    ``phase_diffs`` o histórico de :class:`PhaseDiff` por fase.
    """

    model_config = _STRICT
    arch: str
    flavor: str
    init: str
    snapshot: str
    recipe_hash: str
    seed_done: bool = False
    # SHA-512 do stage3 buildado localmente pelo Catalyst (story 005); vazio
    # quando a seed veio por download. Aditivo/defaultado: JSON antigo (sem o
    # campo) ainda carrega sob extra="forbid".
    seed_sha512: str = ""
    #: The toolchain bootstrap ran over the seed (BOOTSTRAP-PROCESS §5). Additive
    #: and defaulted, like seed_sha512: older state files still load.
    bootstrap_done: bool = False
    completed_phases: tuple[str, ...] = ()
    accumulated_breaks: tuple[UseBreak, ...] = ()
    phase_diffs: tuple[PhaseDiff, ...] = ()


def recipe_hash(recipe: ResolvedRecipe) -> str:
    """Devolve o SHA-256 hex do JSON canônico da receita resolvida (R6.2).

    Estável (mesma receita → mesmo hash) e sensível a qualquer mudança de campo
    da receita, pois deriva de ``recipe.model_dump_json()`` (serialização
    determinística do pydantic v2). Usado por :func:`is_stale` para detectar uma
    receita divergente da persistida.
    """
    payload = recipe.model_dump_json().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def save_state(path: Path, state: BuildState) -> None:
    """Grava ``state`` em ``path`` como JSON, de forma atômica (R6.1).

    Escreve primeiro num arquivo temporário *irmão* (mesmo diretório, para que o
    ``os.replace`` seja atômico no mesmo filesystem) e só então o renomeia sobre
    ``path``. Em sucesso não deixa **nenhum** parcial: o temp ou virou o arquivo
    final (``replace``) ou foi removido. Erros de I/O (:class:`OSError`) propagam
    para o chamador surfacá-los.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump_json()
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def load_state(path: Path) -> BuildState | None:
    """Lê e devolve o :class:`BuildState` persistido em ``path`` (R6.1).

    Devolve ``None`` quando o arquivo está ausente (caso esperado: build novo).
    Se o arquivo existe mas está corrompido — JSON inválido ou não conforme ao
    schema — levanta :class:`StateError` com mensagem clara (nunca devolve
    ``None`` silenciosamente para um arquivo malformado).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return BuildState.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError) as err:
        raise StateError(f"estado de build corrompido em {path}: {err}") from err


def clear_state(path: Path) -> None:
    """Remove o estado persistido em ``path``, idempotente (R6.4).

    Ignora a ausência do arquivo (``missing_ok=True``): chamar repetidamente — ou
    sobre um build sem estado — nunca levanta. Suporta o reset/clear do fluxo
    interativo.
    """
    path.unlink(missing_ok=True)


def is_stale(state: BuildState, *, snapshot: str, recipe_hash: str) -> bool:
    """Diz se ``state`` está obsoleto frente ao ``snapshot``/``recipe_hash`` atuais (R6.2).

    Obsoleto (``True``) quando o ``snapshot`` do stage3 mudou **ou** o
    ``recipe_hash`` da receita mudou frente ao persistido — em ambos os casos o
    progresso salvo não é mais reaproveitável. O parâmetro ``recipe_hash`` é
    keyword-only e sombreia deliberadamente o nome da função :func:`recipe_hash`.
    """
    return state.snapshot != snapshot or state.recipe_hash != recipe_hash
