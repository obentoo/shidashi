"""Testes UNITÁRIOS dos módulos-esqueleto da Fase 0 (R9.1, R9.2, R9.3).

Os seis módulos ``container``/``factory``/``assembler``/``phases``/``binhost``/
``image`` existem com assinaturas públicas tipadas, mas todo corpo levanta
``NotImplementedError`` na Fase 0. Aqui prova-se que:

* cada módulo importa sem exceção (e o pacote permanece import-safe — nenhum
  deles aciona ``kaji.portage_api`` no import);
* cada ponto de entrada público (construtores de classe e funções de módulo),
  quando invocado, levanta ``NotImplementedError`` — não ``pass``/``None``.

As entradas são iteradas explicitamente (uma tabela por módulo), sem
introspecção mágica, para que um esquecimento (corpo com ``pass``) falhe aqui.
"""

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

_SKELETON_MODULES = (
    "kaji.container",
    "kaji.factory",
    "kaji.assembler",
    "kaji.phases",
    "kaji.binhost",
    "kaji.image",
)


# --- cada módulo-esqueleto importa sem exceção (R9.1) ------------------------


@pytest.mark.parametrize("module_name", _SKELETON_MODULES)
def test_skeleton_module_imports(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module is not None


def test_importing_skeletons_does_not_trigger_portage_api() -> None:
    # remove portage_api e re-importa cada esqueleto: nenhum pode puxá-lo (R9.1)
    sys.modules.pop("kaji.portage_api", None)
    for name in _SKELETON_MODULES:
        importlib.import_module(name)
    assert "kaji.portage_api" not in sys.modules


# --- cada ponto de entrada público levanta NotImplementedError (R9.2/R9.3) ---
#
# Cada item: (rótulo legível, fábrica-de-callable, args posicionais). A
# fábrica-de-callable é resolvida na hora do teste (lazy) para que a coleção do
# pytest não importe os módulos antecipadamente. ``_P`` é um Path sentinela
# (nenhum corpo chega a tocá-lo: todos levantam antes).

_P = Path("/nonexistent")


def _resolved_recipe_stub() -> Any:
    # ResolvedRecipe é um modelo pydantic frozen; para alimentar assinaturas que
    # o exigem basta um objeto qualquer — os corpos levantam antes de usá-lo.
    return object()


def _entry_points() -> Iterator[tuple[str, Callable[[], Any]]]:
    factory = importlib.import_module("kaji.factory")
    assembler = importlib.import_module("kaji.assembler")
    phases = importlib.import_module("kaji.phases")
    binhost = importlib.import_module("kaji.binhost")
    image = importlib.import_module("kaji.image")

    rr = _resolved_recipe_stub()

    # NB: container.CommandResult/Container deixaram de ser stubs — a story 002
    # (tarefa 3) preencheu o wrapper systemd-nspawn; seus testes vivem agora em
    # tests/test_container.py. Por isso não figuram mais nos entry-points abaixo.

    yield "factory.FactoryResult", lambda: factory.FactoryResult(_P, ())
    yield "factory.Factory", lambda: factory.Factory(rr, _P)

    yield "assembler.Assembler", lambda: assembler.Assembler(rr, _P)

    yield "phases.PhaseResult", lambda: phases.PhaseResult(object(), None)
    yield "phases.run_phase", lambda: phases.run_phase(object(), rr, object())
    yield "phases.run_phases", lambda: phases.run_phases(object(), rr)
    # NB: phases.fork_point deixou de ser stub — a story 003 (tarefa 3.4) o
    # implementou como decisão de reuso pura; seus testes vivem agora em
    # tests/test_phases.py. Por isso não figura mais nos entry-points acima.

    yield "binhost.BinpkgRef", lambda: binhost.BinpkgRef("cat/pkg-1", (), 1)
    yield "binhost.Binhost", lambda: binhost.Binhost(_P, "v3")

    yield "image.make_squashfs", lambda: image.make_squashfs(_P, _P)
    yield "image.build_iso", lambda: image.build_iso(_P, _P)


_ENTRY_POINTS = list(_entry_points())


@pytest.mark.parametrize(
    "call",
    [call for _, call in _ENTRY_POINTS],
    ids=[label for label, _ in _ENTRY_POINTS],
)
def test_entry_point_raises_not_implemented(call: Callable[[], Any]) -> None:
    with pytest.raises(NotImplementedError):
        call()
