"""Helper de import tolerante para testes Red da story 003.

Permite que módulos de teste cujo contrato ainda não existe em produção
(símbolos ausentes em ``kaji.*``) sejam *coletáveis*: a importação não aborta a
coleção do pytest inteiro; em vez disso cada teste falha (Red) no ponto de uso
com uma mensagem clara apontando o símbolo pendente. Quando a implementação
chega, ``try_import`` devolve o objeto real e os testes passam (Green).
"""

from typing import Any


class _Pending:
    """Sentinela que falha ao ser usada, nomeando o símbolo ainda inexistente."""

    def __init__(self, module: str, name: str) -> None:
        self._module = module
        self._name = name

    def _fail(self) -> Any:
        raise AssertionError(
            f"símbolo pendente: {self._module}.{self._name} ainda não implementado "
            "(story 003 — Red esperado)"
        )

    def __call__(self, *_a: object, **_k: object) -> Any:
        return self._fail()

    def __getattr__(self, _attr: str) -> Any:
        return self._fail()


def try_import(module: str, name: str) -> Any:
    """Devolve ``module.name`` se existir; senão um sentinela ``_Pending``."""
    try:
        mod = __import__(module, fromlist=[name])
    except ImportError:
        return _Pending(module, name)
    return getattr(mod, name, _Pending(module, name))
