"""Phases — execução do build em fases + cache de camadas (OVERVIEW §6.4 / §6.5).

Camada de planejamento PURA da story 003 (grupos 3 + 4.1):

* **3.1** :func:`phase_target` / :func:`phase_emerge_argv` — alvo emerge de cada
  fase e o argv completo (``--emptytree`` só na base, ``-uDN`` nos demais estágios).
* **3.2** :func:`use_break_lines` / :func:`write_use_break` / :func:`clear_use_break`
  — ``package.use`` transitório do break-pass (I/O só contra um rootfs em disco,
  sem root).
* **3.3** :func:`parse_built_atoms` — átomos construídos a partir da saída
  ``emerge --verbose`` (reusa o matcher de :mod:`shidashi.resolve`).
* **3.4** :func:`fork_point` / :func:`trunk_phase_names` — decisão de reuso do
  tronco (só sonda o filesystem) e nomes das fases do tronco.
* **4.1** :func:`snapshot_fork_point` / :func:`restore_fork_point` — captura e
  restauração do tronco como tarball (escrita atômica via temp + ``os.replace``;
  I/O contra uma árvore em disco, sem nspawn).

A orquestração privilegiada (``run_phase``/``settle_pass``/``run_phases``) da
story 003 tarefa 5 roda ``emerge`` *dentro* do container (nspawn) — exige root e
é exercida pelos testes de integração host-gated. Falhas de ``emerge`` (exit
não-zero) são embrulhadas em :class:`FactoryError`.
"""

import dataclasses
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

import pydantic

from shidashi import audit, config, state
from shidashi.container import Container
from shidashi.flow import StagesFlow, active_flow
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from shidashi.resolve import _atom_from_ebuild_line, _iter_atom_lines, apply_portage
from shidashi.seed import ROOTFS_TAR_FLAGS
from shidashi.state import EmergePlanEntry, PhaseDiff

_USE_BREAK_FILE = ("etc", "portage", "package.use", "zz-shidashi-use-break")


class FactoryError(Exception):
    """Falha ao construir uma fase/stage dentro do container (OVERVIEW §6.4).

    Carrega a ``phase`` em que ocorreu (``None`` quando não atrelada a uma fase)
    e a ``output`` capturada do ``emerge`` (stdout+stderr) para diagnóstico.

    Definida aqui (e não em :mod:`shidashi.factory`) para evitar import circular:
    ``factory`` importa de ``phases`` (orquestra fases), e ``phases`` precisa
    levantar este erro; ``shidashi.factory`` re-exporta o símbolo.
    """

    def __init__(self, message: str, *, phase: str | None = None, output: str = "") -> None:
        super().__init__(message)
        self.phase = phase
        self.output = output


class CheckpointDecision(StrEnum):
    """Decisão do usuário num checkpoint pós-fase do build interativo (R2.2/R2.3).

    ``StrEnum`` (não ``(str, Enum)`` — UP042) cujos membros valem o próprio nome:
    ``CONTINUE`` segue para a próxima fase, ``STOP`` interrompe o laço sem rodar o
    settle (R1.3), ``SHELL`` abre um shell no container e re-apresenta o MESMO
    checkpoint. Definida aqui (e não em :mod:`shidashi.factory`) para evitar import
    circular ``factory → phases``; ``shidashi.factory`` re-exporta o símbolo (Task 6).
    """

    CONTINUE = "CONTINUE"
    STOP = "STOP"
    SHELL = "SHELL"


class FailureDecision(StrEnum):
    """Decisão do usuário ante a falha de uma fase do build interativo (R3.1–R3.4).

    ``StrEnum`` (UP042): ``RETRY`` re-roda a MESMA fase (mesmo argv) e ``ABORT``
    persiste o estado e levanta :class:`FactoryError`. NÃO há opção de pular uma
    fase falha (R3.4). Definida aqui pelo mesmo motivo de import circular que
    :class:`CheckpointDecision`; re-exportada por :mod:`shidashi.factory` (Task 6).
    """

    RETRY = "RETRY"
    ABORT = "ABORT"


class PhaseResult(pydantic.BaseModel):
    """Resultado da execução de uma única fase (OVERVIEW §6.4).

    Value object *frozen*: a fase executada, os átomos construídos
    (:func:`parse_built_atoms` da saída do ``emerge``), o caminho do snapshot do
    fork-point quando houver (OVERVIEW §6.5; ``None`` quando a fase não materializa
    fork-point) e a ``output`` crua do ``emerge --verbose`` (stdout+stderr) para o
    driver compor o diff da fase SEM re-rodar emerge (R4.1). ``output`` é defaultada
    a ``""`` — mantém válida a construção da story 003 que não a informa.
    ``arbitrary_types_allowed`` admite :class:`~pathlib.Path`.
    """

    model_config = pydantic.ConfigDict(frozen=True, arbitrary_types_allowed=True)
    phase: Phase
    built_atoms: tuple[str, ...]
    snapshot: Path | None
    output: str = ""
    #: Installed from the generation's binpkgs rather than compiled.
    reused_atoms: tuple[str, ...] = ()


# --- 3.1 phase_target / phase_emerge_argv (PURO) -----------------------------


def phase_target(phase: Phase, recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Devolve o alvo ``emerge`` de uma fase (R3.1/R3.2/R3.3, D24). Puro.

    - every STAGE phase → ``@world`` and then the stage's own sets. The base
      rebuilds it (``--emptytree``); a later stage updates it (``-uDN``), so
      that a package an earlier stage installed is rebuilt for THIS stage's USE
      even when it is outside the new sets' graph (measured on the real
      minimal: desktop left vim, kbd and fastfetch with the old USE without it);
    - a phase that is not a stage → ``phase.packages`` (the ``seat`` atoms).

    Nada aqui depende do NOME da fase. Duas convenções por nome já custaram
    caro: ``apps`` → ``@bentoo-apps`` literal (renomear o set deixou a fase
    apontando para o vazio) e ``desktop`` → ``@<flavor>``. A relação agora é
    dado do estágio, e ``recipe`` fica na assinatura só por compatibilidade.
    """
    del recipe  # the stage says it all; kept for the callers' signature
    sets = tuple("@" + name for name in phase.sets)
    if phase.emptytree or phase.stage:
        return ("@world", *sets)
    return sets or phase.packages


#: How the assembler installs an image: only binpkgs, and all of them -- the
#: stage3's own packages included (OVERVIEW §7). ``--binpkg-respect-use=y``
#: because --usepkgonly turns it OFF by default: emerge would then take any
#: instance of a package whatever its USE -- the cut one or the settled one.
#: The factory's binpkg check resolves with the same options, so the two
#: cannot drift apart.
ISO_EMERGE_OPTIONS = ("--usepkgonly", "--binpkg-respect-use=y", "--emptytree")

#: The assembler's settle: the cut packages again, from their final binpkgs.
ISO_SETTLE_OPTIONS = ("--usepkgonly", "--binpkg-respect-use=y", "--oneshot")

#: Where the binpkg check puts the stage3's vdb inside the build rootfs: the
#: ROOT its emerge resolves against, so it sees what the assembler sees.
_CHECK_ROOT = Path("var/tmp/shidashi-iso-root")


def image_targets(sets: tuple[str, ...]) -> tuple[str, ...]:
    """The emerge targets of an image made of ``sets``. Pure.

    ``@system`` plus the sets; ``@world`` when there are none (= ``@system`` +
    what the base seeded).
    """
    return ("@system", *(f"@{name}" for name in sets)) if sets else ("@world",)


def _stage_phases(recipe: ResolvedRecipe, stage: str) -> tuple[Phase, ...]:
    """The recipe's stage phases up to and including ``stage``. Pure."""
    chosen: list[Phase] = []
    for phase in recipe.phases:
        if not phase.stage:
            continue
        chosen.append(phase)
        if phase.stage == stage:
            return tuple(chosen)
    raise ValueError(f"stage {stage!r} is not in the recipe's chain")


