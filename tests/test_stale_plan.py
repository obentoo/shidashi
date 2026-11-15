"""The stale-binpkgs plan, per slot (``shidashi.stale_plan``; story 019, D2).

Pure: an index text, what the tree answers for the providers, and what it picks in each
slot asked. The measured case is the build host's of 2026-11-15: ``dev-lang/python-3.14.10``
and ``-3.15.2``, built against OpenSSL's old subslot and never rebuilt, kept the whole
package excluded, so ``python-3.15.2_p1`` -- which had a fresh binpkg -- was compiled in
every run.
"""

from shidashi import binpkgs, soname_waivers
from shidashi.stale_plan import (
    StalePlan,
    Superseded,
    plan_stale,
    slot_atom_of,
    superseded_audit,
    tree_pick_queries,
)

HEADER = "ARCH: amd64\nVERSION: 0\n"
OPENSSL = ">=dev-libs/openssl-1.1.1:0"
SIMDUTF = ">=dev-cpp/simdutf-6.2.0:0"
#: the tree's providers: OpenSSL 4 (``0/4``), simdutf ``0/36``
PROVIDERS = {OPENSSL: "0/4", SIMDUTF: "0/36"}


def _entry(
    cpv: str,
    build: int,
    *,
    slot: str = "",
    rdepend: str = "",
    requires: str = "",
    provides: str = "",
) -> str:
    category, name = cpv.split("/")
    package = binpkgs.package_of(cpv).split("/")[1]
    lines = [
        f"BUILD_ID: {build}",
        f"CPV: {cpv}",
        f"PATH: {category}/{package}/{name}-{build}.gpkg.tar",
    ]
    lines += [f"PROVIDES: {provides}"] if provides else []
    lines.append(f"RDEPEND: {rdepend}")
    lines += [f"REQUIRES: {requires}"] if requires else []
    lines += [f"SLOT: {slot}"] if slot else []  # Portage omits the default, 0
    return "\n".join(lines) + "\n"


def _index(*entries: str) -> list[binpkgs.Instance]:
    return binpkgs.parse_index("\n".join([HEADER, *entries]))


def _plan(instances: list[binpkgs.Instance], picks: dict[str, str]) -> StalePlan:
    return plan_stale(binpkgs.stale(instances, PROVIDERS), instances, picks)


def _names(found: tuple[binpkgs.Instance, ...]) -> set[str]:
    return {i.path.rsplit("/", 1)[1] for i in found}


OLD_314 = _entry("dev-lang/python-3.14.10", 1, slot="3.14", rdepend=f"{OPENSSL}/3=")
OLD_315 = _entry("dev-lang/python-3.15.2", 1, slot="3.15", rdepend=f"{OPENSSL}/3=")
NEW_315 = _entry("dev-lang/python-3.15.2_p1", 6, slot="3.15", rdepend=f"{OPENSSL}/4=")
VTE_STALE = _entry("x11-libs/vte-0.84.1", 1, slot="2.91", rdepend=f"{SIMDUTF}/34=")
VTE_FRESH = _entry("x11-libs/vte-0.84.1", 2, slot="2.91", rdepend=f"{SIMDUTF}/36=")


def test_an_all_stale_version_the_tree_picks_excludes_its_slot_and_moves_after() -> None:
    instances = _index(VTE_STALE)
    assert tree_pick_queries(binpkgs.stale(instances, PROVIDERS), instances) == [
        "x11-libs/vte:2.91"
    ]
    plan = _plan(instances, {"x11-libs/vte:2.91": "x11-libs/vte-0.84.1"})
    assert plan.exclude == ("x11-libs/vte:2.91",)  # the slot atom, never the bare package
    assert plan.quarantine_now == () and plan.superseded == ()
    assert _names(plan.quarantine_after) == {"vte-0.84.1-1.gpkg.tar"}


