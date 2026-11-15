"""Stale binpkgs: what the binhost index says a binpkg was built against (story 019, D1).

A binpkg records two things about the libraries it was linked with, and both can
outlive the pinned tree:

* its built slot-operator dependencies -- ``>=dev-cpp/simdutf-6.2.0:0/34=`` in its
  ``RDEPEND``: the provider's subslot at build time;
* its soname dependencies -- ``REQUIRES: x86_64: libsimdutf.so.34``: the libraries
  it actually loads.

The subslot is what Portage compares, and only over an installed provider; the
soname is the truth when an ebuild keeps its subslot across a soname change (the
pinned overlay's ``simdutf-9.2.1``: ``SLOT 0/34``, ``libsimdutf.so.36``). This
module reads both from the ``Packages`` index and judges them. Everything here is
PURE, except :func:`resolve_providers` and :func:`resolve_tree_picks`, which are
PRIVILEGED: one run each in the stage's container (two when its portageq does not load in
one interpreter). It imports nothing from :mod:`shidashi.phases` or
:mod:`shidashi.assembler`; each caller wraps :class:`BinpkgError` in its own error.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from shidashi import audit, portageq_batch
from shidashi.container import CommandResult

#: ``[op]cat/pkg[-version]:SLOT[/SUBSLOT]=[use]`` -- a built slot operator.
_SLOT_DEP = re.compile(
    r"^(?P<head>[<>=~]*[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.*-]+)"
    r":(?P<slot>[A-Za-z0-9+_.-]+)(?:/(?P<subslot>[A-Za-z0-9+_.-]+))?="
    r"(?:\[[^\]]*\])?$"
)

#: A package version: ``-6.2.0``, ``-1.0_p20260922``, ``-9.0.0-r1``.
_VERSION = re.compile(r"-\d+(?:\.\d+)*[a-z]?(?:_(?:alpha|beta|pre|rc|p)\d*)*(?:-r\d+)?(?:\*)?$")


@dataclass(frozen=True)
class SlotDep:
    """One built slot-operator dependency of a binpkg.

    ``query`` is the atom without ``/SUBSLOT``, ``=`` or a USE dependency: what
    ``portageq best_visible`` is asked (kept, it would filter by the very subslot
    whose change is being looked for). ``subslot`` is ``slot`` for ``:SLOT=``.
    """

    atom: str
    cp: str
    slot: str
    subslot: str
    query: str


@dataclass(frozen=True)
class Soname:
    """One soname of a ``REQUIRES``/``PROVIDES`` line, with its ABI category."""

    category: str
    name: str


@dataclass(frozen=True)
class Instance:
    """One entry of the index: one binpkg file (``binpkg-multi-instance``).

    ``slot`` is the entry's ``SLOT`` without its subslot, ``0`` when the entry has none:
    Portage leaves out of the index a field that holds its default.
    """

    cpv: str
    build_id: int
    path: str
    slot_deps: tuple[SlotDep, ...] = ()
    requires: frozenset[Soname] = field(default_factory=frozenset)
    provides: frozenset[Soname] = field(default_factory=frozenset)
    slot: str = "0"


def _cp(head: str) -> str:
    """``cat/pkg`` of an atom's head: operator and version off, the name's ``-`` kept."""
    return _VERSION.sub("", head.lstrip("<>=~"))


def package_of(cpv: str) -> str:
    """``cat/pkg`` of a ``cat/pkg-version``. Pure."""
    return _cp(cpv)


def parse_slot_deps(text: str) -> tuple[SlotDep, ...]:
    """The built slot-operator atoms of a dependency string. Pure.

    ``||``, parentheses, USE conditionals and every other atom are tokens that do
    not match, so they are skipped: only ``:SLOT/SUBSLOT=`` and ``:SLOT=`` count.
    """
    found: list[SlotDep] = []
    for token in text.split():
        match = _SLOT_DEP.match(token)
        if match is None:
            continue
        slot = match["slot"]
        found.append(
            SlotDep(
                atom=token,
                cp=_cp(match["head"]),
                slot=slot,
                subslot=match["subslot"] or slot,
                query=f"{match['head']}:{slot}",
            )
        )
    return tuple(found)


def parse_sonames(text: str) -> frozenset[Soname]:
    """The sonames of a ``REQUIRES``/``PROVIDES`` value. Pure.

    ``x86_32: libc.so.6 x86_64: libc.so.6 libz.so.1``: a token ending in ``:``
    opens a category; a soname before any category has none and is dropped.
    """
    found: set[Soname] = set()
    category = ""
    for token in text.split():
        if token.endswith(":"):
            category = token[:-1]
        elif category:
            found.add(Soname(category=category, name=token))
    return frozenset(found)


def parse_vdb_provides(text: str) -> frozenset[Soname]:
    """The sonames of a rootfs's vdb ``PROVIDES`` files, read as one text. Pure.

    Each file is one ``<category>: <soname> …`` line (``/var/db/pkg/*/*/PROVIDES``,
    absent for a package that provides nothing).
    """
    return frozenset(s for line in text.splitlines() for s in parse_sonames(line))


def _fields(block: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in block.strip().splitlines() if ": " in line)


def parse_index(text: str) -> list[Instance]:
    """One :class:`Instance` per entry of a ``Packages`` index. Pure.

    Entries are split as :func:`shidashi.checkpoint.binhost_slice` splits them; the
    header block has no ``CPV`` and is skipped. A malformed entry (no ``CPV`` or
    ``PATH``, a ``BUILD_ID`` that is not a number) is skipped, never raised: the
    index is written by Portage, and one bad entry must not stop a build.
    """
    instances: list[Instance] = []
    for block in text.split("\n\n"):
        entry = _fields(block)
        cpv, path = entry.get("CPV"), entry.get("PATH")
        build = entry.get("BUILD_ID", "0")
        if not cpv or not path or not build.isdigit():
            continue
        deps = parse_slot_deps(f"{entry.get('RDEPEND', '')} {entry.get('DEPEND', '')}")
        unique = tuple(dict.fromkeys(deps))
        instances.append(
            Instance(
                cpv=cpv,
                build_id=int(build),
                path=path,
                slot_deps=unique,
                requires=parse_sonames(entry.get("REQUIRES", "")),
                provides=parse_sonames(entry.get("PROVIDES", "")),
                slot=(entry.get("SLOT") or "0").split("/", 1)[0],
            )
        )
    return instances


# --- the subslot rule --------------------------------------------------------------


@dataclass(frozen=True)
class Stale:
    """An instance whose slot dep names another subslot than the tree's provider.

    ``built`` and ``tree`` are full SLOTs in one form: ``0/34``, or ``0`` for a slot
    with no subslot -- never ``0/0``.
    """

    instance: Instance
    dep: SlotDep
    built: str
    tree: str


def provider_queries(instances: Iterable[Instance]) -> list[str]:
    """Every provider query of the instances' slot deps, deduplicated and sorted. Pure."""
    return sorted({dep.query for instance in instances for dep in instance.slot_deps})


