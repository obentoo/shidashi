"""Soname waivers in the factory's ``stale-binpkgs`` step.

The weekly release 2026.11.13: ``gentoo-kernel-bin-7.3.3`` needs ``libcrypto.so.3``
for its prebuilt ``certs/extract-cert`` while the stage installs OpenSSL 4. Without a
waiver the step excludes the kernel's binpkg, the stage "compiles" it (a -bin package:
the same prebuilt files again) and the new binpkg is stale again -- for ever.

Same harness as ``tests/test_phases_soname.py``, imported from it; the waivers come
from a copy of ``variants/`` (``tests/_soname_waiver_variants.py``).
"""

from pathlib import Path

import pytest

from shidashi import phases
from tests._soname_waiver_variants import (
    ELFUTILS,
    KERNEL,
    KERNEL_CPV,
    KERNEL_FIXED,
    KERNEL_MIXED,
    KERNEL_PACKAGE,
    KERNEL_SIBLING,
    OPENSSL,
    P_ELFUTILS,
    P_KERNEL,
    P_OPENSSL,
    UNUSED,
    WAIVED,
    variants_with_waivers,
    waiver_yaml,
)
from tests.test_phases_soname import (
    GNOME,
    TREE,
    _binhost,
    _entry,
    _Factory,
    _quarantined,
    _recipe,
    _run,
    _today,
)

#: the slot atom the step excludes; the tree picks the release's kernel in it
KERNEL_SLOT = f"{KERNEL_PACKAGE}:7.3.3"
EXCLUDE_KERNEL = f"--usepkg-exclude={KERNEL_SLOT}"
PICKS = {**TREE, KERNEL_SLOT: [KERNEL_CPV, "7.3.3"]}
OPENSSL_DEP = ">=dev-libs/openssl-3:0"


def test_a_waived_finding_excludes_and_quarantines_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, KERNEL)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    assert container.stage_emerges() == [_today()]  # the kernel merges from its binpkg
    assert container.oneshots() == []
    assert _quarantined(tmp_path) == []
    assert container.tools("emaint") == []
    assert step["soname"] == [] and step["excluded"] == [] and step["quarantined"] == []
    # recorded beside the soname findings: what was waived, why, and until when
    assert step["soname_waived"] == [WAIVED]
    assert step["soname_waivers_unused"] == []


def test_without_a_waiver_the_same_plan_excludes_the_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect itself, so the test above is known to waive something."""
    variants_with_waivers(tmp_path, monkeypatch, None)
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, KERNEL)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    (stage,) = container.stage_emerges()
    assert EXCLUDE_KERNEL in stage
    (entry,) = step["soname"]
    assert entry["cpv"] == KERNEL_CPV and entry["needs"] == "libcrypto.so.3"
    assert step["excluded"] == [KERNEL_SLOT]
    assert step["soname_waived"] == [] and step["soname_waivers_unused"] == []


@pytest.mark.parametrize("waived", [True, False])
def test_a_waived_instance_beside_a_fresh_one_is_not_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waived: bool
) -> None:
    """Build 1 (the prebuilt extract-cert) is planned and build 2 is fresh: unwaived,
    build 1 moves to the quarantine before the emerge; waived, it stays where it is."""
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml() if waived else None)
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, KERNEL, KERNEL_FIXED)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    stale_file = pkgdir / "sys-kernel/gentoo-kernel-bin/gentoo-kernel-bin-7.3.3-1.gpkg.tar"
    assert container.stage_emerges() == [_today()]  # never excluded: a fresh build exists
    if waived:
        assert _quarantined(tmp_path) == [] and stale_file.is_file()
        assert step["soname_waived"] == [WAIVED] and step["quarantined"] == []
    else:
        assert [p.name for p in _quarantined(tmp_path)] == [stale_file.name]
        assert not stale_file.exists() and step["soname_waived"] == []


def test_a_package_with_a_waived_and_an_unwaived_soname_stays_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, ELFUTILS, KERNEL_MIXED)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_ELFUTILS, P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    (stage,) = container.stage_emerges()
    assert EXCLUDE_KERNEL in stage
    assert step["excluded"] == [KERNEL_SLOT]
    # the unwaived soname is the finding; the waived one is recorded as waived
    (entry,) = step["soname"]
    assert entry["cpv"] == KERNEL_CPV and entry["needs"] == "libelf.so.1"
    assert step["soname_waived"] == [WAIVED]


@pytest.mark.parametrize(
    "waiver",
    [
        waiver_yaml(package="sys-kernel/gentoo-kernel"),  # the source kernel, not the -bin
        waiver_yaml(package="net-misc/curl"),
        waiver_yaml(soname="libssl.so.3"),
        waiver_yaml(soname="libcrypto.so.1.1"),
    ],
)
def test_hostile_a_waiver_of_another_package_or_soname_waives_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiver: str
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver)
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, KERNEL)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    (stage,) = container.stage_emerges()
    assert EXCLUDE_KERNEL in stage
    assert step["excluded"] == [KERNEL_SLOT]
    assert step["soname_waived"] == []
    assert len(step["soname_waivers_unused"]) == 1  # the dead waiver is in the trail


def test_a_waiver_that_matched_nothing_is_recorded_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit condition, met: the kernel's binpkg needs ``libcrypto.so.4`` now."""
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    fixed = KERNEL_FIXED.replace("BUILD_ID: 2", "BUILD_ID: 1").replace("-2.gpkg", "-1.gpkg")
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, fixed)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    assert container.stage_emerges() == [_today()]
    assert step["soname"] == [] and step["soname_waived"] == []
    assert step["soname_waivers_unused"] == [UNUSED]


