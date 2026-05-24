"""UNIT (R6.2) — novos helpers de caminho de kaji.config para o fluxo pretend.

Contrato (design.md §config): ``scratch_dir()`` (env ``KAJI_SCRATCH``, default
``/var/tmp/kaji-pretend``), ``cache_dir()`` (env ``KAJI_CACHE``) e
``seeds_dir()`` (env ``KAJI_SEEDS_DIR``). Cada helper lê a variável de ambiente
*por chamada* (mesmo padrão de ``variants_dir``), então forçamos o estado via
``monkeypatch`` — nunca dependemos do host. Comportamento observável apenas:
override quando a env existe, default quando ausente.
"""

from pathlib import Path

import pytest

from kaji import config

# --- scratch_dir -------------------------------------------------------------


def test_scratch_dir_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAJI_SCRATCH", raising=False)
    assert config.scratch_dir() == Path("/var/tmp/kaji-pretend")


def test_scratch_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KAJI_SCRATCH", str(tmp_path / "scratch"))
    assert config.scratch_dir() == tmp_path / "scratch"


# --- cache_dir ---------------------------------------------------------------


def test_cache_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KAJI_CACHE", str(tmp_path / "cache"))
    assert config.cache_dir() == tmp_path / "cache"


def test_cache_dir_read_per_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a env é lida a cada chamada (sem estado global cacheado)
    monkeypatch.setenv("KAJI_CACHE", str(tmp_path / "a"))
    assert config.cache_dir() == tmp_path / "a"
    monkeypatch.setenv("KAJI_CACHE", str(tmp_path / "b"))
    assert config.cache_dir() == tmp_path / "b"


# --- seeds_dir ---------------------------------------------------------------


def test_seeds_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KAJI_SEEDS_DIR", str(tmp_path / "seeds"))
    assert config.seeds_dir() == tmp_path / "seeds"


def test_seeds_dir_returns_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # sem env, ainda assim devolve um Path (default relativo ao repo)
    monkeypatch.delenv("KAJI_SEEDS_DIR", raising=False)
    assert isinstance(config.seeds_dir(), Path)