def _subslot(slot: str) -> str:
    """The subslot of a full SLOT: ``0/36`` → ``36``, ``0`` → ``0``."""
    return slot.split("/", 1)[1] if "/" in slot else slot


def stale(instances: Iterable[Instance], providers: Mapping[str, str]) -> list[Stale]:
    """One :class:`Stale` per slot dep whose provider's subslot differs. Pure.

    ``providers`` maps a query to the full SLOT the pinned tree would install, or
    ``""`` when nothing is visible: such a dep has nothing to compare and is
    :func:`unresolved`, not stale. Sorted by ``(cpv, build_id)``.
    """
    found: list[Stale] = []
    for instance in instances:
        for dep in instance.slot_deps:
            tree = providers.get(dep.query, "")
            if not tree or _subslot(tree) == dep.subslot:
                continue
            built = dep.slot if dep.subslot == dep.slot else f"{dep.slot}/{dep.subslot}"
            found.append(Stale(instance=instance, dep=dep, built=built, tree=tree))
    return sorted(found, key=lambda s: (s.instance.cpv, s.instance.build_id))


def unresolved(instances: Iterable[Instance], providers: Mapping[str, str]) -> list[SlotDep]:
    """The slot deps whose provider the tree does not show (empty or missing). Pure."""
    return [
        dep
        for instance in instances
        for dep in instance.slot_deps
        if not providers.get(dep.query, "")
    ]


# --- the soname rule --------------------------------------------------------------

#: ``libsimdutf.so.34`` → ``libsimdutf.so``: a soname with a numeric version suffix.
_VERSIONED_SONAME = re.compile(r"^(?P<library>.+\.so)\.[0-9][0-9.]*$")


@dataclass(frozen=True)
class SonameStale:
    """A soname an instance requires that its providers offer only at another version."""

    instance: Instance
    needs: Soname
    offered: tuple[str, ...]


def library_of(name: str) -> str | None:
    """The library name of a versioned soname; ``None`` without a version suffix. Pure.

    ``libsimdutf.so.34`` → ``libsimdutf.so``; ``libvte-2.91.so.0`` → ``libvte-2.91.so``.
    """
    match = _VERSIONED_SONAME.match(name)
    return match["library"] if match else None