def shipped_sets(recipe: ResolvedRecipe, stage: str) -> tuple[str, ...]:
    """The sets of the image that ends at ``stage``: every stage's up to it. Pure.

    For the recipe's last stage this is ``recipe.sets``, what the assembler
    installs; for an earlier stage that ships (minimal, inside a flavor's
    recipe) it is that smaller image.
    """
    sets: list[str] = []
    for phase in _stage_phases(recipe, stage):
        sets.extend(s for s in phase.sets if s not in sets)
    return tuple(sets)


def image_cuts(recipe: ResolvedRecipe, stage: str | None = None) -> tuple[UseBreak, ...]:
    """Every cycle cut of the phases up to ``stage`` -- of the whole chain when
    ``stage`` is ``None`` (the assembler's image). Pure.

    The factory built each cut package twice -- cut in its stage, final in the
    settle -- and both binpkgs stay in the PKGDIR. A fresh stage3 has none of
    the cycle's packages installed, so the assembler meets every cycle again
    (ffmpeg -> libsdl2 -> pipewire[ffmpeg] -> ffmpeg stopped the first kde ISO,
    2026-09-29): it installs under the same cuts, then settles them.
    """
    cuts: list[UseBreak] = []
    phases = recipe.phases if stage is None else _stage_phases(recipe, stage)
    for phase in phases:
        cuts.extend(c for c in phase.use_break if c not in cuts)
    return tuple(cuts)


def binpkg_check_argv(recipe: ResolvedRecipe, stage: str, *, root: str) -> list[str]:
    """The ``emerge --pretend`` of the assembler's install, against ``root``. Pure."""
    return [
        "emerge",
        "--pretend",
        f"--root={root}",
        *ISO_EMERGE_OPTIONS,
        *image_targets(shipped_sets(recipe, stage)),
    ]


def binpkg_settle_check_argv(atoms: tuple[str, ...], *, root: str) -> list[str]:
    """The ``emerge --pretend`` of the assembler's settle. Pure.

    ``--nodeps``: the question is only whether each cut package has a binpkg
    with its final USE. Its dependencies were installed by the first pass,
    which a pretend does not do -- resolving them here would meet the very
    cycle the cut exists to avoid.
    """
    return ["emerge", "--pretend", f"--root={root}", *ISO_SETTLE_OPTIONS, "--nodeps", *atoms]


def _extract_vdb(tarball: Path, dest: Path) -> None:
    """Only ``var/db/pkg`` of a stage3 into ``dest`` (~6 s, ~50 MB). I/O."""
    dest.mkdir(parents=True, exist_ok=True)
    _tar(["-xpf", str(tarball), "-C", str(dest), "./var/db/pkg"])


def _seed_tarball(recipe: ResolvedRecipe) -> Path:
    """The cached stage3 the recipe's builds start from (never downloads)."""
    from shidashi.seed import fetch_stage3, load_pointer

    pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
    return fetch_stage3(pointer, cache_dir=config.cache_dir(), download=False)


def check_binpkgs(container: Container, recipe: ResolvedRecipe, stage: str) -> None:
    """Fail when the image ending at ``stage`` cannot be installed from binpkgs.

    PRIVILEGED. Replays the assembler's plan with ``--pretend``: the install
    under the image's cuts, then the settle of the cut packages, against a ROOT
    that holds only the stage3's vdb (what the assembler starts from).

    It catches a package with no binpkg (ccache, from the bootstrap, F75) and a
    binpkg whose USE no longer matches. It does NOT reliably catch a dependency
    cycle (F76): the resolver here is the IMAGE's Portage, while the assembler
    runs the STAGE3's. Measured on 2026-09-29: portage-3.0.82.2 orders the
    ffmpeg -> libsdl2 -> pipewire cycle with no cut, 3.0.81.3 refuses it -- with
    and without --root. A cycle is caught when this resolver also refuses it.
    """
    root = container.rootfs / _CHECK_ROOT
    inside = "/" + _CHECK_ROOT.as_posix()
    cuts = image_cuts(recipe, stage)
    phase = f"{stage}:binpkgs"
    try:
        shutil.rmtree(root, ignore_errors=True)
        _extract_vdb(_seed_tarball(recipe), root)
        write_cuts(container.rootfs, cuts)
        try:
            container.run(binpkg_check_argv(recipe, stage, root=inside), check=True)
            clear_use_break(container.rootfs)
            atoms = tuple(sorted({c.atom for c in cuts}))
            if atoms:
                container.run(binpkg_settle_check_argv(atoms, root=inside), check=True)
        except subprocess.CalledProcessError as exc:
            output = (exc.output or "") + (exc.stderr or "")
            missing = re.findall(r'no binary packages to satisfy "([^"]+)"', output)
            if missing:
                detail = f": no binpkg for {', '.join(missing)}"
            elif "circular dependencies" in output:
                detail = ": a dependency cycle with no cut (use_break) in the chain"
            else:
                detail = ""
            raise FactoryError(
                f"the {stage} image cannot be installed from binpkgs{detail}. The stage's "
                "fork point is saved; fix it before assembling.",
                phase=phase,
                output=output,
            ) from exc
    finally:
        clear_use_break(container.rootfs)
        shutil.rmtree(root, ignore_errors=True)


def stages_flow() -> StagesFlow:
    """The ``stages`` part of the flow in force (``variants/flow.yaml``)."""
    return active_flow(config.variants_dir()).stages


