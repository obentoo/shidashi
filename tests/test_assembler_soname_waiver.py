"""Soname waivers in the assemble: a waived finding does not stop the install.

The weekly release 2026.11.13: every image installs ``gentoo-kernel-bin-7.3.3``, whose
prebuilt ``certs/extract-cert`` needs ``libcrypto.so.3`` beside the plan's OpenSSL 4.

Same harness as ``tests/test_assembler_soname.py``, imported from it; the waivers come
from a copy of ``variants/`` (``tests/_soname_waiver_variants.py``).
"""

from pathlib import Path

import pytest

from shidashi import audit
from shidashi.assembler import AssemblerError
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
from tests.test_assembler import (  # noqa: F401 -- autouse fixtures of the module
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
)
from tests.test_assembler_soname import (
    GLIBC,
    P_GLIBC,
    _assemble,
    _emerges,
    _index,
    _Installed,
    _installs_as_today,
    _pretend,
    _wire,
)

INDEX = _index(OPENSSL, KERNEL, GLIBC)


def _refused(tmp: Path, runs: list[list[str]]) -> tuple[str, InMemoryAuditTrail]:
    """The refusal's message; nothing was installed."""
    trail = InMemoryAuditTrail()
    with audit.recording(trail), pytest.raises(AssemblerError) as err:
        _assemble(tmp)
    assert _emerges(runs) == [_pretend()]
    return str(err.value), trail


def test_a_waived_finding_does_not_stop_the_assemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    runs = _wire(tmp_path, monkeypatch, INDEX, P_OPENSSL, P_KERNEL, P_GLIBC)
    _installs_as_today(tmp_path, runs)


def test_a_waived_finding_is_recorded_in_the_run_s_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    _wire(tmp_path, monkeypatch, INDEX, P_OPENSSL, P_KERNEL, P_GLIBC)
    trail = InMemoryAuditTrail()
    with audit.recording(trail), pytest.raises(_Installed):
        _assemble(tmp_path)

    assert trail.waiver_events() == [{"soname_waived": [WAIVED], "soname_waivers_unused": []}]


def test_without_a_waiver_the_same_plan_stops_the_assemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect itself, so the tests above are known to waive something."""
    variants_with_waivers(tmp_path, monkeypatch, None)
    runs = _wire(tmp_path, monkeypatch, INDEX, P_OPENSSL, P_KERNEL, P_GLIBC)
    message, trail = _refused(tmp_path, runs)

    assert KERNEL_LINE in message
    assert trail.waiver_events() == []


def test_hostile_the_waived_soname_of_another_package_still_stops_the_assemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    index = _index(OPENSSL, KERNEL, CURL_OPENSSL3, GLIBC)
    runs = _wire(tmp_path, monkeypatch, index, P_OPENSSL, P_KERNEL, P_CURL, P_GLIBC)
    message, trail = _refused(tmp_path, runs)

    assert CURL_LINE in message
    assert KERNEL_CPV not in message
    assert trail.waiver_events() == [{"soname_waived": [WAIVED], "soname_waivers_unused": []}]


def test_a_package_with_a_waived_and_an_unwaived_soname_still_stops_the_assemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    index = _index(OPENSSL, ELFUTILS, KERNEL_MIXED, GLIBC)
    runs = _wire(tmp_path, monkeypatch, index, P_OPENSSL, P_ELFUTILS, P_KERNEL, P_GLIBC)
    message, _trail = _refused(tmp_path, runs)

    assert LIBELF_LINE in message
    assert KERNEL_LINE not in message


@pytest.mark.parametrize(
    "waiver",
    [waiver_yaml(package="sys-kernel/gentoo-kernel"), waiver_yaml(soname="libssl.so.3")],
)
def test_hostile_a_waiver_of_another_package_or_soname_waives_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiver: str
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver)
    runs = _wire(tmp_path, monkeypatch, INDEX, P_OPENSSL, P_KERNEL, P_GLIBC)
    message, trail = _refused(tmp_path, runs)

    assert KERNEL_LINE in message
    (event,) = trail.waiver_events()
    assert event["soname_waived"] == [] and len(event["soname_waivers_unused"]) == 1


def test_a_waiver_that_matched_nothing_is_recorded_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants_with_waivers(tmp_path, monkeypatch, waiver_yaml())
    _wire(tmp_path, monkeypatch, INDEX, P_OPENSSL, P_GLIBC)
    trail = InMemoryAuditTrail()
    with audit.recording(trail), pytest.raises(_Installed):
        _assemble(tmp_path)

    assert trail.waiver_events() == [{"soname_waived": [], "soname_waivers_unused": [UNUSED]}]


def test_a_waivers_file_that_does_not_validate_stops_the_assemble_before_any_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = waiver_yaml() + waiver_yaml()  # the same package and soname twice
    root = variants_with_waivers(tmp_path, monkeypatch, broken)
    runs = _wire(tmp_path, monkeypatch, INDEX, P_OPENSSL, P_KERNEL, P_GLIBC)
    message, _trail = _refused(tmp_path, runs)

    assert str(root / "soname-waivers.yaml") in message and "repeated" in message
