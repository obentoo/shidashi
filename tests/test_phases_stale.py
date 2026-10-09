"""The factory's ``stale-binpkgs`` step (story 019, task 3.3; R1.2-R1.5).

``plan_stale`` is pure. ``run_phase`` runs for real against a local container: the
resolver's script and ``emaint`` run here under bash with a fake ``portageq`` and
``emaint`` first on PATH; ``emerge`` is intercepted, and "rebuilds" vte by writing
its fresh instance into the PKGDIR, as a real build with buildpkg does. The PKGDIR
is the factory's own wiring: a host directory bound RW over ``/var/cache/binpkgs``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, phases
from shidashi.container import CommandResult
from shidashi.recipe import Phase, ResolvedRecipe
from tests._pending import try_import

parse_index: Any = try_import("shidashi.binpkgs", "parse_index")
stale: Any = try_import("shidashi.binpkgs", "stale")
plan_stale: Any = try_import("shidashi.phases", "plan_stale")

GEN = "20260823T153057Z"
SIMDUTF = ">=dev-cpp/simdutf-6.2.0:0"
HEADER = "ARCH: amd64\nVERSION: 0\n"


def _entry(cpv: str, build: int, rdepend: str = "") -> str:
    category, name = cpv.split("/")
    path = f"{category}/{name.rsplit('-', 1)[0]}/{name}-{build}.gpkg.tar"
    return f"BUILD_ID: {build}\nCPV: {cpv}\nPATH: {path}\nRDEPEND: {rdepend}\n"


VTE_STALE = _entry("x11-libs/vte-0.84.1", 1, f"{SIMDUTF}/34=")
VTE_FRESH = _entry("x11-libs/vte-0.84.1", 2, f"{SIMDUTF}/36=")
CURL = _entry("net-misc/curl-8.16.0", 1)


# --- plan_stale (pure) ----------------------------------------------------------------


def test_plan_hostile_a_fresh_instance_rescues_only_its_own_version() -> None:
    """vte-0.84.1 has a fresh instance: quarantine its stale one now, no exclusion.
    foo-1.0 is stale and only foo-2.0 is fresh: another VERSION does not rescue it.
    nodejs has no fresh instance: excluded, quarantined after its rebuild."""
    index = "\n".join(
        [
            HEADER,
            VTE_STALE,
            VTE_FRESH,
            _entry("dev-libs/foo-1.0", 1, f"{SIMDUTF}/34="),
            _entry("dev-libs/foo-2.0", 1, f"{SIMDUTF}/36="),
            _entry("net-libs/nodejs-26.9.0", 1, f"{SIMDUTF}/34="),
        ]
    )
    instances = parse_index(index)
    plan = plan_stale(stale(instances, {SIMDUTF: "0/36"}), instances)
    assert set(plan.exclude) == {"dev-libs/foo", "net-libs/nodejs"}
    assert {i.path for i in plan.quarantine_now} == {"x11-libs/vte/vte-0.84.1-1.gpkg.tar"}
    assert {i.path for i in plan.quarantine_after} == {
        "dev-libs/foo/foo-1.0-1.gpkg.tar",
        "net-libs/nodejs/nodejs-26.9.0-1.gpkg.tar",
    }


# --- run_phase with the step ----------------------------------------------------------

_FAKE_TOOL = """
import json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"tool": name, "argv": args, "pkgdir": os.environ.get("PKGDIR")}) + "\\n")
if name == "portageq":
    answers = json.loads(os.environ["FAKE_PORTAGEQ"])
    if args[:1] == ["best_visible"]:
        hit = answers.get(args[-1])
        print(hit[0] if hit else "")
        sys.exit(0 if hit else 1)
    if args[:1] == ["metadata"] and args[-1] == "SLOT":
        slots = {cpv: slot for cpv, slot in answers.values()}
        if args[-2] in slots:
            print(slots[args[-2]])
            sys.exit(0)
    sys.exit(1)
if name == "emaint" and "binhost" in args:
    inside = os.environ.get("PKGDIR") or "/var/cache/binpkgs"
    root = Path(os.environ["FAKE_PKGDIR"] if inside == "/var/cache/binpkgs" else inside)
    kept = []
    for block in (root / "Packages").read_text().split("\\n\\n"):
        fields = dict(l.split(": ", 1) for l in block.splitlines() if ": " in l)
        if "PATH" not in fields or (root / fields["PATH"]).is_file():
            kept.append(block.strip("\\n"))
    (root / "Packages").write_text("\\n\\n".join(kept) + "\\n")
