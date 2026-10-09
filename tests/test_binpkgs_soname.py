"""The soname rule over the binhost ``Packages`` index (story 019, D1; R1.6, R1.8, R2.1).

A planned binpkg is stale when it requires a soname its providers offer only at another
version of the same library name (``libsimdutf.so.34`` against ``libsimdutf.so.36``, same
category). A library name offered at no version is unresolved: recorded, never stale.

Hostile fixtures come first: the cases where the rule would wrongly collapse two
different libraries into one (category, a dashed name carrying a version, a name that
merely starts like another, an unversioned soname) and the case where it would wrongly
split one library in two (another version of the same name in the same category).
"""

from typing import Any

from tests._pending import try_import

parse_index: Any = try_import("shidashi.binpkgs", "parse_index")
stale: Any = try_import("shidashi.binpkgs", "stale")
describe: Any = try_import("shidashi.binpkgs", "describe")
Soname: Any = try_import("shidashi.binpkgs", "Soname")
soname_stale: Any = try_import("shidashi.binpkgs", "soname_stale")
unresolved_sonames: Any = try_import("shidashi.binpkgs", "unresolved_sonames")
parse_vdb_provides: Any = try_import("shidashi.binpkgs", "parse_vdb_provides")

X64 = "x86_64"
SIMDUTF = ">=dev-cpp/simdutf-6.2.0:0"

#: The 2026-10-09 v3 binhost, trimmed: vte build 1 linked against simdutf 0/34, vte
#: build 2 against 0/36, and simdutf-9.2.1 (SLOT 0/34 in its own entry) providing .36.
INDEX = """\
ARCH: amd64
PACKAGES: 3
VERSION: 0

BUILD_ID: 1
CPV: x11-libs/vte-0.84.1
PATH: x11-libs/vte/vte-0.84.1-1.gpkg.tar
PROVIDES: x86_64: libvte-2.91.so.0
RDEPEND: >=dev-cpp/simdutf-6.2.0:0/34=
REQUIRES: x86_64: ld-linux-x86-64.so.2 libc.so.6 libsimdutf.so.34 libstdc++.so.6
SLOT: 2.91

BUILD_ID: 2
CPV: x11-libs/vte-0.84.1
PATH: x11-libs/vte/vte-0.84.1-2.gpkg.tar
PROVIDES: x86_64: libvte-2.91.so.0
RDEPEND: >=dev-cpp/simdutf-6.2.0:0/36=
REQUIRES: x86_64: ld-linux-x86-64.so.2 libc.so.6 libsimdutf.so.36 libstdc++.so.6
SLOT: 2.91

BUILD_ID: 1
CPV: dev-cpp/simdutf-9.2.1
PATH: dev-cpp/simdutf/simdutf-9.2.1-1.gpkg.tar
PROVIDES: x86_64: libsimdutf.so.36
REQUIRES: x86_64: libc.so.6 libgcc_s.so.1 libstdc++.so.6
SLOT: 0/34
"""

#: What glibc and gcc put in the rootfs: always offered, never the subject here.
BASE = ("ld-linux-x86-64.so.2", "libc.so.6", "libgcc_s.so.1", "libstdc++.so.6")


def _so(name: str, category: str = X64) -> Any:
    return Soname(category=category, name=name)


def _pool(*names: str, category: str = X64) -> frozenset[Any]:
    return frozenset(_so(n, category) for n in (*BASE, *names))


def _entry(cpv: str, build: int, *, requires: str = "", provides: str = "") -> str:
    name = cpv.split("/")[1]
    text = f"BUILD_ID: {build}\nCPV: {cpv}\nPATH: {cpv.split('/')[0]}/{name}-{build}.gpkg.tar\n"
    if provides:
        text += f"PROVIDES: {provides}\n"
    if requires:
        text += f"REQUIRES: {requires}\n"
    return text


def _one(requires: str) -> Any:
    return parse_index(_entry("app-misc/needs-1", 1, requires=requires))


def _by(instances: list[Any], cpv: str, build: int) -> Any:
    (found,) = [i for i in instances if i.cpv == cpv and str(i.build_id) == str(build)]
    return found


def _stale_keys(found: list[Any]) -> list[tuple[str, str, str, str]]:
    return sorted(
        (s.instance.cpv, str(s.instance.build_id), s.needs.category, s.needs.name) for s in found
    )


def _unresolved_keys(found: list[Any]) -> list[tuple[str, str, str]]:
    return sorted((i.cpv, s.category, s.name) for i, s in found)


# --- parsing ----------------------------------------------------------------------------