def test_an_all_stale_superseded_version_moves_now_and_excludes_nothing() -> None:
    instances = _index(OLD_315)
    plan = _plan(instances, {"dev-lang/python:3.15": "dev-lang/python-3.15.2_p1"})
    assert plan.exclude == () and plan.quarantine_after == ()
    assert _names(plan.quarantine_now) == {"python-3.15.2-1.gpkg.tar"}
    (gone,) = plan.superseded
    assert gone == Superseded(instances[0], "dev-lang/python:3.15", "dev-lang/python-3.15.2_p1")


def test_the_measured_case_old_pythons_go_now_and_the_current_one_is_not_excluded() -> None:
    """2026-11-15: under the per-package rule ``dev-lang/python`` was excluded here, in
    every run, and ``python-3.15.2_p1`` compiled each time beside its fresh binpkg."""
    instances = _index(OLD_314, OLD_315, NEW_315)
    found = binpkgs.stale(instances, PROVIDERS)
    # only the all-stale versions' slots are asked; the fresh 3.15.2_p1 asks nothing itself
    assert tree_pick_queries(found, instances) == ["dev-lang/python:3.14", "dev-lang/python:3.15"]
    picks = {
        "dev-lang/python:3.14": "dev-lang/python-3.14.10_p1",
        "dev-lang/python:3.15": "dev-lang/python-3.15.2_p1",
    }
    plan = plan_stale(found, instances, picks)
    assert plan.exclude == ()
    assert plan.quarantine_after == ()
    assert _names(plan.quarantine_now) == {"python-3.14.10-1.gpkg.tar", "python-3.15.2-1.gpkg.tar"}
    assert superseded_audit(plan.superseded) == [
        {
            "cpv": "dev-lang/python-3.14.10",
            "build_id": 1,
            "slot": "dev-lang/python:3.14",
            "tree_picks": "dev-lang/python-3.14.10_p1",
        },
        {
            "cpv": "dev-lang/python-3.15.2",
            "build_id": 1,
            "slot": "dev-lang/python:3.15",
            "tree_picks": "dev-lang/python-3.15.2_p1",
        },
    ]


def test_two_slots_of_one_package_and_only_the_stale_one_is_excluded() -> None:
    """python:3.14's picked version has only a stale binpkg; python:3.15's is fresh."""
    picked_314 = _entry("dev-lang/python-3.14.10_p1", 1, slot="3.14", rdepend=f"{OPENSSL}/3=")
    instances = _index(picked_314, NEW_315)
    plan = _plan(instances, {"dev-lang/python:3.14": "dev-lang/python-3.14.10_p1"})
    assert plan.exclude == ("dev-lang/python:3.14",)
    assert _names(plan.quarantine_after) == {"python-3.14.10_p1-1.gpkg.tar"}
    assert plan.quarantine_now == ()


def test_nothing_visible_in_the_slot_moves_now_and_records_that_nothing_is_picked() -> None:
    """Removed or masked: ``app-emulation/spice-0.16.0`` with no ebuild left in its slot."""
    spice = _entry("app-emulation/spice-0.16.0", 1, rdepend=f"{OPENSSL}/3=")
    instances = _index(spice)
    assert slot_atom_of(instances[0]) == "app-emulation/spice:0"  # no SLOT line: the default
    plan = _plan(instances, {"app-emulation/spice:0": ""})
    assert plan.exclude == () and plan.quarantine_after == ()
    assert _names(plan.quarantine_now) == {"spice-0.16.0-1.gpkg.tar"}
    assert superseded_audit(plan.superseded) == [
        {
            "cpv": "app-emulation/spice-0.16.0",
            "build_id": 1,
            "slot": "app-emulation/spice:0",
            "tree_picks": None,
        }
    ]


