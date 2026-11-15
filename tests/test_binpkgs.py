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

from shidashi import audit, portageq_batch
from shidashi.container import CommandResult
from tests._fake_portageq import install_fake_portageq
from tests._pending import try_import

parse_index: Any = try_import("shidashi.binpkgs", "parse_index")
provider_queries: Any = try_import("shidashi.binpkgs", "provider_queries")
stale: Any = try_import("shidashi.binpkgs", "stale")
unresolved: Any = try_import("shidashi.binpkgs", "unresolved")
describe: Any = try_import("shidashi.binpkgs", "describe")
BinpkgError: Any = try_import("shidashi.binpkgs", "BinpkgError")
resolve_script: Any = try_import("shidashi.binpkgs", "resolve_script")
resolve_providers: Any = try_import("shidashi.binpkgs", "resolve_providers")
resolve_argv: Any = try_import("shidashi.binpkgs", "resolve_argv")

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


class _LocalContainer:
    """Runs the command here, with a fake portageq first on PATH, in ``cwd``."""

    def __init__(self, tmp: Path, answers: dict[str, tuple[str, str]], fail: bool = False):
        self.bindir = tmp / "bin"
        #: the same fake, for the one interpreter to load
        self.portageq = str(install_fake_portageq(self.bindir))
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


_ANSWERS = {
    SIMDUTF: ("dev-cpp/simdutf-9.2.1", "0/36"),
    "<dev-libs/foo-2:1": ("dev-libs/foo-1.5", "1/5"),
}
_QUERIES = [SIMDUTF, "<dev-libs/foo-2:1", "dev-libs/gone:0"]
_EXPECTED = {SIMDUTF: "0/36", "<dev-libs/foo-2:1": "1/5", "dev-libs/gone:0": ""}