def test_requires_and_provides_are_parsed_with_their_category() -> None:
    instances = parse_index(INDEX)
    vte1 = _by(instances, "x11-libs/vte-0.84.1", 1)
    assert vte1.requires == frozenset(
        _so(n) for n in ("ld-linux-x86-64.so.2", "libc.so.6", "libsimdutf.so.34", "libstdc++.so.6")
    )
    assert vte1.provides == frozenset({_so("libvte-2.91.so.0")})
    simdutf = _by(instances, "dev-cpp/simdutf-9.2.1", 1)
    assert simdutf.provides == frozenset({_so("libsimdutf.so.36")})


def test_a_token_ending_in_a_colon_opens_a_new_category() -> None:
    """Hostile: two categories on one line. Read as one, ``libc.so.6`` of x86_32
    would satisfy an x86_64 requirement."""
    (inst,) = _one("x86_32: libc.so.6 libz.so.1 x86_64: libc.so.6 libzstd.so.1")
    assert inst.requires == frozenset(
        {
            _so("libc.so.6", "x86_32"),
            _so("libz.so.1", "x86_32"),
            _so("libc.so.6", X64),
            _so("libzstd.so.1", X64),
        }
    )
    assert not any(s.name.endswith(":") for s in inst.requires)  # the category is no soname


def test_an_entry_without_requires_or_provides_has_empty_sets() -> None:
    (inst,) = parse_index(_entry("app-misc/plain-1", 1))
    assert inst.requires == frozenset()
    assert inst.provides == frozenset()


def test_parse_vdb_provides_reads_concatenated_files() -> None:
    """Two ``/var/db/pkg/*/*/PROVIDES`` files, one line each, read as one text."""
    text = "x86_64: libsimdutf.so.36\n" + "x86_64: libz.so.1 libzstd.so.1\n"
    assert parse_vdb_provides(text) == frozenset(
        {_so("libsimdutf.so.36"), _so("libz.so.1"), _so("libzstd.so.1")}
    )


def test_parse_vdb_provides_of_nothing_is_empty() -> None:
    assert parse_vdb_provides("") == frozenset()


# --- the rule: hostile fixtures first ---------------------------------------------------


def test_hostile_the_same_name_in_another_category_is_no_match() -> None:
    """x86_32 ``libsimdutf.so.36`` is another library than x86_64 ``libsimdutf.so.34``."""
    planned = _one("x86_64: libsimdutf.so.34")
    pool = _pool() | {_so("libsimdutf.so.36", "x86_32")}
    assert soname_stale(planned, pool) == []
    assert _unresolved_keys(unresolved_sonames(planned, pool)) == [
        ("app-misc/needs-1", X64, "libsimdutf.so.34")
    ]


def test_hostile_offered_holds_only_the_same_category() -> None:
    """A third soname of the same name in another category stays out of ``offered``."""
    planned = _one("x86_64: libsimdutf.so.34")
    pool = _pool("libsimdutf.so.36") | {_so("libsimdutf.so.35", "x86_32")}
    (hit,) = soname_stale(planned, pool)
    assert hit.offered == ("libsimdutf.so.36",)


def test_hostile_a_version_inside_the_name_is_another_library() -> None:
    """``libvte-2.90.so.0`` is not ``libvte-2.91.so.0`` at another version."""
    planned = _one("x86_64: libvte-2.91.so.0")
    pool = _pool("libvte-2.90.so.0")
    assert soname_stale(planned, pool) == []
    assert _unresolved_keys(unresolved_sonames(planned, pool)) == [
        ("app-misc/needs-1", X64, "libvte-2.91.so.0")
    ]


def test_hostile_a_name_that_starts_like_another_is_another_library() -> None:
    """``libfoobar.so.2`` does not offer ``libfoo.so``."""
    planned = _one("x86_64: libfoo.so.1")
    pool = _pool("libfoobar.so.2", "libfoo-bar.so.1")
    assert soname_stale(planned, pool) == []
    assert _unresolved_keys(unresolved_sonames(planned, pool)) == [
        ("app-misc/needs-1", X64, "libfoo.so.1")
    ]


def test_hostile_a_soname_without_version_suffix_is_unresolved_never_stale() -> None:
    """``libfoo-1.2.so`` and ``libfoo.so`` carry no version suffix: absent from the pool
    they are unresolved, even when the pool offers a similar or versioned soname."""
    planned = _one("x86_64: libfoo-1.2.so libbar.so")
    pool = _pool("libfoo-1.3.so", "libbar.so.1")
    assert soname_stale(planned, pool) == []
    assert _unresolved_keys(unresolved_sonames(planned, pool)) == [
        ("app-misc/needs-1", X64, "libbar.so"),
        ("app-misc/needs-1", X64, "libfoo-1.2.so"),
    ]


def test_hostile_a_soname_present_in_the_pool_is_fresh_beside_other_versions() -> None:
    """Two versions offered side by side (two slots): the required one is there."""
    planned = _one("x86_64: libssl.so.3")
    pool = _pool("libssl.so.1.1", "libssl.so.3")
    assert soname_stale(planned, pool) == []
    assert unresolved_sonames(planned, pool) == []


