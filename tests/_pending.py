"""Tolerant import helper for the Red tests of story 003.

Lets test modules whose contract does not exist in production yet
(symbols missing from ``shidashi.*``) be *collectable*: the import does not abort
collection of the whole pytest run; instead each test fails (Red) at the point of use
with a clear message naming the pending symbol. When the implementation
lands, ``try_import`` returns the real object and the tests pass (Green).
"""

from typing import Any


class _Pending:
    """Sentinel that fails when used, naming the symbol that does not exist yet."""

    def __init__(self, module: str, name: str) -> None:
        self._module = module
        self._name = name

    def _fail(self) -> Any:
        raise AssertionError(
            f"pending symbol: {self._module}.{self._name} not implemented yet "
            "(story 003 — expected Red)"
        )

    def __call__(self, *_a: object, **_k: object) -> Any:
        return self._fail()

    def __getattr__(self, _attr: str) -> Any:
        return self._fail()


def try_import(module: str, name: str) -> Any:
    """Return ``module.name`` if it exists; otherwise a ``_Pending`` sentinel."""
    try:
        mod = __import__(module, fromlist=[name])
    except ImportError:
        return _Pending(module, name)
    return getattr(mod, name, _Pending(module, name))
