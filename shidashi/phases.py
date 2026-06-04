"""Phases — execução do build em fases + cache de camadas (OVERVIEW §6.4 / §6.5).

Camada de planejamento PURA da story 003 (grupos 3 + 4.1):

* **3.1** :func:`phase_target` / :func:`phase_emerge_argv` — alvo emerge de cada
  fase e o argv completo (``--emptytree`` só no ``rebuild``).
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
import subprocess
import tarfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

import pydantic

from shidashi import state
from shidashi.container import Container
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from shidashi.resolve import _atom_from_ebuild_line, _iter_atom_lines
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


# --- 3.1 phase_target / phase_emerge_argv (PURO) -----------------------------


def phase_target(phase: Phase, recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Devolve o alvo ``emerge`` de uma fase (R3.1/R3.2/R3.3). Puro.

    Convenções por nome de fase: ``rebuild`` → ``@world``; ``seat`` →
    ``phase.packages`` (átomos explícitos); ``desktop`` → ``@<flavor>`` (o set do
    flavor); ``apps`` → ``@bentoo-apps``. Para qualquer outro nome, se ele é um
    set declarado em ``recipe.sets`` usa-se ``@<nome>``; senão recai-se nos
    ``phase.packages``.
    """
    if phase.name == "rebuild":
        return ("@world",)
    if phase.name == "seat":
        return phase.packages
    if phase.name == "desktop":
        return ("@" + recipe.flavor,)
    if phase.name == "apps":
        return ("@bentoo-apps",)
    if phase.name in recipe.sets:
        return ("@" + phase.name,)
    return phase.packages


def phase_emerge_argv(phase: Phase, recipe: ResolvedRecipe, *, emptytree: bool) -> list[str]:
    """Monta o argv de ``emerge`` para uma fase (R3.1). Puro.

    Sempre ``emerge --verbose`` seguido do(s) alvo(s) de :func:`phase_target`.
    ``--emptytree`` é emitido apenas na fase ``rebuild`` e somente quando
    ``emptytree`` é verdadeiro (reconstrução total do tronco).
    """
    return [
        "emerge",
        "--verbose",
        *(("--emptytree",) if (emptytree and phase.name == "rebuild") else ()),
        *phase_target(phase, recipe),
    ]


# --- 3.2 package.use transitório do break-pass -------------------------------


def use_break_lines(phase: Phase) -> tuple[str, ...]:
    """Renderiza as linhas ``package.use`` das quebras de ciclo da fase (R4.1). Puro.

    Uma linha por :class:`~shidashi.recipe.UseBreak`: ``"<atom> <±flag>"`` onde o
    sinal é ``""`` (habilita) quando ``enable`` é verdadeiro e ``"-"``
    (desabilita) caso contrário. Fase sem quebras → tupla vazia.
    """
    return tuple(f"{ub.atom} {'' if ub.enable else '-'}{ub.flag}" for ub in phase.use_break)


def write_use_break(rootfs: Path, phase: Phase) -> Path | None:
    """Escreve o ``package.use`` transitório do break-pass (R4.1/R4.4).

    Grava as linhas de :func:`use_break_lines` em
    ``${rootfs}/etc/portage/package.use/zz-shidashi-use-break`` (criando os
    diretórios-pai) e devolve o caminho escrito. Quando a fase não tem quebras,
    nada é escrito e devolve-se ``None``. Apenas I/O de filesystem — sem root.
    """
    lines = use_break_lines(phase)
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


def fork_point(recipe: ResolvedRecipe, *, snapshot: str, fork_points_dir: Path) -> Path | None:
    """Devolve o snapshot do tronco a reusar antes da fase de desktop (R5.1/R5.2).

    A chave do tarball é ``<arch>-<flavor>-<init>-<snapshot>.tar`` sob
    ``fork_points_dir``. Devolve o caminho se o arquivo existir, senão ``None``.
    Apenas sonda o filesystem — não cria, extrai nem escreve nada.
    """
    candidate = fork_points_dir / f"{_variant_key(recipe)}-{snapshot}.tar"
    return candidate if candidate.exists() else None


