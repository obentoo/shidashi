"""The stale-subslot rule over the binhost ``Packages`` index (story 019, D1).

Three sub-tasks, selected by name: ``-k index`` (2.1, ``parse_index``), ``-k rule``
(2.2, ``provider_queries``/``stale``/``unresolved``) and ``-k resolver`` (2.3,
``resolve_script``/``resolve_providers``). The resolver runs its script for real,
under bash, against a fake ``portageq`` -- only the container is faked.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from shidashi.container import CommandResult
from tests._pending import try_import

parse_index: Any = try_import("shidashi.binpkgs", "parse_index")
provider_queries: Any = try_import("shidashi.binpkgs", "provider_queries")
stale: Any = try_import("shidashi.binpkgs", "stale")
unresolved: Any = try_import("shidashi.binpkgs", "unresolved")
describe: Any = try_import("shidashi.binpkgs", "describe")
BinpkgError: Any = try_import("shidashi.binpkgs", "BinpkgError")
resolve_script: Any = try_import("shidashi.binpkgs", "resolve_script")
resolve_providers: Any = try_import("shidashi.binpkgs", "resolve_providers")

SIMDUTF = ">=dev-cpp/simdutf-6.2.0:0"

#: The 2026-10-08 binhost: vte built against simdutf 0/34 (build 1) and rebuilt
#: against 0/36 (build 2); nodejs built against 0/36.
INDEX = """\
ARCH: amd64
PACKAGES: 3
VERSION: 0

BUILD_ID: 1
CPV: x11-libs/vte-0.84.1
PATH: x11-libs/vte/vte-0.84.1-1.gpkg.tar
RDEPEND: >=dev-libs/glib-2.72:2/2.84=[introspection] >=dev-cpp/simdutf-6.2.0:0/34= \
x11-libs/gtk+:3 dev-libs/fast_float:0/34
DEPEND: >=dev-cpp/simdutf-6.2.0:0/34=
SLOT: 2.91

BUILD_ID: 1
CPV: net-libs/nodejs-26.9.0
PATH: net-libs/nodejs/nodejs-26.9.0-1.gpkg.tar
RDEPEND: >=dev-cpp/simdutf-6.2.0:0/36= || ( dev-libs/openssl:0/3= dev-libs/libressl:0/57= ) \
sys-libs/zlib:0=

