"""Check the kit library against the generation's pinned trees.

The kits (``variants/kits/<category>/<set>``) are the library of every package
Bentoo builds -- for its images and, later, for the binhost users install from.
A typo in a kit is otherwise found by an emerge in the middle of a build, or
never: an atom that names nothing simply resolves to nothing. ``shidashi kits
check`` reads every line of every kit and names each atom that no pinned
repository (::gentoo at its snapshot, ::bentoo at its commit) has, and each
``@set`` reference to a kit that does not exist.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from shidashi.resolve import catalog_entry, kit_index

#: Leading operators of a Portage atom (``>=``, ``~``, ``!!``...).
_OPERATOR = re.compile(r"^(!!|!|>=|<=|=|~|<|>)")
#: The version suffix of a versioned atom: ``-<digit>...`` (``-1.2.3-r1``, ``-9999*``).
_VERSION = re.compile(r"-[0-9][^/]*$")


@dataclass(frozen=True)
class KitLine:
    """One atom of one kit, where it is written."""

    kit: str
    line: int
    atom: str


def atom_parts(atom: str) -> tuple[str, str | None]:
    """``(category/package, repository or None)`` of an atom. Pure.

    ``>=dev-lang/rust-1.98:stable[clippy]::gentoo`` -> ``("dev-lang/rust", "gentoo")``.
    A version is stripped only after an operator: ``sys-libs/libstdc++-v3`` has
    no operator and keeps its name whole.
    """
    rest = atom
    repo = None
    if "::" in rest:
        rest, repo = rest.split("::", 1)
    rest = rest.split("[", 1)[0]
    rest = rest.split(":", 1)[0]
    operator = _OPERATOR.match(rest)
    if operator:
        rest = _VERSION.sub("", rest[operator.end() :]).rstrip("*")
    return rest, repo


def iter_kits(kits_dir: Path) -> Iterator[tuple[str, int, str]]:
    """Every token of every kit: ``(kit, line number, token)``, catalog-only lines
    (``#atom``) included -- the binhost builds them, so they are checked too. I/O."""
    for name, path in sorted(kit_index(kits_dir).items()):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            entry = catalog_entry(line)
            token = [entry] if entry is not None else line.split("#", 1)[0].split()
            if token:
                yield name, number, token[0]


def check(kits_dir: Path, repos: Mapping[str, Path]) -> list[str]:
    """Every atom no pinned repository has, every ``@ref`` to no kit. I/O, read-only."""
    kits = set(kit_index(kits_dir))
    problems: list[str] = []
    for kit, number, token in iter_kits(kits_dir):
        where = f"{kit}:{number}"
        if token.startswith("@"):
            if token[1:] not in kits:
                problems.append(f"{where}: {token} names no kit")
            continue
        cp, repo = atom_parts(token)
        if cp.count("/") != 1:
            problems.append(f"{where}: {token} is not category/package")
            continue
        if repo is not None and repo not in repos:
            problems.append(f"{where}: {token} names repository ::{repo}, which is not pinned")
            continue
        candidates = [repos[repo]] if repo is not None else list(repos.values())
        if not any((path / cp).is_dir() for path in candidates):
            where_looked = f"::{repo}" if repo else " or ".join(f"::{r}" for r in repos)
            problems.append(f"{where}: {token} -- no {cp} in {where_looked}")
    problems.extend(_malformed_catalog_lines(kits_dir))
    return problems


def _malformed_catalog_lines(kits_dir: Path) -> list[str]:
    """``#`` glued to text that is no atom: ``#www-client-firefox`` (the ``/``
    lost). Prose has a space after ``#``, so such a line meant ``#atom`` -- and
    without this check it would be a comment, silently left out of the
    catalog. I/O."""
    problems: list[str] = []
    for name, path in sorted(kit_index(kits_dir).items()):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            glued = len(line) > 1 and line[0] == "#" and not line[1].isspace()
            if glued and line[1] != "#" and catalog_entry(line) is None:
                problems.append(
                    f"{name}:{number}: {line} is neither #category/package nor #@kit "
                    "(prose needs a space after #)"
                )
    return problems