def trunk_phase_names(recipe: ResolvedRecipe) -> tuple[str, ...]:
    """Nomes das fases do *tronco*: as que precedem ``desktop`` (R5.4). Puro.

    O tronco é a parte das fases comum entre flavors (depende só do init), antes
    do fork na fase ``desktop``. Para um flavor sem fase ``desktop`` (ex.:
    ``minimal``) o tronco é toda a sequência de fases.
    """
    names: list[str] = []
    for phase in recipe.phases:
        if phase.name == "desktop":
            break
        names.append(phase.name)
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
    :func:`os.replace`, que consome o nome temporário — em caso de sucesso não
    fica nenhum temp pendente ao lado de ``dest``. Falha durante a escrita remove
    o temp parcial. O ``arcname=""`` mantém o conteúdo do rootfs na raiz do tar,
    de modo que :func:`restore_fork_point` o reconstrua diretamente sob outro
    diretório (layout relativo preservado). Ownership/devices fiéis de um rootfs
    real exigem root (coberto pelo teste de integração host-gated); a árvore em
    tmp faz round-trip de conteúdo + layout sem root.
    """
    tmp = dest.with_name(f".{dest.name}.tmp")
    try:
        with tarfile.open(tmp, "w") as tar:
            tar.add(rootfs, arcname="")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def restore_fork_point(tarball: Path, rootfs: Path) -> None:
    """Extrai ``tarball`` dentro de ``rootfs`` (R5.1/R5.2).

    Usa o filtro ``"tar"`` na extração para preservar ownership/permissões quando
    rodando como root (o teste de integração host-gated verifica ``st_uid == 0``);
    sob a árvore tmp sem root isto degrada para conteúdo + layout relativo, que é
    o que o round-trip unitário exige.
    """
    with tarfile.open(tarball, "r") as tar:
        tar.extractall(rootfs, filter="tar")


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


def run_phase(
    container: Container, recipe: ResolvedRecipe, phase: Phase, *, emptytree: bool
) -> PhaseResult:
    """Executa uma fase (um ``emerge`` ordenado) dentro do container (R3.1/R3.4/R4.1).

    PRIVILEGIADO (``emerge`` roda dentro do nspawn). Escreve o ``package.use``
    transitório do break-pass da fase (:func:`write_use_break`), roda
    ``emerge --verbose`` com o(s) alvo(s) de :func:`phase_emerge_argv`
    (``--emptytree`` só em ``rebuild`` quando ``emptytree``) e devolve um
    :class:`PhaseResult` com os átomos de :func:`parse_built_atoms`
    (``snapshot=None`` — o fork-point é materializado por :func:`run_phases`).
    Um ``emerge`` com saída não-zero (``CalledProcessError``) é embrulhado em
    :class:`FactoryError` carregando o nome da fase e a saída capturada (R8.3).
    """
    write_use_break(container.rootfs, phase)
    argv = phase_emerge_argv(phase, recipe, emptytree=emptytree)
    built, output = _run_emerge(container, argv, phase=phase.name)
    return PhaseResult(phase=phase, built_atoms=built, snapshot=None, output=output)


def settle_pass(
    container: Container, recipe: ResolvedRecipe, breaks: tuple[UseBreak, ...]
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
    settle = Phase(name="settle")
    if not breaks:
        return PhaseResult(phase=settle, built_atoms=(), snapshot=None)
    clear_use_break(container.rootfs)
    atoms = sorted({b.atom for b in breaks})
    built, output = _run_emerge(
        container, ["emerge", "--verbose", "--newuse", "--oneshot", *atoms], phase="settle"
    )
    return PhaseResult(phase=settle, built_atoms=built, snapshot=None, output=output)


def run_phases(
    container: Container,
    recipe: ResolvedRecipe,
    *,
    emptytree: bool,
    resume_at: str | None = None,
    snapshot: str,
    fork_points_dir: Path,
) -> tuple[PhaseResult, ...]:
    """Orquestra todas as fases da receita na ordem definida (R3.1/R3.2/R4.x/R5.x).

    PRIVILEGIADO. Quando ``resume_at`` é dado (restauração de um fork-point), as
    fases até e incluindo ``resume_at`` são puladas — o tronco já está no rootfs.
    Cada fase restante roda via :func:`run_phase`; ao concluir a última fase do
    tronco (:func:`trunk_phase_names`) o rootfs é capturado num fork-point via
    :func:`snapshot_fork_point` em ``fork_points_dir`` sob a chave
    ``<arch>-<flavor>-<init>-<snapshot>.tar`` (R5.1/R5.2). As quebras de ciclo de
    todas as fases são acumuladas e reconciliadas por um :func:`settle_pass` final
    (R4.2/R4.3). Devolve a tupla de :class:`PhaseResult` das fases executadas.
    """
    trunk = trunk_phase_names(recipe)
    last_trunk = trunk[-1] if trunk else None
    fork_key = f"{_variant_key(recipe)}-{snapshot}.tar"

    skipping = resume_at is not None
    results: list[PhaseResult] = []
    accumulated: tuple[UseBreak, ...] = ()
    for phase in recipe.phases:
        accumulated += phase.use_break
        if skipping:
            # pula o tronco já materializado pelo fork-point restaurado, até e
            # incluindo a fase nomeada por resume_at.
            if phase.name == resume_at:
                skipping = False
            continue
        results.append(run_phase(container, recipe, phase, emptytree=emptytree))
        if phase.name == last_trunk:
            snapshot_fork_point(container.rootfs, fork_points_dir / fork_key)

    results.append(settle_pass(container, recipe, accumulated))
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
      laço SEM settle (R1.3/R2.3); ``SHELL`` abre ``container.shell()`` e
      re-apresenta o MESMO checkpoint.

    Ao fim roda :func:`settle_pass` e o anexa SOMENTE quando o plano alcançou a
    fase FINAL da receita e NÃO houve stop antecipado (R1.3): sem STOP e ``until``
    ``None`` ou igual ao nome da última fase. Devolve a tupla de
    :class:`PhaseResult` das fases executadas (incluindo o settle quando rodou).
    """
    plan = plan_phase_run(recipe, completed=completed, until=until)
    final_phase = recipe.phases[-1].name if recipe.phases else None

    run = _RunState(recipe=recipe, state_path=state_path, snapshot=snapshot, completed=completed)
    results: list[PhaseResult] = []
    stopped = False

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

        snapshot_fork_point(
            container.rootfs,
            phase_snapshot_path(
                recipe, snapshot=snapshot, phase=phase.name, fork_points_dir=fork_points_dir
            ),
        )

        run.record(phase, diff, result.built_atoms)
        run.persist()

        if _checkpoint_decision(on_checkpoint, container, phase.name, diff) is (
            CheckpointDecision.STOP
        ):
            stopped = True
            break

    reached_final = bool(plan) and plan[-1].name == final_phase
    if reached_final and not stopped and (until is None or until == final_phase):
        results.append(settle_pass(container, recipe, run.accumulated_breaks))
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
