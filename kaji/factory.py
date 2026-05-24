"""Factory — Package Factory: constrói binpkgs a partir de uma receita (OVERVIEW §6).

Orquestra, para uma :class:`~kaji.recipe.ResolvedRecipe`, o build multi-instance
de binpkgs num container limpo por flavor (OVERVIEW §6.1–§6.3): seed/extração do
stage3 (ou reuso de um fork-point do tronco), sobreposição dos layers de portage
+ instalação dos sets, montagem dos binds (repos RO; PKGDIR/ccache/sccache/DISTDIR
RW sobre os caminhos fixos do ``make.conf``) e execução das fases (OVERVIEW §6.4)
seguida do settle-pass. A guarda de privilégio (root) é a **primeira** coisa que
:meth:`Factory.build` faz — o Kaji nunca escala privilégios sozinho (R8.1).

Os símbolos privilegiados de execução são importados a **nível de módulo**
(``fetch_stage3``/``extract_stage3``/``bind_repos``/``apply_portage``) para que os
testes possam monkeypatchá-los em ``kaji.factory`` e :meth:`build` os observe.
:class:`FactoryError` é definida em :mod:`kaji.phases` (evita o ciclo de import
``factory`` → ``phases``) e **re-exportada** aqui.
"""

import os
import shutil
from pathlib import Path

import pydantic

from kaji import config
from kaji.container import Container
from kaji.phases import FactoryError, fork_point, restore_fork_point, run_phases, trunk_phase_names
from kaji.recipe import ResolvedRecipe
from kaji.resolve import apply_portage, bind_repos
from kaji.seed import extract_stage3, fetch_stage3, load_pointer

__all__ = ["Factory", "FactoryError", "FactoryResult"]

# Caminhos fixos do container, definidos pelo ``make.conf`` base (OVERVIEW §6.3):
# a Factory escolhe os diretórios *host-side* (sob ``cache_dir()``) e os bind-monta
# sobre estes alvos fixos — o ``make.conf`` não é editado.
_PKGDIR_DST = Path("/var/cache/binpkgs")
_CCACHE_DST = Path("/var/cache/ccache")
_SCCACHE_DST = Path("/var/cache/sccache")
_DISTDIR_DST = Path("/var/cache/distfiles")


