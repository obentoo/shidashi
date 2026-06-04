"""Pacote de testes do Shidashi.

Torna ``tests`` um pacote real para que os imports já usados pelos testes
(``from tests._pending import try_import``, ``from tests.test_merge import …``)
resolvam de forma canônica — e para que ``mypy .`` mapeie cada arquivo a um
único nome de módulo (``tests.test_*``) em vez de descobri-lo duas vezes.
"""