def test_a_version_with_a_fresh_instance_loses_its_stale_one_now_and_asks_nothing() -> None:
    """As before the slot rule: the tree's answer is not even needed."""
    instances = _index(VTE_STALE, VTE_FRESH)
    assert tree_pick_queries(binpkgs.stale(instances, PROVIDERS), instances) == []
    for picks in ({}, {"x11-libs/vte:2.91": "x11-libs/vte-0.84.1"}, {"x11-libs/vte:2.91": ""}):
        plan = _plan(instances, picks)
        assert plan.exclude == () and plan.quarantine_after == () and plan.superseded == ()
        assert _names(plan.quarantine_now) == {"vte-0.84.1-1.gpkg.tar"}


def test_hostile_a_fresh_instance_rescues_only_its_own_version() -> None:
    """foo-1.0 is stale and only foo-2.0 is fresh: another VERSION does not rescue it.
    The tree picks foo-2.0, so foo-1.0 goes now; had it picked foo-1.0, the slot would be
    excluded -- the fresh foo-2.0 binpkg with it, which is what the exclusion means."""
    instances = _index(
        _entry("dev-libs/foo-1.0", 1, rdepend=f"{SIMDUTF}/34="),
        _entry("dev-libs/foo-2.0", 1, rdepend=f"{SIMDUTF}/36="),
    )
    superseded = _plan(instances, {"dev-libs/foo:0": "dev-libs/foo-2.0"})
    assert superseded.exclude == () and _names(superseded.quarantine_now) == {"foo-1.0-1.gpkg.tar"}
    pinned = _plan(instances, {"dev-libs/foo:0": "dev-libs/foo-1.0"})
    assert pinned.exclude == ("dev-libs/foo:0",) and pinned.quarantine_now == ()
    assert _names(pinned.quarantine_after) == {"foo-1.0-1.gpkg.tar"}


def test_hostile_a_revision_is_another_version() -> None:
    """The tree picks ``-r1``: the unrevised version is superseded, not picked -- and the
    other way around. An exact comparison, never a prefix."""
    plain = _index(_entry("sys-apps/portage-3.0.83", 1, rdepend=f"{OPENSSL}/3="))
    assert _plan(plain, {"sys-apps/portage:0": "sys-apps/portage-3.0.83-r1"}).exclude == ()
    revised = _index(_entry("sys-apps/portage-3.0.83-r1", 1, rdepend=f"{OPENSSL}/3="))
    plan = _plan(revised, {"sys-apps/portage:0": "sys-apps/portage-3.0.83"})
    assert plan.exclude == () and _names(plan.quarantine_now) == {"portage-3.0.83-r1-1.gpkg.tar"}
    kept = _plan(revised, {"sys-apps/portage:0": "sys-apps/portage-3.0.83-r1"})
    assert kept.exclude == ("sys-apps/portage:0",)


def test_a_slot_the_tree_was_not_asked_about_moves_nothing_for_good() -> None:
    """No answer is not "nothing visible": the slot is compiled, as a picked one is."""
    instances = _index(OLD_315)
    plan = _plan(instances, {})
    assert plan.exclude == ("dev-lang/python:3.15",)
    assert plan.quarantine_now == () and plan.superseded == ()
    assert _names(plan.quarantine_after) == {"python-3.15.2-1.gpkg.tar"}


def test_a_version_whose_stale_instances_already_left_the_index_decides_nothing() -> None:
    """The step's second pass: the findings still name what its first pass moved aside."""
    before = _index(OLD_315, VTE_STALE)
    found = binpkgs.stale(before, PROVIDERS)
    after = _index(VTE_STALE)  # python-3.15.2 was moved aside and the index regenerated
    picks = {"x11-libs/vte:2.91": "x11-libs/vte-0.84.1", "dev-lang/python:3.15": ""}
    plan = plan_stale(found, after, picks)
    assert plan.exclude == ("x11-libs/vte:2.91",)
    assert plan.quarantine_now == () and plan.superseded == ()


# --- soname findings: the planned instances -------------------------------------------------

