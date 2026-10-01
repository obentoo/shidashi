"""UNIT tests of the Phase 0/1 modules for import-safety and skeletons (R9.1–R9.3).

The ``container``/``factory``/``phases``/``assembler``/``image`` modules have already been
implemented (stories 002/003 and Phase 1) and their tests live in the respective
``tests/test_*.py``; ``binhost`` remains a skeleton (Phase 2), with every body
raising ``NotImplementedError``. Here we prove that:

* every module imports without an exception;
* every entry point **still a skeleton** (``binhost``), when invoked,
  raises ``NotImplementedError`` — not ``pass``/``None``.

The entries are iterated explicitly (one table per module), without
magic introspection, so that an oversight (a body with ``pass``) fails here.
"""

import importlib
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


# --- every skeleton module imports without an exception (R9.1) --------------


@pytest.mark.parametrize("module_name", _SKELETON_MODULES)
def test_skeleton_module_imports(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module is not None


# --- every public entry point raises NotImplementedError (R9.2/R9.3) --------
#
# Each item: (readable label, callable factory, positional args). The
# callable factory is resolved at test time (lazily) so that pytest collection
# does not import the modules ahead of time. ``_P`` is a sentinel Path
# (no body ever touches it: they all raise first).

_P = Path("/nonexistent")


def _entry_points() -> Iterator[tuple[str, Callable[[], Any]]]:
    binhost = importlib.import_module("shidashi.binhost")

    # NB: container/factory/phases (stories 002/003) and assembler/image (Phase 1)
    # are no longer stubs; their tests live in tests/test_container.py,
    # tests/test_factory.py, tests/test_phases.py, tests/test_assembler.py and
    # tests/test_image.py. That is why they no longer appear in the entry points below.

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