sys.exit(0)
"""


class _Factory:
    """The stage container: emerge intercepted, everything else run here."""

    def __init__(self, tmp: Path, pkgdir: Path, *, resolver_fails: bool = False) -> None:
        self.resolver_fails = resolver_fails
        self.rootfs = tmp / "rootfs"
        self.rootfs.mkdir()
        self.pkgdir = pkgdir
        self.binds: tuple[tuple[Path, Path], ...] = ()
        self.binds_rw = ((pkgdir, Path("/var/cache/binpkgs")),)
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        tool = self.bindir / "fake-tool"
        tool.write_text(f"#!{sys.executable}\n{_FAKE_TOOL}")
        tool.chmod(0o755)
        for name in ("portageq", "emaint"):
            (self.bindir / name).symlink_to(tool)
        self.log = tmp / "tools.jsonl"
        self.log.touch()
        self.emerges: list[list[str]] = []
        self.stale_file_at_emerge: list[bool] = []

    def tools(self, name: str) -> list[dict[str, Any]]:
        lines = self.log.read_text().splitlines()
        return [r for r in map(json.loads, lines) if r["tool"] == name]

    def _rebuild_vte(self) -> None:
        fresh = self.pkgdir / "x11-libs/vte/vte-0.84.1-2.gpkg.tar"
        if not fresh.exists():
            fresh.write_bytes(b"fresh vte")
            index = self.pkgdir / "Packages"
            index.write_text(index.read_text().rstrip("\n") + "\n\n" + VTE_FRESH)

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        argv = list(argv)
        if argv[0] == "emerge" and "--pretend" in argv:
            # the soname pass's plan (3.4): nothing planned, not a stage emerge
            return CommandResult(0, "", "")
        if argv[0] == "emerge":
            self.emerges.append(argv)
            self.stale_file_at_emerge.append(
                (self.pkgdir / "x11-libs/vte/vte-0.84.1-1.gpkg.tar").exists()
            )
            self._rebuild_vte()
            return CommandResult(0, "[ebuild   R    ] x11-libs/vte-0.84.1\n", "")
        if self.resolver_fails and any("portageq" in a for a in argv):
            raise subprocess.CalledProcessError(1, argv, output="", stderr="portageq: boom\n")
        full = {
            **os.environ,
            **(env or {}),
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_PKGDIR": str(self.pkgdir),
            "FAKE_PORTAGEQ": json.dumps({SIMDUTF: ["dev-cpp/simdutf-9.2.1", "0/36"]}),
        }
        done = subprocess.run(
            argv, cwd=self.rootfs, env=full, capture_output=True, text=True, check=False
        )
        if check and done.returncode != 0:
            raise subprocess.CalledProcessError(
                done.returncode, argv, output=done.stdout, stderr=done.stderr
            )
        return CommandResult(done.returncode, done.stdout, done.stderr)


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="gnome",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=("gnome",),
        phases=(),
        portage_layers=("base", "arch/v3", "flavor/gnome", "init/systemd"),
    )


GNOME = Phase(name="gnome", stage="gnome", sets=("gnome",))


def _binhost(tmp: Path, monkeypatch: pytest.MonkeyPatch, *entries: str) -> Path:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp / "cache"))
    pkgdir = tmp / "cache" / "binpkgs" / "v3" / GEN
    pkgdir.mkdir(parents=True)
    for entry in entries:
        path = pkgdir / dict(line.split(": ", 1) for line in entry.splitlines())["PATH"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"binpkg")
    (pkgdir / "Packages").write_text("\n".join([HEADER, *entries]))
    return pkgdir


def _run(tmp: Path, container: _Factory) -> dict[str, Any]:  # the step's recorded data
    with audit.run(tmp / "runs", command="factory", argv=[]) as trail:
        phases.run_phase(container, _recipe(), GNOME, emptytree=False)  # type: ignore[arg-type]
    manifest = audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))
    steps = [s for s in manifest["steps"] if str(s["step"]).endswith("stale-binpkgs")]
    assert len(steps) == 1, [s["step"] for s in manifest["steps"]]
    return dict(steps[0])


def _quarantined(tmp: Path) -> list[Path]:
    return sorted(p for p in (tmp / "cache" / "quarantine").rglob("*.gpkg.tar"))


def test_a_version_with_only_a_stale_binpkg_is_compiled_then_its_old_binpkg_moves_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-10-08 case: vte built against simdutf 0/34, the tree ships 0/36."""
    pkgdir = _binhost(tmp_path, monkeypatch, VTE_STALE, CURL)
    container = _Factory(tmp_path, pkgdir)
    step = _run(tmp_path, container)

    (emerge,) = container.emerges
    assert "--usepkg-exclude=x11-libs/vte" in emerge
    assert not any(a.startswith("--usepkg-exclude=net-misc") for a in emerge)
    # after the rebuild: the stale instance is in the quarantine, by PATH; the fresh stays
    (moved,) = _quarantined(tmp_path)
    assert moved.name == "vte-0.84.1-1.gpkg.tar" and moved.parent.name == "x11-libs"
    assert f"/quarantine/binpkgs/v3/{GEN}/" in str(moved)
    assert (pkgdir / "x11-libs/vte/vte-0.84.1-2.gpkg.tar").is_file()
    assert (pkgdir / "net-misc/curl/curl-8.16.0-1.gpkg.tar").is_file()
    # the index was regenerated: it no longer names the moved file
    assert container.tools("emaint")
    assert "vte-0.84.1-1.gpkg.tar" not in (pkgdir / "Packages").read_text()
    # R1.3: the step names the package, its dependency and both subslots
    (entry,) = step["stale"]
    assert entry["cpv"] == "x11-libs/vte-0.84.1" and str(entry["build_id"]) == "1"
    assert entry["dep"] == ">=dev-cpp/simdutf-6.2.0:0/34="  # serialized as dep.atom
    assert (entry["built"], entry["tree"]) == ("0/34", "0/36")
    assert "x11-libs/vte" in step["excluded"]
    assert any("vte-0.84.1-1.gpkg.tar" in str(q) for q in step["quarantined"])


