"""Unit tests of ``abi_differences`` (story 016, task 1.2).

``abi_differences(recorded, current)`` names the fields that END a generation,
each with its recorded and current strings; ``{}`` means the same generation.

- gcc counts by its major version (D1, R1.x);
- glibc may go up, never down; empty on exactly one side is a libc change (D2, R2.x);
- binutils is never compared (D3, R3.x);
- arch, profile, common_flags, chost and llvm_slot stay exact (D4, R6.1).

Per rule, the hostile halves come first: the fixture where the rule would
wrongly COLLAPSE two generations into one, then the fixture where it would
wrongly SPLIT one generation into two, and only then the benign case.
"""

from typing import Any

import pytest

from shidashi.generation import GenerationFingerprint, abi_differences


def _fp(**over: Any) -> GenerationFingerprint:
    fields: dict[str, Any] = {
        "arch": "v3",
        "profile": "default/linux/amd64/23.0/no-multilib/systemd",
        "common_flags": "-march=x86-64-v3 -O2 -pipe",
        "chost": "x86_64-pc-linux-gnu",
        "llvm_slot": "22",
        "gcc": "16.2.0",
        "binutils": "2.46.1",
        "glibc": "2.43-r4",
    }
    fields.update(over)
    return GenerationFingerprint(**fields)


# --- gcc: by major (R1.1-R1.3) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("was", "now"),
    [
        ("9.5.0", "19.1.0"),  # a prefix match would call them one major
        ("19.1.0", "9.5.0"),
        ("1.6.2", "16.2.0"),
        ("16.2.0", "17.1.0"),
        ("17.1.0", "16.2.0"),
    ],
)
def test_a_gcc_major_change_is_named_with_both_strings(was: str, now: str) -> None:
    """Hostile (wrong collapse) first, then the plain 16 -> 17 crossing (R1.2)."""
    assert abi_differences(_fp(gcc=was), _fp(gcc=now)) == {"gcc": (was, now)}


@pytest.mark.parametrize(
    ("was", "now"),
    [
        ("16.2.0", "16.2.1_p20260926"),  # the pilot's refusal
        ("16.1.0-r3", "16.3.0"),
        ("16.2.0", "16.2.0-r1"),  # a Gentoo revision is the same release
    ],
)
def test_a_gcc_upgrade_within_its_major_is_the_same_generation(was: str, now: str) -> None:
    """Hostile (wrong split): an ABI-neutral gcc release must not end the generation (R1.1)."""
    assert abi_differences(_fp(gcc=was), _fp(gcc=now)) == {}


@pytest.mark.parametrize(
    ("was", "now"),
    [("16.2.1_p20260926", "16.2.0"), ("16.2.0", "16"), ("16.3.0", "16.1.0-r3")],
)
def test_a_gcc_downgrade_within_its_major_ends_the_generation(was: str, now: str) -> None:
    """Review of 2026-10-08: a minor release adds GLIBCXX_* symbols, so binpkgs built
    by the newer gcc may not run against the older libstdc++."""
    assert set(abi_differences(_fp(gcc=was), _fp(gcc=now))) == {"gcc"}


def test_the_same_gcc_is_the_same_generation() -> None:
    assert abi_differences(_fp(), _fp()) == {}


@pytest.mark.parametrize(
    ("was", "now"),
    [("", "16.2.0"), ("16.2.0", ""), ("custom", "other"), ("custom", "16.2.0"), ("x16", "x16.1")],
)
def test_an_unparseable_gcc_is_compared_as_a_string(was: str, now: str) -> None:
    """R1.3: without a leading digit, only equal strings are the same generation."""
    assert abi_differences(_fp(gcc=was), _fp(gcc=now)) == {"gcc": (was, now)}


@pytest.mark.parametrize("value", ["custom", ""])
def test_an_unparseable_gcc_equal_on_both_sides_is_the_same_generation(value: str) -> None:
    assert abi_differences(_fp(gcc=value), _fp(gcc=value)) == {}


# --- glibc: up yes, down no (R2.1-R2.5) ------------------------------------------------


@pytest.mark.parametrize(
    ("was", "now"),
    [
        ("2.43", "2.9"),  # as text "2.9" > "2.43": a string compare accepts this downgrade
        ("2.43-r4", "2.42"),
        ("2.43", "2.42-r9"),  # a higher revision does not outweigh an older release
        ("2.43.1", "2.43"),
    ],
)
def test_a_glibc_downgrade_is_named_with_both_strings(was: str, now: str) -> None:
    """Hostile (wrong collapse): an older release must be refused (R2.3)."""
    assert abi_differences(_fp(glibc=was), _fp(glibc=now)) == {"glibc": (was, now)}


