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

import hashlib
import shutil
import subprocess
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


# --- invocação privilegiada + orquestração (Task 4) --------------------------

# sufixos de tarball reconhecidos ao derivar o subpath da semente de bootstrap
_ARCHIVE_SUFFIXES = (".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst", ".tar")


def _seed_subpath(seed: Path) -> str:
    """Subpath da semente de bootstrap p/ o ``source_subpath`` do stage1.

    Deriva do nome do tarball genérico sem o sufixo de arquivo (o Catalyst
    referencia stages sem extensão sob o storedir).
    """
    name = seed.name
    for suffix in _ARCHIVE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _stage3_tarball(output_dir: Path, version_stamp: str) -> Path:
    """Caminho do stage3 que o Catalyst deve produzir (convenção de nome)."""
    return output_dir / f"stage3-{_SUBARCH}-{version_stamp}.tar.xz"


def _sha512_file(path: Path) -> str:
    """SHA-512 hex de ``path``, lido em blocos (idêntico a seed.verify_digest)."""
    h = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_catalyst(spec: Path) -> None:
    """Executa ``catalyst -f <spec>`` no host (privilegiado). Isolado p/ monkeypatch.

    Nunca ignora o código de retorno: saída não-zero levanta :class:`CatalystError`
    nomeando o spec (e portanto o stage) que falhou, espelhando
    ``seed.verify_signature``.
    """
    try:
        result = subprocess.run(
            ["catalyst", "-f", str(spec)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:
        raise CatalystError(f"falha ao executar catalyst -f {spec.name}: {err}") from err
    if result.returncode != 0:
        raise CatalystError(f"catalyst falhou em {spec.name}:\n{result.stderr.strip()}")


def build_stage3_catalyst(
    recipe: ResolvedRecipe,
    generic_seed: Path,
    *,
    version_stamp: str,
    snapshot_treeish: str,
    confdir: Path,
    scratch_dir: Path,
    output_dir: Path,
    expected_sha512: str = "",
) -> tuple[Path, str]:
    """Constrói o stage3 por microarquitetura via Catalyst (R3.1–R3.5, R4.1, R4.3).

    Orquestrador *thin* sobre :func:`render_specs` e :func:`_run_catalyst`:

    1. Falha cedo (antes de qualquer build) se ``catalyst`` não estiver no
       ``PATH`` — :class:`CatalystError` nomeando ``dev-util/catalyst`` (R3.3).
    2. Gera os specs e os grava em ``scratch_dir``; invoca ``catalyst`` uma vez
       por spec na ordem stage1→stage2→stage3 (R3.2). Uma saída não-zero
       propaga e aborta sem prosseguir aos stages seguintes (R3.4).
    3. Localiza o stage3 produzido em ``output_dir`` (erro claro se ausente) e
       computa seu SHA-512 (R4.1/R3.5).
    4. Se ``expected_sha512`` for fornecido e divergir, levanta
       :class:`CatalystError` em vez de devolver bytes divergentes (R4.3).

    Devolve ``(tarball, sha512)``. A colocação privilegiada de ``generic_seed``
    no storedir do Catalyst (o ``--seed`` de bootstrap) e o ``catalyst`` real são
    host-gated; aqui ``generic_seed`` define o ``source_subpath`` do stage1.
    """
    if shutil.which("catalyst") is None:
        raise CatalystError(
            "catalyst indisponível no host; instale dev-util/catalyst para seed_source=catalyst"
        )

    specs = render_specs(
        recipe,
        seed_subpath=_seed_subpath(generic_seed),
        version_stamp=version_stamp,
        snapshot_treeish=snapshot_treeish,
        confdir=confdir,
    )
    scratch_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    for target in ("stage1", "stage2", "stage3"):  # ordem do seed chain (R3.2)
        spec_path = scratch_dir / f"{target}.spec"
        spec_path.write_text(specs[target], encoding="utf-8")
        _run_catalyst(spec_path)  # propaga CatalystError, abortando (R3.4)

    tarball = _stage3_tarball(output_dir, version_stamp)
    if not tarball.is_file():
        raise CatalystError(f"catalyst não produziu o stage3 esperado: {tarball}")

    sha512 = _sha512_file(tarball)
    if expected_sha512 and sha512 != expected_sha512:
        raise CatalystError(
            f"sha512 divergente do stage3 buildado {tarball.name}: "
            f"esperado {expected_sha512}, obtido {sha512}"
        )
    return tarball, sha512