def _judge(
    planned: Iterable[Instance], pool: Iterable[Soname]
) -> tuple[list[SonameStale], list[tuple[Instance, Soname]]]:
    """Every required soname absent from ``pool``: stale, or unresolved."""
    offered = frozenset(pool)
    versions: dict[tuple[str, str], list[str]] = {}
    for soname in offered:
        library = library_of(soname.name)
        if library is not None:
            versions.setdefault((soname.category, library), []).append(soname.name)
    found: list[SonameStale] = []
    missing: list[tuple[Instance, Soname]] = []
    for instance in planned:
        for needs in sorted(instance.requires, key=lambda s: (s.category, s.name)):
            if needs in offered:
                continue
            library = library_of(needs.name)
            others = versions.get((needs.category, library), []) if library else []
            if others:
                found.append(SonameStale(instance, needs, tuple(sorted(others))))
            else:
                missing.append((instance, needs))
    found.sort(key=lambda s: (s.instance.cpv, s.instance.build_id))
    return found, missing


def soname_stale(planned: Iterable[Instance], pool: Iterable[Soname]) -> list[SonameStale]:
    """The required sonames the pool offers only at another version. Pure (R1.6).

    ``pool`` is what the providers offer: the vdb's ``PROVIDES`` and the planned
    binpkgs' in the factory, the plan's alone in the assemble (``--emptytree``).
    ``libsimdutf.so.34`` against a pool with ``libsimdutf.so.36`` (same category)
    is stale; ``libvte-2.90.so.0`` is another library than ``libvte-2.91.so.0``.
    """
    return _judge(planned, pool)[0]


def unresolved_sonames(
    planned: Iterable[Instance], pool: Iterable[Soname]
) -> list[tuple[Instance, Soname]]:
    """The required sonames whose library the pool offers at no version. Pure (R1.8).

    Recorded, never stale: a soname with no version suffix, a library the pool
    does not carry at all, the same name in another category.
    """
    return _judge(planned, pool)[1]


def describe(
    stale: Sequence[Stale],
    soname_stale: Sequence[SonameStale] = (),
    *,
    arch: str,
    image: str,
    init: str,
) -> str:
    """The refusal's detail: the subslot lines, the soname lines, the rebuild. Pure.

    The only formatter of the refusals of the factory's ``check-binpkgs`` and the
    assemble, so the two cannot word the same defect differently.
    """
    lines = [
        f"{s.instance.cpv} (build {s.instance.build_id}): {s.dep.atom}"
        f" — {s.dep.cp} {s.built} → {s.tree}"
        for s in stale
    ]
    lines += [
        f"{s.instance.cpv} (build {s.instance.build_id}): needs {s.needs.name}"
        f" — offered {', '.join(s.offered)}"
        for s in soname_stale
    ]
    lines.append(f"rebuild: shidashi factory {arch} {image} {init}")
    return "\n".join(lines)


# --- the provider resolver (PRIVILEGED) ------------------------------------------