@pytest.mark.parametrize(("was", "now"), [("2.43", ""), ("", "2.43")])
def test_glibc_empty_on_exactly_one_side_is_a_libc_change(was: str, now: str) -> None:
    """R2.4: the libc came or went -- whichever side is empty."""
    assert abi_differences(_fp(glibc=was), _fp(glibc=now)) == {"glibc": (was, now)}


@pytest.mark.parametrize(
    ("was", "now"),
    [
        ("2.9", "2.43"),  # as text this looks like a downgrade; it is an upgrade
        ("2.43", "2.43-r4"),
        ("2.43-r4", "2.43"),  # only the revision differs: same release (R2.2)
        ("2.43-r4", "2.43_p1"),
        ("2.43", "2.43.1"),
    ],
)
def test_a_glibc_upgrade_or_a_revision_is_the_same_generation(was: str, now: str) -> None:
    """Hostile (wrong split): a newer release or a revision must not end it (R2.1, R2.2)."""
    assert abi_differences(_fp(glibc=was), _fp(glibc=now)) == {}


def test_a_plain_glibc_upgrade_is_the_same_generation() -> None:
    assert abi_differences(_fp(glibc="2.42"), _fp(glibc="2.43")) == {}


@pytest.mark.parametrize(("was", "now"), [("custom", "other"), ("2.43", "musl-1.2.5")])
def test_an_unparseable_glibc_is_compared_as_a_string(was: str, now: str) -> None:
    """R2.5."""
    assert abi_differences(_fp(glibc=was), _fp(glibc=now)) == {"glibc": (was, now)}


def test_an_unparseable_glibc_equal_on_both_sides_is_the_same_generation() -> None:
    assert abi_differences(_fp(glibc="custom"), _fp(glibc="custom")) == {}


def test_glibc_empty_on_both_sides_is_the_same_generation() -> None:
    assert abi_differences(_fp(glibc=""), _fp(glibc="")) == {}


# --- binutils: never compared (R3.1, R3.2) ----------------------------------------------


@pytest.mark.parametrize(("was", "now"), [("2.46.1", "2.47"), ("2.47", "2.46.1"), ("2.46.1", "")])
def test_a_binutils_only_change_is_the_same_generation(was: str, now: str) -> None:
    assert abi_differences(_fp(binutils=was), _fp(binutils=now)) == {}


def test_binutils_is_left_out_when_another_field_ends_the_generation() -> None:
    differences = abi_differences(
        _fp(common_flags="-O2", binutils="2.46.1"), _fp(common_flags="-O3", binutils="2.47")
    )
    assert differences == {"common_flags": ("-O2", "-O3")}


# --- the exact fields (R6.1) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "was", "now"),
    [
        # near-identical values that a normalizing comparison would merge
        ("common_flags", "-O2 -pipe", "-O2  -pipe"),
        ("common_flags", "-O2 -pipe", "-pipe -O2"),
        ("common_flags", "-march=x86-64-v3 -O2", "-march=x86-64-v4 -O2"),
        ("arch", "v3", "v4"),
        ("profile", "default/linux/amd64/23.0/systemd", "default/linux/amd64/23.0/systemd/"),
        ("chost", "x86_64-pc-linux-gnu", "x86_64-pc-linux-musl"),
        ("llvm_slot", "21", "22"),
        ("llvm_slot", "21", "21 "),
    ],
)
def test_an_exact_field_differing_by_one_character_is_named(field: str, was: str, now: str) -> None:
    assert abi_differences(_fp(**{field: was}), _fp(**{field: now})) == {field: (was, now)}


def test_every_ending_field_is_named_in_model_order() -> None:
    recorded = _fp()
    current = _fp(
        arch="v4",
        profile="p2",
        common_flags="-O3",
        chost="aarch64-unknown-linux-gnu",
        llvm_slot="23",
        gcc="17.1.0",
        binutils="2.47",
        glibc="2.42",
    )
    differences = abi_differences(recorded, current)
    assert list(differences) == [
        "arch",
        "profile",
        "common_flags",
        "chost",
        "llvm_slot",
        "gcc",
        "glibc",
    ]
    assert differences["gcc"] == ("16.2.0", "17.1.0")
    assert differences["glibc"] == ("2.43-r4", "2.42")


def test_the_pilot_toolchain_bump_with_unchanged_flags_is_the_same_generation() -> None:
    """The refused resume of 2026-10-05: gcc patch, binutils and glibc revision moved."""
    recorded = _fp(gcc="16.2.0", binutils="2.46.1", glibc="2.43-r4")
    current = _fp(gcc="16.2.1_p20260926", binutils="2.47", glibc="2.43-r5")
    assert abi_differences(recorded, current) == {}
