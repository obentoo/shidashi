"""Soname waivers: the file, its strict loader and the pure matcher.

The case behind the first entry (weekly release 2026.11.13, read on 2026-11-15):
``sys-kernel/gentoo-kernel-bin-7.3.3`` ships ``certs/extract-cert`` prebuilt against
OpenSSL 3 while the pinned tree has OpenSSL 4, so the soname rule calls it stale after
every rebuild. The three build paths are in ``tests/test_*_soname_waiver.py``.
"""

from pathlib import Path

import pydantic
import pytest

from shidashi import binpkgs, config
from shidashi.soname_waivers import (
    SONAME_WAIVERS_FILE,
    SonameWaiver,
    SonameWaiverError,
    apply_soname_waivers,
    load_soname_waivers,
)

KERNEL_ENTRY = {
    "package": "sys-kernel/gentoo-kernel-bin",
    "soname": "libcrypto.so.3",
    "why": "ships certs/extract-cert prebuilt against OpenSSL 3",
    "found": "weekly release 2026.11.13",
    "until": "Gentoo ships one linked to libcrypto.so.4",
}
KERNEL_WAIVER = SonameWaiver(**KERNEL_ENTRY)


def _yaml(*entries: dict[str, str]) -> str:
    return "".join(
        "- " + "\n  ".join(f"{key}: {value!r}" for key, value in entry.items()) + "\n"
        for entry in entries
    )


def _variants(tmp: Path, text: str) -> Path:
    (tmp / SONAME_WAIVERS_FILE).write_text(text, encoding="utf-8")
    return tmp


def _refusal(tmp: Path, text: str) -> str:
    """What the loader says of ``text``, without the file's path (whose test directory
    would match any field name asked of it)."""
    with pytest.raises(SonameWaiverError) as err:
        load_soname_waivers(_variants(tmp, text))
    prefix = f"{tmp / SONAME_WAIVERS_FILE}: "
    assert str(err.value).startswith(prefix)
    return str(err.value)[len(prefix) :]


def _finding(cpv: str, needs: str, offered: str, *, build: int = 1) -> binpkgs.SonameStale:
    category, name = cpv.split("/")
    instance = binpkgs.Instance(
        cpv=cpv, build_id=build, path=f"{category}/{name}/{name}-{build}.gpkg.tar"
    )
    return binpkgs.SonameStale(
        instance=instance, needs=binpkgs.Soname("x86_64", needs), offered=(offered,)
    )


KERNEL_FINDING = _finding("sys-kernel/gentoo-kernel-bin-7.3.3", "libcrypto.so.3", "libcrypto.so.4")
VTE_FINDING = _finding("x11-libs/vte-0.84.1", "libsimdutf.so.34", "libsimdutf.so.36")


# --- the loader ---------------------------------------------------------------------------


def test_a_valid_file_loads_every_entry_in_order(tmp_path: Path) -> None:
    other = {**KERNEL_ENTRY, "package": "app-misc/prebuilt", "soname": "libfoo-2.0.so.1"}
    waivers = load_soname_waivers(_variants(tmp_path, _yaml(KERNEL_ENTRY, other)))
    assert waivers == (KERNEL_WAIVER, SonameWaiver(**other))
    assert waivers[0].package == "sys-kernel/gentoo-kernel-bin"
    assert waivers[0].soname == "libcrypto.so.3"
    assert waivers[0].until == "Gentoo ships one linked to libcrypto.so.4"


def test_a_missing_file_means_no_waivers(tmp_path: Path) -> None:
    assert load_soname_waivers(tmp_path) == ()


def test_a_file_with_only_its_header_means_no_waivers(tmp_path: Path) -> None:
    assert load_soname_waivers(_variants(tmp_path, "# no entry left\n")) == ()


def test_block_text_is_kept_without_its_trailing_newline(tmp_path: Path) -> None:
    text = (
        "- package: sys-kernel/gentoo-kernel-bin\n"
        "  soname: libcrypto.so.3\n"
        "  why: |\n    first line\n    second line\n"
        "  found: 2026-11-15\n"  # YAML reads a bare date as a date; the field is text
        "  until: |\n    a fixed kernel\n"
    )
    (waiver,) = load_soname_waivers(_variants(tmp_path, text))
    assert waiver.why == "first line\nsecond line"
    assert waiver.found == "2026-11-15"
    assert waiver.until == "a fixed kernel"


def test_an_entry_is_frozen() -> None:
    with pytest.raises(pydantic.ValidationError):
        KERNEL_WAIVER.soname = "libcrypto.so.4"


@pytest.mark.parametrize("missing", ["package", "soname", "why", "found", "until"])
def test_every_field_is_required(tmp_path: Path, missing: str) -> None:
    entry = {key: value for key, value in KERNEL_ENTRY.items() if key != missing}
    said = _refusal(tmp_path, _yaml(entry))
    assert f"\n{missing}\n" in said and "Field required" in said


