"""Pipeline *pretend-resolve* do Kaji (OVERVIEW §18) — coração da story 002.

Sobrepõe os layers de portage da receita + os repos do host num rootfs seedado,
roda ``emerge --pretend --emptytree @world`` dentro de um ``systemd-nspawn`` e
parseia a saída em um :class:`PretendReport` (lista de pacotes + sugestões de
quebra de ciclo que alimentam a curadoria manual de ``use_break``, §18.7).

A lógica pura (mapeamento de layers, parse de repos.conf, parse da saída do
emerge) é unit-testada em CI; a orquestração privilegiada (seed/extract/nspawn)
é host-gated. ``emerge`` roda como subprocesso *dentro* do container — nunca
via ``import portage``.
"""

import configparser
import os
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kaji import config, seed
from kaji.container import CommandResult, Container
from kaji.recipe import ResolvedRecipe

_STRICT = ConfigDict(frozen=True, extra="forbid")

# Raiz dos repos sincronizados do host (bind-mounted RO no container). Atributo
# de módulo para que os testes possam redirecioná-lo via monkeypatch.
_HOST_REPOS_ROOT = Path("/var/db/repos")


class ResolveError(Exception):
    """Falha do pipeline pretend-resolve (não-root, repo ausente, hard-conflict).

    Carrega opcionalmente ``raw_output`` — a saída crua do ``emerge`` quando o
    erro é um conflito de dependências genuíno (hard-conflict, R5.4), para que a
    CLI a surfaceie ao usuário. Ausente (``None``) nos demais casos.
    """

    def __init__(self, message: str, *, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class CycleBreak(BaseModel):
    """Uma sugestão de quebra de ciclo extraída da saída do emerge (R5.2).

    Frozen pydantic. ``atom`` é o pacote, ``flag`` a USE flag sugerida, ``enable``
    o sinal (``True`` = ``+flag``, ``False`` = ``-flag``) e ``raw_line`` a linha
    crua de origem (rastreabilidade da curadoria §18.7).
    """

    model_config = _STRICT
    atom: str
    flag: str
    enable: bool
    raw_line: str


class PretendReport(BaseModel):
    """Resultado tipado de um ``kaji pretend`` (R1.1/R1.3/R5.2). Frozen pydantic."""

    model_config = _STRICT
    arch: str
    flavor: str
    init: str
    packages: tuple[str, ...]
    cycle_breaks: tuple[CycleBreak, ...]
    raw_output: str


# --- layering (R3.1) ---------------------------------------------------------


def _layer_dirs(recipe: ResolvedRecipe, variants_dir: Path) -> list[Path]:
    """Mapeia cada entrada de ``portage_layers`` → ``variants_dir/<entry>/portage``.

    As entradas são valores de layer crus (``"base"``, ``"arch/v3"`` …) **sem**
    prefixo ``variants/`` — não se faz double-join. **Pura.**
    """
    return [variants_dir / entry / "portage" for entry in recipe.portage_layers]


def apply_portage(rootfs: Path, recipe: ResolvedRecipe, *, variants_dir: Path) -> None:
    """Sobrepõe os ``portage/`` dos layers em ``${rootfs}/etc/portage`` (R3.1).

    Copia cada layer na ordem base→arch→flavor→init; layers posteriores
    sobrescrevem arquivos de mesmo caminho (layering file-level, sem merge de
    conteúdo). Um diretório de layer ausente levanta :class:`ResolveError`.
    """
    dest = rootfs / "etc" / "portage"
    dest.mkdir(parents=True, exist_ok=True)
    for layer_dir in _layer_dirs(recipe, variants_dir):
        if not layer_dir.is_dir():
            raise ResolveError(
                f"layer de portage ausente: {layer_dir} (receita "
                f"{recipe.arch}×{recipe.flavor}×{recipe.init})"
            )
        _copy_tree(layer_dir, dest)


def _copy_tree(src: Path, dest: Path) -> None:
    """Copia recursivamente ``src`` sobre ``dest``, sobrescrevendo arquivos."""
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dest / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())


# --- repo binding (R3.2, R3.3, R6.3) -----------------------------------------


def bind_repos(repos_conf_dir: Path) -> list[tuple[Path, Path]]:
    """Produz binds RO host→container para cada repo declarado (R3.2/R3.3).

    ``repos.conf`` é um **diretório** (estilo eselect-repo): itera seus ``*.conf``
    e parseia as stanzas ``[<name>]`` / ``location = …`` (stdlib ``configparser``).
    Para cada repo declarado mapeia o host ``_HOST_REPOS_ROOT/<name>`` para o
    mesmo caminho no container (RO). Se o host path não existir, levanta
    :class:`ResolveError` nomeando o repo e sugerindo ``emerge --sync``.
    """
    declared: list[str] = []
    for conf in sorted(repos_conf_dir.glob("*.conf")):
        parser = configparser.ConfigParser()
        parser.read(conf, encoding="utf-8")
        declared.extend(section for section in parser.sections())

    pairs: list[tuple[Path, Path]] = []
    for name in declared:
        host_path = _HOST_REPOS_ROOT / name
        if not host_path.is_dir():
            raise ResolveError(
                f"repo declarado {name!r} ausente em {host_path}; "
                f"rode 'emerge --sync' (ou 'eselect repo enable {name}') no host"
            )
        pairs.append((host_path, host_path))
    return pairs


# --- emerge output parsing (R5.2) — puro -------------------------------------