class FactoryResult(pydantic.BaseModel):
    """Resultado de uma execução da Factory (OVERVIEW §6).

    Value object *frozen* (pydantic v2, ``extra="forbid"``;
    ``arbitrary_types_allowed`` admite :class:`~pathlib.Path`). Carrega o
    ``pkgdir`` produzido, os ``built_atoms`` compilados, os nomes das ``phases``
    executadas, o ``fork_point`` materializado/reusado (``None`` quando não há),
    ``fork_point_reused`` (reuso do tronco) e os ``settle_atoms`` do settle-pass.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    pkgdir: Path
    built_atoms: tuple[str, ...]
    phases: tuple[str, ...]
    fork_point: Path | None
    fork_point_reused: bool
    settle_atoms: tuple[str, ...]


def _build_binds(
    recipe: ResolvedRecipe, *, pkgdir: Path, repos_conf_dir: Path | None = None
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """Monta os binds RO (repos) e RW (PKGDIR/caches) do container (R6.2/R6.3/R7.2). Pura.

    - ``binds_ro`` = :func:`kaji.resolve.bind_repos` sobre ``repos_conf_dir`` (o
      ``repos.conf`` do rootfs resolvido; os repos sincronizados do host ficam RO,
      a árvore do host nunca é mutada).
    - ``binds_rw`` mapeia os diretórios *host-side* (sob ``cache_dir()``) sobre os
      alvos fixos do ``make.conf`` do container: ``pkgdir`` → ``/var/cache/binpkgs``,
      ``ccache_dir()`` → ``/var/cache/ccache``, ``sccache_dir()`` → ``/var/cache/sccache``,
      ``distdir()`` → ``/var/cache/distfiles``. Binpkgs e caches persistem no host.

    ``repos_conf_dir`` é opcional para manter a chamada de teste (que monkeypatcha
    ``bind_repos``) trivial; :meth:`Factory.build` passa o ``repos.conf`` real do
    rootfs. ``bind_repos`` é resolvido via global do módulo (monkeypatchável).
    """
    binds_ro = bind_repos(repos_conf_dir if repos_conf_dir is not None else Path())
    binds_rw: list[tuple[Path, Path]] = [
        (pkgdir, _PKGDIR_DST),
        (config.ccache_dir(), _CCACHE_DST),
        (config.sccache_dir(), _SCCACHE_DST),
        (config.distdir(), _DISTDIR_DST),
    ]
    return binds_ro, binds_rw


class Factory:
    """Constrói o stage4 (binpkgs) de uma receita resolvida (OVERVIEW §6).

    Recebe a receita já resolvida e o ``pkgdir`` (PKGDIR host-side) de saída e
    orquestra o build em fases num container limpo e **não-efêmero** (o rootfs
    persiste para reuso de fork-point e depuração — R8.4).
    """

    def __init__(self, recipe: ResolvedRecipe, pkgdir: Path) -> None:
        self.recipe = recipe
        self.pkgdir = pkgdir

    def build(
        self, *, emptytree: bool = True, download: bool = True, keep: bool = False
    ) -> FactoryResult:
        """Compila os binpkgs da receita num container nspawn (OVERVIEW §6, R1.1/R8.x).

        Ordem (ver Sequence do design):

        1. **Guarda de root** (R8.1): se não-root, levanta :class:`FactoryError`
           acionável **antes de qualquer trabalho** — o Kaji não escala privilégios.
        2. Resolve o ``snapshot`` do pointer do stage3 (``seed.load_pointer``).
           Se :func:`kaji.phases.fork_point` acha o tronco pinado, restaura-o no
           rootfs e retoma após a última fase do tronco (``resume_at``,
           ``fork_point_reused=True``); senão ``fetch_stage3`` + ``extract_stage3``
           num rootfs fresco (``fork_point_reused=False``).
        3. ``apply_portage`` (layers) + instala ``recipe.sets`` em
           ``/etc/portage/sets/``.
        4. Monta os binds (:func:`_build_binds`) e abre um :class:`Container`
           **não-efêmero** (``binds`` RO, ``binds_rw`` RW).
        5. :func:`kaji.phases.run_phases` (fases + settle-pass).
        6. Monta o :class:`FactoryResult`. Em sucesso e sem ``keep``, remove o
           rootfs de build; em falha ou ``keep``, preserva-o (R8.4). Uma falha de
           ``emerge`` já sobe como :class:`FactoryError` de ``run_phase``/
           ``settle_pass`` e propaga.
        """
        if os.geteuid() != 0:
            raise FactoryError(
                "kaji factory requer root (systemd-nspawn + extração de stage3); "
                "rode como root — o Kaji não escala privilégios sozinho"
            )

        recipe = self.recipe
        rootfs = config.build_root() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}"

        pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
        snapshot = pointer.snapshot
        fork_points_dir = config.fork_points_dir()

        existing = fork_point(recipe, snapshot=snapshot, fork_points_dir=fork_points_dir)
        if existing is not None:
            shutil.rmtree(rootfs, ignore_errors=True)
            rootfs.mkdir(parents=True, exist_ok=True)
            restore_fork_point(existing, rootfs)
            trunk = trunk_phase_names(recipe)
            resume_at = trunk[-1] if trunk else None
            fork_point_path: Path | None = existing
            fork_point_reused = True
        else:
            tarball = fetch_stage3(pointer, cache_dir=config.cache_dir(), download=download)
            extract_stage3(tarball, rootfs)
            resume_at = None
            fork_point_path = fork_points_dir / (
                f"{recipe.arch}-{recipe.flavor}-{recipe.init}-{snapshot}.tar"
            )
            fork_point_reused = False

        apply_portage(rootfs, recipe, variants_dir=config.variants_dir())
        self._install_sets(rootfs, recipe)

        binds_ro, binds_rw = _build_binds(
            recipe, pkgdir=self.pkgdir, repos_conf_dir=rootfs / "etc" / "portage" / "repos.conf"
        )

        keep_rootfs = keep
        try:
            with Container(
                rootfs, ephemeral=False, binds=binds_ro, binds_rw=binds_rw
            ) as container:
                results = run_phases(
                    container,
                    recipe,
                    emptytree=emptytree,
                    resume_at=resume_at,
                    snapshot=snapshot,
                    fork_points_dir=fork_points_dir,
                )
        except BaseException:
            keep_rootfs = True  # preserva o rootfs para depuração em falha (R8.4)
            raise

        phase_names = tuple(r.phase.name for r in results if r.phase.name != "settle")
        built_atoms: tuple[str, ...] = ()
        settle_atoms: tuple[str, ...] = ()
        for r in results:
            if r.phase.name == "settle":
                settle_atoms = r.built_atoms
            else:
                built_atoms += r.built_atoms

        result = FactoryResult(
            pkgdir=self.pkgdir,
            built_atoms=built_atoms,
            phases=phase_names,
            fork_point=fork_point_path,
            fork_point_reused=fork_point_reused,
            settle_atoms=settle_atoms,
        )

        if not keep_rootfs:
            shutil.rmtree(rootfs, ignore_errors=True)
        return result

    @staticmethod
    def _install_sets(rootfs: Path, recipe: ResolvedRecipe) -> None:
        """Instala os sets da receita em ``${rootfs}/etc/portage/sets/`` (R6.4).

        Cada nome em ``recipe.sets`` (``bentoo-apps``, ``graphics``, o set do
        flavor …) tem seu arquivo curado em ``variants/<layer>/sets/<name>``
        (irmão de ``portage/``, logo *não* copiado por :func:`apply_portage`).
        Resolve-se cada set varrendo os layers na ordem base→arch→flavor→init
        (layer posterior sobrescreve), copiando o arquivo encontrado para
        ``${rootfs}/etc/portage/sets/<name>`` para que ``@<set>`` resolva dentro
        do container. Sets sem arquivo em nenhum layer são silenciosamente
        ignorados (a curadoria vive em ``variants/``).
        """
        dest_dir = rootfs / "etc" / "portage" / "sets"
        dest_dir.mkdir(parents=True, exist_ok=True)
        variants_dir = config.variants_dir()
        for name in recipe.sets:
            for layer in recipe.portage_layers:
                src = variants_dir / layer / "sets" / name
                if src.is_file():
                    (dest_dir / name).write_bytes(src.read_bytes())