class _ResolverEvents(audit.Recorder):
    """Stands in for the run in force: keeps the events, nothing else."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))


def test_resolver_answers_every_query_in_one_interpreter(tmp_path: Path) -> None:
    """One container run, each query its own argument: `>=` and `<` never meet a shell;
    nothing visible is an empty answer, not an error."""
    container = _LocalContainer(tmp_path, _ANSWERS)
    got = resolve_providers(container, _QUERIES, portageq=container.portageq)
    assert len(container.calls) == 1
    argv = container.calls[0]
    assert argv[:4] == ["python3", "-X", "utf8", "-c"] and argv[-3:] == _QUERIES
    assert got == _EXPECTED
    assert list(container.cwd.iterdir()) == []  # no redirection created a file


def test_resolver_without_a_portageq_to_load_asks_one_per_query_and_says_so(
    tmp_path: Path,
) -> None:
    """Same answers, the slow way (~4 minutes for the v3 index): an audit event names why."""
    container = _LocalContainer(tmp_path, _ANSWERS)
    # the loop quotes each query: `>=` and `<` reach portageq as atoms
    assert all(
        q in resolve_script(_QUERIES) or repr(q) in resolve_script(_QUERIES) for q in _QUERIES
    )
    trail = _ResolverEvents()
    with audit.recording(trail):
        got = resolve_providers(container, _QUERIES, portageq=str(tmp_path / "no-portageq"))
    assert [call[0] for call in container.calls] == ["python3", "bash"]
    assert got == _EXPECTED
    assert list(container.cwd.iterdir()) == []
    [(kind, fields)] = trail.events
    assert kind == "binpkgs.portageq_per_query" and fields["queries"] == 3
    assert "no-portageq" in fields["cause"]


def test_resolver_in_one_interpreter_prints_what_the_per_query_loop_prints(
    tmp_path: Path,
) -> None:
    """The contract with the step: the very same ``query<TAB>slot`` lines."""
    one = _LocalContainer(tmp_path / "one", _ANSWERS)
    loop = _LocalContainer(tmp_path / "loop", _ANSWERS)
    batch = one.run(resolve_argv(_QUERIES, portageq=one.portageq))
    per_query = loop.run(["bash", "-c", resolve_script(_QUERIES)])
    assert batch.stdout == per_query.stdout
    assert batch.stdout.count("\n") == len(_QUERIES)


def test_resolver_an_invalid_atom_ends_the_run_with_portageqs_code_and_no_fallback(
    tmp_path: Path,
) -> None:
    container = _LocalContainer(tmp_path, _ANSWERS)
    with pytest.raises(BinpkgError) as err:
        resolve_providers(container, [SIMDUTF, "!!broken"], portageq=container.portageq)
    assert len(container.calls) == 1
    assert "Invalid atom: '!!broken'" in err.value.output


def _batch(
    *args: str,
    python: str = sys.executable,
    flags: tuple[str, ...] = ("-X", "utf8"),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``shidashi.portageq_batch`` as a stage runs it: its source under ``-c``."""
    source = Path(portageq_batch.__file__).read_text(encoding="utf-8")
    return subprocess.run(
        [python, *flags, "-c", source, *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def test_resolver_batch_without_the_commands_it_needs_gives_no_answer_and_says_why(
    tmp_path: Path,
) -> None:
    """Exit 75, the fallback's cue: a Portage whose portageq changed shape."""
    script = tmp_path / "portageq"
    script.write_text("non_commands = frozenset()\ncommands = []\ndef main(argv):\n    pass\n")
    done = _batch(str(script), SIMDUTF)
    assert done.returncode == portageq_batch.PORTAGEQ_UNLOADABLE, done.stderr
    assert done.stdout == "" and "best_visible" in done.stderr


def test_resolver_batch_outside_utf8_mode_refuses_before_portageq_can_re_exec_it(
    tmp_path: Path,
) -> None:
    """portageq re-execs a non-UTF-8 interpreter with sys.argv, which under ``-c`` holds a
    query where the program was: refused, so the query never runs as code."""
    marker = tmp_path / "ran"
    query = f"open({str(marker)!r}, 'w')"
    done = _batch(
        str(tmp_path / "portageq"),
        query,
        flags=("-X", "utf8=0"),
        env={"LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"},
    )
    assert done.returncode == portageq_batch.PORTAGEQ_UNLOADABLE, done.stderr
    assert "UTF-8" in done.stderr and not marker.exists()


def _host_portageq_python() -> str | None:
    """``/usr/bin/python3`` when it imports Portage and python-exec has its portageq."""
    python = "/usr/bin/python3"
    probe = "import portage, sys; print('%d.%d' % sys.version_info[:2])"
    try:
        done = subprocess.run([python, "-c", probe], capture_output=True, text=True, check=False)
    except OSError:
        return None
    version = done.stdout.strip()
    if done.returncode != 0 or not Path(f"/usr/lib/python-exec/python{version}/portageq").is_file():
        return None
    return python


@pytest.mark.skipif(_host_portageq_python() is None, reason="no Portage on this host")
def test_resolver_batch_answers_like_this_hosts_portageq_one_query_at_a_time() -> None:
    """The real thing, where there is one: this host's Portage, its tree and configuration,
    one portageq per query against one interpreter for all -- the same bytes."""
    queries = [
        ">=app-arch/bzip2-1.0.5:0",
        "sys-apps/portage:0",
        "<sys-apps/portage-2:0",
        "dev-libs/no-such-package:0",
    ]
    python = _host_portageq_python()
    assert python is not None
    batch = _batch("", *queries, python=python)
    per_query = subprocess.run(
        ["bash", "-c", resolve_script(queries)], capture_output=True, text=True, check=False
    )
    assert (batch.returncode, batch.stdout) == (per_query.returncode, per_query.stdout)
    assert batch.returncode == 0 and batch.stdout.count("\n") == len(queries)


def test_resolver_a_failed_run_is_a_binpkg_error_carrying_its_output(tmp_path: Path) -> None:
    """The module's own error: each caller wraps it in its own (FactoryError, AssemblerError)."""
    container = _LocalContainer(tmp_path, {}, fail=True)
    with pytest.raises(BinpkgError) as err:
        resolve_providers(container, [SIMDUTF])
    assert "portageq: boom" in err.value.output
