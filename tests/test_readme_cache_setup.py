"""The README documents the one-time host cache setup (story 010, task 8.2; R6.5, Q9).

Host builds run as root and ``shidashi worker`` as the user: without a shared group,
setgid directories and a default group ACL on the cache and runs directories, a
first pull stops on a root-owned path (R6.5 refuses before any transfer).
"""

import re
from pathlib import Path

import shidashi

#: The checkout's README, found from the package (the test runs from tests/ and from
#: a draft directory alike).
README = Path(shidashi.__file__).resolve().parents[1] / "README.md"
DIRS = ("/var/cache/shidashi", "/var/log/shidashi")
SETUP_WORDS = ("chgrp", "setfacl", "g+s")


def _section(text: str, title: str) -> str:
    """The body of the markdown section headed ``title``, up to the next heading of
    the same or a higher level (headings inside fenced code blocks do not count)."""
    lines = text.splitlines()
    start = level = None
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else re.match(r"^(#+)\s+(.*?)\s*$", line)
        if not m:
            continue
        if start is None:
            if m.group(2) == title:
                start, level = i + 1, len(m.group(1))
        elif len(m.group(1)) <= (level or 0):
            return "\n".join(lines[start:i])
    assert start is not None, f"README has no section {title!r}"
    return "\n".join(lines[start:])


def _setup_text(section: str) -> str:
    """The setup commands: the fenced blocks that carry them, or else the lines."""
    blocks = re.findall(r"```[^\n]*\n(.*?)```", section, flags=re.S)
    chosen = [b for b in blocks if any(w in b for w in SETUP_WORDS)]
    if chosen:
        return "\n".join(chosen)
    return "\n".join(line for line in section.splitlines() if any(w in line for w in SETUP_WORDS))


def test_readme_documents_the_cache_setup_in_using_a_worker() -> None:
    section = _section(README.read_text(encoding="utf-8"), "Using a worker")
    setup = _setup_text(section)
    assert "chgrp -R" in setup, "no recursive chgrp to the shared group"
    assert "g+s" in setup, "no setgid on the directories"
    assert "setfacl" in setup, "no default group ACL"
    # both directories in the setup itself -- a mention elsewhere in the section
    # (where the data lives, a results path) does not set either of them up
    for d in DIRS:
        assert d in setup, f"the setup does not cover {d}"