def phase_emerge_argv(
    phase: Phase, recipe: ResolvedRecipe, *, emptytree: bool, flow: StagesFlow | None = None
) -> list[str]:
    """Monta o argv de ``emerge`` para uma fase (R3.1, D24). Puro.

    O modo vem do estágio, não do nome da fase:

    - a base (``phase.emptytree``) → ``--emptytree`` quando ``emptytree`` é
      verdadeiro: a única reconstrução completa, que "cozinha" o stage3;
    - todo estágio depois dela → ``--update --deep --newuse``: recompila só o
      que a configuração daquele estágio muda (o USE gráfico, no desktop) e
      instala os seus sets. Também a base quando ``emptytree`` é falso;
    - uma fase sem estágio (a ``seat`` do openrc) → só os seus átomos.

    ``--usepkg`` always: a binpkg of the same package, version and USE is
    reused. Safe only because the PKGDIR belongs to ONE generation -- the
    fingerprint check (:mod:`shidashi.generation`) runs before the first phase,
    since Portage itself never compares CFLAGS or the toolchain (D26).
    """
    emerge = (flow or stages_flow()).emerge
    if phase.emptytree and emptytree:
        mode: tuple[str, ...] = emerge.rebuild
    elif phase.stage:
        mode = emerge.update
    else:
        mode = ()
    target = phase_target(phase, recipe)
    if phase.stage and not phase.emptytree and not emerge.world:
        target = tuple(t for t in target if t != "@world")
    return ["emerge", *emerge.options, *mode, *target]


# --- 3.2 package.use transitório do break-pass -------------------------------


def use_break_lines(phase: Phase) -> tuple[str, ...]:
    """Renderiza as linhas ``package.use`` das quebras de ciclo da fase (R4.1). Puro.

    Uma linha por :class:`~shidashi.recipe.UseBreak`: ``"<atom> <±flag>"`` onde o
    sinal é ``""`` (habilita) quando ``enable`` é verdadeiro e ``"-"``
    (desabilita) caso contrário. Fase sem quebras → tupla vazia.
    """
    return cut_lines(phase.use_break)


def cut_lines(cuts: tuple[UseBreak, ...]) -> tuple[str, ...]:
    """The ``package.use`` lines of ``cuts``. Pure."""
    return tuple(f"{ub.atom} {'' if ub.enable else '-'}{ub.flag}" for ub in cuts)


def write_use_break(rootfs: Path, phase: Phase) -> Path | None:
    """Escreve o ``package.use`` transitório do break-pass (R4.1/R4.4).

    Grava as linhas de :func:`use_break_lines` em
    ``${rootfs}/etc/portage/package.use/zz-shidashi-use-break`` (criando os
    diretórios-pai) e devolve o caminho escrito. Quando a fase não tem quebras,
    nada é escrito e devolve-se ``None``. Apenas I/O de filesystem — sem root.
    """
    return write_cuts(rootfs, phase.use_break)


