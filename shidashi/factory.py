"""Factory — Package Factory: constrói binpkgs a partir de uma receita (OVERVIEW §6).

Orquestra, para uma :class:`~shidashi.recipe.ResolvedRecipe`, o build multi-instance
de binpkgs num container limpo por flavor (OVERVIEW §6.1–§6.3): seed/extração do
stage3 (ou reuso de um fork-point do tronco), sobreposição dos layers de portage
+ instalação dos sets, montagem dos binds (repos RO; PKGDIR/ccache/sccache/DISTDIR
RW sobre os caminhos fixos do ``make.conf``) e execução das fases (OVERVIEW §6.4)
seguida do settle-pass. A guarda de privilégio (root) é a **primeira** coisa que
:meth:`Factory.build` faz — o Shidashi nunca escala privilégios sozinho (R8.1).

Os símbolos privilegiados de execução são importados a **nível de módulo**
(``fetch_stage3``/``extract_stage3``/``bind_repos``/``apply_portage``) para que os
testes possam monkeypatchá-los em ``shidashi.factory`` e :meth:`build` os observe.
:class:`FactoryError` é definida em :mod:`shidashi.phases` (evita o ciclo de import
``factory`` → ``phases``) e **re-exportada** aqui.
"""

import os
import shutil
import time
from collections.abc import Mapping
from pathlib import Path

import pydantic

from shidashi import audit, config, isacheck, state
from shidashi.bootstrap import BootstrapResult, run_bootstrap
from shidashi.catalyst import build_stage3_catalyst
from shidashi.container import Container
from shidashi.generation import FINGERPRINT_FILE, check_or_record, fingerprint
from shidashi.phases import (
    CheckpointDecision,
    CheckpointHook,
    FactoryError,
    FailureDecision,
    FailureHook,
    PhaseResult,
    attach_packages,
    fork_point,
    latest_resumable,
    restore_fork_point,
    run_phases,
    run_phases_stepwise,
    snapshot_fork_point,
    stage_fork_point_path,
)
from shidashi.recipe import ResolvedRecipe
from shidashi.resolve import apply_portage, apply_rootfs, bind_repos, install_sets
from shidashi.seed import Stage3Pointer, extract_stage3, fetch_stage3, load_pointer
from shidashi.state import PhaseDiff
from shidashi.tree import pinned_repos
from shidashi.update import run_update

