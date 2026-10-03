"""UNIT (R6.2) — new shidashi.config path helpers for the pretend flow.

Contract (design.md §config): ``scratch_dir()`` (env ``SHIDASHI_SCRATCH``, default
``/var/tmp/shidashi``), ``cache_dir()`` (env ``SHIDASHI_CACHE``) and
``seeds_dir()`` (env ``SHIDASHI_SEEDS_DIR``). Each helper reads the environment
variable *per call* (same pattern as ``variants_dir``), so we force the state via
``monkeypatch`` — we never depend on the host. Observable behavior only:
the override when the env var exists, the default when it is absent.

Story 003 (2.1): new build/cache helpers — ``build_root()``
(``scratch_dir()/build``), ``pkgdir(arch)`` (``cache_dir()/binpkgs/<arch>``,
partitioned per arch), ``ccache_dir()``/``sccache_dir()``/``distdir()``
(``cache_dir()/{ccache,sccache,distfiles}``) and ``fork_points_dir()``
(``cache_dir()/fork-points``). All read the env per call.
"""

from pathlib import Path

import pytest

from shidashi import config

# --- scratch_dir -------------------------------------------------------------


def test_scratch_dir_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHIDASHI_SCRATCH", raising=False)
    assert config.scratch_dir() == Path("/var/tmp/shidashi")


def test_scratch_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    assert config.scratch_dir() == tmp_path / "scratch"


# --- cache_dir ---------------------------------------------------------------


def test_cache_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    assert config.cache_dir() == tmp_path / "cache"


def test_cache_dir_read_per_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # the env is read on every call (no cached global state)
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "a"))
    assert config.cache_dir() == tmp_path / "a"
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "b"))
    assert config.cache_dir() == tmp_path / "b"


# --- seeds_dir ---------------------------------------------------------------


def test_seeds_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(tmp_path / "seeds"))
    assert config.seeds_dir() == tmp_path / "seeds"


def test_seeds_dir_returns_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # without the env, it still returns a Path (default relative to the repo)
    monkeypatch.delenv("SHIDASHI_SEEDS_DIR", raising=False)
    assert isinstance(config.seeds_dir(), Path)


# --- build_root (story 003 2.1 — R5.1/R6.2/R8.2) -----------------------------


def test_build_root_under_scratch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    assert config.build_root() == tmp_path / "scratch" / "build"


def test_build_root_honors_scratch_env_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "a"))
    assert config.build_root() == tmp_path / "a" / "build"
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "b"))
    assert config.build_root() == tmp_path / "b" / "build"


# --- pkgdir partitions per arch ----------------------------------------------


def test_pkgdir_under_cache_binpkgs_arch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    assert config.pkgdir("v3") == tmp_path / "cache" / "binpkgs" / "v3"


def test_pkgdir_partitions_per_arch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    assert config.pkgdir("v3") != config.pkgdir("znver5")
    assert config.pkgdir("v3").name == "v3"
    assert config.pkgdir("znver5").name == "znver5"
    # a generation gets its own directory under the arch (D26)
    assert config.pkgdir("v3", "20260823T153057Z") == config.pkgdir("v3") / "20260823T153057Z"


# --- cache subdirs (shared across flavors/archs) -----------------------------


def test_ccache_sccache_distdir_under_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    base = tmp_path / "cache"
    assert config.ccache_dir() == base / "ccache"
    assert config.sccache_dir() == base / "sccache"
    assert config.distdir() == base / "distfiles"


def test_fork_points_dir_under_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    assert config.fork_points_dir() == tmp_path / "cache" / "fork-points"


def test_cache_helpers_read_env_per_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "a"))
    assert config.fork_points_dir() == tmp_path / "a" / "fork-points"
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "b"))
    assert config.fork_points_dir() == tmp_path / "b" / "fork-points"