class BinpkgError(Exception):
    """The resolver could not run: the message and the run's output.

    The module's own error -- the factory wraps it in ``FactoryError``, the
    assembler in ``AssemblerError`` -- so this module imports neither of them.
    """

    def __init__(self, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.output = output


class Runner(Protocol):
    """What the resolver needs of a container: one command run inside it."""

    def run(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, check: bool = True
    ) -> CommandResult: ...


def resolve_script(queries: Sequence[str]) -> str:
    """The bash script that answers every query in one run, ``query<TAB>slot<TAB>cpv``:
    the ``best_visible`` version and its full SLOT, both empty when nothing is visible. Pure.

    Each query is single-quoted, so ``>=`` and ``<`` reach ``portageq`` as atoms,
    never as redirections. ``best_visible`` exits 1 when nothing is visible: that,
    and only that, is an empty answer. Any other failure -- ``portageq`` missing
    (127), an invalid atom (2), ``metadata`` failing on a visible package -- ends
    the run with that code, its diagnostics left on stderr.
    """
    quoted = " ".join(shlex.quote(q) for q in queries)
    return (
        f"for q in {quoted}; do\n"
        '  cpv=$(portageq best_visible / ebuild "$q"); rc=$?\n'
        '  if [ "$rc" -eq 1 ]; then cpv=; elif [ "$rc" -ne 0 ]; then exit "$rc"; fi\n'
        "  slot=\n"
        '  if [ -n "$cpv" ]; then\n'
        '    slot=$(portageq metadata / ebuild "$cpv" SLOT) || exit "$?"\n'
        "  fi\n"
        '  printf \'%s\\t%s\\t%s\\n\' "$q" "$slot" "$cpv"\n'
        "done\n"
    )


def resolve_argv(queries: Sequence[str], *, portageq: str = "") -> list[str]:
    """The command that answers every query with ONE interpreter in the stage: the source
    of :mod:`shidashi.portageq_batch` under ``python3 -X utf8 -c``, then ``portageq`` (the
    script it loads, empty for the stage's own) and one argument per query. Reads that
    module's source.

    ``-X utf8``: portageq re-execs any other interpreter, which under ``-c`` would lose
    the program. One argument per query: no single argument nears the kernel's 128 KiB cap.
    """
    source = Path(portageq_batch.__file__).read_text(encoding="utf-8")
    return ["python3", "-X", "utf8", "-c", source, portageq, *queries]


def _resolve_run(container: Runner, queries: Sequence[str], *, portageq: str) -> CommandResult:
    """:func:`resolve_argv` in the container; :func:`resolve_script`'s per-query loop only
    when that interpreter can load no portageq (an audit event says so: ~90 times slower)."""
    try:
        return container.run(resolve_argv(queries, portageq=portageq), check=True)
    except subprocess.CalledProcessError as err:
        if err.returncode != portageq_batch.PORTAGEQ_UNLOADABLE:
            raise
        cause = " ".join((err.stderr or "").split())
    audit.current().event("binpkgs.portageq_per_query", queries=len(queries), cause=cause)
    return container.run(["bash", "-c", resolve_script(queries)], check=True)


def _ask_tree(
    container: Runner, queries: Sequence[str], *, portageq: str, what: str
) -> tuple[dict[str, tuple[str, str]], str]:
    """One resolver run: ``query → (full SLOT, cpv)``, both ``""`` for a query with no line
    or an unparsable one, and the run's output. A failed run raises :class:`BinpkgError`."""
    answers = dict.fromkeys(queries, ("", ""))
    try:
        result = _resolve_run(container, queries, portageq=portageq)
    except subprocess.CalledProcessError as err:
        output = (err.output or "") + (err.stderr or "")
        raise BinpkgError(f"portageq could not resolve {what}", output=output) from err
    except OSError as err:  # past the kernel's 128 KiB cap on one argument
        raise BinpkgError(f"the script resolving {what} could not start: {err}") from err
    for line in result.stdout.splitlines():
        query, tab, answer = line.partition("\t")
        if tab and query in answers:
            slot, _tab, cpv = answer.partition("\t")
            answers[query] = (slot.strip(), cpv.strip())
    return answers, result.stdout + result.stderr


def resolve_providers(
    container: Runner, queries: Sequence[str], *, portageq: str = ""
) -> dict[str, str]:
    """The full SLOT the stage's tree would install for each query. PRIVILEGED.

    ONE interpreter in the stage's own rootfs answers them all (:func:`resolve_argv`;
    ``portageq``, the script it loads, is only ever given by tests): the answer is the
    pinned tree under that stage's configuration, as one ``portageq`` per query gives it
    (:func:`resolve_script`, still the way when no portageq loads). A query with no
    line, or an unparsable line, answers ``""`` (unresolved). A failed run raises
    :class:`BinpkgError` with its output -- and so does a run in which NO query
    resolves: a broken Portage configuration exits 1 like "nothing visible", and
    taken at its word it would judge every binpkg fresh.
    """
    if not queries:
        return {}
    what = f"the providers of {len(queries)} slot deps"
    answers, output = _ask_tree(container, queries, portageq=portageq, what=what)
    if not any(slot for slot, _cpv in answers.values()):
        raise BinpkgError(
            f"portageq resolved none of {what}: the stage's Portage configuration is likely broken",
            output=output,
        )
    return {query: slot for query, (slot, _cpv) in answers.items()}


def resolve_tree_picks(
    container: Runner, slot_atoms: Sequence[str], *, known_visible: str = "", portageq: str = ""
) -> dict[str, str]:
    """The version the stage's tree picks in each slot: ``cat/pkg:SLOT`` → its
    ``best_visible`` cpv, ``""`` when nothing is visible there. PRIVILEGED: one more run
    of the resolver of :func:`resolve_providers`.

    A slot with nothing visible is an ordinary answer here (a removed or masked package),
    and every slot asked may be one: an all-empty run is NOT refused. What keeps a broken
    Portage configuration from reading as "nothing visible anywhere" -- which would move
    every binpkg asked about aside -- is ``known_visible``: a query this stage already
    answered (a provider), asked again in the same run and refused when IT comes back
    empty. Without one the answers are taken at their word. A failed run raises
    :class:`BinpkgError` either way.
    """
    if not slot_atoms:
        return {}
    what = f"the versions the tree picks in {len(slot_atoms)} slots"
    asked = list(dict.fromkeys([*slot_atoms, *([known_visible] if known_visible else [])]))
    answers, output = _ask_tree(container, asked, portageq=portageq, what=what)
    if known_visible and not answers[known_visible][1]:
        raise BinpkgError(
            f"portageq no longer resolves {known_visible} while asked {what}: the stage's "
            "Portage configuration is likely broken",
            output=output,
        )
    return {atom: answers[atom][1] for atom in slot_atoms}
