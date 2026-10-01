"""Shidashi test package.

Makes ``tests`` a real package so that the imports the tests already use
(``from tests._pending import try_import``, ``from tests.test_merge import …``)
resolve canonically — and so that ``mypy .`` maps each file to a
single module name (``tests.test_*``) instead of discovering it twice.
"""
