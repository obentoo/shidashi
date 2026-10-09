"""The soname pass against an installed provider, the base's mode, the providers-first
exclusions and stale siblings (story 019, task 3.4; the tech review's four shapes).

Same harness as ``tests/test_phases_soname.py``, imported from it.
"""

from pathlib import Path

import pytest

from shidashi import phases
from shidashi.recipe import Phase
from tests.test_phases_soname import (
    CURL,
    GEN,
    ICU,
    P_SIMDUTF_BIN,
    P_SIMDUTF_EBUILD,
    P_VTE_1,
    SIMDUTF,
    SIMDUTF_BIN,
    SIMDUTF_INSTALLED,
    SIMDUTF_OLD,
    TREE,
    VTE_STALE,
    _binhost,
    _entry,
    _Factory,
    _quarantined,
    _recipe,
    _run,
    _targets,
    _today,
    _vdb_install,
)

#: the provider vte was built against, installed in the stage's rootfs (slot 0)
OLD_INSTALLED = ("dev-cpp/simdutf-9.0.0", "x86_64: libsimdutf.so.34")


def _with_old_simdutf(container: _Factory) -> None:
    _vdb_install(container.rootfs, OLD_INSTALLED[0], provides=OLD_INSTALLED[1])
    (container.rootfs / "var/db/pkg" / OLD_INSTALLED[0] / "SLOT").write_text("0/34\n")


def test_planned_slots_maps_each_line_to_the_cpv_it_merges() -> None:
    plan = "\n".join(
        [
            "[binary     U  ] dev-cpp/simdutf-9.2.1-1:0/34::bentoo [9.0.0:0/34::bentoo] 0 KiB",
            "[ebuild   R    ] x11-libs/vte-0.84.1:2.91::gentoo  USE=-vala 0 KiB",
            "[binary  N     ] dev-cpp/fast_float-8.0.2-1::gentoo  USE=-test 0 KiB",
            "Total: 3 packages",
        ]
    )
    assert phases.planned_slots(plan) == {
        ("dev-cpp/simdutf", "0"): "dev-cpp/simdutf-9.2.1",
        ("x11-libs/vte", "2.91"): "x11-libs/vte-0.84.1",
        ("dev-cpp/fast_float", "0"): "dev-cpp/fast_float-8.0.2",
    }


def test_an_installed_provider_the_plan_replaces_no_longer_offers_its_soname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """simdutf-9.0.0 (.so.34) installed, the plan upgrades it to the 9.2.1 binpkg (.so.36):
    the old .so.34 must not satisfy vte -- the 2026-10-08 shape on a stage that has it."""
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, VTE_STALE, CURL)
    plan = ["[binary     U  ] dev-cpp/simdutf-9.2.1-1::bentoo [9.0.0::bentoo] 0 KiB", P_VTE_1]
    container = _Factory(tmp_path, pkgdir, plan=plan, installs=SIMDUTF_INSTALLED)
    _with_old_simdutf(container)
    step = _run(tmp_path, container)
    (entry,) = step["soname"]
    assert entry["cpv"] == "x11-libs/vte-0.84.1" and entry["needs"] == "libsimdutf.so.34"
    (stage,) = container.stage_emerges()
    assert "--usepkg-exclude=x11-libs/vte" in stage


def test_an_installed_provider_upgraded_from_its_ebuild_is_compiled_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_OLD, VTE_STALE, CURL)
    plan = ["[ebuild     U  ] dev-cpp/simdutf-9.2.1::bentoo [9.0.0::bentoo] 0 KiB", P_VTE_1]
    container = _Factory(tmp_path, pkgdir, plan=plan, installs=SIMDUTF_INSTALLED)
    _with_old_simdutf(container)
    step = _run(tmp_path, container)
    (oneshot,) = container.oneshots()
    assert _targets(oneshot) == ["=dev-cpp/simdutf-9.2.1"]
    (entry,) = step["soname"]
    assert entry["needs"] == "libsimdutf.so.34" and entry["offered"] == ["libsimdutf.so.36"]


def test_a_stale_sibling_of_a_stale_planned_instance_is_judged_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builds 1 and 3 of vte both need .so.34; the plan picks build 1. Moving build 1
    aside alone would let the emerge merge build 3: the version must be compiled."""
    vte3 = VTE_STALE.replace("BUILD_ID: 1", "BUILD_ID: 3").replace("-1.gpkg", "-3.gpkg")
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, VTE_STALE, vte3, CURL)
    container = _Factory(
        tmp_path, pkgdir, plan=[P_SIMDUTF_BIN, P_VTE_1], installs=SIMDUTF_INSTALLED
    )
    step = _run(tmp_path, container)
    assert sorted(e["build_id"] for e in step["soname"]) == [1, 3]
    (stage,) = container.stage_emerges()
    assert "--usepkg-exclude=x11-libs/vte" in stage
    assert sorted(p.name for p in _quarantined(tmp_path)) == [
        "vte-0.84.1-1.gpkg.tar",
        "vte-0.84.1-3.gpkg.tar",
    ]
    assert f"/quarantine/binpkgs/v3/{GEN}/" in str(_quarantined(tmp_path)[0])


def test_the_providers_first_emerge_keeps_the_subslot_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nodejs is excluded by the subslot rule: the oneshot must not merge its binpkg."""
    nodejs = _entry("net-libs/nodejs-26.9.0", 1, rdepend=f"{ICU}/77=")
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_OLD, VTE_STALE, nodejs, CURL)
    tree = {**TREE, ICU: ["dev-libs/icu-78.1", "0/78"]}
    container = _Factory(
        tmp_path,
        pkgdir,
        plan=[P_SIMDUTF_EBUILD, P_VTE_1],
        tree=tree,
        installs=SIMDUTF_INSTALLED,
    )
    _run(tmp_path, container)
    (oneshot,) = container.oneshots()
    assert "--usepkg-exclude=net-libs/nodejs" in oneshot
    assert oneshot.index("--usepkg-exclude=net-libs/nodejs") < oneshot.index("--oneshot")


def test_the_base_is_planned_in_the_mode_it_emerges_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, CURL)
    container = _Factory(tmp_path, pkgdir, plan=[P_SIMDUTF_BIN])
    base = Phase(name="base", stage="base", sets=(), emptytree=True)
    from shidashi import audit

    with audit.run(tmp_path / "runs", command="factory", argv=[]):
        phases.run_phase(container, _recipe(), base, emptytree=True)  # type: ignore[arg-type]
    (pretend,) = container.pretends()
    assert "--emptytree" in pretend
    assert container.stage_emerges() == [phases.phase_emerge_argv(base, _recipe(), emptytree=True)]
    assert _today() != container.stage_emerges()[0]
    assert SIMDUTF in TREE