__all__ = [
    "CheckpointDecision",
    "Factory",
    "FactoryError",
    "FactoryResult",
    "FailureDecision",
    "StaleStateError",
]

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

    Os campos do build interativo (story 004) são **defaultados** para manter a
    construção da story 003 (sem eles) válida apesar do ``extra="forbid"`` (R8.2):
    ``stopped_at`` é o rótulo onde um stepwise parou cedo (``--until``/STOP) ou
    ``None`` quando rodou até o fim; ``phase_diffs`` o histórico de
    :class:`~shidashi.state.PhaseDiff` por fase e ``completed_phases`` os nomes das
    fases já encerradas — ambos lidos do estado persistido pelo stepwise.
    """

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    pkgdir: Path
    built_atoms: tuple[str, ...]
    phases: tuple[str, ...]
    fork_point: Path | None
    fork_point_reused: bool
    settle_atoms: tuple[str, ...]
    stopped_at: str | None = None
    phase_diffs: tuple[PhaseDiff, ...] = ()
    completed_phases: tuple[str, ...] = ()
    #: Binaries carrying instructions this build host cannot execute (R9.x).
    #: Non-empty is a WARNING, not a failure: the binpkgs are valid FOR THE
    #: TARGET, they simply cannot be test-run or smoke-tested here. Always empty
    #: when target and host share an ISA, which is the common case.
    unrunnable_here: tuple[str, ...] = ()
    #: Installed from the generation's binpkgs instead of compiled (--usepkg).
    reused_atoms: tuple[str, ...] = ()
    #: The toolchain bootstrap this build ran over a fresh stage3; ``None`` when
    #: it resumed from the bootstrap checkpoint or from a stage fork point.
    bootstrap: BootstrapResult | None = None


class StaleStateError(FactoryError):
    """Estado de build persistido obsoleto frente ao snapshot/receita atuais (R6.3).

    Levantada por :meth:`Factory.build_stepwise` quando :func:`shidashi.state.is_stale`
    acusa divergência (snapshot do stage3 ou hash da receita mudou) e nem
    ``--reset`` nem ``--force-resume`` foram passados — o stepwise NUNCA prossegue
    silenciosamente sobre progresso obsoleto. Subclasse de :class:`FactoryError`
    (carrega a mesma ``phase``/``output``); a CLI (Task 7) a captura para emitir um
    prompt/diagnóstico e sair com código 1, distinguindo-a de uma falha de emerge.
    """


def _require_root() -> None:
    """Guarda de privilégio (R8.1): levanta :class:`FactoryError` se não-root.

    Primeira coisa que :meth:`Factory.build` e :meth:`Factory.build_stepwise`
    chamam — **antes** de qualquer fetch/extração/I/O de estado. O Shidashi nunca
    escala privilégios sozinho; a mensagem é acionável e menciona ``root``.
    """
    if os.geteuid() != 0:
        raise FactoryError(
            "shidashi factory requer root (systemd-nspawn + extração de stage3); "
            "rode como root — o Shidashi não escala privilégios sozinho"
        )


def _fresh_seed(
    rootfs: Path, pointer: Stage3Pointer, *, download: bool, recipe: ResolvedRecipe
) -> str:
    """Seeda um rootfs **fresco** a partir do stage3 do ``pointer`` (R1.4/R8.2/R5.x).

    Devolve o ``seed_sha512`` do stage3 buildado localmente — ``""`` quando a
    seed veio por download (story 005). Ramifica em ``recipe.seed_source``:

    * ``download`` (default): :func:`shidashi.seed.fetch_stage3` (cache de
      :func:`shidashi.config.cache_dir`) seguido de :func:`shidashi.seed.extract_stage3`
      (que já cria ``rootfs``). É o corpo EXATO do ramo fresh original — sem
      ``rmtree``/``mkdir`` extra — para que o one-shot não mude (R8.2/R5.2).
    * ``catalyst``: o stage3 genérico baixado/verificado vira a SEMENTE de
      bootstrap de :func:`shidashi.catalyst.build_stage3_catalyst`, que gera um
      stage3 com o ``-march`` do alvo (specs sob ``catalyst_spec_dir``, saída sob
      ``catalyst_dir``, ``portage_confdir`` = ``variants/arch/<arch>/portage``);
      extrai-se o tarball produzido e devolve-se seu SHA-512 (R5.1/R4.1). O
      ``catalyst`` roda no host — NUNCA aninhado no :class:`Container`/nspawn.

    Sub-passo PRIVILEGIADO compartilhado pelo caminho fresh de
    :func:`_seed_or_restore` e pelo caso "sem estado" de :meth:`Factory.build_stepwise`;
    ``fetch_stage3``/``extract_stage3``/``build_stage3_catalyst`` são globais do
    módulo (monkeypatcháveis nos testes).
    """
    tarball = fetch_stage3(pointer, cache_dir=config.cache_dir(), download=download)
    if recipe.seed_source == "catalyst":
        stage3, seed_sha512 = build_stage3_catalyst(
            recipe,
            tarball,
            version_stamp=pointer.snapshot,
            snapshot_treeish=pointer.snapshot,
            confdir=config.variants_dir() / "arch" / recipe.arch / "portage",
            scratch_dir=config.catalyst_spec_dir(recipe.arch),
            output_dir=config.catalyst_dir(recipe.arch),
        )
        extract_stage3(stage3, rootfs)
        return seed_sha512
    extract_stage3(tarball, rootfs)
    return ""


def bootstrap_fork_point_path(
    recipe: ResolvedRecipe, *, snapshot: str, fork_points_dir: Path
) -> Path:
    """Where the bootstrap checkpoint lives: the stage3 with its toolchain rebuilt.

    ``<arch>-<init>-<snapshot>-bootstrap.tar``, beside the stage fork points and
    keyed like them (no target): every image of one arch × init starts here. The
    arch is in the key because the toolchain is built with the arch's CFLAGS.
    """
    return fork_points_dir / f"{recipe.arch}-{recipe.init}-{snapshot}-bootstrap.tar"


def _bootstrap(
    container: Container,
    recipe: ResolvedRecipe,
    *,
    pkgdir: Path,
    snapshot: str,
    fork_points_dir: Path,
) -> BootstrapResult:
    """Run the toolchain bootstrap and checkpoint it (BOOTSTRAP-PROCESS §5, items 1-2).

    The checkpoint is what a failed base build restores to -- not the raw stage3,
    which would redo the ~15 min of toolchain first. The bootstrap's
    ``check-generation`` step runs the fingerprint check as soon as the toolchain
    is final, before the steps that write binpkgs (ccache and its dependencies,
    which no later stage rebuilds).
    """
    rootfs = container.rootfs
    result = run_bootstrap(
        container, on_generation=lambda: check_or_record(pkgdir, fingerprint(rootfs, recipe))
    )
    dest = bootstrap_fork_point_path(recipe, snapshot=snapshot, fork_points_dir=fork_points_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    snapshot_fork_point(container.rootfs, dest)
    return result


def _restore_into(tarball: Path, rootfs: Path) -> None:
    shutil.rmtree(rootfs, ignore_errors=True)
    rootfs.mkdir(parents=True, exist_ok=True)
    restore_fork_point(tarball, rootfs)


def _seed_or_restore(
    recipe: ResolvedRecipe,
    rootfs: Path,
    pointer: Stage3Pointer,
    *,
    snapshot: str,
    fork_points_dir: Path,
    download: bool,
) -> tuple[str | None, Path, bool, bool]:
    """Decide entre reusar o fork-point do tronco e um seed fresco (R5.1/R5.2/R8.2).

    Bloco *seed-or-restore* extraído de :meth:`Factory.build` SEM mudança de
    comportamento (R8.2): se :func:`shidashi.phases.fork_point` acha o tronco pinado
    para ``snapshot``, restaura-o num rootfs limpo e devolve
    ``(resume_at, fork_point_path, True, True)`` onde ``resume_at`` é a fase do estágio
    restaurado; senão restaura o checkpoint do bootstrap, se existe, ou faz
    :func:`_fresh_seed` -- ``(None, <chave do primeiro estágio>, False,
    bootstrapped)``. The last flag says whether the toolchain bootstrap is
    already in the rootfs; ``False`` means the caller must run it. PRIVILEGIADO.
    """
    found = fork_point(recipe, snapshot=snapshot, fork_points_dir=fork_points_dir)
    if found is not None:
        # the deepest STAGE already built for this arch × init, by any image
        # (D24, F70): resume right after it
        phase, existing = found
        _restore_into(existing, rootfs)
        return phase.name, existing, True, True
    first_stage = next((p.stage for p in recipe.phases if p.stage), "base")
    fork_point_path = stage_fork_point_path(
        recipe, first_stage, snapshot=snapshot, fork_points_dir=fork_points_dir
    )
    checkpoint = bootstrap_fork_point_path(
        recipe, snapshot=snapshot, fork_points_dir=fork_points_dir
    )
    if checkpoint.exists():
        _restore_into(checkpoint, rootfs)
        return None, fork_point_path, False, True
    # a fresh seed is a fresh ROOTFS: a failed --keep run leaves its tree behind,
    # and a stage3 extracted over it would inherit whatever that run broke
    shutil.rmtree(rootfs, ignore_errors=True)
    _fresh_seed(rootfs, pointer, download=download, recipe=recipe)
    return None, fork_point_path, False, False


def _phases_through(recipe: ResolvedRecipe, name: str | None) -> tuple[str, ...]:
    """Names of the phases up to and including ``name``; ``()`` for ``None``. Pure."""
    names: list[str] = []
    if name is None:
        return ()
    for phase in recipe.phases:
        names.append(phase.name)
        if phase.name == name:
            break
    return tuple(names)


def _entry_layers(recipe: ResolvedRecipe, *, done: tuple[str, ...]) -> tuple[str, ...] | None:
    """The layers of the first phase still to run, skipping ``done`` (D24). Pure.

    The configuration grows along the chain and ``run_phase`` re-applies it per
    phase; before the container opens, only what the FIRST phase needs goes in
    -- applying every layer here would leave the flavor's package.use in place
    while the base is still being built. ``None`` means "all of them": a recipe
    whose phases carry no layers (built directly, as in the tests).
    """
    for phase in recipe.phases:
        if phase.name not in done and phase.layers:
            return phase.layers
    return None


def _prepare_portage(
    rootfs: Path, recipe: ResolvedRecipe, *, layers: tuple[str, ...] | None = None
) -> None:
    """Sobrepõe os layers de portage e instala os sets da receita (R6.4/R8.2).

    Bloco *portage-apply* extraído de :meth:`Factory.build` SEM mudança de
    comportamento (R8.2): :func:`shidashi.resolve.apply_portage` (layers sob
    :func:`shidashi.config.variants_dir`) seguido de :meth:`Factory._install_sets`.
    Compartilhado por :meth:`Factory.build` e :meth:`Factory.build_stepwise`.

    The layers' ``rootfs/`` trees go first (:func:`shidashi.resolve.apply_rootfs`):
    the bootstrap's ``locale-gen`` reads the curated ``/etc/locale.gen``.
    """
    apply_rootfs(rootfs, recipe, variants_dir=config.variants_dir(), layers=layers)
    apply_portage(rootfs, recipe, variants_dir=config.variants_dir(), layers=layers)
    Factory._install_sets(rootfs, recipe)


def _build_binds(
    recipe: ResolvedRecipe,
    *,
    pkgdir: Path,
    repos_conf_dir: Path | None = None,
    repos: Mapping[str, Path] | None = None,
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """Monta os binds RO (repos) e RW (PKGDIR/caches) do container (R6.2/R6.3/R7.2). Pura.

    - ``binds_ro`` = :func:`shidashi.resolve.bind_repos` sobre ``repos_conf_dir`` (o
      ``repos.conf`` do rootfs resolvido; os repos sincronizados do host ficam RO,
      a árvore do host nunca é mutada).
    - ``binds_rw`` mapeia os diretórios *host-side* (sob ``cache_dir()``) sobre os
      alvos fixos do ``make.conf`` do container: ``pkgdir`` → ``/var/cache/binpkgs``,
      ``ccache_dir()`` → ``/var/cache/ccache``, ``sccache_dir()`` → ``/var/cache/sccache``,
      ``distdir()`` → ``/var/cache/distfiles``. Binpkgs e caches persistem no host.

    ``repos_conf_dir`` é opcional para manter a chamada de teste (que monkeypatcha
    ``bind_repos``) trivial; :meth:`Factory.build` passa o ``repos.conf`` real do
    rootfs. ``bind_repos`` é resolvido via global do módulo (monkeypatchável).
    ``repos`` are the pinned repositories by name (D26: the ::gentoo snapshot
    and the overlays' commits), bound in place of the host's clones.
    """
    binds_ro = bind_repos(repos_conf_dir if repos_conf_dir is not None else Path(), overrides=repos)
    binds_rw: list[tuple[Path, Path]] = [
        (pkgdir, _PKGDIR_DST),
        (config.ccache_dir(), _CCACHE_DST),
        (config.sccache_dir(), _SCCACHE_DST),
        (config.distdir(), _DISTDIR_DST),
    ]
    return binds_ro, binds_rw


def portage_ids(rootfs: Path) -> tuple[int, int] | None:
    """The ``portage`` uid and gid of the ROOTFS (not the host's). Pure I/O.

    ``None`` when either file lacks the entry. Read from the image because the
    host may have no portage user, or another id for it.
    """

    def _lookup(path: Path) -> int | None:
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split(":")
            if len(fields) > 2 and fields[0] == "portage" and fields[2].isdigit():
                return int(fields[2])
        return None

    uid = _lookup(rootfs / "etc" / "passwd")
    gid = _lookup(rootfs / "etc" / "group")
    return None if uid is None or gid is None else (uid, gid)


def _ensure_bind_dirs(binds_rw: list[tuple[Path, Path]], *, rootfs: Path | None = None) -> None:
    """Cria os diretórios host-side dos binds RW antes do nspawn.

    ``systemd-nspawn`` exige que o *source* de cada ``--bind=`` exista no host;
    sem isto o spawn aborta com ``Failed to clone …``. Fica fora de
    :func:`_build_binds` para preservar a pureza (e o unit test) daquela montagem.

    With ``rootfs``, the ccache directory is also handed to the image's
    ``portage`` user: compiles run under ``userpriv``, and a root-owned cache
    fails the first one (BOOTSTRAP-PROCESS §5, item 7).
    """
    for src, _dst in binds_rw:
        src.mkdir(parents=True, exist_ok=True)
    ids = portage_ids(rootfs) if rootfs is not None else None
    if ids is None:
        return
    for src, dst in binds_rw:
        if dst == _CCACHE_DST:
            os.chown(src, *ids)


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
        self,
        *,
        emptytree: bool = True,
        download: bool = True,
        keep: bool = False,
        stop_after: str | None = None,
    ) -> FactoryResult:
        """Compila os binpkgs da receita num container nspawn (OVERVIEW §6, R1.1/R8.x).

        Ordem (ver Sequence do design):

        1. **Guarda de root** (R8.1): se não-root, levanta :class:`FactoryError`
           acionável **antes de qualquer trabalho** — o Shidashi não escala privilégios.
        2. Resolve o ``snapshot`` do pointer do stage3 (``seed.load_pointer``).
           Se :func:`shidashi.phases.fork_point` acha o tronco pinado, restaura-o no
           rootfs e retoma após a última fase do tronco (``resume_at``,
           ``fork_point_reused=True``); else the bootstrap checkpoint, when it
           exists; else ``fetch_stage3`` + ``extract_stage3`` num rootfs fresco.
        3. ``apply_rootfs`` + ``apply_portage`` (layers) + instala ``recipe.sets``
           em ``/etc/portage/sets/``.
        4. Monta os binds (:func:`_build_binds`, ccache owned by the image's
           ``portage``) e abre um :class:`Container` **não-efêmero**.
        5. Over a fresh stage3 only: the toolchain bootstrap
           (:func:`shidashi.bootstrap.run_bootstrap`), then its checkpoint. Then
           the generation fingerprint is recorded in, or checked against, the
           PKGDIR (:func:`shidashi.generation.check_or_record`, D26).
        6. :func:`shidashi.phases.run_phases` (fases + settle-pass).
        7. Monta o :class:`FactoryResult`. Em sucesso e sem ``keep``, remove o
           rootfs de build; em falha ou ``keep``, preserva-o (R8.4). Uma falha de
           ``emerge`` já sobe como :class:`FactoryError` de ``run_phase``/
           ``settle_pass`` e propaga.
        """
        _require_root()
        run = audit.current()

        recipe = self.recipe
        rootfs = config.build_root() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}"

        pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
        snapshot = pointer.snapshot
        fork_points_dir = config.fork_points_dir()
        with run.step("seed") as step:
            # first: a pin that is missing or inside the cooldown refuses the build
            # before any seed is extracted (D26)
            repos = pinned_repos(
                seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
            )
            resume_at, fork_point_path, fork_point_reused, bootstrapped = _seed_or_restore(
                recipe,
                rootfs,
                pointer,
                snapshot=snapshot,
                fork_points_dir=fork_points_dir,
                download=download,
            )
            entry = _entry_layers(recipe, done=_phases_through(recipe, resume_at))
            _prepare_portage(rootfs, recipe, layers=entry)
            step.add(
                resume_at=resume_at,
                fork_point=str(fork_point_path) if fork_point_path else None,
                fork_point_reused=fork_point_reused,
                bootstrapped=bootstrapped,
                layers=list(entry) if entry is not None else None,
            )

        binds_ro, binds_rw = _build_binds(
            recipe,
            pkgdir=self.pkgdir,
            repos_conf_dir=rootfs / "etc" / "portage" / "repos.conf",
            repos=repos,
        )
        _ensure_bind_dirs(binds_rw, rootfs=rootfs)

        keep_rootfs = keep
        bootstrap: BootstrapResult | None = None
        try:
            with Container(
                rootfs,
                ephemeral=False,
                binds=binds_ro,
                binds_rw=binds_rw,
                log=config.build_log_path(recipe),
            ) as container:
                if not bootstrapped:
                    with run.step("bootstrap") as step:
                        bootstrap = _bootstrap(
                            container,
                            recipe,
                            pkgdir=self.pkgdir,
                            snapshot=snapshot,
                            fork_points_dir=fork_points_dir,
                        )
                        step.add(
                            binutils=bootstrap.binutils,
                            gcc=bootstrap.gcc,
                            locales_before=bootstrap.locales_before,
                            locales_after=bootstrap.locales_after,
                        )
                # before any emerge can reuse a binpkg (D26)
                with run.step("generation") as step:
                    generation_print = fingerprint(rootfs, recipe)
                    recorded = check_or_record(self.pkgdir, generation_print)
                    step.add(recorded=recorded, fingerprint=generation_print.model_dump())
                with run.step("stages"):
                    results = run_phases(
                        container,
                        recipe,
                        emptytree=emptytree,
                        resume_at=resume_at,
                        snapshot=snapshot,
                        fork_points_dir=fork_points_dir,
                        stop_after=stop_after,
                    )
        except BaseException:
            keep_rootfs = True  # preserva o rootfs para depuração em falha (R8.4)
            raise

        phase_names = tuple(r.phase.name for r in results if r.phase.name != "settle")
        built_atoms: tuple[str, ...] = ()
        settle_atoms: tuple[str, ...] = ()
        for r in results:
            if r.phase.name == "settle":
                settle_atoms += r.built_atoms  # one settle per shipped stage (D24)
            else:
                built_atoms += r.built_atoms
        reused_atoms = tuple(a for r in results for a in r.reused_atoms)

        # ISA gap check (R9.x). Scans the ROOTFS, not pkgdir: binpkgs are
        # compressed .gpkg.tar archives objdump cannot read, while the rootfs
        # holds the same binaries already unpacked. It must therefore run before
        # the rootfs is discarded below.
        #
        # Deliberately NOT fatal. The build succeeded and the binpkgs are correct
        # for the target; the finding means only that THIS host cannot execute
        # them, so test suites and ISO smoke tests have to happen elsewhere.
        # Failing here would discard hours of correct work over a fact about the
        # build machine.
        with run.step("isa-check") as step:
            unrunnable = tuple(
                str(f.path.relative_to(rootfs)) for f in isacheck.check_rootfs(rootfs, recipe.arch)
            )
            step.add(unrunnable=len(unrunnable))
        run.metric("packages.built", len(built_atoms))
        run.metric("packages.reused", len(reused_atoms))
        run.metric("packages.settled", len(settle_atoms))

        result = FactoryResult(
            pkgdir=self.pkgdir,
            built_atoms=built_atoms,
            phases=phase_names,
            fork_point=fork_point_path,
            fork_point_reused=fork_point_reused,
            settle_atoms=settle_atoms,
            unrunnable_here=unrunnable,
            bootstrap=bootstrap,
            reused_atoms=reused_atoms,
        )

        if not keep_rootfs:
            with run.step("cleanup"):
                shutil.rmtree(rootfs, ignore_errors=True)
        return result

    def update(self, *, download: bool = True, keep: bool = False) -> FactoryResult:
        """Bring a shipped image to the week's pinned tree, within its generation (D26).

        1. Root guard; the pinned ::gentoo tree (its cooldown refuses early).
        2. The image's OWN fork point must exist -- an update updates a built
           image, it never builds one -- and so must the generation's
           fingerprint in the PKGDIR: an empty PKGDIR is a new generation, which
           is a full build, not an update.
        3. Restore it; apply the full configuration (every layer) and the sets.
        4. In the container: the fingerprint must still match, then
           :func:`shidashi.update.run_update` -- plan, refuse a toolchain change,
           ``-uDN --changed-deps`` with ``--usepkg``, ``@preserved-rebuild``.
        5. Snapshot the updated image over its fork point.
        """
        _require_root()

        recipe = self.recipe
        rootfs = config.build_root() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}"
        snapshot = load_pointer(recipe.init, seeds_dir=config.seeds_dir()).snapshot
        fork_points_dir = config.fork_points_dir()
        repos = pinned_repos(
            seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
        )

        target = recipe.stages[-1] if recipe.stages else recipe.flavor
        image = stage_fork_point_path(
            recipe, target, snapshot=snapshot, fork_points_dir=fork_points_dir
        )
        if not image.exists():
            raise FactoryError(
                f"nothing to update: {image.name} does not exist -- build the image first",
                phase="update",
            )
        if not (self.pkgdir / FINGERPRINT_FILE).is_file():
            raise FactoryError(
                f"{self.pkgdir} has no generation fingerprint: an update continues a "
                "generation, it never starts one -- run a full build",
                phase="update",
            )

        run = audit.current()
        with run.step("restore", fork_point=str(image)):
            _restore_into(image, rootfs)
            _prepare_portage(rootfs, recipe)

        binds_ro, binds_rw = _build_binds(
            recipe,
            pkgdir=self.pkgdir,
            repos_conf_dir=rootfs / "etc" / "portage" / "repos.conf",
            repos=repos,
        )
        _ensure_bind_dirs(binds_rw, rootfs=rootfs)

        keep_rootfs = keep
        try:
            with Container(
                rootfs,
                ephemeral=False,
                binds=binds_ro,
                binds_rw=binds_rw,
                log=config.build_log_path(recipe),
            ) as container:
                with run.step("generation"):
                    check_or_record(self.pkgdir, fingerprint(rootfs, recipe))
                with run.step("update") as step:
                    since = int(time.time())
                    result = run_update(container, recipe)
                    step.add(built=len(result.built_atoms))
                    attach_packages(rootfs, "update", since=since, built=result.built_atoms)
                with run.step("fork-point", path=str(image)):
                    snapshot_fork_point(rootfs, image)
        except BaseException:
            keep_rootfs = True  # preserved for debugging, as in build()
            raise

        if not keep_rootfs:
            shutil.rmtree(rootfs, ignore_errors=True)
        return FactoryResult(
            pkgdir=self.pkgdir,
            built_atoms=result.built_atoms,
            phases=("update",),
            fork_point=image,
            fork_point_reused=True,
            settle_atoms=(),
        )

    def build_stepwise(
        self,
        *,
        until: str | None = None,
        interactive: bool = False,
        emptytree: bool = True,
        download: bool = True,
        reset: bool = False,
        force_resume: bool = False,
        on_checkpoint: CheckpointHook | None = None,
        on_failure: FailureHook | None = None,
    ) -> FactoryResult:
        """Constrói os binpkgs passo-a-passo, com resume/checkpoints (OVERVIEW §6, R1.x/R2.x/R6.x).

        Variante interativa/resumível de :meth:`build`. Ordem:

        1. **Guarda de root** (R8.1, :func:`_require_root`) — PRIMEIRA coisa, antes
           de qualquer fetch/extração/I/O de estado.
        2. Resolve ``snapshot`` (pointer do stage3) e ``recipe_hash``; o caminho do
           estado persistido é :func:`shidashi.config.build_state_path`.
        3. ``reset`` (R6.4): limpa o estado persistido e remove o rootfs, recomeçando
           do zero. Senão carrega o estado: se existe e está obsoleto
           (:func:`shidashi.state.is_stale`) e nem ``force_resume`` → levanta
           :class:`StaleStateError` (R6.3) — NUNCA prossegue sobre progresso stale.
        4. **Seed-or-restore** em três casos: (a) há fases completadas →
           :func:`shidashi.phases.latest_resumable` + :func:`restore_fork_point` (se o
           tarball some/corrompe, levanta :class:`FactoryError` e MANTÉM o rootfs);
           (b) ``seed_done`` mas sem fases (ex.: ``--until seed`` anterior) → reusa o
           rootfs persistente AS-IS (R1.4); (c) sem estado/``reset`` → seed fresco
           (:func:`_fresh_seed`), marca ``seed_done`` e persiste; se ``interactive``,
           checkpoint ``"seed"`` honrando CONTINUE/STOP/SHELL (R2.5).
        5. :func:`_prepare_portage` (layers + sets).
        6. Abre um :class:`Container` **não-efêmero** persistente (binds RO/RW).
        7. :func:`shidashi.phases.run_phases_stepwise` (resume, checkpoints, retry,
           snapshot por fase, persistência por fase).
        8. Monta o :class:`FactoryResult` estendido (``stopped_at``/``phase_diffs``/
           ``completed_phases`` lidos do estado persistido).
        9. **Teardown: NUNCA auto-deleta o rootfs** — stop, conclusão e falha TODOS
           o mantêm (R1.6); só ``reset`` (passo 3) o remove. Uma falha de ``emerge``
           sobe como :class:`FactoryError` (estado já persistido pelo stepwise,
           rootfs mantido) e propaga → CLI exit 1.
        """
        _require_root()

        recipe = self.recipe
        rootfs = config.build_root() / f"{recipe.arch}-{recipe.flavor}-{recipe.init}"
        pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
        snapshot = pointer.snapshot
        fork_points_dir = config.fork_points_dir()
        state_path = config.build_state_path(recipe)
        rh = state.recipe_hash(recipe)
        repos = pinned_repos(
            seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
        )

        if reset:
            state.clear_state(state_path)
            shutil.rmtree(rootfs, ignore_errors=True)
            loaded: state.BuildState | None = None
        else:
            loaded = state.load_state(state_path)
            if (
                loaded is not None
                and state.is_stale(loaded, snapshot=snapshot, recipe_hash=rh)
                and not force_resume
            ):
                raise StaleStateError(
                    f"estado de build obsoleto para {recipe.arch}-{recipe.flavor}-"
                    f"{recipe.init} (snapshot/receita mudaram); rode com --reset para "
                    "recomeçar do zero ou --force-resume para retomar assim mesmo"
                )

        completed = loaded.completed_phases if loaded is not None else ()
        seed_done = loaded.seed_done if loaded is not None else False

        stopped_at_seed = self._seed_or_restore_stepwise(
            recipe,
            rootfs,
            pointer,
            snapshot=snapshot,
            recipe_hash=rh,
            fork_points_dir=fork_points_dir,
            state_path=state_path,
            completed=completed,
            seed_done=seed_done,
            interactive=interactive,
            download=download,
            on_checkpoint=on_checkpoint,
        )
        if stopped_at_seed:
            return self._assemble_result(state_path, stopped_at="seed", results=())

        seeded = state.load_state(state_path)
        needs_bootstrap = not completed and (seeded is None or not seeded.bootstrap_done)

        _prepare_portage(rootfs, recipe, layers=_entry_layers(recipe, done=completed))

        binds_ro, binds_rw = _build_binds(
            recipe,
            pkgdir=self.pkgdir,
            repos_conf_dir=rootfs / "etc" / "portage" / "repos.conf",
            repos=repos,
        )
        _ensure_bind_dirs(binds_rw, rootfs=rootfs)

        # Teardown rule (R1.6): o stepwise NUNCA auto-deleta o rootfs — stop,
        # conclusão e falha TODOS o mantêm; só ``reset`` (acima) o remove. Por isso
        # NÃO há cláusula de remoção aqui, e uma falha de emerge propaga com o
        # estado já persistido por run_phases_stepwise e o rootfs intacto (R3.5).
        bootstrap: BootstrapResult | None = None
        with Container(
            rootfs,
            ephemeral=False,
            binds=binds_ro,
            binds_rw=binds_rw,
            log=config.build_log_path(recipe),
        ) as container:
            run = audit.current()
            if needs_bootstrap:
                with run.step("bootstrap"):
                    bootstrap = _bootstrap(
                        container,
                        recipe,
                        pkgdir=self.pkgdir,
                        snapshot=snapshot,
                        fork_points_dir=fork_points_dir,
                    )
                if seeded is not None:
                    state.save_state(state_path, seeded.model_copy(update={"bootstrap_done": True}))
            # before any emerge can reuse a binpkg (D26)
            with run.step("generation"):
                check_or_record(self.pkgdir, fingerprint(rootfs, recipe))
            results = run_phases_stepwise(
                container,
                recipe,
                emptytree=emptytree,
                completed=completed,
                until=until,
                snapshot=snapshot,
                fork_points_dir=fork_points_dir,
                state_path=state_path,
                on_checkpoint=on_checkpoint,
                on_failure=on_failure,
            )

        return self._assemble_result(
            state_path,
            stopped_at=self._stopped_label(
                results,
                until=until,
                final_phase=recipe.phases[-1].name if recipe.phases else None,
            ),
            results=results,
            bootstrap=bootstrap,
        )

    def _seed_or_restore_stepwise(
        self,
        recipe: ResolvedRecipe,
        rootfs: Path,
        pointer: Stage3Pointer,
        *,
        snapshot: str,
        recipe_hash: str,
        fork_points_dir: Path,
        state_path: Path,
        completed: tuple[str, ...],
        seed_done: bool,
        interactive: bool,
        download: bool,
        on_checkpoint: CheckpointHook | None,
    ) -> bool:
        """Resolve o seed-or-restore do stepwise nos três casos (R1.4/R2.5/R5.2/R6.4).

        Devolve ``True`` se um checkpoint ``"seed"`` interativo pediu STOP (o
        chamador retorna cedo, rootfs mantido); ``False`` caso o build deva seguir.

        * (a) ``completed`` não-vazio → :func:`shidashi.phases.latest_resumable` e, se há
          snapshot por-fase, :func:`restore_fork_point` num rootfs limpo. Se o
          restore levanta (tarball sumiu/corrompido — ``latest_resumable`` só sondou
          ``.exists()``), embrulha em :class:`FactoryError` e MANTÉM o rootfs
          (Reviewer #8) — não segue com rootfs indefinido.
        * (b) ``seed_done`` sem fases completadas → reusa o rootfs persistente AS-IS,
          sem re-fetch/extract e sem restore (R1.4 — Reviewer #5).
        * (c) sem estado → :func:`_fresh_seed`, marca ``seed_done`` e persiste; se
          ``interactive`` apresenta o checkpoint ``"seed"`` (CONTINUE/STOP/SHELL,
          R2.5) — no seed o Container ainda não está aberto, então SHELL abre um
          shell transitório sobre o rootfs persistente.
        """
        if completed:
            phase, path = latest_resumable(
                recipe, snapshot=snapshot, completed=completed, fork_points_dir=fork_points_dir
            )
            if path is not None:
                shutil.rmtree(rootfs, ignore_errors=True)
                rootfs.mkdir(parents=True, exist_ok=True)
                try:
                    restore_fork_point(path, rootfs)
                except Exception as err:  # tarball sumiu/corrompido entre o probe e o uso
                    raise FactoryError(
                        f"falha ao restaurar o snapshot da fase {phase!r} de {path}: {err}; "
                        "o rootfs foi mantido — rode com --reset para recomeçar do zero"
                    ) from err
            return False

        if seed_done:
            # (b) seed já feito por um --until seed anterior, sem fases completadas:
            # reusa o rootfs persistente como está — não re-seeda nem restaura (R1.4).
            return False

        # (c) sem estado. The bootstrap checkpoint, when one exists, replaces
        # the raw stage3 AND the ~15 min toolchain rebuild on top of it.
        checkpoint = bootstrap_fork_point_path(
            recipe, snapshot=snapshot, fork_points_dir=fork_points_dir
        )
        if checkpoint.exists():
            _restore_into(checkpoint, rootfs)
            state.save_state(
                state_path,
                state.BuildState(
                    arch=recipe.arch,
                    flavor=recipe.flavor,
                    init=recipe.init,
                    snapshot=snapshot,
                    recipe_hash=recipe_hash,
                    seed_done=True,
                    bootstrap_done=True,
                ),
            )
            return self._seed_checkpoint(rootfs, recipe, on_checkpoint) if interactive else False

        # Seed fresco e persiste o marco seed_done. Quando
        # seed_source=catalyst, _fresh_seed devolve o sha512 do stage3 buildado
        # localmente, pinado no BuildState (R4.1); vazio no caminho download.
        shutil.rmtree(rootfs, ignore_errors=True)  # no state: nothing to keep
        seed_sha512 = _fresh_seed(rootfs, pointer, download=download, recipe=recipe)
        state.save_state(
            state_path,
            state.BuildState(
                arch=recipe.arch,
                flavor=recipe.flavor,
                init=recipe.init,
                snapshot=snapshot,
                recipe_hash=recipe_hash,
                seed_done=True,
                seed_sha512=seed_sha512,
            ),
        )
        if interactive:
            return self._seed_checkpoint(rootfs, recipe, on_checkpoint)
        return False

    @staticmethod
    def _seed_checkpoint(
        rootfs: Path, recipe: ResolvedRecipe, on_checkpoint: CheckpointHook | None
    ) -> bool:
        """Apresenta o checkpoint ``"seed"`` e devolve ``True`` se o usuário pediu STOP (R2.5).

        Sem ``on_checkpoint`` ⇒ auto-CONTINUE (devolve ``False``). Caso contrário
        consulta o hook com um :class:`~shidashi.state.PhaseDiff` base (fase ``"seed"``,
        sem átomos): CONTINUE segue (``False``); STOP interrompe (``True``); SHELL
        abre um shell transitório sobre o rootfs persistente (o Container do build
        ainda não está aberto no seed) e re-apresenta o MESMO checkpoint.
        """
        if on_checkpoint is None:
            return False
        diff = PhaseDiff(phase="seed", built=())
        while True:
            decision = on_checkpoint("seed", diff)
            if decision is CheckpointDecision.CONTINUE:
                return False
            if decision is CheckpointDecision.STOP:
                return True
            Container(rootfs, ephemeral=False).shell()

    def _assemble_result(
        self,
        state_path: Path,
        *,
        stopped_at: str | None,
        results: tuple[PhaseResult, ...],
        bootstrap: BootstrapResult | None = None,
    ) -> FactoryResult:
        """Monta o :class:`FactoryResult` estendido lendo o estado persistido (R6.1).

        ``phase_diffs``/``completed_phases`` vêm do :class:`~shidashi.state.BuildState`
        relido (a fonte de verdade do progresso, persistido por fase); na ausência
        de estado caem para ``()``. ``built_atoms``/``settle_atoms``/``phases`` são
        derivados das fases efetivamente executadas em ``results`` (o settle só
        consta quando rodou), espelhando :meth:`build`.
        """
        persisted = state.load_state(state_path)
        phase_diffs = persisted.phase_diffs if persisted is not None else ()
        completed_phases = persisted.completed_phases if persisted is not None else ()

        phase_names = tuple(r.phase.name for r in results if r.phase.name != "settle")
        built_atoms: tuple[str, ...] = ()
        settle_atoms: tuple[str, ...] = ()
        for r in results:
            if r.phase.name == "settle":
                settle_atoms += r.built_atoms  # one settle per shipped stage (D24)
            else:
                built_atoms += r.built_atoms

        return FactoryResult(
            pkgdir=self.pkgdir,
            built_atoms=built_atoms,
            phases=phase_names,
            fork_point=None,
            fork_point_reused=False,
            settle_atoms=settle_atoms,
            stopped_at=stopped_at,
            phase_diffs=phase_diffs,
            completed_phases=completed_phases,
            bootstrap=bootstrap,
            reused_atoms=tuple(a for r in results for a in r.reused_atoms),
        )

    @staticmethod
    def _stopped_label(
        results: tuple[PhaseResult, ...], *, until: str | None, final_phase: str | None
    ) -> str | None:
        """Rótulo onde o stepwise parou: a última fase real se não foi a final.

        ``None`` (rodou até o fim) quando a última fase REAL executada é a fase
        final da receita. Antes o critério era "houve settle", que deixou de
        valer com um settle por estágio entregue (D24): o minimal é assentado no
        meio do caminho do kde. Sem nenhuma fase real executada (tudo já estava
        completado), devolve ``until``.
        """
        real = [r.phase.name for r in results if r.phase.name != "settle"]
        if real:
            return None if real[-1] == final_phase else real[-1]
        return until

    @staticmethod
    def _install_sets(rootfs: Path, recipe: ResolvedRecipe) -> None:
        """Instala os sets da receita (R6.4). Delega a :func:`resolve.install_sets`.

        A Factory e o Assembler consomem a MESMA curadoria de sets, e por muito
        tempo cada um teve a sua CÓPIA da lógica -- "espelha o outro", dizia a
        docstring. As duas divergiram assim que uma foi corrigida: o Assembler
        seguia sem resolver ``@refs`` e sem aplicar ``exclude``, montando ISOs a
        partir de sets quebrados. Agora há uma implementação só.
        """
        install_sets(rootfs, recipe)
