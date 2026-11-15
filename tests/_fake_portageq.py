"""A fake ``portageq`` in the installed script's shape, for every provider-resolver test.

Command functions, the ``commands`` list of a ``__main__`` module's functions, ``main(argv)``
and the ``__main__`` guard, as in Portage's own script: the per-query loop runs it from
PATH, and :mod:`shidashi.portageq_batch` loads it with runpy. It answers from the JSON
object in ``FAKE_PORTAGEQ``, ``{query: [cpv, slot]}``; a query starting with ``!`` is an
invalid atom (exit 2), as Portage treats one.
"""

import sys
from collections.abc import Sequence
from pathlib import Path

FAKE_PORTAGEQ_SCRIPT = """
import json, os, sys, types
answers = json.loads(os.environ["FAKE_PORTAGEQ"])


def best_visible(argv):
    if argv[-1].startswith("!"):
        sys.stderr.write(f"ERROR: Invalid atom: '{argv[-1]}'\\n")
        return 2
    hit = answers.get(argv[-1])
    print(hit[0] if hit else "")  # the real one prints an empty line and exits 1
    return 0 if hit else 1


def metadata(argv):
    slots = {cpv: slot for cpv, slot in answers.values()}
    if argv[-1] == "SLOT" and argv[-2] in slots:
        print(slots[argv[-2]])
        return None
    return 1


non_commands = frozenset(["main"])
commands = sorted(
    k for k, v in globals().items()
    if k not in non_commands and isinstance(v, types.FunctionType) and v.__module__ == "__main__"
)


def main(argv):
    args = argv[1:]
    if not args or args[0] not in commands:
        sys.exit(64)
    retval = globals()[args[0]](args[1:])
    if retval:
        sys.exit(retval)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
"""


def install_fake_portageq(bindir: Path) -> Path:
    """Write the fake as ``bindir/portageq``, executable by this interpreter; its path."""
    bindir.mkdir(parents=True, exist_ok=True)
    tool = bindir / "portageq"
    tool.write_text(f"#!{sys.executable}\n{FAKE_PORTAGEQ_SCRIPT}", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def with_stage_portageq(argv: Sequence[str], script: Path) -> list[str]:
    """``argv``, with ``script`` as the portageq the one-interpreter resolver loads when the
    production code leaves that to the stage (an empty argument): in a fake stage, the
    stage's own portageq is the fake. Any other command is returned as it is."""
    command = list(argv)
    if command[:4] == ["python3", "-X", "utf8", "-c"] and len(command) > 5 and not command[5]:
        command[5] = str(script)
    return command