BUILD_ID: 2
CPV: x11-libs/vte-0.84.1
PATH: x11-libs/vte/vte-0.84.1-2.gpkg.tar
RDEPEND: >=dev-cpp/simdutf-6.2.0:0/36= >=net-libs/webkit-gtk-2.48.1:6/4=
"""


def _entry(cpv: str, build: int, rdepend: str) -> str:
    name = cpv.split("/")[1]
    return (
        f"BUILD_ID: {build}\nCPV: {cpv}\n"
        f"PATH: {cpv.split('/')[0]}/{name}-{build}.gpkg.tar\nRDEPEND: {rdepend}\n"
    )


def _deps(instance: Any) -> dict[str, Any]:
    return {d.cp: d for d in instance.slot_deps}


def _by(instances: list[Any], cpv: str, build: int) -> Any:
    (found,) = [i for i in instances if i.cpv == cpv and str(i.build_id) == str(build)]
    return found


# --- 2.1 parse_index ------------------------------------------------------------------


def test_index_hostile_two_instances_of_one_cpv_stay_apart() -> None:
    """binpkg-multi-instance: one CPV, two entries told apart by BUILD_ID and PATH.
    Merged by CPV, the fresh instance would be quarantined with the stale one."""
    instances = parse_index(INDEX)
    assert len(instances) == 3  # the header block is not an instance
    vte = [i for i in instances if i.cpv == "x11-libs/vte-0.84.1"]
    assert sorted(str(i.build_id) for i in vte) == ["1", "2"]
    assert sorted(i.path for i in vte) == [
        "x11-libs/vte/vte-0.84.1-1.gpkg.tar",
        "x11-libs/vte/vte-0.84.1-2.gpkg.tar",
    ]
    assert _deps(_by(instances, "x11-libs/vte-0.84.1", 1))["dev-cpp/simdutf"].subslot == "34"
    assert _deps(_by(instances, "x11-libs/vte-0.84.1", 2))["dev-cpp/simdutf"].subslot == "36"


def test_index_hostile_only_built_slot_operators_are_slot_deps() -> None:
    """``dev-libs/fast_float:0/34`` (no ``=``) and ``x11-libs/gtk+:3`` look like slot
    deps but are not built slot operators: the rule must not see them."""
    vte1 = _by(parse_index(INDEX), "x11-libs/vte-0.84.1", 1)
    assert set(_deps(vte1)) == {"dev-libs/glib", "dev-cpp/simdutf"}


def test_index_splits_slot_and_subslot_and_strips_the_operator_for_the_query() -> None:
    instances = parse_index(INDEX)
    simdutf = _deps(_by(instances, "x11-libs/vte-0.84.1", 1))["dev-cpp/simdutf"]
    assert (simdutf.slot, simdutf.subslot, simdutf.query) == ("0", "34", SIMDUTF)
    assert "dev-cpp/simdutf" in simdutf.atom
    glib = _deps(_by(instances, "x11-libs/vte-0.84.1", 1))["dev-libs/glib"]
    assert (glib.slot, glib.subslot) == ("2", "2.84")  # a USE dep after the operator
    node = _deps(_by(instances, "net-libs/nodejs-26.9.0", 1))
    assert node["dev-libs/openssl"].subslot == "3"  # inside an || group
    # `:0=` carries no subslot: it is the slot itself (GOTCHA)
    assert (node["sys-libs/zlib"].slot, node["sys-libs/zlib"].subslot) == ("0", "0")
    assert node["sys-libs/zlib"].query == "sys-libs/zlib:0"
    # the version comes off the package name, the name's own '-' stays
    webkit = _deps(_by(instances, "x11-libs/vte-0.84.1", 2))
    assert "net-libs/webkit-gtk" in webkit and webkit["net-libs/webkit-gtk"].subslot == "4"


# --- 2.2 the rule ------------------------------------------------------------------------


def _fresh_answers() -> dict[str, str]:
    return {
        SIMDUTF: "0/36",
        "dev-libs/glib-2.72:2": "2/2.84",
        ">=dev-libs/glib-2.72:2": "2/2.84",
        "dev-libs/openssl:0": "0/3",
        "dev-libs/libressl:0": "0/57",
        "sys-libs/zlib:0": "0",
        ">=net-libs/webkit-gtk-2.48.1:6": "6/4",
    }


def test_rule_hostile_one_provider_answer_judges_each_binpkg_by_its_own_subslot() -> None:
    """vte-1, vte-2 and nodejs share ONE provider query; only vte-1 was built
    against 0/34. The 2026-10-08 case."""
    instances = parse_index(INDEX)
    providers = {q: _fresh_answers().get(q, "") for q in provider_queries(instances)}
    found = stale(instances, providers)
    assert [(s.instance.cpv, str(s.instance.build_id)) for s in found] == [
        ("x11-libs/vte-0.84.1", "1")
    ]
    (hit,) = found
    assert (hit.dep.atom, hit.dep.cp) == (">=dev-cpp/simdutf-6.2.0:0/34=", "dev-cpp/simdutf")
    assert (hit.built, hit.tree) == ("0/34", "0/36")  # both full SLOTs, one form


def test_rule_describe_names_each_stale_dep_and_the_rebuild() -> None:
    instances = parse_index(INDEX)
    found = stale(instances, {q: _fresh_answers().get(q, "") for q in provider_queries(instances)})
    assert describe(found, arch="v3", image="gnome", init="systemd") == (
        "x11-libs/vte-0.84.1 (build 1): >=dev-cpp/simdutf-6.2.0:0/34="
        " — dev-cpp/simdutf 0/34 → 0/36\n"
        "rebuild: shidashi factory v3 gnome systemd"
    )


def test_rule_hostile_a_slot_without_subslot_is_its_own_subslot() -> None:
    """`:0=` against a tree SLOT of `0` is fresh -- else every such dep reads stale;
    against `0/1` it is stale."""
    instances = parse_index(_entry("dev-libs/a-1", 1, "sys-libs/zlib:0="))
    assert stale(instances, {"sys-libs/zlib:0": "0"}) == []
    (hit,) = stale(instances, {"sys-libs/zlib:0": "0/1"})
    assert (hit.built, hit.tree) == ("0", "0/1")  # never "0/0"


def test_rule_hostile_providers_are_keyed_by_query_not_by_package() -> None:
    """Two slots of one package: slot 1 is fresh, slot 2 moved to a new subslot."""
    instances = parse_index(
        _entry("app-misc/one-1", 1, "dev-libs/foo:1/5=")
        + "\n"
        + _entry("app-misc/two-1", 1, "dev-libs/foo:2/7=")
    )
    found = stale(instances, {"dev-libs/foo:1": "1/5", "dev-libs/foo:2": "2/8"})
    assert [s.instance.cpv for s in found] == ["app-misc/two-1"]


def test_rule_an_unresolved_provider_is_not_stale_but_reported() -> None:
    instances = parse_index(_entry("dev-libs/a-1", 1, "dev-libs/gone:0/3="))
    assert stale(instances, {"dev-libs/gone:0": ""}) == []
    assert stale(instances, {}) == []
    assert [d.cp for d in unresolved(instances, {"dev-libs/gone:0": ""})] == ["dev-libs/gone"]


def test_rule_provider_queries_are_deduplicated_sorted_and_operator_free() -> None:
    queries = provider_queries(parse_index(INDEX))
    assert queries == sorted(set(queries))
    assert queries.count(SIMDUTF) == 1  # RDEPEND and DEPEND of two binpkgs
    assert not any(q.endswith("=") or ":0/34" in q or ":0/36" in q for q in queries)


# --- 2.3 the resolver ----------------------------------------------------------------------

_FAKE_PORTAGEQ = """
import json, os, sys
answers = json.loads(os.environ["FAKE_PORTAGEQ"])
args = sys.argv[1:]
if args[:1] == ["best_visible"]:
    hit = answers.get(args[-1])
    print(hit[0] if hit else "")  # the real one prints an empty line and exits 1
    sys.exit(0 if hit else 1)
