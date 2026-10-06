"""Unit tests of the version helpers of shidashi.generation (story 016, task 1.1).

``gcc_major`` and ``glibc_release`` derive, from a recorded version string, what
the generation comparison needs (D1, D2, D6): gcc's major version (its Portage
SLOT) and glibc's numeric release. Neither ever raises; a string they cannot
parse gives ``None``, and the comparison then falls back to string equality.

Hostile cases first: the strings a naive comparison gets wrong (a prefix read
as a major, a release compared as text) come before the benign ones.
"""

import pytest

from shidashi.generation import gcc_major, glibc_release

# --- gcc_major -------------------------------------------------------------------


def test_gcc_major_never_reads_a_shorter_major_as_a_prefix_of_a_longer_one() -> None:
    """Hostile (wrong collapse): 9 and 19, 1 and 16 are different majors."""
    assert gcc_major("9.5.0") == "9"
    assert gcc_major("19.1.0") == "19"
    assert gcc_major("1.6.2") == "1"
    assert gcc_major("16.2.0") == "16"
    assert gcc_major("9.5.0") != gcc_major("19.1.0")
    assert gcc_major("1.6.2") != gcc_major("16.2.0")


def test_gcc_major_is_the_same_across_a_patch_release_and_a_snapshot_suffix() -> None:
    """Hostile (wrong split): the pilot's 16.2.0 -> 16.2.1_p20260926 is one major."""
    assert gcc_major("16.2.1_p20260926") == "16"
    assert gcc_major("16.2.0") == gcc_major("16.2.1_p20260926") == gcc_major("16")
    assert gcc_major("16.1.0-r3") == "16"


def test_gcc_major_of_a_plain_release() -> None:
    assert gcc_major("17.1.0") == "17"


@pytest.mark.parametrize("version", ["", "custom", "x16.2.0", "_p20260926", "-16", ".16"])
def test_gcc_major_is_none_without_a_leading_digit(version: str) -> None:
    assert gcc_major(version) is None


# --- glibc_release ----------------------------------------------------------------


def test_glibc_release_compares_numerically_not_as_text() -> None:
    """Hostile (wrong collapse): as text "2.9" > "2.43"; as a release it is older."""
    assert glibc_release("2.9") == (2, 9)
    old, new = glibc_release("2.9"), glibc_release("2.43")
    assert old is not None and new is not None
    assert old < new


def test_glibc_release_ignores_a_gentoo_revision_and_a_patch_suffix() -> None:
    """Hostile (wrong split): -rN and _pN are not part of the numeric release."""
    assert glibc_release("2.43-r4") == (2, 43)
    assert glibc_release("2.43_p1") == (2, 43)
    assert glibc_release("2.43-r4") == glibc_release("2.43") == glibc_release("2.43_p1")


def test_glibc_release_keeps_every_dotted_component() -> None:
    assert glibc_release("2.43") == (2, 43)
    assert glibc_release("2.43.1") == (2, 43, 1)


@pytest.mark.parametrize("version", ["", "custom", "musl-1.2.5", "r4", "-2.43"])
def test_glibc_release_is_none_without_a_leading_digit(version: str) -> None:
    assert glibc_release(version) is None


@pytest.mark.parametrize("version", ["", "-", ".", "_p", "16..2", "9999", "2.", "2.43-r", "€"])
def test_the_helpers_never_raise(version: str) -> None:
    major = gcc_major(version)
    release = glibc_release(version)
    assert major is None or major.isdigit()
    assert release is None or all(isinstance(part, int) for part in release)
