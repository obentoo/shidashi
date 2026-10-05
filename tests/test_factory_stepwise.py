"""UNIT + INTEGRATION tests of the stepwise shidashi.factory (story 004 group 6).

UNIT (deterministic, non-Gentoo CI):
* 6.1 ``FactoryResult`` gains the defaulted fields ``stopped_at``/``phase_diffs``/
  ``completed_phases`` — story 003's construction (without them) REMAINS valid
  (back-compat) and the ``CheckpointDecision``/``FailureDecision`` enums are pure
  (R8.2);
* 6.2 (unit) ``Factory.build_stepwise`` raises the root guard BEFORE
  any work (``os.geteuid`` monkeypatched) — R8.1; and a stepwise run NEVER
  auto-deletes the rootfs (the teardown rule is "keep" — R1.6).

INTEGRATION (host-gated, DEFERRED Red): real seed-or-restore, per-phase snapshot,
``--reset`` that clears state+rootfs, real persistent Container — SKIPPED in CI.

New symbols imported tolerantly; each unit test goes Red on use
(expected Red 004). ``FactoryError``/``FactoryResult`` already exist from story 003.
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from shidashi import factory
from shidashi.recipe import Phase, ResolvedRecipe
from tests._pending import try_import

Factory: Any = try_import("shidashi.factory", "Factory")
FactoryResult: Any = try_import("shidashi.factory", "FactoryResult")
FactoryError: Any = try_import("shidashi.factory", "FactoryError")
CheckpointDecision: Any = try_import("shidashi.factory", "CheckpointDecision")
FailureDecision: Any = try_import("shidashi.factory", "FailureDecision")
PhaseDiff: Any = try_import("shidashi.state", "PhaseDiff")

_NEEDS_HOST = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST, reason="requires root + systemd-nspawn + a seeded stage3 (Gentoo host)"
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
        sets=(),
        phases=(Phase(name="rebuild"), Phase(name="graphics")),
        portage_layers=("base", "arch/v3", "flavor/minimal", "init/systemd"),
    )


# --- 6.1 FactoryResult back-compat + extended fields -------------------------


def test_factory_result_story003_construction_still_valid() -> None:
    # back-compat: without the new fields story 003's construction works and the
    # new fields take their defaults (R8.2).
    result = FactoryResult(
        pkgdir=Path("/var/cache/shidashi/binpkgs/v3"),
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


# --- 6.1 pure enums ----------------------------------------------------------


def test_checkpoint_and_failure_decisions_are_str_enums() -> None:
    assert CheckpointDecision.CONTINUE == "CONTINUE"
    assert CheckpointDecision.STOP == "STOP"
    assert CheckpointDecision.SHELL == "SHELL"
    assert FailureDecision.RETRY == "RETRY"
    assert FailureDecision.ABORT == "ABORT"


# --- 6.2 (unit) root guard before any work (R8.1) ----------------------------


def test_build_stepwise_non_root_raises_factory_error_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("work done before the root guard")

    monkeypatch.setattr(factory, "fetch_stage3", _boom, raising=False)
    monkeypatch.setattr(factory, "extract_stage3", _boom, raising=False)

    f = Factory(_recipe(), Path("/var/cache/shidashi/binpkgs/v3"))
    # the guard MUST raise a FactoryError mentioning root — NOT an AttributeError
    # for a missing method (which would pass by mistake before the impl existed).
    with pytest.raises(FactoryError) as exc:
        f.build_stepwise(until="rebuild")
    assert "root" in str(exc.value).lower()


def test_build_stepwise_refuses_an_invalid_until_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (2026-10-05): ``--until bootstrap`` was refused only after the
    seed, the generation check and the state file were already done."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("work done before --until was checked")

    monkeypatch.setattr(factory, "load_pointer", _boom)
    monkeypatch.setattr(factory, "pinned_repos", _boom)

    f = Factory(_recipe(), Path("/var/cache/shidashi/binpkgs/v3"))
    with pytest.raises(ValueError, match="valid values: seed, rebuild, graphics"):
        f.build_stepwise(until="bootstrap")


# --- host-gated INTEGRATION (Red DEFERRED to the real privileged host) -------


@_skip_privileged
def test_build_stepwise_until_rebuild_keeps_rootfs_and_state() -> None:
    # R1.1/R1.6/R5.1/R6.1 (int): --until rebuild seeds+runs rebuild, persists
    # state with completed=("rebuild",), writes a per-phase snapshot, does NOT run settle
    # and NEVER deletes the rootfs (teardown rule = keep). Deferred to the host.
    pytest.skip("privileged integration: requires a seeded Gentoo host (deferred Red)")


@_skip_privileged
def test_build_stepwise_reset_clears_state_and_rootfs() -> None:
    # R6.4 (int): --reset discards the persisted state and the rootfs, starts over from scratch.
    pytest.skip("privileged integration: requires a seeded Gentoo host (deferred Red)")
