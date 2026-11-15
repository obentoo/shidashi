"""Soname waivers in the factory's ``check-binpkgs``.

The refusal of the weekly release 2026.11.13, word for word::

    sys-kernel/gentoo-kernel-bin-7.3.3 (build 1): needs libcrypto.so.3 — offered libcrypto.so.4

Same harness as ``tests/test_factory_recheck_soname.py``, imported from it; the waivers
come from a copy of ``variants/`` (``tests/_soname_waiver_variants.py``).
"""

from pathlib import Path

import pytest

import tests.test_factory_recheck_soname as recheck
from shidashi import audit, phases
from tests._soname_waiver_variants import (
    CURL_LINE,
    CURL_OPENSSL3,
    ELFUTILS,
    KERNEL,
    KERNEL_CPV,
    KERNEL_LINE,
    KERNEL_MIXED,
    LIBELF_LINE,
    OPENSSL,
    P_CURL,
    P_ELFUTILS,
    P_KERNEL,
    P_OPENSSL,
    UNUSED,
    WAIVED,
    InMemoryAuditTrail,
    variants_with_waivers,
    waiver_yaml,
)
from tests.test_factory_recheck_soname import GLIBC, P_GLIBC, _entry, _index, _recipe, _stage

pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

OPENSSL_DEP = ">=dev-libs/openssl-3:0"


def _judged(container: object) -> tuple[dict[str, object], InMemoryAuditTrail]:
    """The check's step data and the audit trail of the run it was made in."""
    trail = InMemoryAuditTrail()
    with audit.recording(trail):
        data = phases.check_binpkgs(container, _recipe(), "gnome")  # type: ignore[arg-type]
    return data, trail


def _refused(container: object) -> tuple[phases.FactoryError, InMemoryAuditTrail]:
    trail = InMemoryAuditTrail()
    with audit.recording(trail), pytest.raises(phases.FactoryError) as err:
        phases.check_binpkgs(container, _recipe(), "gnome")  # type: ignore[arg-type]
    assert err.value.phase == "gnome:binpkgs"
    return err.value, trail


def test_a_waived_finding_does_not_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    index = _index(OPENSSL, KERNEL, GLIBC)
    container = _stage(tmp_path, monkeypatch, index, P_OPENSSL, P_KERNEL, P_GLIBC)
    data, trail = _judged(container)

    assert data == {"index": "judged"}
    # recorded in the run's trail, inside the step that judged: what, why, until when
    assert trail.waiver_events() == [{"soname_waived": [WAIVED], "soname_waivers_unused": []}]


def test_without_a_waiver_the_same_plan_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect itself, so the test above is known to waive something."""
    variants_with_waivers(tmp_path, monkeypatch, None)
    container = _stage(tmp_path, monkeypatch, _index(OPENSSL, KERNEL, GLIBC), P_OPENSSL, P_KERNEL)
    err, trail = _refused(container)

    assert KERNEL_LINE in str(err)
    assert trail.waiver_events() == []  # no waiver in force: nothing to say of them


def test_hostile_the_waived_soname_of_another_package_still_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """curl is still on OpenSSL 3 too, and a rebuild DOES fix curl: only it is named."""
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    index = _index(OPENSSL, KERNEL, CURL_OPENSSL3, GLIBC)
    container = _stage(tmp_path, monkeypatch, index, P_OPENSSL, P_KERNEL, P_CURL, P_GLIBC)
    err, trail = _refused(container)

    assert CURL_LINE in str(err)
    assert KERNEL_CPV not in str(err)
    # the refusal does not lose the record of what was waived beside it
    assert trail.waiver_events() == [{"soname_waived": [WAIVED], "soname_waivers_unused": []}]


def test_a_package_with_a_waived_and_an_unwaived_soname_still_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    index = _index(OPENSSL, ELFUTILS, KERNEL_MIXED, GLIBC)
    container = _stage(tmp_path, monkeypatch, index, P_OPENSSL, P_ELFUTILS, P_KERNEL, P_GLIBC)
    err, _trail = _refused(container)

    assert LIBELF_LINE in str(err)
    assert KERNEL_LINE not in str(err)  # the waived soname is not what must be fixed


@pytest.mark.parametrize(
    "waiver",
    [waiver_yaml(package="sys-kernel/gentoo-kernel"), waiver_yaml(soname="libssl.so.3")],
)
def test_hostile_a_waiver_of_another_package_or_soname_waives_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiver: str
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver)
    container = _stage(tmp_path, monkeypatch, _index(OPENSSL, KERNEL, GLIBC), P_OPENSSL, P_KERNEL)
    err, trail = _refused(container)

    assert KERNEL_LINE in str(err)
    (event,) = trail.waiver_events()
    assert event["soname_waived"] == [] and len(event["soname_waivers_unused"]) == 1


def test_a_waiver_that_matched_nothing_is_recorded_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This image installs no kernel: the waiver is in force and covers nothing."""
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    container = _stage(tmp_path, monkeypatch, _index(OPENSSL, KERNEL, GLIBC), P_OPENSSL, P_GLIBC)
    data, trail = _judged(container)

    assert data == {"index": "judged"}
    assert trail.waiver_events() == [{"soname_waived": [], "soname_waivers_unused": [UNUSED]}]


def test_the_subslot_rule_still_fails_a_package_whose_soname_is_waived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel's binpkg also records ``openssl:0/3=`` while the tree ships ``0/4``."""
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    kernel = _entry(
        KERNEL_CPV,
        1,
        slot="7.3.3",
        rdepend=f"{OPENSSL_DEP}/3=",
        requires="x86_64: libc.so.6 libcrypto.so.3",
    )
    monkeypatch.setattr(
        recheck, "TREE", {**recheck.TREE, OPENSSL_DEP: ["dev-libs/openssl-4.0.1", "0/4"]}
    )
    container = _stage(tmp_path, monkeypatch, _index(OPENSSL, kernel, GLIBC), P_OPENSSL, P_KERNEL)
    err, _trail = _refused(container)

    assert f"{KERNEL_CPV} (build 1): {OPENSSL_DEP}/3= — dev-libs/openssl 0/3 → 0/4" in str(err)
    assert KERNEL_LINE not in str(err)


def test_a_waivers_file_that_does_not_validate_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = waiver_yaml().replace("libcrypto.so.3", "libcrypto.so.*")
    root = variants_with_waivers(tmp_path, monkeypatch, broken)
    container = _stage(tmp_path, monkeypatch, _index(OPENSSL, KERNEL, GLIBC), P_OPENSSL, P_KERNEL)
    err, _trail = _refused(container)

    assert str(root / "soname-waivers.yaml") in str(err) and "versioned soname" in str(err)
