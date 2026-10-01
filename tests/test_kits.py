"""Tests of shidashi.kits -- the kit library checked against the pinned trees."""

from pathlib import Path

import pytest

from shidashi import kits


@pytest.mark.parametrize(("atom", "parts"), [
    ("app-editors/vim", ("app-editors/vim", None)),
    ("sys-fs/fuse:0", ("sys-fs/fuse", None)),
    ("net-libs/webkit-gtk:4.1", ("net-libs/webkit-gtk", None)),
    (">=dev-lang/rust-1.98:stable[clippy]::gentoo", ("dev-lang/rust", "gentoo")),
    ("=sys-kernel/gentoo-kernel-7.2.6*", ("sys-kernel/gentoo-kernel", None)),
    ("~dev-util/pkgdev-0.2.12-r1", ("dev-util/pkgdev", None)),
    # no operator, no version: a name that looks versioned stays whole
    ("sys-libs/libstdc++-v3", ("sys-libs/libstdc++-v3", None)),
    ("app-portage/bentoolkit::bentoo", ("app-portage/bentoolkit", "bentoo")),
])
def test_atom_parts(atom: str, parts: tuple[str, str | None]) -> None:
    assert kits.atom_parts(atom) == parts


def _library(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    lib = tmp_path / "kits"
    (lib / "core").mkdir(parents=True)
    (lib / "core" / "base").write_text(
        "# the base\napp-editors/vim\napp-editors/vmi  # a typo\n@extra\n@missing\n"
        "sys-fs/fuse:0\napp-portage/bentoolkit::bentoo\nx/y::guru\nnotanatom\n"
    )
    (lib / "core" / "extra").write_text(">=dev-lang/rust-1.98\n")
    gentoo, bentoo = tmp_path / "gentoo", tmp_path / "bentoo"
    for repo, cp in ((gentoo, "app-editors/vim"), (gentoo, "sys-fs/fuse"),
                     (gentoo, "dev-lang/rust"), (bentoo, "app-portage/bentoolkit")):
        (repo / cp).mkdir(parents=True)
    return lib, {"gentoo": gentoo, "bentoo": bentoo}


def test_check_names_every_atom_no_pinned_tree_has(tmp_path: Path) -> None:
    lib, repos = _library(tmp_path)
    assert kits.check(lib, repos) == [
        "base:3: app-editors/vmi -- no app-editors/vmi in ::gentoo or ::bentoo",
        "base:5: @missing names no kit",
        "base:8: x/y::guru names repository ::guru, which is not pinned",
        "base:9: notanatom is not category/package",
    ]


def test_a_clean_library_has_no_problem(tmp_path: Path) -> None:
    lib, repos = _library(tmp_path)
    (lib / "core" / "base").write_text("app-editors/vim\n@extra\n")
    assert kits.check(lib, repos) == []
