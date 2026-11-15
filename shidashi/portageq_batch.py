"""Many ``portageq`` questions, one interpreter: runs INSIDE a stage, for :mod:`shidashi.binpkgs`.

The stale-binpkgs step asks the stage's tree, for every slot dep of the binhost index, the
SLOT it would install (``portageq best_visible``, then ``portageq metadata ... SLOT``; story
019). One ``portageq`` per question loads Portage's configuration and repositories each
time: the v3 index's 677 queries took ~4 minutes, in every stage of every build (2026-11-15).

This program loads the stage's own ``portageq`` script ONCE and calls its ``main()`` with
each question's command line, so every answer -- visibility, masks, exit codes -- is still
the installed Portage's; only the interpreter is shared. Over this host's Portage 3.0.83,
the 677 answers were byte for byte the per-query loop's, in 2.2 s instead of 203 s.

It runs alone, under the stage's ``python3``: it imports nothing from :mod:`shidashi`, and
:func:`shidashi.binpkgs.resolve_argv` sends its source with ``python3 -X utf8 -c``. Its
arguments: the portageq script to load (empty: the stage's own, in python-exec's layout for
this interpreter), then the queries. Its stdout: what :func:`shidashi.binpkgs.resolve_script`
prints, one ``query<TAB>slot`` line per query, the slot empty when nothing is visible. Any
other portageq failure ends the run with portageq's own exit code, as that script does.
:data:`PORTAGEQ_UNLOADABLE` means no answer at all: the caller asks one portageq per query.
"""

import contextlib
import io
import locale
import runpy
import sys
import types
from collections.abc import Callable, Sequence
from typing import Any, cast

#: No portageq could be loaded here: another python-exec layout, a Portage whose script
#: changed shape. 75 is EX_TEMPFAIL, which no portageq exit uses.
PORTAGEQ_UNLOADABLE = 75

#: The module name the loaded script runs under: anything but ``__main__``, whose guard
#: would run portageq's own ``main(sys.argv)`` -- this program's argv -- on load.
_LOADED_AS = "shidashi_portageq"


def load_portageq_main(script: str) -> Callable[[list[str]], Any]:
    """The ``main`` of the portageq ``script``, its command list rebuilt for this module name.

    portageq lists as commands only the functions of a module named ``__main__``, and
    ``runpy.run_path`` returns a copy of the module's dict: the list is rebuilt in the dict
    ``main`` itself reads. Raises ``LookupError`` when the script lacks what this relies on.
    """
    main = runpy.run_path(script, run_name=_LOADED_AS)["main"]
    live = main.__globals__
    live["commands"] = sorted(
        name
        for name, value in live.items()
        if name not in live["non_commands"]
        and isinstance(value, types.FunctionType)
        and value.__module__ == _LOADED_AS
    )
    if not {"best_visible", "metadata"} <= set(live["commands"]):
        raise LookupError(f"{script} has no best_visible or metadata command")
    return cast(Callable[[list[str]], Any], main)


def _exit_code(value: object) -> int:
    """The exit code ``sys.exit(value)`` ends a process with."""
    if value is None:
        return 0
    return value if isinstance(value, int) else 1


def _ask(main: Callable[[list[str]], Any], argv: list[str]) -> tuple[int, str]:
    """``(exit code, stdout)`` of the command line ``portageq <argv>``: ``sys.exit(main(...))``."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = _exit_code(main(["portageq", *argv]))
    except SystemExit as stop:
        code = _exit_code(stop.code)
    return code, out.getvalue()


def answer_portageq_queries(argv: Sequence[str]) -> int:
    """Print ``query<TAB>slot`` for each query of ``argv[1:]``; the exit code. See the module."""
    utf8 = sys.getfilesystemencoding().lower() == "utf-8"
    if not utf8 or locale.getpreferredencoding(False).lower() != "utf-8":
        # portageq re-execs a non-UTF-8 interpreter with sys.argv, which under ``-c`` no
        # longer holds this program: it would run the first query as code
        print("portageq_batch: not in UTF-8 mode (python3 -X utf8)", file=sys.stderr)
        return PORTAGEQ_UNLOADABLE
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    script = argv[0] if argv and argv[0] else f"/usr/lib/python-exec/python{version}/portageq"
    try:
        main = load_portageq_main(script)
    except Exception as err:  # whatever stops the load, the caller still has the slow way
        print(f"portageq_batch: cannot load {script}: {err!r}", file=sys.stderr)
        return PORTAGEQ_UNLOADABLE
    for query in argv[1:]:
        code, out = _ask(main, ["best_visible", "/", "ebuild", query])
        if code not in (0, 1):
            return code
        # 1 is "nothing visible"; otherwise what $(...) keeps, the output less its newlines
        cpv = out.rstrip("\n") if code == 0 else ""
        slot = ""
        if cpv:
            code, out = _ask(main, ["metadata", "/", "ebuild", cpv, "SLOT"])
            if code != 0:
                return code
            slot = out.rstrip("\n")
        print(f"{query}\t{slot}")
    return 0


if __name__ == "__main__":
    sys.exit(answer_portageq_queries(sys.argv[1:]))