def test_hostile_another_version_of_the_same_name_is_stale() -> None:
    """The converse: ``.34`` and ``.36`` differ in spelling but name one library.
    The 2026-10-09 case: vte build 1 against the simdutf-9.2.1 binpkg."""
    instances = parse_index(INDEX)
    vte1 = _by(instances, "x11-libs/vte-0.84.1", 1)
    pool = _pool() | _by(instances, "dev-cpp/simdutf-9.2.1", 1).provides
    (hit,) = soname_stale([vte1], pool)
    assert hit.instance.cpv == "x11-libs/vte-0.84.1"
    assert str(hit.instance.build_id) == "1"
    assert hit.needs == _so("libsimdutf.so.34")
    assert hit.offered == ("libsimdutf.so.36",)
    assert unresolved_sonames([vte1], pool) == []  # stale is not also unresolved


def test_hostile_each_instance_of_one_cpv_is_judged_by_its_own_requires() -> None:
    """vte build 1 and build 2 share a CPV; only build 1 needs the old soname."""
    instances = parse_index(INDEX)
    planned = [i for i in instances if i.cpv == "x11-libs/vte-0.84.1"]
    pool = _pool("libsimdutf.so.36")
    assert _stale_keys(soname_stale(planned, pool)) == [
        ("x11-libs/vte-0.84.1", "1", X64, "libsimdutf.so.34")
    ]


# --- the rule: benign -------------------------------------------------------------------


def test_a_satisfied_soname_is_fresh() -> None:
    planned = _one("x86_64: libc.so.6 libsimdutf.so.36")
    pool = _pool("libsimdutf.so.36")
    assert soname_stale(planned, pool) == []
    assert unresolved_sonames(planned, pool) == []


def test_offered_lists_every_other_version_sorted() -> None:
    planned = _one("x86_64: libsimdutf.so.34")
    (hit,) = soname_stale(planned, _pool("libsimdutf.so.36", "libsimdutf.so.35"))
    assert hit.offered == ("libsimdutf.so.35", "libsimdutf.so.36")


def test_a_library_name_nobody_offers_is_unresolved() -> None:
    planned = _one("x86_64: libc.so.6 libgone.so.3")
    pool = _pool()
    assert soname_stale(planned, pool) == []
    assert _unresolved_keys(unresolved_sonames(planned, pool)) == [
        ("app-misc/needs-1", X64, "libgone.so.3")
    ]


def test_the_pool_is_any_iterable_of_sonames() -> None:
    planned = _one("x86_64: libsimdutf.so.34")
    (hit,) = soname_stale(planned, [*_pool(), _so("libsimdutf.so.36")])
    assert hit.offered == ("libsimdutf.so.36",)


# --- describe ---------------------------------------------------------------------------


def test_describe_puts_the_soname_line_before_the_rebuild_line() -> None:
    instances = parse_index(INDEX)
    found = soname_stale([_by(instances, "x11-libs/vte-0.84.1", 1)], _pool("libsimdutf.so.36"))
    assert describe([], found, arch="v3", image="gnome", init="systemd") == (
        "x11-libs/vte-0.84.1 (build 1): needs libsimdutf.so.34 — offered libsimdutf.so.36\n"
        "rebuild: shidashi factory v3 gnome systemd"
    )


def test_describe_joins_several_offered_sonames() -> None:
    planned = _one("x86_64: libsimdutf.so.34")
    found = soname_stale(planned, _pool("libsimdutf.so.36", "libsimdutf.so.35"))
    assert describe([], found, arch="v3", image="gnome", init="systemd") == (
        "app-misc/needs-1 (build 1): needs libsimdutf.so.34"
        " — offered libsimdutf.so.35, libsimdutf.so.36\n"
        "rebuild: shidashi factory v3 gnome systemd"
    )


def test_describe_lists_subslot_lines_then_soname_lines_then_the_rebuild() -> None:
    instances = parse_index(INDEX)
    vte1 = _by(instances, "x11-libs/vte-0.84.1", 1)
    by_subslot = stale([vte1], {SIMDUTF: "0/36"})
    by_soname = soname_stale([vte1], _pool("libsimdutf.so.36"))
    assert describe(by_subslot, by_soname, arch="v3", image="gnome", init="systemd") == (
        "x11-libs/vte-0.84.1 (build 1): >=dev-cpp/simdutf-6.2.0:0/34="
        " — dev-cpp/simdutf 0/34 → 0/36\n"
        "x11-libs/vte-0.84.1 (build 1): needs libsimdutf.so.34 — offered libsimdutf.so.36\n"
        "rebuild: shidashi factory v3 gnome systemd"
    )
