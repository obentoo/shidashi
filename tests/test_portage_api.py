"""Testes da integração protegida com o Portage (shidashi.portage_api).

UNIT: deterministas independentemente do host. O estado do Portage é forçado
via ``monkeypatch`` sobre ``shidashi.portage_api`` — nunca dependemos de o host ter
(ou não) ``sys-apps/portage`` instalado. Verifica-se:

* import do módulo nunca levanta e ``PORTAGE_AVAILABLE`` é ``bool`` (R7.1);
* com Portage ausente, ``require_portage`` e os auxiliares de leitura levantam
  :class:`PortageUnavailableError` (R7.2);
* com Portage presente (fake), o portão abre e ``require_portage`` devolve o
  módulo.

NB: a tarefa T7.2 ADICIONARÁ a este arquivo um teste de integração (importar
``shidashi.recipe``/``shidashi.cli`` com Portage ausente). O arquivo é mantido
extensível; T7.2 não é implementada aqui.
"""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typer.testing import CliRunner

import shidashi.portage_api as portage_api
from shidashi.portage_api import (
    PortageUnavailableError,
    configured_repos,
    portage_version,
    require_portage,
)


@pytest.fixture
def portage_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Força o estado 'host não-Gentoo' em shidashi.portage_api."""
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", False)
    monkeypatch.setattr(portage_api, "_portage", None)


# --- import é seguro e PORTAGE_AVAILABLE é bool (R7.1) -----------------------


def test_import_never_raises_and_flag_is_bool() -> None:
    # o módulo já foi importado no topo sem exceção; reforça-se o contrato
    import importlib

    reloaded = importlib.import_module("shidashi.portage_api")
    assert isinstance(reloaded.PORTAGE_AVAILABLE, bool)


def test_unavailable_error_is_exception_subclass() -> None:
    assert issubclass(PortageUnavailableError, Exception)


# --- Portage ausente: o portão e os auxiliares levantam (R7.2) --------------


def test_require_portage_raises_when_absent(portage_absent: None) -> None:
    with pytest.raises(PortageUnavailableError):
        require_portage()


def test_require_portage_message_is_actionable(portage_absent: None) -> None:
    with pytest.raises(PortageUnavailableError) as excinfo:
        require_portage()
    msg = str(excinfo.value)
    assert "Gentoo" in msg
    assert "sys-apps/portage" in msg


def test_portage_version_raises_when_absent(portage_absent: None) -> None:
    with pytest.raises(PortageUnavailableError):
        portage_version()


def test_configured_repos_raises_when_absent(portage_absent: None) -> None:
    with pytest.raises(PortageUnavailableError):
        configured_repos()


# --- guarda defensiva: flag True mas módulo None ainda levanta --------------


def test_require_portage_raises_when_flag_true_but_module_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", None)
    with pytest.raises(PortageUnavailableError):
        require_portage()


# --- Portage presente (fake): o portão abre e devolve o módulo --------------


def test_require_portage_returns_module_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(VERSION="3.0.66")
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", fake)
    # o portão abre e devolve o objeto-módulo configurado (prova via atributo,
    # evitando identity-check entre ModuleType e SimpleNamespace)
    returned = require_portage()
    assert getattr(returned, "VERSION", None) == "3.0.66"


def test_portage_version_reads_fake_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(VERSION="3.0.66")
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", fake)
    assert portage_version() == "3.0.66"


def test_configured_repos_reads_fake_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fake espelha a forma portage.settings.repositories.prepos (iterável de nomes)
    repositories = SimpleNamespace(prepos=["gentoo", "bentoo", "guru"])
    fake = SimpleNamespace(settings=SimpleNamespace(repositories=repositories))
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", fake)
    assert configured_repos() == ("bentoo", "gentoo", "guru")


def test_module_object_satisfies_require_portage_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # um ModuleType real também é aceito pelo portão
    fake_mod = ModuleType("fake_portage")
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", fake_mod)
    assert require_portage() is fake_mod


# --- T7.2: o caminho recipe/CLI nunca import-aciona portage_api (R7.3) -------
#
# INTEGRAÇÃO: exercita o caminho livre de Portage de ponta a ponta (importar
# ``shidashi.recipe`` e ``shidashi.cli``, rodar ``recipe show``/``validate`` via Typer
# CliRunner sobre uma árvore variants/ em tmp_path) e prova que NADA nesse
# caminho importa ``shidashi.portage_api``. Como esse módulo já está carregado pelo
# topo deste arquivo de teste, removemo-lo de ``sys.modules`` ANTES de exercitar
# o caminho e asseguramos que ele NÃO reaparece depois — isto é, o
# recipe/CLI path não dispara ``import shidashi.portage_api`` (o Portage está
# ausente: o portão jamais é acionado).

_runner = CliRunner()

_BASE_YAML = """\
profile_base: default/linux/amd64/23.0/no-multilib
sets:
  - graphics
  - bentoo-apps