def write_cuts(rootfs: Path, cuts: tuple[UseBreak, ...]) -> Path | None:
    """Write ``cuts`` as the transient ``package.use`` file; ``None`` when empty. I/O."""
    lines = cut_lines(cuts)
    if not lines:
        return None
    target = rootfs.joinpath(*_USE_BREAK_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def clear_use_break(rootfs: Path) -> None:
    """Remove o ``package.use`` transitório do break-pass se presente (R4.4).

    Idempotente: limpar quando o arquivo já não existe é um no-op (não levanta).
    """
    rootfs.joinpath(*_USE_BREAK_FILE).unlink(missing_ok=True)


# --- 3.3 parse de átomos construídos -----------------------------------------


def parse_reused_atoms(emerge_output: str) -> tuple[str, ...]:
    """The ``cat/pkg-version`` of the ``[binary ...]`` lines: installed from a binpkg. Pure.

    Kept apart from :func:`parse_built_atoms` (compiled): a stage served wholly
    from the generation's binpkgs used to report nothing at all.
    """
    atoms = (_atom_from_ebuild_line(line.strip(), "[binary") for line in emerge_output.splitlines())
    return tuple(a for a in atoms if a is not None)


def parse_built_atoms(emerge_output: str) -> tuple[str, ...]:
    """Extrai os átomos ``cat/pkg-version`` de uma saída ``emerge --verbose`` (R3.4). Puro.

    Reusa o matcher compartilhado :func:`shidashi.resolve._iter_atom_lines` (mesmo
    casamento de linha ``[ebuild ...]`` de :func:`shidashi.resolve.parse_packages`).
    Saída sem linhas ``[ebuild ...]`` → tupla vazia.
    """
    return tuple(_iter_atom_lines(emerge_output))


# --- 3.4 decisão de fork-point (PURO, só sonda o filesystem) -----------------


def _variant_key(recipe: ResolvedRecipe) -> str:
    """Prefixo de chave por variante: ``<arch>-<flavor>-<init>`` (R5.1/R5.2). Puro.

    Componente comum às chaves de fork-point (tronco e por-fase) e ao estado de
    build (:func:`shidashi.config.build_state_path`), isolando o build por variante.
    """
    return f"{recipe.arch}-{recipe.flavor}-{recipe.init}"


def stage_fork_point_path(
    recipe: ResolvedRecipe, stage: str, *, snapshot: str, fork_points_dir: Path
) -> Path:
    """Where the fork point of one STAGE lives (D24, F70). Pure.

    ``<arch>-<init>-<snapshot>-<stage>.tar`` -- with no target in it, so that
    the fork point of ``base``, ``minimal`` or ``desktop`` built on the way to
    one image is found by every other image of the same arch × init. The old
    key carried the flavor, so the trunk was never shared between flavors.
    """
    return fork_points_dir / f"{recipe.arch}-{recipe.init}-{snapshot}-{stage}.tar"


def fork_point(
    recipe: ResolvedRecipe, *, snapshot: str, fork_points_dir: Path
) -> tuple[Phase, Path] | None:
    """The deepest stage fork point on disk BEFORE the target (D24). Probes only.

    Walks the chain backwards from the stage before the target and returns
    the first whose tarball exists, with its phase -- the point to resume
    after. The target's own stage is never restored: asking for an image is
    asking to build its last stage. ``None`` when nothing is reusable.
    """
    stage_phases = [p for p in recipe.phases if p.stage]
    for phase in reversed(stage_phases[:-1]):
        path = stage_fork_point_path(
            recipe, phase.stage, snapshot=snapshot, fork_points_dir=fork_points_dir
        )
        if path.exists():
            return phase, path
    return None


def pending_breaks(recipe: ResolvedRecipe, *, through: str | None) -> tuple[UseBreak, ...]:
    """The cycle cuts still in force after phase ``through`` (D24). Pure.

    A shipped stage settles every cut accumulated since the previous settle, so
    what is pending is the cuts of the phases after the last shipped one, up to
    and including ``through``. Resuming from a fork point needs it: the base's
    fork point carries its cuts unsettled, and minimal's settle must undo them.
    """
    pending: tuple[UseBreak, ...] = ()
    if through is None:
        return pending
    for phase in recipe.phases:
        pending = () if phase.ships else pending + phase.use_break
        if phase.name == through:
            return pending
    return pending


def trunk_phase_names(recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Nomes das fases do *tronco*: até e incluindo a da base (R5.4, D24). Puro.

    O tronco é o que toda imagem do mesmo arch × init compartilha: o seed, as
    fases que o ``init`` antepõe (``seat``) e a base, a única reconstrução
    completa. É o fork-point 1 da árvore ``base → minimal → desktop → flavor``.
    """
    names: list[str] = []
    for phase in recipe.phases:
        names.append(phase.name)
        if phase.emptytree:
            break
    return tuple(names)


# --- 3.1 (story 004) parse_emerge_plan (PURO) --------------------------------


def _clean_use_flag(token: str) -> str:
    """Normaliza um token de USE-delta ao nome puro da flag. Pura.

    Remove os parênteses externos, o sinal ``-`` de desabilitação e os marcadores
    de mudança ``%``/``*`` (em qualquer combinação), devolvendo só o nome da flag
    (ex.: ``(sound%)`` → ``sound``; ``-wayland*`` → ``wayland``;
    ``(rsync-verify%*)`` → ``rsync-verify``).
    """
    return token.strip("()").lstrip("-").rstrip("%*")


def _use_changes_from_segment(stripped: str) -> tuple[str, ...]:
    """Extrai as USE-deltas do segmento ``USE="..."`` de uma linha ``[ebuild]``. Pura.

    Lê apenas o conteúdo entre aspas do primeiro ``USE="..."`` e devolve as flags
    *alteradas* — as marcadas por ``()``/``%``/``*`` (default mudou, mudou desde a
    última build, asterisco). Flags sem marcador (ex.: ``X``, ``vulkan``) são
    estado corrente, não delta, e são ignoradas. Sem segmento ``USE`` ou sem
    flags marcadas → tupla vazia.
    """
    match = re.search(r'USE="([^"]*)"', stripped)
    if match is None:
        return ()
    changes: list[str] = []
    for token in match.group(1).split():
        if "(" in token or "%" in token or "*" in token:
            flag = _clean_use_flag(token)
            if flag:
                changes.append(flag)
    return tuple(changes)


def parse_emerge_plan(
    output: str,
) -> tuple[tuple[EmergePlanEntry, ...], tuple[str, ...]]:
    """Parseia uma saída ``emerge --verbose`` em entradas de plano + blockers (R4.1/R4.3). Pura.

    Caminha as linhas ``[ebuild ...]`` reusando o núcleo de casamento compartilhado
    :func:`shidashi.resolve._atom_from_ebuild_line` (mesmo átomo de
    :func:`parse_built_atoms`), lendo de cada uma: a coluna de operação (o token
    logo após ``[ebuild`` — ``N``/``R``/``rR``/``U``/``D``/``r``/``NS``/``UD``) em
    :attr:`~shidashi.state.EmergePlanEntry.op` e as USE-deltas do segmento
    ``USE="..."`` (:func:`_use_changes_from_segment`) em ``use_changes``. Linhas
    ``[blocks B ...]`` são coletadas (cruas, stripadas) na segunda tupla. Saída sem
    merge (ex.: ``"Nothing to merge"``) → ``((), ())``. NÃO faz I/O nem dispara
    emerge — opera sobre a saída já capturada.
    """
    entries: list[EmergePlanEntry] = []
    blockers: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("[blocks"):
            blockers.append(stripped)
            continue
        atom = _atom_from_ebuild_line(stripped)
        if atom is None:
            continue
        # coluna de op = tokens entre ``[ebuild`` e ``]`` (ex.: ``N``, ``rR``);
        # _atom_from_ebuild_line já garantiu o prefixo e a presença do ``]``.
        op_column = stripped[len("[ebuild") :].split("]", 1)[0].split()
        if not op_column:
            continue
        entries.append(
            EmergePlanEntry(
                atom=atom,
                op=op_column[0],
                use_changes=_use_changes_from_segment(stripped),
            )
        )
    return tuple(entries), tuple(blockers)


# --- 3.2 (story 004) compute_phase_diff (PURO) -------------------------------


def _category_pn(atom: str) -> str:
    """Reduz ``cat/pkg-version`` ao identificador ``cat/pkg`` (version-stripped). Pura.

    Remove o sufixo de versão do nome do pacote — tudo a partir do último ``-``
    seguido de dígito (cobre revisões ``-rN``, que são parte da versão). Assim
    ``media-libs/mesa-24.0.7`` e ``media-libs/mesa-24.0.5`` colapsam ambos em
    ``media-libs/mesa``, permitindo casar rebuilds por category/PN
    independentemente da versão. Átomos sem componente de versão são devolvidos
    inalterados.
    """
    return re.sub(r"-\d.*$", "", atom)


def compute_phase_diff(
    phase: str,
    plan_entries: tuple[EmergePlanEntry, ...],
    blockers: tuple[str, ...],
    *,
    prior_atoms: tuple[str, ...],
) -> PhaseDiff:
    """Classifica o plano de uma fase num :class:`~shidashi.state.PhaseDiff` (R4.1/R4.2). Pura.

    A partir das entradas de :func:`parse_emerge_plan` compõe o diff da fase
    ``phase``:

    * ``built`` — os átomos das entradas, na ordem;
    * ``unexpected_rebuilds`` — entradas com op ``R``/``rR`` cujo identificador
      category/PN (:func:`_category_pn`, *version-stripped*) já consta em
      ``prior_atoms`` (uma fase reconstruindo o que uma fase anterior já
      construiu, R4.2) — registra o átomo da entrada (com versão);
    * ``use_changes`` — todas as flags das entradas que carregam ``use_changes``,
      achatadas na ordem;
    * ``blockers`` — passthrough do argumento ``blockers``.

    NÃO faz I/O nem dispara emerge.
    """
    prior_pn = {_category_pn(atom) for atom in prior_atoms}
    built = tuple(entry.atom for entry in plan_entries)
    unexpected_rebuilds = tuple(
        entry.atom
        for entry in plan_entries
        if entry.op in ("R", "rR") and _category_pn(entry.atom) in prior_pn
    )
    use_changes = tuple(flag for entry in plan_entries for flag in entry.use_changes)
    return PhaseDiff(
        phase=phase,
        built=built,
        unexpected_rebuilds=unexpected_rebuilds,
        use_changes=use_changes,
        blockers=blockers,
    )


# --- 2.1 (story 004) checkpoint_sequence / plan_phase_run (PURO) -------------


def checkpoint_sequence(recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Sequência de checkpoints do build: ``seed`` + fases + ``settle`` (R2.5). Puro.

    O ``seed`` é o checkpoint 0 (o stage3 seedado, antes de qualquer fase) e
    ``settle`` o checkpoint final (settle-pass de reconciliação do USE). Ambos são
    rótulos de checkpoint/``--until`` apenas — NUNCA membros de
    ``completed_phases``/``phase_diffs``/snapshots por fase, que rastreiam só as
    fases reais da receita.
    """
    return ("seed", *(p.name for p in recipe.phases), "settle")


def plan_phase_run(
    recipe: ResolvedRecipe, *, completed: tuple[str, ...], until: str | None
) -> tuple[Phase, ...]:
    """Plano de fases a rodar do ponto de resume até ``until`` (R1.1/R1.2/R1.4/R1.5). Puro.

    Parte de ``recipe.phases``, descarta toda fase cujo nome está em ``completed``
    (resume pula o que já foi construído) e, quando ``until`` não é ``None``, para
    **após** a fase nomeada por ``until`` (inclusive). ``until="seed"`` ⇒ plano
    vazio (apenas seed, nenhuma fase). ``until`` inválido — fora de
    ``{"seed"} ∪ {nomes de fase}`` — levanta :class:`ValueError` cuja mensagem
    **lista os nomes válidos** (incluindo ``"seed"``); a CLI mapeia esse erro para
    exit 1. ``seed`` e ``settle`` são rótulos de checkpoint, não fases: nunca
    entram em ``completed`` nem no plano devolvido.
    """
    phase_names = tuple(p.name for p in recipe.phases)
    valid = ("seed", *phase_names)
    if until is not None and until not in valid:
        raise ValueError(f"--until {until!r} inválido; valores válidos: {', '.join(valid)}")
    plan: list[Phase] = []
    for phase in recipe.phases:
        if phase.name in completed:
            continue
        plan.append(phase)
        if phase.name == until:
            break
    if until == "seed":
        return ()
    return tuple(plan)


# --- 2.2 (story 004) phase_snapshot_path / latest_resumable (PURO) -----------


def phase_snapshot_path(
    recipe: ResolvedRecipe, *, snapshot: str, phase: str, fork_points_dir: Path
) -> Path:
    """Caminho do snapshot por-fase sob ``fork_points_dir`` (R5.1/R5.2). Puro.

    A chave é ``<arch>-<flavor>-<init>-<snapshot>-<phase>.tar`` — DISTINTA da chave
    do fork-point do tronco da story 003 (:func:`fork_point`, que omite ``phase``):
    cada fase completada materializa seu próprio snapshot para resume granular. Não
    sonda nem escreve nada — apenas compõe o caminho.
    """
    return fork_points_dir / f"{_variant_key(recipe)}-{snapshot}-{phase}.tar"


def latest_resumable(
    recipe: ResolvedRecipe,
    *,
    snapshot: str,
    completed: tuple[str, ...],
    fork_points_dir: Path,
) -> tuple[str | None, Path | None]:
    """Última fase completada com snapshot em disco e seu caminho (R5.2). Puro.

    Caminha as fases completadas na ordem de ``recipe.phases`` (não na ordem de
    ``completed``) e devolve a ÚLTIMA cujo :func:`phase_snapshot_path` existe no
    disco, junto do caminho — o ponto de restauração do resume. Se nenhuma fase
    completada tem snapshot em disco devolve ``(None, None)``. Apenas sonda o
    filesystem.
    """
    found: tuple[str, Path] | None = None
    for phase in recipe.phases:
        if phase.name not in completed:
            continue
        candidate = phase_snapshot_path(
            recipe, snapshot=snapshot, phase=phase.name, fork_points_dir=fork_points_dir
        )
        if candidate.exists():
            found = (phase.name, candidate)
    if found is None:
        return (None, None)
    return found


# --- 4.1 snapshot / restore do fork-point (tarball, I/O em disco) ------------


def snapshot_fork_point(rootfs: Path, dest: Path) -> Path:
    """Captura ``rootfs`` num tarball em ``dest`` e devolve ``dest`` (R5.1/R5.2).

    Escrita atômica: o tar é gravado primeiro num arquivo temporário irmão de
    ``dest`` (mesmo diretório, logo mesmo filesystem) e só então promovido via
    :func:`os.replace`. Falha durante a escrita remove o temp parcial. O
    conteúdo fica na raiz do tar (``-C rootfs .``), de modo que
    :func:`restore_fork_point` o reconstrua diretamente sob outro diretório.

    GNU tar with :data:`shidashi.seed.ROOTFS_TAR_FLAGS`: every mode bit and the
    xattrs (file capabilities) survive, which Python's ``tarfile`` did not.
    """
    tmp = dest.with_name(f".{dest.name}.tmp")
    try:
        _tar(["--create", "--file", str(tmp), "--directory", str(rootfs), *ROOTFS_TAR_FLAGS, "."])
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def restore_fork_point(tarball: Path, rootfs: Path) -> None:
    """Extrai ``tarball`` dentro de ``rootfs`` (R5.1/R5.2), modes and xattrs intact."""
    _tar(["--extract", "--file", str(tarball), "--directory", str(rootfs), *ROOTFS_TAR_FLAGS])


def _tar(args: list[str]) -> None:
    result = subprocess.run(["tar", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise FactoryError(f"tar {args[0]} failed: {result.stderr.strip()}", phase="fork-point")


# --- orquestração privilegiada (story 003 tarefa 5) --------------------------


def _run_emerge(
    container: Container, argv: list[str], *, phase: str
) -> tuple[tuple[str, ...], str]:
    """Roda um ``emerge`` no container; devolve ``(átomos, saída crua)`` (R3.4/R8.3).

    Embrulha um ``emerge`` não-zero (``CalledProcessError``) em
    :class:`FactoryError` carregando ``phase`` e a saída capturada
    (stdout+stderr). No sucesso devolve a tupla de :func:`parse_built_atoms`
    JUNTO da saída crua ``stdout+stderr`` — o driver stepwise reusa essa saída
    para compor o diff da fase (:func:`parse_emerge_plan`/:func:`compute_phase_diff`)
    SEM re-rodar emerge (R4.1).
    """
    try:
        result = container.run(argv, check=True)
    except subprocess.CalledProcessError as exc:
        output = (exc.output or "") + (exc.stderr or "")
        raise FactoryError(f"emerge falhou na fase {phase!r}", phase=phase, output=output) from exc
    raw_output = result.stdout + result.stderr
    return parse_built_atoms(raw_output), raw_output


# --- module-rebuild (F83) --------------------------------------------------------
#
# A binpkg of an out-of-tree module (nvidia-drivers, r8168...) carries modules for
# the kernel it was BUILT against. Portage reuses it while `virtual/dist-kernel`'s
# subslot is unchanged -- and gentoo-kernel-7.2.6 and gentoo-kernel-bin-7.2.6 share
# it, though their modules live in different directories (7.2.6-gentoo-dist vs
# 7.2.6-gentoo-dist-bin). After the swap the image had its modules for a kernel it
# did not have (2026-10-01). This step finds such directories and rebuilds their
# owners from source, against the kernel actually installed.

#: The rootfs's modules directory (``/lib`` is a symlink to ``usr/lib``).
_MODULES = Path("usr") / "lib" / "modules"
_VDB = Path("var") / "db" / "pkg"


def stale_module_dirs(rootfs: Path) -> tuple[str, ...]:
    """Kernel-version directories under /lib/modules with no kernel in them. I/O.

    A kernel's directory holds its image (``vmlinuz``, where kernel-install and
    the dist-kernels put it); one without is left by modules built for a kernel
    that is not installed. ``()`` when no kernel is installed at all: nothing to
    compare against yet.
    """
    root = rootfs / _MODULES
    if not root.is_dir():
        return ()
    dirs = sorted(d for d in root.iterdir() if d.is_dir())
    kernels = {d.name for d in dirs if (d / "vmlinuz").exists() or (d / "vmlinuz").is_symlink()}
    if not kernels:
        return ()
    return tuple(d.name for d in dirs if d.name not in kernels)


def module_owners(rootfs: Path, kernel_versions: Sequence[str]) -> tuple[str, ...]:
    """The packages (``category/package-version``) that installed files under
    those /lib/modules directories, read from the vdb's CONTENTS. I/O."""
    if not kernel_versions:
        return ()
    prefixes = tuple(
        f"{base}/modules/{kv}/" for kv in kernel_versions for base in ("/lib", "/usr/lib")
    )
    owners: set[str] = set()
    vdb = rootfs / _VDB
    for contents in sorted(vdb.glob("*/*/CONTENTS")):
        for line in contents.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0] in ("obj", "sym") and parts[1].startswith(prefixes):
                owners.add(f"{contents.parent.parent.name}/{contents.parent.name}")
                break
    return tuple(sorted(owners))


def module_rebuild(container: Container, *, phase: str) -> dict[str, object]:
    """Rebuild, from source, every package with modules for a kernel that is not
    installed; drop the directories left empty of packages. PRIVILEGED.

    Built WITH buildpkg: the new binpkg instance is the one later builds and the
    assembler pick. Fails when a stale directory still has an owner afterwards.
    """
    rootfs = container.rootfs
    stale = stale_module_dirs(rootfs)
    if not stale:
        return {"stale": []}
    owners = module_owners(rootfs, stale)
    if owners:
        argv = ["emerge", "--oneshot", "--verbose", "--usepkg=n", *(f"={o}" for o in owners)]
        _run_emerge(container, argv, phase=phase)
    left = module_owners(rootfs, stale)
    if left:
        raise FactoryError(
            f"modules for a kernel that is not installed ({', '.join(stale)}) are still "
            f"owned by {', '.join(left)} after the rebuild",
            phase=phase,
        )
    # what remains is depmod's output for a kernel that is gone: no package owns it
    container.run(["rm", "-rf", *(f"/{_MODULES}/{kv}" for kv in stale)], check=True)
    return {"stale": list(stale), "rebuilt": list(owners)}


def run_phase(
    container: Container, recipe: ResolvedRecipe, phase: Phase, *, emptytree: bool
) -> PhaseResult:
    """Executa uma fase (um ``emerge`` ordenado) dentro do container (R3.1/R3.4/R4.1).

    PRIVILEGIADO (``emerge`` roda dentro do nspawn). Escreve o ``package.use``
    transitório do break-pass da fase (:func:`write_use_break`), roda
    ``emerge --verbose`` com o(s) alvo(s) de :func:`phase_emerge_argv`
    (``--emptytree`` só na base, ``-uDN`` nos estágios seguintes) e devolve um
    :class:`PhaseResult` com os átomos de :func:`parse_built_atoms`
    (``snapshot=None`` — o fork-point é materializado por :func:`run_phases`).
    Um ``emerge`` com saída não-zero (``CalledProcessError``) é embrulhado em
    :class:`FactoryError` carregando o nome da fase e a saída capturada (R8.3).
    """
    if not phase_target(phase, recipe):
        # Fase sem alvo é NO-OP, no mesmo espírito de settle_pass com breaks
        # vazio: nenhum `emerge` é executado. Hoje só uma fase que não é estágio
        # (a `seat` do openrc) sem átomos cairia aqui; um estágio sempre tem @world.
        return PhaseResult(phase=phase, built_atoms=(), snapshot=None, output="")
    flow = stages_flow()
    before, _after = flow.split()
    built: tuple[str, ...] = ()
    output = ""
    run = audit.current()
    # the steps up to the emerge, in the order variants/flow.yaml declares them
    for kind in before:
        with run.step(kind) as step:
            if kind == "apply-config" and phase.layers:
                # The configuration in force for THIS stage (D24). Layers only grow
                # along the chain, so re-applying is additive.
                apply_portage(
                    container.rootfs,
                    recipe,
                    variants_dir=config.variants_dir(),
                    layers=phase.layers,
                )
                step.add(layers=list(phase.layers))
            elif kind == "write-cuts":
                write_use_break(container.rootfs, phase)
                step.add(cuts=list(use_break_lines(phase)))
            elif kind == "emerge-stage":
                argv = phase_emerge_argv(phase, recipe, emptytree=emptytree, flow=flow)
                since = int(time.time())
                built, output = _run_emerge(container, argv, phase=phase.name)
                reused = parse_reused_atoms(output)
                step.add(argv=argv, built=len(built), reused=len(reused))
                attach_packages(
                    container.rootfs,
                    phase.stage or phase.name,
                    since=since,
                    built=built,
                    reused=reused,
                )
    return PhaseResult(
        phase=phase,
        built_atoms=built,
        snapshot=None,
        output=output,
        reused_atoms=parse_reused_atoms(output),
    )


def attach_packages(
    rootfs: Path,
    label: str,
    *,
    since: int,
    built: Sequence[str] = (),
    reused: Sequence[str] = (),
) -> None:
    """``packages-<label>.json``: the rootfs's packages after this emerge, with the
    ones it merged marked built or binpkg and timed from emerge.log (audit)."""
    run = audit.current()
    if run.root is None:
        return
    try:
        text = (rootfs / "var" / "log" / "emerge.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    run.attach(
        f"packages-{label}",
        audit.harvest_packages(
            rootfs, merges=audit.parse_emerge_log(text, since=since), built=built, reused=reused
        ),
    )


def is_installed(rootfs: Path, cp: str) -> bool:
    """Whether ``category/package`` has an entry in the rootfs's vdb. Pure I/O.

    Matches ``<name>-<digit>`` so that ``python`` is not taken for
    ``python-exec``.
    """
    category, name = cp.split("/", 1)
    vdb = rootfs / "var" / "db" / "pkg" / category
    if not vdb.is_dir():
        return False
    prefix = f"{name}-"
    return any(
        d.name.startswith(prefix) and d.name[len(prefix) :][:1].isdigit() for d in vdb.iterdir()
    )


def settle_pass(
    container: Container, recipe: ResolvedRecipe, breaks: tuple[UseBreak, ...], *, stage: str = ""
) -> PhaseResult:
    """Settle-pass: re-emerge os átomos quebrados com o USE final (R4.2/R4.3/R4.4).

    PRIVILEGIADO. Quando ``breaks`` é vazio é um **no-op**: devolve um
    :class:`PhaseResult` da fase ``settle`` sem átomos e **sem** chamar
    ``container.run`` (nenhum ``emerge``; R4.4). Caso contrário remove o
    ``package.use`` transitório do break-pass (:func:`clear_use_break`) e re-emerge
    os átomos distintos das quebras (ordenados) com ``--newuse --oneshot`` para
    reconstruí-los com o USE definitivo. Falha de ``emerge`` (não-zero) embrulha em
    :class:`FactoryError` (``phase="settle"``).
    """
    settle = Phase(name="settle", stage=stage)
    if not breaks:
        return PhaseResult(phase=settle, built_atoms=(), snapshot=None)
    clear_use_break(container.rootfs)
    # Only what is INSTALLED has a cut to undo. A cut declared by the base can
    # name a package the shipped image does not contain -- pipewire is cut in the
    # base, but minimal has no audio server (D24) -- and `--oneshot` on it would
    # install it (first real run, 2026-09-27).
    atoms = sorted({b.atom for b in breaks if is_installed(container.rootfs, b.atom)})
    if not atoms:
        return PhaseResult(phase=settle, built_atoms=(), snapshot=None)
    built, output = _run_emerge(
        container, ["emerge", *stages_flow().settle.options, *atoms], phase="settle"
    )
    return PhaseResult(phase=settle, built_atoms=built, snapshot=None, output=output)


def _audited_settle(
    container: Container, recipe: ResolvedRecipe, breaks: tuple[UseBreak, ...], stage: str
) -> PhaseResult:
    """:func:`settle_pass` as an audited step, with the atoms it rebuilt."""
    with audit.current().step("settle", cuts=len(breaks)) as step:
        result = settle_pass(container, recipe, breaks, stage=stage)
        step.add(atoms=list(result.built_atoms))
    return result


def _audited_snapshot(rootfs: Path, dest: Path) -> None:
    """:func:`snapshot_fork_point` as an audited step, with the tarball's size.

    Size only: hashing a 25 GB fork point would add minutes to every stage.
    """
    with audit.current().step("fork-point", path=str(dest)) as step:
        snapshot_fork_point(rootfs, dest)
        step.add(size_bytes=dest.stat().st_size if dest.is_file() else None)


def run_phases(
    container: Container,
    recipe: ResolvedRecipe,
    *,
    emptytree: bool,
    resume_at: str | None = None,
    snapshot: str,
    fork_points_dir: Path,
    stop_after: str | None = None,
) -> tuple[PhaseResult, ...]:
    """Orquestra a cadeia de estágios na ordem (R3.x/R4.x/R5.x, D24).

    PRIVILEGIADO. Quando ``resume_at`` é dado (um fork-point restaurado), as
    fases até e incluindo ele são puladas e os cortes que ainda valiam ali
    (:func:`pending_breaks`) seguem pendentes. Para cada fase restante:

    1. :func:`run_phase` -- aplica as camadas do estágio, os cortes e o emerge;
    2. se o estágio é ENTREGUE (``ships``), :func:`settle_pass` desfaz os cortes
       acumulados desde o último settle -- a imagem é assentada aqui, e o que
       vem depois parte dela assentada;
    3. se a fase é de um estágio, grava o fork-point dele
       (:func:`stage_fork_point_path`), depois do settle.

    Devolve os :class:`PhaseResult` na ordem, cada settle logo após o seu estágio.

    ``stop_after`` names a STAGE: the run ends right after that stage's fork
    point (and its settle, when it ships). The next run without it resumes from
    that fork point -- e.g. build the desktop stage alone before a flavor.
    """
    pending = pending_breaks(recipe, through=resume_at)
    _before, after = stages_flow().split()
    skipping = resume_at is not None
    results: list[PhaseResult] = []
    for phase in recipe.phases:
        if skipping:
            if phase.name == resume_at:
                skipping = False
            continue
        with audit.current().step(
            f"stage:{phase.stage or phase.name}", ships=phase.ships, sets=list(phase.sets)
        ):
            results.append(run_phase(container, recipe, phase, emptytree=emptytree))
            pending += phase.use_break
            # the steps after the emerge, in the order variants/flow.yaml declares them
            for kind in after:
                if kind == "module-rebuild":
                    with audit.current().step("module-rebuild") as step:
                        step.add(**module_rebuild(container, phase=phase.name))
                elif kind == "settle" and phase.ships:
                    results.append(_audited_settle(container, recipe, pending, phase.stage))
                    pending = ()
                elif kind == "snapshot" and phase.stage:
                    _audited_snapshot(
                        container.rootfs,
                        stage_fork_point_path(
                            recipe,
                            phase.stage,
                            snapshot=snapshot,
                            fork_points_dir=fork_points_dir,
                        ),
                    )
                elif kind == "check-binpkgs" and phase.ships:
                    with audit.current().step("check-binpkgs"):
                        check_binpkgs(container, recipe, phase.stage)
        if stop_after is not None and phase.stage == stop_after:
            break
    return tuple(results)


# --- 5.1/5.2 (story 004) orquestração stepwise interativa --------------------


CheckpointHook = Callable[[str, PhaseDiff], CheckpointDecision]
FailureHook = Callable[[str, Exception], FailureDecision]


@dataclasses.dataclass
class _RunState:
    """Estado mutável acumulado ao longo das fases do build stepwise (R4.1/R6.1).

    Concentra o progresso corrente — ``completed`` (nomes de fase já encerradas),
    ``phase_diffs`` (diff por fase), ``accumulated_breaks`` (quebras de ciclo
    acumuladas) e ``prior_atoms`` (todos os átomos construídos até aqui, base do
    ``prior_atoms`` de :func:`compute_phase_diff`) — e sabe se persistir via
    :meth:`persist` (``state.save_state`` módulo-qualificado; ``OSError`` propaga).
    Isolar o estado num objeto evita capturar variáveis de laço numa closure de
    persistência no caminho de ABORT.
    """

    recipe: ResolvedRecipe
    state_path: Path
    snapshot: str
    completed: tuple[str, ...]
    phase_diffs: tuple[PhaseDiff, ...] = ()
    accumulated_breaks: tuple[UseBreak, ...] = ()
    prior_atoms: tuple[str, ...] = ()

    def record(self, phase: Phase, diff: PhaseDiff, built_atoms: tuple[str, ...]) -> None:
        """Incorpora uma fase concluída: nome, diff, quebras e átomos construídos."""
        self.completed += (phase.name,)
        self.phase_diffs += (diff,)
        self.accumulated_breaks += phase.use_break
        self.prior_atoms += built_atoms

    def persist(self) -> None:
        """Persiste o :class:`~shidashi.state.BuildState` corrente (R6.1; ``OSError`` propaga)."""
        state.save_state(
            self.state_path,
            state.BuildState(
                arch=self.recipe.arch,
                flavor=self.recipe.flavor,
                init=self.recipe.init,
                snapshot=self.snapshot,
                recipe_hash=state.recipe_hash(self.recipe),
                seed_done=True,
                # phases only ever run after the toolchain bootstrap
                bootstrap_done=True,
                completed_phases=self.completed,
                accumulated_breaks=self.accumulated_breaks,
                phase_diffs=self.phase_diffs,
            ),
        )


def _run_phase_retrying(
    container: Container,
    recipe: ResolvedRecipe,
    phase: Phase,
    *,
    emptytree: bool,
    on_failure: FailureHook | None,
    on_abort: Callable[[], None],
) -> PhaseResult:
    """Roda uma fase via :func:`run_phase` num laço de retry guiado por ``on_failure``.

    Numa falha (``FactoryError`` — que :func:`run_phase` levanta embrulhando o
    ``CalledProcessError`` do emerge): sem ``on_failure`` re-levanta (caminho
    não-interativo ``--until``; o estado das fases anteriores já está persistido e
    o rootfs é mantido → exit 1, R3.5). Com ``on_failure``, consulta
    ``on_failure(phase.name, err)``: ``RETRY`` re-roda a MESMA fase (mesmo argv —
    novo laço); ``ABORT`` invoca ``on_abort`` (persistir o estado das fases
    anteriores) e levanta a :class:`FactoryError`. NUNCA pula uma fase falha (R3.4).
    """
    while True:
        try:
            return run_phase(container, recipe, phase, emptytree=emptytree)
        except (FactoryError, subprocess.CalledProcessError) as err:
            if on_failure is None:
                raise
            if on_failure(phase.name, err) is FailureDecision.RETRY:
                continue
            on_abort()
            if isinstance(err, FactoryError):
                raise
            raise FactoryError(f"build abortado na fase {phase.name!r}", phase=phase.name) from err


def run_phases_stepwise(
    container: Container,
    recipe: ResolvedRecipe,
    *,
    emptytree: bool,
    completed: tuple[str, ...],
    until: str | None,
    snapshot: str,
    fork_points_dir: Path,
    state_path: Path,
    on_checkpoint: CheckpointHook | None = None,
    on_failure: FailureHook | None = None,
) -> tuple[PhaseResult, ...]:
    """Orquestra as fases do build passo-a-passo, com checkpoints e retry (R1.x/R2.x/R3.x/R5.x).

    PRIVILEGIADO. Itera o plano de :func:`plan_phase_run` (resume a partir de
    ``completed``, parando após ``until`` inclusive). Por fase:

    * roda-a via :func:`run_phase` num laço de retry (:func:`_run_phase_retrying`):
      sem ``on_failure`` uma falha propaga com o estado anterior persistido e o
      rootfs mantido (R3.5); com ``on_failure``, ``RETRY`` re-roda a mesma fase e
      ``ABORT`` persiste e levanta (R3.1–R3.3); jamais pula (R3.4);
    * compõe o diff via :func:`compute_phase_diff` a partir da saída capturada da
      fase (sem re-rodar emerge), com ``prior_atoms`` = todos os átomos das fases
      anteriores (R4.1/R4.2);
    * captura o fork-point por-fase em :func:`phase_snapshot_path` via
      :func:`snapshot_fork_point` (R5.1/R5.2);
    * acumula ``completed``/``phase_diffs``/quebras e persiste o
      :class:`~shidashi.state.BuildState` via ``state.save_state`` (módulo-qualificado
      para ser monkeypatchável; ``OSError`` propaga — um build que não consegue
      gravar progresso falha alto);
    * consulta ``on_checkpoint(phase.name, diff)`` (``None`` ⇒ auto-CONTINUE) e
      honra a :class:`CheckpointDecision`: ``CONTINUE`` segue; ``STOP`` interrompe o
      laço antes da fase seguinte (R1.3/R2.3); ``SHELL`` abre ``container.shell()`` e
      re-apresenta o MESMO checkpoint.

    Cada estágio ENTREGUE (``ships``) é assentado logo depois da sua fase
    (D24): :func:`settle_pass` desfaz os cortes acumulados desde o último
    settle, e o snapshot e o checkpoint daquela fase já veem a imagem assentada.
    Um STOP interrompe antes da fase seguinte, nunca no meio de uma imagem. Ao
    retomar, os cortes ainda pendentes vêm de :func:`pending_breaks`. Devolve os
    :class:`PhaseResult` executados, cada settle logo após o seu estágio.
    """
    plan = plan_phase_run(recipe, completed=completed, until=until)
    _before, after = stages_flow().split()
    last_done = next((p.name for p in reversed(recipe.phases) if p.name in completed), None)

    run = _RunState(
        recipe=recipe,
        state_path=state_path,
        snapshot=snapshot,
        completed=completed,
        accumulated_breaks=pending_breaks(recipe, through=last_done),
    )
    results: list[PhaseResult] = []

    for phase in plan:
        result = _run_phase_retrying(
            container,
            recipe,
            phase,
            emptytree=emptytree,
            on_failure=on_failure,
            on_abort=run.persist,
        )
        results.append(result)

        entries, blockers = parse_emerge_plan(result.output)
        diff = compute_phase_diff(phase.name, entries, blockers, prior_atoms=run.prior_atoms)
        run.record(phase, diff, result.built_atoms)

        # the steps after the emerge, in the order variants/flow.yaml declares them
        for kind in after:
            if kind == "module-rebuild":
                with audit.current().step("module-rebuild") as step:
                    step.add(**module_rebuild(container, phase=phase.name))
            elif kind == "settle" and phase.ships:
                results.append(
                    _audited_settle(container, recipe, run.accumulated_breaks, phase.stage)
                )
                run.accumulated_breaks = ()
            elif kind == "snapshot":
                _audited_snapshot(
                    container.rootfs,
                    phase_snapshot_path(
                        recipe,
                        snapshot=snapshot,
                        phase=phase.name,
                        fork_points_dir=fork_points_dir,
                    ),
                )
            elif kind == "check-binpkgs" and phase.ships:
                with audit.current().step("check-binpkgs"):
                    check_binpkgs(container, recipe, phase.stage)
        run.persist()

        if _checkpoint_decision(on_checkpoint, container, phase.name, diff) is (
            CheckpointDecision.STOP
        ):
            break

    return tuple(results)


def _checkpoint_decision(
    on_checkpoint: CheckpointHook | None,
    container: Container,
    phase_name: str,
    diff: PhaseDiff,
) -> CheckpointDecision:
    """Resolve a decisão do checkpoint pós-fase honrando ``SHELL`` (R2.2/R2.3).

    Sem ``on_checkpoint`` ⇒ auto-``CONTINUE``. Caso contrário consulta o hook; numa
    decisão ``SHELL`` abre ``container.shell()`` e re-apresenta o MESMO checkpoint
    (re-chama o hook), repetindo até uma decisão terminal ``CONTINUE``/``STOP``.
    """
    if on_checkpoint is None:
        return CheckpointDecision.CONTINUE
    while True:
        decision = on_checkpoint(phase_name, diff)
        if decision is not CheckpointDecision.SHELL:
            return decision
        container.shell()