@pytest.mark.parametrize("blank", ["why", "found", "until"])
def test_a_blank_reason_is_refused(tmp_path: Path, blank: str) -> None:
    said = _refusal(tmp_path, _yaml({**KERNEL_ENTRY, blank: "  "}))
    assert f"\n{blank}\n" in said and "must say" in said


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    said = _refusal(tmp_path, _yaml({**KERNEL_ENTRY, "file": "extract-cert"}))
    assert "\nfile\n" in said and "Extra inputs are not permitted" in said


@pytest.mark.parametrize(
    "package",
    [
        "sys-kernel/gentoo-kernel-bin-7.3.3",  # a version
        "sys-kernel/gentoo-kernel-bin-7.3.3-r1",
        "sys-kernel/gentoo-kernel-bin-7",  # a version with no dot: only its shape tells
        "sys-kernel/gentoo-kernel-bin-7_p20261113-r2",
        "=sys-kernel/gentoo-kernel-bin",  # an operator
        "sys-kernel/*",  # a wildcard
        "*/gentoo-kernel-bin",
        "sys-kernel/gentoo-kernel-bin:0",  # a slot
        "sys-kernel/gentoo-kernel-bin::gentoo",
        "gentoo-kernel-bin",  # no category
        "sys-kernel/",
        "sys-kernel/gentoo kernel",
        "",
    ],
)
def test_a_package_that_is_not_an_exact_category_and_name_is_refused(
    tmp_path: Path, package: str
) -> None:
    said = _refusal(tmp_path, _yaml({**KERNEL_ENTRY, "package": package}))
    assert "\npackage\n" in said and "category/name" in said


@pytest.mark.parametrize(
    "soname",
    [
        "libcrypto.so",  # no version: such a soname is never stale, only unresolved
        "libcrypto",
        "libcrypto.so.*",
        "libcrypto.so.3 libssl.so.3",
        "x86_64: libcrypto.so.3",
        "",
    ],
)
def test_a_soname_that_is_not_one_exact_versioned_soname_is_refused(
    tmp_path: Path, soname: str
) -> None:
    said = _refusal(tmp_path, _yaml({**KERNEL_ENTRY, "soname": soname}))
    assert "\nsoname\n" in said and "versioned soname" in said


def test_the_same_package_and_soname_twice_is_refused(tmp_path: Path) -> None:
    again = {**KERNEL_ENTRY, "why": "another reason"}
    said = _refusal(tmp_path, _yaml(KERNEL_ENTRY, again))
    assert "repeated" in said
    assert "sys-kernel/gentoo-kernel-bin" in said and "libcrypto.so.3" in said


def test_one_package_may_waive_two_sonames(tmp_path: Path) -> None:
    second = {**KERNEL_ENTRY, "soname": "libssl.so.3"}
    assert len(load_soname_waivers(_variants(tmp_path, _yaml(KERNEL_ENTRY, second)))) == 2


@pytest.mark.parametrize(
    "text",
    [
        "package: sys-kernel/gentoo-kernel-bin\nsoname: libcrypto.so.3\n",  # a mapping
        "- sys-kernel/gentoo-kernel-bin\n",  # an entry that is not a mapping
        "- [sys-kernel/gentoo-kernel-bin, libcrypto.so.3]\n",
        "just text\n",
        "- package: [unclosed\n",  # not YAML
        "{}\n",
        "0\n",
    ],
)
def test_a_file_of_another_shape_is_refused(tmp_path: Path, text: str) -> None:
    assert _refusal(tmp_path, text)  # refused, and the error names the file


def test_an_unreadable_file_is_refused_not_taken_as_absent(tmp_path: Path) -> None:
    (tmp_path / SONAME_WAIVERS_FILE).write_bytes(b"- package: \xff\xfe\n")
    with pytest.raises(SonameWaiverError):
        load_soname_waivers(tmp_path)


# --- the matcher --------------------------------------------------------------------------


def test_a_finding_of_the_waived_package_and_soname_is_not_stale() -> None:
    verdict = apply_soname_waivers([KERNEL_FINDING], [KERNEL_WAIVER])
    assert verdict.stale == ()
    assert verdict.waived == ((KERNEL_FINDING, KERNEL_WAIVER),)
    assert verdict.unused == ()


def test_without_waivers_every_finding_stays_stale() -> None:
    verdict = apply_soname_waivers([KERNEL_FINDING, VTE_FINDING], [])
    assert verdict.stale == (KERNEL_FINDING, VTE_FINDING)
    assert verdict.waived == () and verdict.unused == ()


def test_hostile_the_waived_soname_of_another_package_stays_stale() -> None:
    """Matching on the soname alone would waive every binpkg still on OpenSSL 3."""
    curl = _finding("net-misc/curl-8.16.0", "libcrypto.so.3", "libcrypto.so.4")
    verdict = apply_soname_waivers([curl], [KERNEL_WAIVER])
    assert verdict.stale == (curl,)
    assert verdict.unused == (KERNEL_WAIVER,)


