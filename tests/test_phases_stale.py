"""The factory's ``stale-binpkgs`` step (story 019, task 3.3; R1.2-R1.5).

``run_phase`` runs for real against a local container: the resolver's script and
``emaint`` run here under bash with a fake ``portageq`` and ``emaint`` first on PATH;
``emerge`` is intercepted, and "rebuilds" vte when vte's slot is excluded by writing its
fresh instance into the PKGDIR, as a real build with buildpkg does. The PKGDIR is the
factory's own wiring: a host directory bound RW over ``/var/cache/binpkgs``. The plan
itself is pure and has its own tests (``tests/test_stale_plan.py``); the slot rule
through the step is in ``tests/test_phases_stale_slots.py``.
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

GEN = "20260823T153057Z"
SIMDUTF = ">=dev-cpp/simdutf-6.2.0:0"
HEADER = "ARCH: amd64\nVERSION: 0\n"
#: the slot atom the step excludes and asks the tree about (``shidashi.stale_plan``)
VTE_SLOT = "x11-libs/vte:2.91"
EXCLUDE_VTE = f"--usepkg-exclude={VTE_SLOT}"
#: what the pinned tree answers: simdutf's provider, and the version it picks in vte's slot
TREE = {
    SIMDUTF: ["dev-cpp/simdutf-9.2.1", "0/36"],
    VTE_SLOT: ["x11-libs/vte-0.84.1", "2.91"],
}


def _entry(cpv: str, build: int, rdepend: str = "", slot: str = "") -> str:
    """One index entry; ``SLOT`` only when given (Portage leaves the default, 0, out)."""
    category, name = cpv.split("/")
    path = f"{category}/{name.rsplit('-', 1)[0]}/{name}-{build}.gpkg.tar"
    entry = f"BUILD_ID: {build}\nCPV: {cpv}\nPATH: {path}\nRDEPEND: {rdepend}\n"
    return entry + (f"SLOT: {slot}\n" if slot else "")


VTE_STALE = _entry("x11-libs/vte-0.84.1", 1, f"{SIMDUTF}/34=", slot="2.91")
VTE_FRESH = _entry("x11-libs/vte-0.84.1", 2, f"{SIMDUTF}/36=", slot="2.91")
CURL = _entry("net-misc/curl-8.16.0", 1)


# --- run_phase with the step ----------------------------------------------------------

#: ``portageq`` and ``emaint`` as one script, told apart by the name it is run under;
#: shared with ``tests/test_phases_soname.py``
FAKE_PORTAGEQ_AND_EMAINT = """
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

    def __init__(
        self,
        tmp: Path,
        pkgdir: Path,
        *,
        resolver_fails: bool = False,
        tree: dict[str, list[str]] | None = None,
        later_trees: tuple[dict[str, list[str]], ...] = (),
    ) -> None:
        self.resolver_fails = resolver_fails
        #: what portageq answers in each resolver run, the last one for every run after it
        self.trees = (TREE if tree is None else tree, *later_trees)
        self.resolver_runs = 0
        self.rootfs = tmp / "rootfs"
        self.rootfs.mkdir(parents=True)
        self.pkgdir = pkgdir
        self.binds: tuple[tuple[Path, Path], ...] = ()
        self.binds_rw = ((pkgdir, Path("/var/cache/binpkgs")),)
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        tool = self.bindir / "fake-tool"
        tool.write_text(f"#!{sys.executable}\n{FAKE_PORTAGEQ_AND_EMAINT}")
        tool.chmod(0o755)
        for name in ("portageq", "emaint"):
            (self.bindir / name).symlink_to(tool)
        self.log = tmp / "tools.jsonl"
        self.log.touch()
        self.emerges: list[list[str]] = []
        self.pretends: list[list[str]] = []
        self.stale_file_at_emerge: list[bool] = []
        #: the binpkg file names in the PKGDIR when each stage emerge starts
        self.pool_at_emerge: list[set[str]] = []

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
            self.pretends.append(argv)
            return CommandResult(0, "", "")
        if argv[0] == "emerge":
            self.emerges.append(argv)
            self.stale_file_at_emerge.append(
                (self.pkgdir / "x11-libs/vte/vte-0.84.1-1.gpkg.tar").exists()
            )
            self.pool_at_emerge.append({p.name for p in self.pkgdir.rglob("*.gpkg.tar")})
            if EXCLUDE_VTE in argv:  # compiled only when its slot's binpkgs are excluded
                self._rebuild_vte()
            return CommandResult(0, "[ebuild   R    ] x11-libs/vte-0.84.1\n", "")
        if self.resolver_fails and any("portageq" in a for a in argv):
            raise subprocess.CalledProcessError(1, argv, output="", stderr="portageq: boom\n")
        tree = self.trees[min(self.resolver_runs, len(self.trees) - 1)]
        if argv[0] == "bash" and "portageq best_visible" in argv[-1]:
            self.resolver_runs += 1  # the per-query loop: this python loads no portageq
        full = {
            **os.environ,
            **(env or {}),
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_PKGDIR": str(self.pkgdir),
            "FAKE_PORTAGEQ": json.dumps(tree),
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
    # the SLOT is excluded, not the package: vte's other slots keep their binpkgs
    assert [a for a in emerge if a.startswith("--usepkg-exclude")] == [EXCLUDE_VTE]
    assert container.stale_file_at_emerge == [True]  # still there: nothing fresh yet
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
    assert step["excluded"] == [VTE_SLOT] and step["superseded"] == []
    assert any("vte-0.84.1-1.gpkg.tar" in str(q) for q in step["quarantined"])


def test_a_stale_binpkg_with_a_fresh_instance_moves_aside_before_the_emerge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--usepkg-exclude would drop the FRESH instance too (GOTCHA): no exclusion, and
    the tree is not asked what it picks -- a fresh instance settles it."""
    pkgdir = _binhost(tmp_path, monkeypatch, VTE_STALE, VTE_FRESH)
    container = _Factory(tmp_path, pkgdir)
    step = _run(tmp_path, container)
    assert step["excluded"] == [] and step["superseded"] == []
    asked = [r["argv"][-1] for r in container.tools("portageq") if r["argv"][0] == "best_visible"]
    assert asked == [SIMDUTF]

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