def test_hostile_the_siblings_of_a_waived_instance_are_not_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build 3 is stale by ``libelf`` but not planned: it is judged only when the planned
    build goes away. Waived, build 1 stays, so build 3 is none of this plan's business;
    unwaived (the control below) both are stale and the kernel is excluded."""
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, ELFUTILS, KERNEL, KERNEL_SIBLING)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_ELFUTILS, P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    assert container.stage_emerges() == [_today()]
    assert _quarantined(tmp_path) == []
    assert step["soname"] == [] and step["excluded"] == []
    assert step["soname_waived"] == [WAIVED]


def test_unwaived_the_stale_sibling_is_judged_with_the_planned_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, None)
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, ELFUTILS, KERNEL, KERNEL_SIBLING)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_ELFUTILS, P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    assert sorted(e["build_id"] for e in step["soname"]) == [1, 3]
    assert step["excluded"] == [KERNEL_SLOT]


def test_an_unresolved_soname_is_recorded_as_always_and_is_not_a_waived_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing offers ``libcrypto.so`` at any version: unresolved, never stale, so the
    waiver has nothing to waive and the record of the unresolved soname is untouched."""
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    pkgdir = _binhost(tmp_path, monkeypatch, KERNEL)
    container = _Factory(tmp_path, pkgdir, plan=[P_KERNEL], tree=PICKS)
    step = _run(tmp_path, container)

    assert step["unresolved_sonames"] == [
        {"cpv": KERNEL_CPV, "build_id": 1, "needs": "libcrypto.so.3"}
    ]
    assert step["soname"] == [] and step["soname_waived"] == []
    assert step["soname_waivers_unused"] == [UNUSED]


def test_the_subslot_rule_still_condemns_a_package_whose_soname_is_waived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel's binpkg also records ``openssl:0/3=`` while the tree ships ``0/4``:
    stale by the subslot rule, which no waiver touches."""
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    kernel = _entry(
        KERNEL_CPV,
        1,
        slot="7.3.3",
        rdepend=f"{OPENSSL_DEP}/3=",
        requires="x86_64: libc.so.6 libcrypto.so.3",
    )
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, kernel)
    tree = {**PICKS, OPENSSL_DEP: ["dev-libs/openssl-4.0.1", "0/4"]}
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_KERNEL], tree=tree)
    step = _run(tmp_path, container)

    (stage,) = container.stage_emerges()
    assert EXCLUDE_KERNEL in stage
    (entry,) = step["stale"]
    assert entry["cpv"] == KERNEL_CPV and entry["built"] == "0/3" and entry["tree"] == "0/4"
    assert step["excluded"] == [KERNEL_SLOT]
    assert step["soname"] == [] and step["soname_waived"] == [WAIVED]


def test_a_waivers_file_that_does_not_validate_fails_the_step_before_it_moves_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = waiver_yaml().replace("  until:", "  untill:")
    root = variants_with_waivers(tmp_path, monkeypatch, broken)
    pkgdir = _binhost(tmp_path, monkeypatch, OPENSSL, KERNEL, KERNEL_FIXED)
    container = _Factory(tmp_path, pkgdir, plan=[P_OPENSSL, P_KERNEL], tree=PICKS)
    with pytest.raises(phases.FactoryError) as err:
        phases.run_phase(container, _recipe(), GNOME, emptytree=False)  # type: ignore[arg-type]

    assert err.value.phase == "gnome:stale-binpkgs"
    assert str(root / "soname-waivers.yaml") in str(err.value) and "untill" in str(err.value)
    assert container.emerges == []
    assert _quarantined(tmp_path) == []