KERNEL = "sys-kernel/gentoo-kernel-bin"
KERNEL_ENTRY = _entry(f"{KERNEL}-7.3.3", 1, slot="7.3.3", requires="x86_64: libcrypto.so.3")
#: what the plan's OpenSSL 4 offers
POOL = [binpkgs.Soname("x86_64", "libcrypto.so.4")]


def test_a_soname_finding_on_a_planned_instance_the_tree_picks_excludes_its_slot() -> None:
    instances = _index(KERNEL_ENTRY)
    found = binpkgs.soname_stale(instances, POOL)
    assert tree_pick_queries(found, instances) == [f"{KERNEL}:7.3.3"]
    plan = plan_stale(found, instances, {f"{KERNEL}:7.3.3": f"{KERNEL}-7.3.3"})
    assert plan.exclude == (f"{KERNEL}:7.3.3",)
    assert plan.quarantine_now == () and len(plan.quarantine_after) == 1


def test_a_planned_instance_of_a_superseded_version_moves_now_and_its_slot_is_compiled() -> None:
    """The one superseded case that still excludes: the plan merges this version (a pin, or
    a binpkg that outlived its ebuild), and with it gone the emerge may take another
    version's binpkg in the slot, which the soname rule never judged."""
    instances = _index(KERNEL_ENTRY)
    found = binpkgs.soname_stale(instances, POOL)
    plan = plan_stale(found, instances, {f"{KERNEL}:7.3.3": f"{KERNEL}-7.3.3-r1"})
    assert plan.exclude == (f"{KERNEL}:7.3.3",)
    assert len(plan.quarantine_now) == 1 and plan.quarantine_after == ()
    assert [s.tree_picks for s in plan.superseded] == [f"{KERNEL}-7.3.3-r1"]


def test_a_subslot_and_a_soname_finding_together_make_a_version_all_stale() -> None:
    """Build 1 is stale by subslot, build 2 by soname: no fresh instance is left."""
    by_subslot = _entry("x11-libs/vte-0.84.1", 1, slot="2.91", rdepend=f"{SIMDUTF}/34=")
    by_soname = _entry(
        "x11-libs/vte-0.84.1",
        2,
        slot="2.91",
        rdepend=f"{SIMDUTF}/36=",
        requires="x86_64: libcrypto.so.3",
    )
    instances = _index(by_subslot, by_soname)
    subslot = binpkgs.stale(instances, PROVIDERS)
    soname = binpkgs.soname_stale([instances[1]], POOL)
    assert tree_pick_queries(subslot, instances) == []  # build 2 still looks fresh
    assert tree_pick_queries([*subslot, *soname], instances) == ["x11-libs/vte:2.91"]
    plan = plan_stale([*subslot, *soname], instances, {"x11-libs/vte:2.91": "x11-libs/vte-0.84.1"})
    assert plan.exclude == ("x11-libs/vte:2.91",)
    assert plan.quarantine_now == () and len(plan.quarantine_after) == 2


def test_a_waived_soname_finding_excludes_and_quarantines_nothing() -> None:
    """A waived finding never reaches the plan (``shidashi.soname_waivers``)."""
    waiver = soname_waivers.SonameWaiver(
        package=KERNEL,
        soname="libcrypto.so.3",
        why="certs/extract-cert is prebuilt against OpenSSL 3",
        found="weekly release 2026.11.13",
        until="Gentoo ships a gentoo-kernel-bin whose extract-cert links libcrypto.so.4",
    )
    instances = _index(KERNEL_ENTRY)
    found = binpkgs.soname_stale(instances, POOL)
    assert len(found) == 1  # the control: unwaived, it is a finding
    left = list(soname_waivers.apply_soname_waivers(found, [waiver]).stale)
    assert tree_pick_queries(left, instances) == []
    for picks in ({}, {f"{KERNEL}:7.3.3": f"{KERNEL}-7.3.3"}, {f"{KERNEL}:7.3.3": ""}):
        assert plan_stale(left, instances, picks) == StalePlan()
