"""Testes da integração protegida com o Portage (kaji.portage_api).

UNIT: deterministas independentemente do host. O estado do Portage é forçado
via ``monkeypatch`` sobre ``kaji.portage_api`` — nunca dependemos de o host ter
(ou não) ``sys-apps/portage`` instalado. Verifica-se:

* import do módulo nunca levanta e ``PORTAGE_AVAILABLE`` é ``bool`` (R7.1);
* com Portage ausente, ``require_portage`` e os auxiliares de leitura levantam
  :class:`PortageUnavailableError` (R7.2);
* com Portage presente (fake), o portão abre e ``require_portage`` devolve o
  módulo.

NB: a tarefa T7.2 ADICIONARÁ a este arquivo um teste de integração (importar
``kaji.recipe``/``kaji.cli`` com Portage ausente). O arquivo é mantido
extensível; T7.2 não é implementada aqui.
"""

from types import ModuleType, SimpleNamespace

import pytest

import kaji.portage_api as portage_api
from kaji.portage_api import (
    PortageUnavailableError,
    configured_repos,
    portage_version,
    require_portage,
)


@pytest.fixture
def portage_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Força o estado 'host não-Gentoo' em kaji.portage_api."""
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", False)
    monkeypatch.setattr(portage_api, "_portage", None)


# --- import é seguro e PORTAGE_AVAILABLE é bool (R7.1) -----------------------


def test_import_never_raises_and_flag_is_bool() -> None:
    # o módulo já foi importado no topo sem exceção; reforça-se o contrato
    import importlib

    reloaded = importlib.import_module("kaji.portage_api")
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
