"""Testes UNITÁRIOS dos módulos da Fase 0/1 quanto a import-safety e esqueletos (R9.1–R9.3).

Os módulos ``container``/``factory``/``phases``/``assembler``/``image`` já foram
implementados (stories 002/003 e Fase 1) e seus testes vivem nos respectivos
``tests/test_*.py``; ``binhost`` permanece esqueleto (Fase 2), com todo corpo
levantando ``NotImplementedError``. Aqui prova-se que:

* cada módulo importa sem exceção (e o pacote permanece import-safe — nenhum
  deles aciona ``shidashi.portage_api`` no import);
* cada ponto de entrada **ainda esqueleto** (``binhost``), quando invocado,
  levanta ``NotImplementedError`` — não ``pass``/``None``.

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
    "shidashi.container",
    "shidashi.factory",
    "shidashi.assembler",
    "shidashi.phases",
    "shidashi.binhost",
    "shidashi.image",
)


# --- cada módulo-esqueleto importa sem exceção (R9.1) ------------------------


@pytest.mark.parametrize("module_name", _SKELETON_MODULES)
def test_skeleton_module_imports(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module is not None


def test_importing_skeletons_does_not_trigger_portage_api() -> None:
    # remove portage_api e re-importa cada esqueleto: nenhum pode puxá-lo (R9.1)
    sys.modules.pop("shidashi.portage_api", None)
    for name in _SKELETON_MODULES:
        importlib.import_module(name)
    assert "shidashi.portage_api" not in sys.modules


# --- cada ponto de entrada público levanta NotImplementedError (R9.2/R9.3) ---
#
# Cada item: (rótulo legível, fábrica-de-callable, args posicionais). A
# fábrica-de-callable é resolvida na hora do teste (lazy) para que a coleção do
# pytest não importe os módulos antecipadamente. ``_P`` é um Path sentinela
# (nenhum corpo chega a tocá-lo: todos levantam antes).

_P = Path("/nonexistent")


def _entry_points() -> Iterator[tuple[str, Callable[[], Any]]]:
    binhost = importlib.import_module("shidashi.binhost")

    # NB: container/factory/phases (stories 002/003) e assembler/image (Fase 1)
    # deixaram de ser stubs; seus testes vivem em tests/test_container.py,
    # tests/test_factory.py, tests/test_phases.py, tests/test_assembler.py e
    # tests/test_image.py. Por isso não figuram mais nos entry-points abaixo.

    yield "binhost.BinpkgRef", lambda: binhost.BinpkgRef("cat/pkg-1", (), 1)
    yield "binhost.Binhost", lambda: binhost.Binhost(_P, "v3")


_ENTRY_POINTS = list(_entry_points())


@pytest.mark.parametrize(
    "call",
    [call for _, call in _ENTRY_POINTS],
    ids=[label for label, _ in _ENTRY_POINTS],
)
def test_entry_point_raises_not_implemented(call: Callable[[], Any]) -> None:
    with pytest.raises(NotImplementedError):
        call()
