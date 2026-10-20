"""UNIT tests of the Phase 0/1 modules for import-safety (R9.1).

Every module of the original skeleton now has a body (``binhost`` since story 029) and
its tests live in the respective ``tests/test_*.py``. Here we prove only that every
module imports without an exception.
"""

import importlib

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