phases:
  - name: rebuild
  - name: desktop
  - name: apps
"""

_ARCH_V3 = """\
arch: v3
tier: 1
runnable_on_build_host: true
"""

# The compile knobs live in the arch layer's make.conf, not in recipe.yaml --
# load_arch() reads them from here (single source of truth).
_ARCH_V3_MAKE_CONF = """\
COMMON_FLAGS="-O2 -march=x86-64-v3 -pipe"
GOAMD64="v3"
RUSTFLAGS="-C target-cpu=x86-64-v3"
CPU_FLAGS_X86="sse4_2 avx2"
"""

_FLAVOR_MINIMAL = """\
flavor: minimal
sets: []
override_ok: true
"""

_INIT_SYSTEMD = """\
init: systemd
profile_suffix: systemd
use_prefer:
  add: [systemd]
"""

_RECIPES = {
    ("flavor", "minimal"): _FLAVOR_MINIMAL,
    ("arch", "v3"): _ARCH_V3,
    ("init", "systemd"): _INIT_SYSTEMD,
}


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Monta uma árvore variants/ mínima e aponta SHIDASHI_VARIANTS_DIR para ela.

    Espelha o padrão de fixture de tests/test_cli.py (subconjunto suficiente
    para um merge limpo de ``v3 × minimal × systemd``).
    """
    root = tmp_path / "variants"
    base = root / "base" / "base.yaml"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(_BASE_YAML, encoding="utf-8")
    for (axis, name), text in _RECIPES.items():
        recipe = root / axis / name / "recipe.yaml"
        recipe.parent.mkdir(parents=True, exist_ok=True)
        recipe.write_text(text, encoding="utf-8")
        if axis == "arch":
            make_conf = recipe.parent / "portage" / "make.conf"
            make_conf.parent.mkdir(parents=True, exist_ok=True)
            make_conf.write_text(_ARCH_V3_MAKE_CONF, encoding="utf-8")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    return root


def test_recipe_cli_path_never_imports_portage_api(variants_tree: Path) -> None:
    # parte de um estado em que portage_api NÃO está carregado: removemos o
    # módulo (e o pacote-pai, para garantir que um re-import de shidashi não o puxe).
    # Reimportar shidashi.recipe cria NOVAS classes pydantic; se não restaurarmos
    # sys.modules ao final, testes posteriores (ex.: test_resolve) que importam
    # essas classes em momentos distintos veem cópias divergentes (model_type).
    # Por isso salvamos e restauramos os módulos afetados num try/finally.
    _names = ("shidashi.portage_api", "shidashi.recipe", "shidashi.cli", "shidashi")
    _saved = {name: sys.modules.get(name) for name in _names}
    try:
        for name in _names:
            sys.modules.pop(name, None)
        assert "shidashi.portage_api" not in sys.modules

        # importar a camada de receitas e a CLI NÃO deve acionar portage_api
        import shidashi.cli as cli
        import shidashi.recipe as recipe

        assert "shidashi.portage_api" not in sys.modules

        # exercita o merge diretamente pela camada de receitas (Portage ausente)
        resolved = recipe.merge(
            recipe.load_base(_BASE_PATH(variants_tree)),
            recipe.load_arch(_RECIPE_PATH(variants_tree, "arch", "v3")),
            recipe.load_flavor(_RECIPE_PATH(variants_tree, "flavor", "minimal")),
            recipe.load_init(_RECIPE_PATH(variants_tree, "init", "systemd")),
        )
        assert resolved.arch == "v3"
        assert "shidashi.portage_api" not in sys.modules

        # exercita o caminho da CLI: recipe show / validate saem com 0 sem Portage
        show = _runner.invoke(cli.app, ["recipe", "show", "v3", "minimal", "systemd"])
        assert show.exit_code == 0, show.stdout
        validate = _runner.invoke(cli.app, ["recipe", "validate", "v3", "minimal", "systemd"])
        assert validate.exit_code == 0, validate.stdout

        # prova central de R7.3: nenhum passo do caminho recipe/CLI importou
        # portage_api (o módulo continua fora de sys.modules)
        assert "shidashi.portage_api" not in sys.modules
    finally:
        # restaura os módulos originais para não poluir o resto da suíte
        for name, module in _saved.items():
            if module is not None:
                sys.modules[name] = module
            else:
                sys.modules.pop(name, None)


def _BASE_PATH(root: Path) -> Path:
    return root / "base" / "base.yaml"


def _RECIPE_PATH(root: Path, axis: str, name: str) -> Path:
    return root / axis / name / "recipe.yaml"