def test_hostile_another_soname_of_the_waived_package_stays_stale() -> None:
    """Matching on the package alone would waive whatever the kernel comes to need."""
    libelf = _finding("sys-kernel/gentoo-kernel-bin-7.3.3", "libelf.so.1", "libelf.so.2")
    verdict = apply_soname_waivers([libelf], [KERNEL_WAIVER])
    assert verdict.stale == (libelf,)
    assert verdict.unused == (KERNEL_WAIVER,)


def test_hostile_a_package_whose_name_starts_with_the_waived_one_stays_stale() -> None:
    longer = _finding("sys-kernel/gentoo-kernel-bin-extras-1.0", "libcrypto.so.3", "libcrypto.so.4")
    assert apply_soname_waivers([longer], [KERNEL_WAIVER]).stale == (longer,)


def test_hostile_another_version_of_the_waived_soname_stays_stale() -> None:
    older = _finding("sys-kernel/gentoo-kernel-bin-7.3.3", "libcrypto.so.1.1", "libcrypto.so.4")
    assert apply_soname_waivers([older], [KERNEL_WAIVER]).stale == (older,)


def test_every_version_and_build_of_the_package_is_waived() -> None:
    """The entry carries no version: next week's kernel has the same prebuilt binary."""
    found = [
        _finding("sys-kernel/gentoo-kernel-bin-7.3.3", "libcrypto.so.3", "libcrypto.so.4", build=2),
        _finding("sys-kernel/gentoo-kernel-bin-7.3.4-r1", "libcrypto.so.3", "libcrypto.so.4"),
    ]
    verdict = apply_soname_waivers(found, [KERNEL_WAIVER])
    assert verdict.stale == ()
    assert [finding for finding, _waiver in verdict.waived] == found


def test_an_instance_with_a_waived_and_an_unwaived_soname_stays_stale() -> None:
    libelf = _finding("sys-kernel/gentoo-kernel-bin-7.3.3", "libelf.so.1", "libelf.so.2")
    verdict = apply_soname_waivers([KERNEL_FINDING, libelf], [KERNEL_WAIVER])
    assert verdict.stale == (libelf,)
    assert verdict.waived == ((KERNEL_FINDING, KERNEL_WAIVER),)


def test_the_order_of_the_findings_is_kept() -> None:
    last = _finding("x11-libs/zzz-1", "libsimdutf.so.34", "libsimdutf.so.36")
    verdict = apply_soname_waivers([VTE_FINDING, KERNEL_FINDING, last], [KERNEL_WAIVER])
    assert verdict.stale == (VTE_FINDING, last)


def test_a_waiver_that_matched_nothing_is_reported_unused() -> None:
    other = SonameWaiver(**{**KERNEL_ENTRY, "package": "app-misc/prebuilt"})
    verdict = apply_soname_waivers([KERNEL_FINDING, VTE_FINDING], [other, KERNEL_WAIVER])
    assert verdict.unused == (other,)
    assert verdict.stale == (VTE_FINDING,)


def test_the_audit_fields_name_each_waived_finding_and_each_unused_waiver() -> None:
    other = SonameWaiver(**{**KERNEL_ENTRY, "package": "app-misc/prebuilt", "until": "never"})
    verdict = apply_soname_waivers([KERNEL_FINDING, VTE_FINDING], [KERNEL_WAIVER, other])
    assert verdict.audit_fields() == {
        "soname_waived": [
            {
                "cpv": "sys-kernel/gentoo-kernel-bin-7.3.3",
                "build_id": 1,
                "needs": "libcrypto.so.3",
                "offered": ["libcrypto.so.4"],
                "why": KERNEL_ENTRY["why"],
                "until": KERNEL_ENTRY["until"],
            }
        ],
        "soname_waivers_unused": [
            {"package": "app-misc/prebuilt", "soname": "libcrypto.so.3", "until": "never"}
        ],
    }


def test_no_waiver_and_no_finding_records_two_empty_lists() -> None:
    assert apply_soname_waivers([], []).audit_fields() == {
        "soname_waived": [],
        "soname_waivers_unused": [],
    }


# --- the shipped file ---------------------------------------------------------------------


def test_the_shipped_file_exists_and_loads() -> None:
    """Kept with its header even when its last entry is deleted."""
    assert (config.variants_dir() / SONAME_WAIVERS_FILE).is_file()
    for waiver in load_soname_waivers(config.variants_dir()):
        assert waiver.why and waiver.found and waiver.until


def test_the_shipped_file_waives_the_finding_of_the_2026_11_13_release() -> None:
    """``sys-kernel/gentoo-kernel-bin-7.3.3 (build 1): needs libcrypto.so.3 — offered
    libcrypto.so.4``, word for word. Delete this test WITH the entry, once Gentoo ships
    a gentoo-kernel-bin whose ``certs/extract-cert`` links ``libcrypto.so.4``."""
    verdict = apply_soname_waivers([KERNEL_FINDING], load_soname_waivers(config.variants_dir()))
    assert verdict.stale == ()
    ((finding, waiver),) = verdict.waived
    assert finding is KERNEL_FINDING
    assert waiver.package == "sys-kernel/gentoo-kernel-bin" and waiver.soname == "libcrypto.so.3"
