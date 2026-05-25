"""UNIT + INTEGRAÇÃO de kaji.factory stepwise (story 004 grupo 6).

UNIT (determinista, CI não-Gentoo):
* 6.1 ``FactoryResult`` ganha campos defaultados ``stopped_at``/``phase_diffs``/
  ``completed_phases`` — a construção da story 003 (sem eles) PERMANECE válida
  (back-compat) e os enums ``CheckpointDecision``/``FailureDecision`` são puros
  (R8.2);
* 6.2 (unit) ``Factory.build_stepwise`` levanta a guarda de root ANTES de
  qualquer trabalho (``os.geteuid`` monkeypatched) — R8.1; e um stepwise NUNCA
  auto-deleta o rootfs (a teardown rule é "keep" — R1.6).

INTEGRAÇÃO (host-gated, Red DIFERIDO): seed-or-restore real, snapshot por fase,
``--reset`` que limpa state+rootfs, Container persistente real — PULA em CI.

Símbolos novos importados de forma tolerante; cada teste unit fica Red no uso
(Red esperado 004). ``FactoryError``/``FactoryResult`` já existem da story 003.
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from kaji import factory
from kaji.recipe import Phase, ResolvedRecipe, ResolvedUse
from tests._pending import try_import

Factory: Any = try_import("kaji.factory", "Factory")
FactoryResult: Any = try_import("kaji.factory", "FactoryResult")
FactoryError: Any = try_import("kaji.factory", "FactoryError")
CheckpointDecision: Any = try_import("kaji.factory", "CheckpointDecision")
FailureDecision: Any = try_import("kaji.factory", "FailureDecision")
PhaseDiff: Any = try_import("kaji.state", "PhaseDiff")

_NEEDS_HOST = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST, reason="exige root + systemd-nspawn + stage3 seedado (host Gentoo)"
)


def _recipe(*, flavor: str = "minimal") -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor=flavor,
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        use=ResolvedUse(enabled=(), disabled=()),
        sets=(),
        phases=(Phase(name="rebuild"), Phase(name="graphics")),
        portage_layers=("base", "arch/v3", "flavor/minimal", "init/systemd"),
    )


# --- 6.1 FactoryResult back-compat + extended fields -------------------------


def test_factory_result_story003_construction_still_valid() -> None:
    # back-compat: sem os campos novos a construção da story 003 funciona e os
    # novos campos assumem defaults (R8.2).
    result = FactoryResult(
        pkgdir=Path("/var/cache/kaji/binpkgs/v3"),
        built_atoms=("media-libs/libsdl2-2.30.5",),
        phases=("rebuild",),
        fork_point=None,
        fork_point_reused=False,
        settle_atoms=(),
    )
    assert result.stopped_at is None
    assert result.phase_diffs == ()
    assert result.completed_phases == ()


def test_factory_result_extended_fields_carry_values() -> None:
    diff = PhaseDiff(phase="rebuild", built=("a/b-1",))
    result = FactoryResult(
        pkgdir=Path("/p"),
        built_atoms=(),
        phases=("rebuild",),
        fork_point=None,
        fork_point_reused=False,
        settle_atoms=(),
        stopped_at="rebuild",
        phase_diffs=(diff,),
        completed_phases=("rebuild",),
    )
    assert result.stopped_at == "rebuild"
    assert result.phase_diffs == (diff,)
    assert result.completed_phases == ("rebuild",)


# --- 6.1 enums puros ---------------------------------------------------------


def test_checkpoint_and_failure_decisions_are_str_enums() -> None:
    assert CheckpointDecision.CONTINUE == "CONTINUE"
    assert CheckpointDecision.STOP == "STOP"
    assert CheckpointDecision.SHELL == "SHELL"
    assert FailureDecision.RETRY == "RETRY"
    assert FailureDecision.ABORT == "ABORT"


# --- 6.2 (unit) guarda de root antes de qualquer trabalho (R8.1) -------------


def test_build_stepwise_non_root_raises_factory_error_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("trabalho executado antes da guarda de root")

    monkeypatch.setattr(factory, "fetch_stage3", _boom, raising=False)
    monkeypatch.setattr(factory, "extract_stage3", _boom, raising=False)

    f = Factory(_recipe(), Path("/var/cache/kaji/binpkgs/v3"))
    # a guarda DEVE levantar FactoryError mencionando root — NÃO um AttributeError
    # de método ausente (que passaria por engano antes da impl existir).
    with pytest.raises(FactoryError) as exc:
        f.build_stepwise(until="rebuild")
    assert "root" in str(exc.value).lower()


# --- INTEGRAÇÃO host-gated (Red DIFERIDO ao host privilegiado real) ----------


@_skip_privileged
def test_build_stepwise_until_rebuild_keeps_rootfs_and_state() -> None:
    # R1.1/R1.6/R5.1/R6.1 (int): --until rebuild seeda+roda rebuild, persiste
    # state com completed=("rebuild",), escreve snapshot por fase, NÃO roda settle
    # e NUNCA deleta o rootfs (teardown rule = keep). Diferido ao host.
    pytest.skip("integração privilegiada: requer host Gentoo seedado (Red diferido)")


@_skip_privileged
def test_build_stepwise_reset_clears_state_and_rootfs() -> None:
    # R6.4 (int): --reset descarta o state persistido e o rootfs, recomeça do zero.
    pytest.skip("integração privilegiada: requer host Gentoo seedado (Red diferido)")
