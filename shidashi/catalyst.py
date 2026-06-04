"""Geração de stage3 por microarquitetura via Catalyst (story 005).

Separa a lógica **pura** (geração do texto dos specs stage1/2/3 a partir da
receita resolvida — :func:`render_specs`) da execução **privilegiada** (invocar
``catalyst`` por shell-out, computar o SHA-512 do tarball — Task 4), no mesmo
idioma de ``seed.py``. A parte pura é unit-testada em CI não-Gentoo; a invocação
do ``catalyst`` é isolada num helper monkeypatchável.

O ajuste de microarquitetura (``-march`` etc.) NÃO entra pelo ``subarch`` do
Catalyst (não há subarch ``znver5`` upstream) e sim pelo ``portage_confdir`` —
que reusa o ``variants/arch/<arch>/portage`` já existente. O ``subarch`` fica no
baseline genérico ``amd64``.
"""

from pathlib import Path

from shidashi.recipe import ResolvedRecipe

# subarch baseline (o march específico vem via portage_confdir, não pelo subarch)
_SUBARCH = "amd64"
# compressão do tarball produzido — casa o `.tar.xz` da seed genérica
_COMPRESSION = "xz"
# ordem estável das chaves no texto do spec (determinismo, R2.6)
_KEY_ORDER = (
    "subarch",
    "target",
    "rel_type",
    "profile",
    "version_stamp",
    "snapshot_treeish",
    "source_subpath",
    "portage_confdir",
    "compression_mode",
)


class CatalystError(Exception):
    """Falha ao gerar specs ou ao executar o Catalyst (story 005).

    Levantada por entrada obrigatória ausente em :func:`render_specs` e, na
    Task 4, por ``catalyst`` indisponível, saída não-zero ou divergência de
    SHA-512 do tarball produzido.
    """


def _built_subpath(rel_type: str, stage_n: int, version_stamp: str) -> str:
    """Subpath do stage construído sob o storedir do Catalyst.

    Convenção do Catalyst: ``<rel_type>/stage<N>-<subarch>-<version_stamp>``.
    Usado para encadear ``source_subpath`` (stage2 parte do stage1, etc.).
    """
    return f"{rel_type}/stage{stage_n}-{_SUBARCH}-{version_stamp}"


def _render_one(fields: dict[str, str]) -> str:
    """Formata um spec como linhas ``chave: valor`` em ordem estável."""
    return "".join(f"{key}: {fields[key]}\n" for key in _KEY_ORDER)


def render_specs(
    recipe: ResolvedRecipe,
    *,
    seed_subpath: str,
    version_stamp: str,
    snapshot_treeish: str,
    confdir: Path,
) -> dict[str, str]:
    """Gera os specs stage1/2/3 a partir da receita resolvida (R2.1–R2.6). Pura.

    Mapeia ``recipe`` + stamps → ``{"stage1": <texto>, "stage2": ..., "stage3":
    ...}``. Todas as entradas variáveis são parâmetros — sem relógio nem
    aleatoriedade — de modo que mesmas entradas produzem texto byte-idêntico
    (R2.6). ``source_subpath`` é encadeado: stage1 parte de ``seed_subpath`` (a
    semente genérica de bootstrap, R2.2), stage2 do stage1 construído e stage3 do
    stage2 (R2.3). ``portage_confdir`` reusa o diretório portage do arch (R2.4) e
    ``rel_type`` deriva do arch com ``subarch`` no baseline ``amd64`` (R2.5).

    Levanta :class:`CatalystError` se uma entrada obrigatória vier vazia.
    """
    for name, value in (
        ("seed_subpath", seed_subpath),
        ("version_stamp", version_stamp),
        ("snapshot_treeish", snapshot_treeish),
    ):
        if not value:
            raise CatalystError(f"entrada obrigatória {name!r} vazia ao gerar specs do catalyst")

    rel_type = f"shidashi/{recipe.arch}"
    common = {
        "subarch": _SUBARCH,
        "rel_type": rel_type,
        "profile": recipe.profile,
        "version_stamp": version_stamp,
        "snapshot_treeish": snapshot_treeish,
        "portage_confdir": str(confdir),
        "compression_mode": _COMPRESSION,
    }
    sources = {
        "stage1": seed_subpath,
        "stage2": _built_subpath(rel_type, 1, version_stamp),
        "stage3": _built_subpath(rel_type, 2, version_stamp),
    }
    return {
        target: _render_one({**common, "target": target, "source_subpath": source})
        for target, source in sources.items()
    }
