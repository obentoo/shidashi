"""Unit tests of the successor PKGDIR naming (story 016, task 1.3, D9).

``successor_pkgdir(pkgdir, current, differences)`` is the sibling PKGDIR a
refused build is told to use: ``<pkgdir>-gcc<current gcc major>``, plus
``-<first 8 hex of the sha256 of the compared values>`` when a field other than
gcc ends the generation. ``generation_key(current)`` is those compared values.
Pure: the name is deterministic and nothing is created (R5.4, R5.5, R5.8, R5.9).

The successor is a DERIVED value, so its hostile halves are asked of the name
itself: two different generations must never get one name (wrong collapse), and
two fingerprints of one generation must never get two names (wrong split).
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from shidashi.generation import (
    GenerationFingerprint,
    abi_differences,
    generation_key,
    successor_pkgdir,
)

PKGDIR = Path("/var/cache/shidashi/binpkgs/v3/20260823T153057Z")


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


def _successor(recorded: GenerationFingerprint, current: GenerationFingerprint) -> Path:
    return successor_pkgdir(PKGDIR, current, abi_differences(recorded, current))


def _hash_of(current: GenerationFingerprint) -> str:
    """D9/R5.8: the first 8 hex of the sha256 of the compared values."""
    payload = json.dumps(list(generation_key(current)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


# --- wrong collapse: two generations, one name --------------------------------------------


def test_two_different_flags_changes_get_two_different_successors() -> None:
    recorded = _fp(common_flags="-O2")
    one = _successor(recorded, _fp(common_flags="-O3"))
    other = _successor(recorded, _fp(common_flags="-Os"))
    assert one != other
    assert one.name.startswith(f"{PKGDIR.name}-gcc16-")
    assert other.name.startswith(f"{PKGDIR.name}-gcc16-")


def test_successors_differ_on_the_gcc_major() -> None:
    recorded = _fp(gcc="16.2.0")
    assert _successor(recorded, _fp(gcc="17.1.0")) != _successor(recorded, _fp(gcc="18.1.0"))
    # 1 and 16 are not one major, so the names differ too
    assert _successor(_fp(gcc="9.5.0"), _fp(gcc="19.1.0")).name == f"{PKGDIR.name}-gcc19"
    assert _successor(_fp(gcc="19.1.0"), _fp(gcc="9.5.0")).name == f"{PKGDIR.name}-gcc9"


def test_a_gcc_only_crossing_and_a_crossing_with_a_flags_change_get_different_names() -> None:
    gcc_only = _successor(_fp(gcc="16.2.0"), _fp(gcc="17.1.0"))
    with_flags = _successor(_fp(gcc="16.2.0"), _fp(gcc="17.1.0", common_flags="-O3"))
    assert gcc_only != with_flags


def test_generation_key_separates_what_the_comparison_separates() -> None:
    assert generation_key(_fp(gcc="9.5.0")) != generation_key(_fp(gcc="19.1.0"))
    assert generation_key(_fp(glibc="2.9")) != generation_key(_fp(glibc="2.43"))
    assert generation_key(_fp(llvm_slot="21")) != generation_key(_fp(llvm_slot="22"))


# --- wrong split: one generation, two names ------------------------------------------------


def test_fingerprints_of_one_generation_share_the_successor() -> None:
    """binutils, a gcc patch and a glibc revision are not in the compared values."""
    recorded = _fp(common_flags="-O2")
    base = _fp(common_flags="-O3")
    same_generation = _fp(common_flags="-O3", binutils="2.47", gcc="16.2.1_p20260926", glibc="2.43")
    assert generation_key(base) == generation_key(same_generation)
    assert _successor(recorded, base) == _successor(recorded, same_generation)


def test_the_same_refusal_names_the_same_successor_twice() -> None:
    """R5.4: deterministic, whatever ran in between."""
    recorded, current = _fp(glibc="2.43"), _fp(glibc="2.42")
    assert _successor(recorded, current) == _successor(recorded, current)


# --- the names D9 prescribes (benign) ------------------------------------------------------


def test_a_gcc_only_crossing_is_named_by_the_new_major() -> None:
    """R5.5: the pilot after a gcc 17 crossing."""
    successor = _successor(_fp(gcc="16.2.0"), _fp(gcc="17.1.0"))
    assert successor == PKGDIR.parent / "20260823T153057Z-gcc17"


def test_a_glibc_downgrade_appends_the_hash_of_the_compared_values() -> None:
    """R5.8: a field besides gcc ended the generation."""
    current = _fp(glibc="2.42")
    successor = _successor(_fp(glibc="2.43-r4"), current)
    assert successor.parent == PKGDIR.parent
    assert re.fullmatch(r"20260823T153057Z-gcc16-[0-9a-f]{8}", successor.name)
    assert successor.name == f"20260823T153057Z-gcc16-{_hash_of(current)}"


def test_a_flags_change_with_a_gcc_crossing_carries_both_parts() -> None:
    current = _fp(gcc="17.1.0", common_flags="-O3")
    successor = _successor(_fp(), current)
    assert successor.name == f"20260823T153057Z-gcc17-{_hash_of(current)}"


def test_an_unparseable_gcc_is_named_none() -> None:
    successor = _successor(_fp(gcc="16.2.0"), _fp(gcc=""))
    assert successor.name == "20260823T153057Z-gccnone"


def test_generation_key_holds_the_major_and_the_release_not_the_raw_versions() -> None:
    key = generation_key(_fp(gcc="16.2.1_p20260926", glibc="2.43-r4"))
    assert isinstance(key, tuple)
    assert "16" in key and "2.43" in key
    assert "16.2.1_p20260926" not in key and "2.43-r4" not in key
    assert "2.46.1" not in key  # binutils is not compared


# --- pure: nothing is created (R5.9) -------------------------------------------------------


def test_naming_the_successor_creates_nothing(tmp_path: Path) -> None:
    pkgdir = tmp_path / "binpkgs" / "v3" / "20260823T153057Z"
    pkgdir.mkdir(parents=True)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    current = _fp(gcc="17.1.0", glibc="2.42")
    successor = successor_pkgdir(pkgdir, current, abi_differences(_fp(), current))
    assert successor.parent == pkgdir.parent
    assert not successor.exists()
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
