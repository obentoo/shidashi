"""The README's weekly section says what ``weekly`` deletes and which VM it stops (story 032,
task 12.2; R6.1, R4.6, Q15).

Contract (story.md R6.1, Q15 "the README's release section names ... that ``weekly`` deletes
``.pruned/`` entries beyond the 4 newest"): the section "The weekly release on the builder VM"
names the real prune call (``--purge-pruned``), says that ``.pruned/`` entries beyond 4 are
deleted, and says the VM is stopped only when the cycle booted it.

The wording is the README's to choose: each check accepts a few phrasings, case-insensitive,
across line breaks.
"""

import re

from tests._fake_builder_guest import CHECKOUT

WEEKLY_README = CHECKOUT / "README.md"
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def weekly_section() -> str:
    """The text of the README section whose heading names the weekly release, whitespace
    collapsed; headings inside fenced code are not headings."""
    lines = WEEKLY_README.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    level = 0
    fenced = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        heading = None if fenced else HEADING.match(line)
        if heading and level and len(heading.group(1)) <= level:
            break
        if level:
            body.append(line)
        elif heading and re.search(r"\bweekly release\b", heading.group(2), re.IGNORECASE):
            level = len(heading.group(1))
    assert level, "README.md has no section on the weekly release"
    return " ".join(" ".join(body).split())


def test_readme_weekly_section_names_the_purge_flag() -> None:
    assert "--purge-pruned" in weekly_section(), "the weekly section never names --purge-pruned"


DELETES = re.compile(r"\b(delet\w*|remov\w*|purged?\b|purges\b|erase\w*)", re.IGNORECASE)
FOUR = re.compile(r"\b(4|four)\b", re.IGNORECASE)


def test_readme_weekly_section_says_pruned_entries_beyond_four_are_deleted() -> None:
    text = weekly_section()
    # The flag's own name is not the sentence saying what it does.
    prose = text.replace("--purge-pruned", " ")
    windows = [
        prose[max(0, m.start() - 160) : m.end() + 200] for m in re.finditer(r"\.pruned\b", prose)
    ]
    assert windows, "the weekly section never names .pruned/"
    assert any(DELETES.search(w) and FOUR.search(w) for w in windows), (
        f"the weekly section does not say .pruned/ entries beyond 4 are deleted: {windows}"
    )


VM_STOP_WORDINGS = (
    # "stops the VM only when it booted it", "stopped only if this cycle started it"
    re.compile(
        r"\bstop\w*\b[^.]{0,80}\bonly\b[^.]{0,60}\b(boot\w*|start\w*|brought\s+(it\s+)?up)",
        re.IGNORECASE,
    ),
    # "a VM that was already up is left running", "already running ... stays up"
    re.compile(
        r"\balready\s+(up|running)\b[^.]{0,120}\b(left|leaves|keeps?|stays?|remains?|survives?)\b",
        re.IGNORECASE,
    ),
    # "leaves a VM that was already up running"
    re.compile(
        r"\b(left|leaves|keeps?)\b[^.]{0,80}\balready\s+(up|running)\b",
        re.IGNORECASE,
    ),
)


def test_readme_weekly_section_says_the_vm_is_stopped_only_when_the_cycle_booted_it() -> None:
    text = weekly_section()
    assert any(w.search(text) for w in VM_STOP_WORDINGS), (
        "the weekly section does not say the VM is stopped only when the cycle booted it "
        "(a VM that was already up is left running)"
    )
