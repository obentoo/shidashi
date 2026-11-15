"""What a stage does with its stale binpkgs: compile a slot, or move them aside at once.

The ``stale-binpkgs`` step (:func:`shidashi.phases.stale_binpkgs`, story 019) finds the
binpkgs built against a library the stage no longer installs. A version that still has a
fresh binpkg just loses its stale ones. A version whose binpkgs are ALL stale has to be
compiled, and ``emerge --usepkg-exclude`` is how the emerge is told so -- but that option
drops every binpkg its atom matches, so what is excluded, and for how long, decides how
much the stage recompiles.

The rule here, per SLOT, replaced a per-package one (2026-11-15). Then, the whole package
was excluded until a fresh instance of the stale VERSION appeared. An old version the tree
no longer picks is never rebuilt, so it never got that instance and never left the pool:
``dev-lang/python-3.14.10`` and ``-3.15.2``, built against OpenSSL's old subslot, kept
``dev-lang/python`` excluded in every stage of every build, and the current
``python-3.15.2_p1``, which had fresh binpkgs, was compiled each time (six identical
builds in the pool, one per run). In one toolbox run 148 packages were excluded and 76 of
them had a fresh binpkg of some version.

So the tree is asked which version it picks in the slot of each all-stale version
(``portageq best_visible cat/pkg:SLOT``, under the stage's configuration):

* it picks THAT version → only the slot atom ``cat/pkg:SLOT`` is excluded (``man emerge``:
  ``--usepkg-exclude`` takes "package names or slot atoms"; proven against Portage 3.0.83,
  where ``dev-lang/python:3.14`` left the ``:3.15`` binpkg merged), and its stale
  instances move aside after the emerge, once the rebuild wrote a fresh one;
* it picks another version, or nothing is visible there (superseded, masked, removed) →
  nothing will ever rebuild it: its stale instances move aside NOW and nothing is excluded.
  If the emerge still wants that version (a dependency pins it) it finds no binpkg and
  compiles it, which is right; merging the stale one would not be.

:func:`plan_stale` is pure; :func:`ask_tree_picks` is PRIVILEGED (one resolver run in the
stage's container). Nothing here imports :mod:`shidashi.phases`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from shidashi import binpkgs


@dataclass(frozen=True)
class Superseded:
    """A stale instance of a version the tree no longer picks in its slot.

    ``tree_picks`` is the cpv the tree picks there instead, ``""`` when nothing is visible.
    """

    instance: binpkgs.Instance
    slot_atom: str
    tree_picks: str


@dataclass(frozen=True)
class StalePlan:
    """What the stage does with its stale binpkgs (design D2; see the module).

    ``exclude``: the slot atoms kept from the emerge with ``--usepkg-exclude``, so what
    the emerge picks there is compiled. ``quarantine_now``: the stale instances moved
    aside BEFORE the emerge -- those beside a fresh one, and those of a version the tree
    no longer picks (``superseded``, the same instances with what the tree picks
    instead). ``quarantine_after``: the stale instances of the excluded slots' picked
    versions, moved once the rebuild wrote a fresh one.
    """

    exclude: tuple[str, ...] = ()
    quarantine_now: tuple[binpkgs.Instance, ...] = ()
    quarantine_after: tuple[binpkgs.Instance, ...] = ()
    superseded: tuple[Superseded, ...] = ()


def slot_atom_of(instance: binpkgs.Instance) -> str:
    """``cat/pkg:SLOT`` of an instance: what ``--usepkg-exclude`` and the tree are given. Pure."""
    return f"{binpkgs.package_of(instance.cpv)}:{instance.slot}"


@dataclass(frozen=True)
class _StaleVersion:
    """The stale instances of one version still in the index, and how they were found."""

    cpv: str
    stale: tuple[binpkgs.Instance, ...]
    all_stale: bool
    planned: bool


def _stale_versions(
    found: Sequence[binpkgs.Stale | binpkgs.SonameStale],
    instances: Sequence[binpkgs.Instance],
) -> list[_StaleVersion]:
    """One entry per version with a stale instance in ``instances``, sorted by cpv.

    A version whose stale instances already left the index (moved aside by an earlier
    pass of the same step) has nothing left to decide and is skipped.
    """
    bad = {(f.instance.cpv, f.instance.build_id) for f in found}
    # the soname rule judges only what the stage's plan merges (and its siblings)
    planned = {f.instance.cpv for f in found if isinstance(f, binpkgs.SonameStale)}
    by_cpv: dict[str, list[binpkgs.Instance]] = {}
    for instance in instances:
        by_cpv.setdefault(instance.cpv, []).append(instance)
    versions: list[_StaleVersion] = []
    for cpv in sorted({cpv for cpv, _build in bad}):
        mine = by_cpv.get(cpv, [])
        stale = tuple(i for i in mine if (i.cpv, i.build_id) in bad)
        if stale:
            versions.append(_StaleVersion(cpv, stale, len(stale) == len(mine), cpv in planned))
    return versions


def tree_pick_queries(
    found: Sequence[binpkgs.Stale | binpkgs.SonameStale],
    instances: Sequence[binpkgs.Instance],
) -> list[str]:
    """The slot atoms of the versions whose instances are all stale, sorted. Pure.

    Only these are asked of the tree: a version with a fresh instance is decided without it.
    """
    return sorted(
        {
            slot_atom_of(instance)
            for version in _stale_versions(found, instances)
            if version.all_stale
            for instance in version.stale
        }
    )


def plan_stale(
    found: Sequence[binpkgs.Stale | binpkgs.SonameStale],
    instances: Sequence[binpkgs.Instance],
    tree_picks: Mapping[str, str],
) -> StalePlan:
    """Split the stale instances by whether their VERSION has a fresh one and, when it
    has none, by whether the tree still picks that version in its slot. Pure.

    ``tree_picks`` maps a slot atom (:func:`tree_pick_queries`) to the cpv the tree picks
    there, ``""`` for nothing visible. A fresh instance of another version does not rescue
    a stale one: the emerge picks the version, ``--usepkg-exclude`` only decides binpkg or
    source. GOTCHA: the exclusion drops every instance in the slot, so a version that
    still has a fresh instance is never excluded -- its stale ones just move aside.

    A slot the tree was not asked about is treated as picked: nothing moves aside for
    good without the tree's answer.

    One superseded version still excludes its slot: one the soname rule found, so one the
    stage's own plan merges (a dependency pins it, or its binpkg outlived its ebuild).
    With its binpkgs gone the emerge may take ANOTHER version's binpkg in that slot, which
    the soname rule never judged; compiled this once, it cannot be a stale one. The stale
    instances are gone by then, so the exclusion does not come back in the next run.
    """
    now: list[binpkgs.Instance] = []
    after: list[binpkgs.Instance] = []
    superseded: list[Superseded] = []
    exclude: set[str] = set()
    for version in _stale_versions(found, instances):
        if not version.all_stale:
            now.extend(version.stale)
            continue
        for instance in version.stale:
            atom = slot_atom_of(instance)
            picked = tree_picks.get(atom, version.cpv)
            if picked == version.cpv:
                exclude.add(atom)
                after.append(instance)
                continue
            now.append(instance)
            superseded.append(Superseded(instance, atom, picked))
            if version.planned:
                exclude.add(atom)
    return StalePlan(tuple(sorted(exclude)), tuple(now), tuple(after), tuple(superseded))


def ask_tree_picks(
    container: binpkgs.Runner,
    found: Sequence[binpkgs.Stale | binpkgs.SonameStale],
    instances: Sequence[binpkgs.Instance],
    providers: Mapping[str, str],
    *,
    known: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """``known`` plus the tree's pick for every slot of :func:`tree_pick_queries` it
    lacks. PRIVILEGED: one resolver run in ``container``, none when nothing new is asked.

    ``providers`` is what the stage's tree already answered
    (:func:`shidashi.binpkgs.resolve_providers`): one of its resolved queries is asked
    again as the run's witness that Portage still answers
    (:func:`shidashi.binpkgs.resolve_tree_picks`). Raises
    :class:`~shidashi.binpkgs.BinpkgError`; the caller wraps it.
    """
    picks = dict(known or {})
    asked = [atom for atom in tree_pick_queries(found, instances) if atom not in picks]
    if asked:
        witness = next((query for query in sorted(providers) if providers[query]), "")
        picks.update(binpkgs.resolve_tree_picks(container, asked, known_visible=witness))
    return picks


def superseded_audit(entries: Sequence[Superseded]) -> list[dict[str, object]]:
    """The step's ``superseded`` audit field: each instance moved aside because the tree
    picks another version in its slot, with that version (``None``: nothing visible).
    One entry per binpkg file, sorted. Pure."""
    by_path = {entry.instance.path: entry for entry in entries}
    return [
        {
            "cpv": entry.instance.cpv,
            "build_id": entry.instance.build_id,
            "slot": entry.slot_atom,
            "tree_picks": entry.tree_picks or None,
        }
        for _path, entry in sorted(by_path.items())
    ]
