"""Unit tests of shidashi.quirks -- the per-package exceptions registry."""

from pathlib import Path

import pytest

from shidashi import config
from shidashi.quirks import (
    PACKAGE_ENV,
    PACKAGE_MASK,
    Quirk,
    QuirksError,
    load_quirks,
    render_quirks,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "quirks.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_quirk_needs_an_effect_a_reason_and_evidence() -> None:
    with pytest.raises(ValueError, match="needs `features` or `mask"):
        Quirk(atom="dev-java/openjdk", why="x", found="F73")
    with pytest.raises(ValueError, match="must say why"):
        Quirk(atom="dev-java/openjdk", features=("-ccache",), why=" ", found="F73")
    with pytest.raises(ValueError, match="not FEATURES tokens"):
        Quirk(atom="dev-java/openjdk", features=("-ccache; rm -rf /",), why="x", found="F73")
    with pytest.raises(ValueError, match="not a package atom"):
        Quirk(atom="openjdk", features=("-ccache",), why="x", found="F73")


def test_load_quirks_refuses_repeated_atoms_and_non_lists(tmp_path: Path) -> None:
    entry = "- {atom: dev-java/openjdk, features: [-ccache], why: w, found: F73}\n"
    with pytest.raises(QuirksError, match="repeated"):
        load_quirks(_write(tmp_path, entry + entry))
    with pytest.raises(QuirksError, match="list of entries"):
        load_quirks(_write(tmp_path, "atom: dev-java/openjdk\n"))
    assert load_quirks(tmp_path / "absent.yaml") == ()


def test_render_quirks_writes_env_package_env_and_mask(tmp_path: Path) -> None:
    quirks = load_quirks(
        _write(
            tmp_path,
            """
- atom: dev-java/openjdk
  features: [-ccache, -network-sandbox]
  why: |
    pkg_pretend dies under ccache.
    Its javac server needs loopback.
  found: F73, F74
- atom: app-alternatives/*
  features: [-collision-protect, protect-owned]
  why: unowned symlinks
  found: F71
- atom: sys-firmware/seabios
  mask: true
  why: python 3.13
  found: 2026-09-27
""",
        )
    )
    files = render_quirks(quirks, source="variants/base/quirks.yaml")

    assert files[PACKAGE_ENV].splitlines()[1:] == [
        "dev-java/openjdk  quirk-dev-java_openjdk.conf",
        "app-alternatives/*  quirk-app-alternatives.conf",
    ]
    env = files["env/quirk-dev-java_openjdk.conf"]
    assert env.startswith("# GENERATED from variants/base/quirks.yaml")
    assert "# Its javac server needs loopback." in env
    assert env.rstrip().endswith('FEATURES="-ccache -network-sandbox"')
    assert files[PACKAGE_MASK].rstrip().endswith("sys-firmware/seabios")
    assert set(files) == {
        PACKAGE_ENV,
        PACKAGE_MASK,
        "env/quirk-dev-java_openjdk.conf",
        "env/quirk-app-alternatives.conf",
    }


def test_the_base_registry_loads() -> None:
    quirks = load_quirks(config.variants_dir() / "base" / "quirks.yaml")
    assert {q.atom for q in quirks} >= {
        "dev-java/openjdk",
        "app-alternatives/*",
        "sys-firmware/seabios",
    }