def _iter_atom_lines(emerge_output: str) -> Iterator[str]:
    """Itera os átomos ``cat/pkg-version`` das linhas ``[ebuild ...]``. Pura.

    Matcher compartilhado (R5.2 / R3.4): para cada linha cujo strip começa com
    ``[ebuild`` toma o primeiro token após o ``]`` e descarta o sufixo de
    slot/repo (``:slot::repo``), devolvendo ``cat/pkg-version``. Consumido tanto
    por :func:`parse_packages` (resolve) quanto por
    :func:`kaji.phases.parse_built_atoms`.
    """
    for line in emerge_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("[ebuild"):
            continue
        after = stripped.split("]", 1)
        if len(after) != 2:
            continue
        tokens = after[1].split()
        if not tokens:
            continue
        yield tokens[0].split(":", 1)[0]


def parse_packages(emerge_output: str) -> tuple[str, ...]:
    """Extrai a lista de átomos resolvidos das linhas ``[ebuild ...]`` (R5.2). Pura.

    Para cada linha que começa com ``[ebuild`` toma o primeiro token após o
    ``]`` e descarta o sufixo de slot/repo (``:slot::repo``), devolvendo
    ``cat/pkg-version``. Saída sem linhas ``[ebuild ...]`` → tupla vazia.
    Delega o casamento de linha a :func:`_iter_atom_lines`.
    """
    return tuple(_iter_atom_lines(emerge_output))


def parse_cycle_breaks(emerge_output: str) -> tuple[CycleBreak, ...]:
    """Extrai sugestões "Change USE" de dependências circulares (R5.2). Pura.

    Procura linhas do tipo ``- <atom> (Change USE: <±flag>)`` e mapeia cada uma
    a um :class:`CycleBreak` (atom, flag, sinal). Saída sem sugestões → tupla
    vazia. É o instrumento de curadoria de ``use_break`` (§18.7).
    """
    breaks: list[CycleBreak] = []
    for line in emerge_output.splitlines():
        stripped = line.strip()
        marker = "(Change USE:"
        if not stripped.startswith("-") or marker not in stripped:
            continue
        # "- media-libs/libsdl2-2.30.5 (Change USE: -pipewire)"
        head, _, tail = stripped.partition(marker)
        atom = head.lstrip("-").strip()
        change = tail.rstrip(")").strip()  # "-pipewire" / "+sdl"
        if not change or change[0] not in "+-":
            continue
        enable = change[0] == "+"
        flag = change[1:].strip()
        if not atom or not flag:
            continue
        breaks.append(CycleBreak(atom=atom, flag=flag, enable=enable, raw_line=stripped))
    return tuple(breaks)


# --- run + orchestration -----------------------------------------------------


def run_pretend(container: Container) -> CommandResult:
    """Roda ``emerge --pretend --emptytree @world`` no container (R5.1).

    Usa ``check=False`` — a semântica de saída (ciclo vs hard-conflict) é
    decidida por :func:`pretend_resolve`. Captura stdout+stderr no resultado.
    """
    return container.run(["emerge", "--pretend", "--emptytree", "@world"], check=False)


def pretend_resolve(
    arch: str,
    flavor: str,
    init: str,
    *,
    download: bool = True,
    keep: bool = False,
) -> PretendReport:
    """Orquestra merge→seed→layer→bind→nspawn→parse num report (R5.1/R5.3/R5.4).

    Guarda de privilégio (R6.1): se não-root, levanta :class:`ResolveError`
    acionável **antes** de qualquer trabalho — não tenta escalar privilégios.
    Um ciclo reportado é sucesso (exit 0): vira ``cycle_breaks`` no report. Um
    hard-conflict (saída não-zero sem sugestões de ciclo) levanta
    :class:`ResolveError` carregando ``raw_output`` (R5.4).
    """
    if os.geteuid() != 0:
        raise ResolveError(
            "kaji pretend requer root (systemd-nspawn + extração de stage3); "
            "rode como root — o Kaji não escala privilégios sozinho"
        )

    # import local evita ciclo de import (cli importa resolve no grupo 6).
    from kaji.cli import _resolve

    recipe = _resolve(arch, flavor, init)

    variants_dir = config.variants_dir()
    scratch = config.scratch_dir()
    rootfs = scratch / f"{arch}-{flavor}-{init}" / "rootfs"

    pointer = seed.load_pointer(init, seeds_dir=config.seeds_dir())
    tarball = seed.fetch_stage3(pointer, cache_dir=config.cache_dir(), download=download)
    seed.extract_stage3(tarball, rootfs)

    apply_portage(rootfs, recipe, variants_dir=variants_dir)
    binds = bind_repos(rootfs / "etc" / "portage" / "repos.conf")

    with Container(rootfs, ephemeral=not keep, binds=binds) as container:
        result = run_pretend(container)

    combined = result.stdout + result.stderr
    packages = parse_packages(combined)
    cycle_breaks = parse_cycle_breaks(combined)

    # hard-conflict: emerge falhou e não há sugestões de ciclo → erro (R5.4).
    if result.exit_code != 0 and not cycle_breaks:
        raise ResolveError(
            f"resolução insatisfatível para {arch}×{flavor}×{init} "
            "(conflito de dependências, não um ciclo)",
            raw_output=combined,
        )

    return PretendReport(
        arch=arch,
        flavor=flavor,
        init=init,
        packages=packages,
        cycle_breaks=cycle_breaks,
        raw_output=combined,
    )