if args[:1] == ["metadata"] and args[-1] == "SLOT":
    slots = {cpv: slot for cpv, slot in answers.values()}
    if args[-2] in slots:
        print(slots[args[-2]])
        sys.exit(0)
sys.exit(1)
"""


class _LocalContainer:
    """Runs the command here, with a fake portageq first on PATH, in ``cwd``."""

    def __init__(self, tmp: Path, answers: dict[str, tuple[str, str]], fail: bool = False):
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        tool = self.bindir / "portageq"
        tool.write_text(f"#!{sys.executable}\n{_FAKE_PORTAGEQ}")
        tool.chmod(0o755)
        self.cwd = tmp / "cwd"
        self.cwd.mkdir()
        self.answers, self.fail = answers, fail
        self.calls: list[list[str]] = []
        self.rootfs = tmp / "rootfs"

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        self.calls.append(list(argv))
        if self.fail:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="portageq: boom\n")
        full = {
            **os.environ,
            **(env or {}),
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PORTAGEQ": json.dumps(self.answers),
        }
        done = subprocess.run(
            list(argv), cwd=self.cwd, env=full, capture_output=True, text=True, check=False
        )
        if check and done.returncode != 0:
            raise subprocess.CalledProcessError(
                done.returncode, argv, output=done.stdout, stderr=done.stderr
            )
        return CommandResult(done.returncode, done.stdout, done.stderr)


def test_resolver_answers_every_query_in_one_container_run(tmp_path: Path) -> None:
    """`>=` and `<` reach portageq as atoms, never as shell redirections; nothing
    visible is an empty answer, not an error."""
    container = _LocalContainer(
        tmp_path,
        {
            SIMDUTF: ("dev-cpp/simdutf-9.2.1", "0/36"),
            "<dev-libs/foo-2:1": ("dev-libs/foo-1.5", "1/5"),
        },
    )
    queries = [SIMDUTF, "<dev-libs/foo-2:1", "dev-libs/gone:0"]
    assert all(q in resolve_script(queries) or repr(q) in resolve_script(queries) for q in queries)
    got = resolve_providers(container, queries)
    assert len(container.calls) == 1
    assert got[SIMDUTF] == "0/36"
    assert got["<dev-libs/foo-2:1"] == "1/5"
    assert got.get("dev-libs/gone:0", "") == ""
    assert list(container.cwd.iterdir()) == []  # no redirection created a file


def test_resolver_a_failed_run_is_a_binpkg_error_carrying_its_output(tmp_path: Path) -> None:
    """The module's own error: each caller wraps it in its own (FactoryError, AssemblerError)."""
    container = _LocalContainer(tmp_path, {}, fail=True)
    with pytest.raises(BinpkgError) as err:
        resolve_providers(container, [SIMDUTF])
    assert "portageq: boom" in err.value.output
