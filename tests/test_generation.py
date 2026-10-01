"""Unit tests of shidashi.generation -- the generation fingerprint (D26)."""

import json
from pathlib import Path

import pytest

from shidashi.generation import (
    FINGERPRINT_FILE,
    GenerationMismatchError,
    check_or_record,
    fingerprint,
    installed_version,
    make_conf_value,
)
from shidashi.recipe import ResolvedRecipe


def _recipe(common_flags: str = "-march=x86-64-v3 -O2 -pipe") -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags=common_flags,
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(),
        portage_layers=(),
    )


@pytest.fixture
def rootfs(tmp_path: Path) -> Path:
    root = tmp_path / "rootfs"
    for cpv in (
        "sys-devel/gcc-9.5.0",
        "sys-devel/gcc-15.3.0",
        "sys-devel/gcc-config-2.12",
        "sys-devel/binutils-2.46.1",
        "sys-devel/binutils-2.47",
        "sys-devel/binutils-config-5.6",
        "sys-libs/glibc-2.43-r4",
    ):
        (root / "var/db/pkg" / cpv).mkdir(parents=True)
    (root / "etc/portage").mkdir(parents=True)
    (root / "etc/portage/make.conf").write_text(
        '# layer: arch/v3\nCHOST="x86_64-pc-linux-gnu"\nLLVM_SLOT="21"\n'
        '# layer: base\nLLVM_SLOT="22"  # the base wins\nCFLAGS="${COMMON_FLAGS}"\n',
        encoding="utf-8",
    )
    return root


def test_installed_version_is_the_newest_slot_and_ignores_sibling_packages(rootfs: Path) -> None:
    # gcc-config-2.12 starts with "gcc-" too; "9.5.0" < "15.3.0" numerically
    assert installed_version(rootfs, "sys-devel/gcc") == "15.3.0"
    assert installed_version(rootfs, "sys-devel/binutils") == "2.47"
    assert installed_version(rootfs, "dev-lang/rust") == ""


def test_make_conf_value_takes_the_last_literal_assignment(rootfs: Path) -> None:
    text = (rootfs / "etc/portage/make.conf").read_text(encoding="utf-8")
    assert make_conf_value(text, "LLVM_SLOT") == "22"
    assert make_conf_value(text, "CFLAGS") == ""  # an expansion, not a literal


def test_fingerprint_reads_config_and_vdb(rootfs: Path) -> None:
    fp = fingerprint(rootfs, _recipe())
    assert fp.model_dump() == {
        "arch": "v3",
        "profile": "default/linux/amd64/23.0/no-multilib/systemd",
        "common_flags": "-march=x86-64-v3 -O2 -pipe",
        "chost": "x86_64-pc-linux-gnu",
        "llvm_slot": "22",
        "gcc": "15.3.0",
        "binutils": "2.47",
        "glibc": "2.43-r4",
    }


def test_check_or_record_starts_a_generation_then_accepts_the_same_one(
    rootfs: Path, tmp_path: Path
) -> None:
    pkgdir = tmp_path / "binpkgs"
    fp = fingerprint(rootfs, _recipe())
    assert check_or_record(pkgdir, fp) is True
    assert json.loads((pkgdir / FINGERPRINT_FILE).read_text(encoding="utf-8"))["gcc"] == "15.3.0"
    assert check_or_record(pkgdir, fp) is False


def test_check_or_record_refuses_another_generation_and_names_what_changed(
    rootfs: Path, tmp_path: Path
) -> None:
    """The August case: binpkgs built with -mtune=generic must not be reused."""
    pkgdir = tmp_path / "binpkgs"
    check_or_record(pkgdir, fingerprint(rootfs, _recipe("-march=x86-64-v3 -mtune=generic -O2")))
    (rootfs / "var/db/pkg/sys-devel/gcc-16.2.0").mkdir()

    with pytest.raises(GenerationMismatchError) as err:
        check_or_record(pkgdir, fingerprint(rootfs, _recipe()))
    assert err.value.differences == {
        "common_flags": ("-march=x86-64-v3 -mtune=generic -O2", "-march=x86-64-v3 -O2 -pipe"),
        "gcc": ("15.3.0", "16.2.0"),
    }
    assert "new generation" in str(err.value)
    # never overwritten: ending a generation is a decision
    assert "mtune" in (pkgdir / FINGERPRINT_FILE).read_text(encoding="utf-8")