def test_a_stale_binpkg_with_a_fresh_instance_moves_aside_before_the_emerge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--usepkg-exclude would drop the FRESH instance too (GOTCHA): no exclusion."""
    pkgdir = _binhost(tmp_path, monkeypatch, VTE_STALE, VTE_FRESH)
    container = _Factory(tmp_path, pkgdir)
    _run(tmp_path, container)

    (emerge,) = container.emerges
    assert emerge == phases.phase_emerge_argv(GNOME, _recipe(), emptytree=False)
    assert container.stale_file_at_emerge == [False]  # moved before the emerge ran
    assert [p.name for p in _quarantined(tmp_path)] == ["vte-0.84.1-1.gpkg.tar"]
    assert (pkgdir / "x11-libs/vte/vte-0.84.1-2.gpkg.tar").is_file()


@pytest.mark.parametrize("index", ["fresh", "absent"])
def test_without_a_stale_binpkg_the_stage_emerges_as_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: str
) -> None:
    """R1.4 and the unchanged behavior: matching binpkgs are merged, nothing moves."""
    pkgdir = _binhost(tmp_path, monkeypatch, VTE_FRESH, CURL)
    if index == "absent":
        (pkgdir / "Packages").unlink()
    container = _Factory(tmp_path, pkgdir)
    step = _run(tmp_path, container)
    if index == "absent":
        assert step.get("index") == "absent", step

    assert container.emerges == [phases.phase_emerge_argv(GNOME, _recipe(), emptytree=False)]
    assert _quarantined(tmp_path) == []
    assert container.tools("emaint") == []


def test_a_resolver_failure_stops_the_stage_before_its_emerge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """binpkgs' own error becomes the factory's, with the stage's phase and the output."""
    pkgdir = _binhost(tmp_path, monkeypatch, VTE_STALE, CURL)
    container = _Factory(tmp_path, pkgdir, resolver_fails=True)
    with pytest.raises(phases.FactoryError) as err:
        phases.run_phase(container, _recipe(), GNOME, emptytree=False)  # type: ignore[arg-type]
    assert err.value.phase == "gnome:stale-binpkgs"
    assert "portageq: boom" in err.value.output
    assert container.emerges == []
    assert _quarantined(tmp_path) == []
