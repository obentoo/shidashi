"""Unit tests of ``check_or_record`` on the ABI rules (story 016, task 1.4).

The contract is unchanged: ``True`` when it records (the generation starts),
``False`` when it accepts, a :class:`GenerationMismatchError` when it refuses,
and it never writes over a recorded file. What changes:

- it accepts by :func:`abi_differences`, not by equality (the pilot's refusal);
- the error names the successor ``--pkgdir`` and, for a re-check, the phase;
- an unreadable or unparseable fingerprint file is a ``FactoryError`` of phase
  ``generation`` naming the file, and the file is left alone.

The pilot case runs through the real :func:`fingerprint` of a rootfs, so the
versions compared are the ones the vdb really yields.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from shidashi.generation import (
    FINGERPRINT_FILE,
    GenerationFingerprint,
    GenerationMismatchError,
    check_or_record,
    fingerprint,
)
from shidashi.phases import FactoryError
from shidashi.recipe import ResolvedRecipe

_FIELDS = {"arch", "profile", "common_flags", "chost", "llvm_slot", "gcc", "binutils", "glibc"}


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


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-march=x86-64-v3 -O2 -pipe",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(),
        portage_layers=(),
    )


def _rootfs(root: Path, *cpvs: str) -> Path:
    for cpv in cpvs:
        (root / "var/db/pkg" / cpv).mkdir(parents=True)
    (root / "etc/portage").mkdir(parents=True, exist_ok=True)
    (root / "etc/portage/make.conf").write_text(
        'CHOST="x86_64-pc-linux-gnu"\nLLVM_SLOT="22"\n', encoding="utf-8"
    )
    return root


def _pkgdir(tmp_path: Path) -> Path:
    return tmp_path / "binpkgs" / "v3" / "20260823T153057Z"


def _fingerprint_text_as_written_before_the_fix(fp: GenerationFingerprint) -> str:
    """The file format of every PKGDIR on disk today: indent=1 JSON, one newline."""
    return fp.model_dump_json(indent=1) + "\n"


def _write_recorded(pkgdir: Path, fp: GenerationFingerprint) -> Path:
    pkgdir.mkdir(parents=True, exist_ok=True)
    path = pkgdir / FINGERPRINT_FILE
    path.write_text(_fingerprint_text_as_written_before_the_fix(fp), encoding="utf-8")
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    return path


def _snapshot(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


# --- accept: the pilot case and the other ABI-neutral moves (R1.1, R2.1, R3.1) ------------


def test_the_pilots_gcc_patch_release_is_accepted(tmp_path: Path) -> None:
    """The refused resume of 2026-10-05, through the real fingerprint of a rootfs."""
    rootfs = _rootfs(
        tmp_path / "rootfs",
        "sys-devel/gcc-16.2.0",
        "sys-devel/binutils-2.46.1",
        "sys-libs/glibc-2.43-r4",
    )
    pkgdir = _pkgdir(tmp_path)
    assert check_or_record(pkgdir, fingerprint(rootfs, _recipe())) is True
    path = pkgdir / FINGERPRINT_FILE
    before = path.read_bytes()

    (rootfs / "var/db/pkg/sys-devel/gcc-16.2.1_p20260926").mkdir()
    current = fingerprint(rootfs, _recipe())
    assert current.gcc == "16.2.1_p20260926"
    assert check_or_record(pkgdir, current) is False
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [{"binutils": "2.47"}, {"glibc": "2.44"}, {"glibc": "2.43-r5"}, {"gcc": "16.3.0"}],
)
def test_an_abi_neutral_change_is_accepted(tmp_path: Path, change: dict[str, str]) -> None:
    pkgdir = _pkgdir(tmp_path)
    _write_recorded(pkgdir, _fp())
    assert check_or_record(pkgdir, _fp(**change)) is False


# --- refuse: what the error says (R1.2, R5.1, R5.2, R5.3, R5.5) ----------------------------


def test_a_gcc_major_crossing_names_gcc_and_the_successor(tmp_path: Path) -> None:
    pkgdir = _pkgdir(tmp_path)
    _write_recorded(pkgdir, _fp(gcc="16.2.0"))
    with pytest.raises(GenerationMismatchError) as err:
        check_or_record(pkgdir, _fp(gcc="17.1.0"))
    successor = pkgdir.parent / "20260823T153057Z-gcc17"
    assert err.value.differences == {"gcc": ("16.2.0", "17.1.0")}
    assert err.value.successor == successor
    assert err.value.after_phase is None
    assert err.value.phase == "generation"
    message = str(err.value)
    assert "gcc: '16.2.0' -> '17.1.0'" in message
    assert f"--pkgdir {successor}" in message
    assert "new generation" in message
    assert "after phase" not in message


def test_a_refusal_after_a_phase_names_the_phase(tmp_path: Path) -> None:
    pkgdir = _pkgdir(tmp_path)
    _write_recorded(pkgdir, _fp(gcc="16.2.0"))
    with pytest.raises(GenerationMismatchError) as err:
        check_or_record(pkgdir, _fp(gcc="17.1.0"), after_phase="base")
    assert err.value.after_phase == "base"
    assert "'base'" in str(err.value)
    assert f"--pkgdir {pkgdir.parent / '20260823T153057Z-gcc17'}" in str(err.value)


def test_a_glibc_downgrade_names_glibc_and_a_hashed_successor(tmp_path: Path) -> None:
    pkgdir = _pkgdir(tmp_path)
    _write_recorded(pkgdir, _fp(glibc="2.43-r4"))
    with pytest.raises(GenerationMismatchError) as err:
        check_or_record(pkgdir, _fp(glibc="2.42"))
    assert err.value.differences == {"glibc": ("2.43-r4", "2.42")}
    assert err.value.successor.parent == pkgdir.parent
    assert err.value.successor.name.startswith("20260823T153057Z-gcc16-")
    assert "glibc: '2.43-r4' -> '2.42'" in str(err.value)


def test_binutils_never_appears_in_a_refusal(tmp_path: Path) -> None:
    """R3.2: it changed too, but it does not end a generation."""
    pkgdir = _pkgdir(tmp_path)
    _write_recorded(pkgdir, _fp(common_flags="-O2", binutils="2.46.1"))
    with pytest.raises(GenerationMismatchError) as err:
        check_or_record(pkgdir, _fp(common_flags="-O3", binutils="2.47"))
    assert set(err.value.differences) == {"common_flags"}
    # pytest names tmp_path after the test, so "binutils" is in the path itself
    message = str(err.value).replace(str(pkgdir.parent), "<binpkgs>")
    assert "binutils" not in message
    assert "2.47" not in message


def test_an_exact_field_differing_by_one_character_is_still_refused(tmp_path: Path) -> None:
    """R6.1."""
    pkgdir = _pkgdir(tmp_path)
    _write_recorded(pkgdir, _fp(chost="x86_64-pc-linux-gnu"))
    with pytest.raises(GenerationMismatchError) as err:
        check_or_record(pkgdir, _fp(chost="x86_64-pc-linux-gnux"))
    assert set(err.value.differences) == {"chost"}


def test_the_same_refusal_twice_names_the_same_successor(tmp_path: Path) -> None:
    """R5.4."""
    pkgdir = _pkgdir(tmp_path)
    _write_recorded(pkgdir, _fp())
    current = _fp(gcc="17.1.0", common_flags="-O3")
    successors = []
    for _ in range(2):
        with pytest.raises(GenerationMismatchError) as err:
            check_or_record(pkgdir, current)
        successors.append(err.value.successor)
    assert successors[0] == successors[1]
    assert not successors[0].exists()  # named, never created (R5.9)


# --- the recorded file: written once, never touched again (R6.2-R6.5) ----------------------


def test_a_pkgdir_without_a_fingerprint_records_eight_exact_fields(tmp_path: Path) -> None:
    pkgdir = _pkgdir(tmp_path)
    current = _fp(gcc="16.2.1_p20260926", binutils="2.46.1", glibc="2.43-r4")
    assert check_or_record(pkgdir, current) is True
    path = pkgdir / FINGERPRINT_FILE
    recorded = json.loads(path.read_text(encoding="utf-8"))
    assert set(recorded) == _FIELDS
    assert recorded["gcc"] == "16.2.1_p20260926"
    assert recorded["binutils"] == "2.46.1"
    assert recorded["glibc"] == "2.43-r4"
    assert path.read_text(encoding="utf-8") == _fingerprint_text_as_written_before_the_fix(current)


def test_a_file_written_before_the_fix_is_untouched_by_an_accept(tmp_path: Path) -> None:
    """R6.4, R6.5: no migration, no rewrite."""
    path = _write_recorded(_pkgdir(tmp_path), _fp())
    before = _snapshot(path)
    assert check_or_record(path.parent, _fp(gcc="16.2.1_p20260926", binutils="2.47")) is False
    assert _snapshot(path) == before


def test_a_file_written_before_the_fix_is_untouched_by_a_refusal(tmp_path: Path) -> None:
    path = _write_recorded(_pkgdir(tmp_path), _fp())
    before = _snapshot(path)
    with pytest.raises(GenerationMismatchError):
        check_or_record(path.parent, _fp(gcc="17.1.0"), after_phase="base")
    assert _snapshot(path) == before


# --- an unreadable fingerprint file (R5.6) -------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        '{"arch": "v3", "profile": "default/linux/amd64',  # truncated
        json.dumps({**_fp().model_dump(), "gcc_major": "16"}),  # unknown field
        json.dumps({"arch": "v3"}),  # missing fields
        "",
    ],
)
def test_an_unparseable_fingerprint_fails_naming_the_file(tmp_path: Path, content: str) -> None:
    pkgdir = _pkgdir(tmp_path)
    pkgdir.mkdir(parents=True)
    path = pkgdir / FINGERPRINT_FILE
    path.write_text(content, encoding="utf-8")
    before = _snapshot(path)
    with pytest.raises(FactoryError) as err:
        check_or_record(pkgdir, _fp())
    assert not isinstance(err.value, GenerationMismatchError)
    assert err.value.phase == "generation"
    assert str(path) in str(err.value)
    assert err.value.__cause__ is not None
    assert _snapshot(path) == before


def test_a_non_utf8_fingerprint_fails_naming_the_file(tmp_path: Path) -> None:
    pkgdir = _pkgdir(tmp_path)
    pkgdir.mkdir(parents=True)
    path = pkgdir / FINGERPRINT_FILE
    path.write_bytes(b"\xff")
    before = _snapshot(path)
    with pytest.raises(FactoryError) as err:
        check_or_record(pkgdir, _fp())
    assert not isinstance(err.value, GenerationMismatchError)
    assert err.value.phase == "generation"
    assert str(path) in str(err.value)
    assert _snapshot(path) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_an_unreadable_fingerprint_fails_naming_the_file(tmp_path: Path) -> None:
    pkgdir = _pkgdir(tmp_path)
    path = _write_recorded(pkgdir, _fp())
    path.chmod(0)
    try:
        with pytest.raises(FactoryError) as err:
            check_or_record(pkgdir, _fp())
        assert err.value.phase == "generation"
        assert str(path) in str(err.value)
    finally:
        path.chmod(0o644)
