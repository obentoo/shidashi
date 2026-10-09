"""The soname judgement and providers first in the ``stale-binpkgs`` step (story 019,
task 3.4; R1.3, R1.4, R1.6, R1.7, R1.8).

The real case of 2026-10-08: vte-0.84.1 build 1 REQUIRES ``libsimdutf.so.34``; the
pinned simdutf-9.2.1 keeps ``SLOT 0/34`` while it PROVIDES ``libsimdutf.so.36``. The
subslot rule sees 0/34 on both sides and judges vte fresh, so only the soname rule
(REQUIRES x PROVIDES) can keep vte's binpkg out of the stage.

Same harness as ``tests/test_phases_stale.py``: ``run_phase`` runs for real against a
local container; the resolver's script and ``emaint`` run here under bash with a fake
``portageq`` and ``emaint``; ``emerge`` is intercepted. Its three shapes are told
apart by their options: ``--pretend`` answers the plan the test sets, ``--oneshot``
(providers first) installs its ``=cpv`` targets into the rootfs's vdb, and the stage
emerge rebuilds vte when vte's binpkg is excluded.
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
ICU = ">=dev-libs/icu-76:0"
HEADER = "ARCH: amd64\nVERSION: 0\n"
#: what the pinned tree answers: simdutf-9.2.1 is SLOT 0/34 (the overlay's bug)
TREE = {SIMDUTF: ["dev-cpp/simdutf-9.2.1", "0/34"]}


def _entry(
    cpv: str,
    build: int,
    *,
    slot: str = "0",
    rdepend: str = "",
    requires: str = "",
    provides: str = "",
) -> str:
    category, name = cpv.split("/")
    path = f"{category}/{name.rsplit('-', 1)[0]}/{name}-{build}.gpkg.tar"
    lines = [f"BUILD_ID: {build}", f"CPV: {cpv}", f"PATH: {path}"]
    if provides:
        lines.append(f"PROVIDES: {provides}")
    lines.append(f"RDEPEND: {rdepend}")
    if requires:
        lines.append(f"REQUIRES: {requires}")
    lines.append(f"SLOT: {slot}")
    return "\n".join(lines) + "\n"


# The index entries, as read on 2026-10-08 (build ids and sonames are the real ones).
VTE_STALE = _entry(
    "x11-libs/vte-0.84.1",
    1,
    slot="2.91",
    rdepend=f"{SIMDUTF}/34=",
    requires="x86_64: libc.so.6 libsimdutf.so.34",
    provides="x86_64: libvte-2.91.so.0",
)
#: the rebuild against simdutf-9.2.1: still records 0/34 (what the tree says), needs .so.36
VTE_FRESH = _entry(
    "x11-libs/vte-0.84.1",
    2,
    slot="2.91",
    rdepend=f"{SIMDUTF}/34=",
    requires="x86_64: libc.so.6 libsimdutf.so.36",
    provides="x86_64: libvte-2.91.so.0",
)
SIMDUTF_BIN = _entry("dev-cpp/simdutf-9.2.1", 1, slot="0/34", provides="x86_64: libsimdutf.so.36")
#: the binpkg vte was built against (binhost of 2026-09-19)
SIMDUTF_OLD = _entry("dev-cpp/simdutf-9.0.0", 1, slot="0/34", provides="x86_64: libsimdutf.so.34")
CURL = _entry("net-misc/curl-8.16.0", 1, requires="x86_64: libc.so.6")

#: plan lines as ``emerge --verbose --pretend`` prints them (slot and repo in the token)
P_SIMDUTF_BIN = "[binary   N    ] dev-cpp/simdutf-9.2.1-1::bentoo  0 KiB"
P_SIMDUTF_EBUILD = "[ebuild   N    ] dev-cpp/simdutf-9.2.1::bentoo  0 KiB"
P_VTE_1 = '[binary   N    ] x11-libs/vte-0.84.1-1:2.91::gentoo  USE="-vala" 0 KiB'
P_VTE_2 = '[binary   N    ] x11-libs/vte-0.84.1-2:2.91::gentoo  USE="-vala" 0 KiB'

#: what installing simdutf-9.2.1 leaves in the vdb
SIMDUTF_INSTALLED = {"dev-cpp/simdutf-9.2.1": "x86_64: libsimdutf.so.36"}

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


def _vdb_install(rootfs: Path, cpv: str, provides: str = "", requires: str = "") -> None:
    entry = rootfs / "var" / "db" / "pkg" / cpv
    entry.mkdir(parents=True, exist_ok=True)
    if provides:
        (entry / "PROVIDES").write_text(provides + "\n")
    if requires:
        (entry / "REQUIRES").write_text(requires + "\n")


class _Factory:
    """The stage container: emerge intercepted, everything else run here."""

    def __init__(
        self,
        tmp: Path,
        pkgdir: Path,
        *,
        plan: list[str],
        tree: dict[str, list[str]] | None = None,
        installs: dict[str, str] | None = None,
        oneshot_fails: bool = False,
    ) -> None:
        self.plan = plan
        self.tree = TREE if tree is None else tree
        self.installs = installs or {}
        self.oneshot_fails = oneshot_fails
        self.rootfs = tmp / "rootfs"
        self.rootfs.mkdir()
        # the fork point's own libraries: glibc offers libc.so.6 to every plan
        _vdb_install(self.rootfs, "sys-libs/glibc-2.42", provides="x86_64: libc.so.6 libm.so.6")
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
        self.stale_file_at_stage: list[bool] = []

    # --- what the test reads back -------------------------------------------------

    def tools(self, name: str) -> list[dict[str, Any]]:
        lines = self.log.read_text().splitlines()
        return [r for r in map(json.loads, lines) if r["tool"] == name]

    def pretends(self) -> list[list[str]]:
        return [a for a in self.emerges if "--pretend" in a]

    def oneshots(self) -> list[list[str]]:
        return [a for a in self.emerges if "--oneshot" in a and "--pretend" not in a]

    def stage_emerges(self) -> list[list[str]]:
        return [a for a in self.emerges if "--pretend" not in a and "--oneshot" not in a]

    # --- emerge -------------------------------------------------------------------

    def _installed(self, cpv: str) -> bool:
        return (self.rootfs / "var" / "db" / "pkg" / cpv).is_dir()

    def _pretend(self) -> str:
        """The plan; an ``[ebuild]`` already installed (providers first) drops out."""
        kept = []
        for line in self.plan:
            if line.startswith("[ebuild"):
                cpv = line.split("]", 1)[1].split()[0].split(":", 1)[0]
                if self._installed(cpv):
                    continue
            kept.append(line)
        return "\n".join(kept) + "\n"

    def _oneshot(self, argv: list[str]) -> CommandResult:
        if self.oneshot_fails:
            raise subprocess.CalledProcessError(
                1, argv, output="", stderr="!!! simdutf: compile failed\n"
            )
        for target in (a for a in argv[1:] if a.startswith("=")):
            cpv = target[1:]
            _vdb_install(self.rootfs, cpv, provides=self.installs.get(cpv, ""))
        return CommandResult(0, "", "")

    def _stage(self, argv: list[str]) -> CommandResult:
        self.stale_file_at_stage.append(
            (self.pkgdir / "x11-libs/vte/vte-0.84.1-1.gpkg.tar").exists()
        )
        for cpv, provides in self.installs.items():  # the stage installs the providers too
            _vdb_install(self.rootfs, cpv, provides=provides)
        if "--usepkg-exclude=x11-libs/vte" not in argv:
            return CommandResult(0, self._pretend(), "")
        # vte compiled from its ebuild: buildpkg writes its new instance into the PKGDIR
        fresh = self.pkgdir / "x11-libs/vte/vte-0.84.1-2.gpkg.tar"
        fresh.parent.mkdir(parents=True, exist_ok=True)
        fresh.write_bytes(b"fresh vte")
        index = self.pkgdir / "Packages"
        index.write_text(index.read_text().rstrip("\n") + "\n\n" + VTE_FRESH)
        _vdb_install(
            self.rootfs,
            "x11-libs/vte-0.84.1",
            provides="x86_64: libvte-2.91.so.0",
            requires="x86_64: libc.so.6 libsimdutf.so.36",
        )
        return CommandResult(0, "[ebuild   N    ] x11-libs/vte-0.84.1:2.91::gentoo\n", "")

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        argv = list(argv)
        if argv[0] == "emerge":
            self.emerges.append(argv)
            if "--pretend" in argv:
                return CommandResult(0, self._pretend(), "")
            if "--oneshot" in argv:
                return self._oneshot(argv)
            return self._stage(argv)
        full = {
            **os.environ,
            **(env or {}),
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_PKGDIR": str(self.pkgdir),
            "FAKE_PORTAGEQ": json.dumps(self.tree),
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


def _today() -> list[str]:
    return phases.phase_emerge_argv(GNOME, _recipe(), emptytree=False)


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


def _targets(argv: list[str]) -> list[str]:
    return [a for a in argv[1:] if not a.startswith("-")]


# --- hostile halves: which index instance a plan token denotes --------------------------


def test_hostile_a_planned_fresh_build_does_not_condemn_its_stale_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong collapse: the plan takes vte build 2 (needs .so.36). Build 1 of the SAME
    cpv needs .so.34 but is not planned: a match by cpv alone would judge it."""
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, VTE_STALE, VTE_FRESH, CURL)
    container = _Factory(tmp_path, pkgdir, plan=[P_SIMDUTF_BIN, P_VTE_2])
    step = _run(tmp_path, container)

    assert step.get("soname", []) == [], step
    assert container.stage_emerges() == [_today()]
    assert container.oneshots() == []
    assert _quarantined(tmp_path) == []


def test_hostile_a_planned_token_with_slot_and_repo_is_its_index_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong split: ``x11-libs/vte-0.84.1-1:2.91::gentoo`` IS index CPV vte-0.84.1 with
    BUILD_ID 1. It needs .so.34 while the plan offers .so.36: stale by soname. Build 2
    is fresh, so build 1 moves aside BEFORE the emerge and nothing is excluded
    (``--usepkg-exclude`` would drop the fresh instance too)."""
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, VTE_STALE, VTE_FRESH, CURL)
    container = _Factory(tmp_path, pkgdir, plan=[P_SIMDUTF_BIN, P_VTE_1])
    step = _run(tmp_path, container)

    (entry,) = step["soname"]
    assert entry["cpv"] == "x11-libs/vte-0.84.1" and str(entry["build_id"]) == "1"
    assert container.stage_emerges() == [_today()]
    assert container.stale_file_at_stage == [False]  # moved before the stage emerge
    assert [p.name for p in _quarantined(tmp_path)] == ["vte-0.84.1-1.gpkg.tar"]
    assert (pkgdir / "x11-libs/vte/vte-0.84.1-2.gpkg.tar").is_file()
    assert container.tools("emaint")  # the index no longer names the moved file
    assert "vte-0.84.1-1.gpkg.tar" not in (pkgdir / "Packages").read_text()


# --- the soname rule alone: simdutf planned as a binary ---------------------------------


def test_the_soname_rule_alone_excludes_vte_when_the_subslot_rule_sees_it_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-10-08 case: the tree answers simdutf 0/34, vte recorded 0/34 (fresh by
    subslot); the plan's simdutf binpkg offers only .so.36, vte needs .so.34."""
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, VTE_STALE, CURL)
    container = _Factory(
        tmp_path, pkgdir, plan=[P_SIMDUTF_BIN, P_VTE_1], installs=SIMDUTF_INSTALLED
    )
    step = _run(tmp_path, container)

    # the subslot rule was asked and found nothing: only the soname rule fires
    assert any(r["argv"][:1] == ["best_visible"] for r in container.tools("portageq"))
    assert step.get("stale", []) == [], step
    # planned with today's argv (no subslot exclusion), then one stage emerge excluding vte
    assert container.pretends() and container.pretends()[0] == [*_today(), "--pretend"]
    (stage,) = container.stage_emerges()
    assert "--usepkg-exclude=x11-libs/vte" in stage
    assert not any(a.startswith("--usepkg-exclude=dev-cpp") for a in stage)
    assert not any(a.startswith("--usepkg-exclude=net-misc") for a in stage)
    assert container.oneshots() == []  # simdutf is a binary in the plan: no providers first
    # rebuilt, then the stale instance moves aside by PATH; the fresh one stays
    (moved,) = _quarantined(tmp_path)
    assert moved.name == "vte-0.84.1-1.gpkg.tar" and moved.parent.name == "x11-libs"
    assert f"/quarantine/binpkgs/v3/{GEN}/" in str(moved)
    assert (pkgdir / "x11-libs/vte/vte-0.84.1-2.gpkg.tar").is_file()
    assert (pkgdir / "dev-cpp/simdutf/simdutf-9.2.1-1.gpkg.tar").is_file()
    assert "vte-0.84.1-1.gpkg.tar" not in (pkgdir / "Packages").read_text()
    # R1.3: the step names the package, the soname it needs and the one offered
    (entry,) = step["soname"]
    assert entry["cpv"] == "x11-libs/vte-0.84.1" and str(entry["build_id"]) == "1"
    assert "libsimdutf.so.34" in str(entry["needs"])
    assert "libsimdutf.so.36" in str(entry["offered"])
    assert "libsimdutf.so.34" not in str(entry["offered"])
    assert "x11-libs/vte" in step["excluded"]
    assert any("vte-0.84.1-1.gpkg.tar" in str(q) for q in step["quarantined"])
    assert step.get("providers_first", []) == []
    assert step.get("unresolved_sonames", []) == []


# --- providers first: simdutf planned from its ebuild -----------------------------------


def test_a_provider_compiled_from_its_ebuild_is_installed_first_then_vte_is_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No binpkg of simdutf-9.2.1: the plan compiles it, so before the stage nothing
    offers libsimdutf.so at all. The old simdutf-9.0.0 binpkg names dev-cpp/simdutf
    as the library's provider; the PLANNED version is the one compiled first.
    Decoy: dev-libs/foo, also planned from its ebuild, provides libsimdutf.so.34 for
    another ABI (x86_32): not a provider of the x86_64 library."""
    foo_old = _entry("dev-libs/foo-0.9", 1, provides="x86_32: libsimdutf.so.34")
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_OLD, VTE_STALE, foo_old, CURL)
    plan = [P_SIMDUTF_EBUILD, "[ebuild   N    ] dev-libs/foo-1.0::gentoo  0 KiB", P_VTE_1]
    container = _Factory(tmp_path, pkgdir, plan=plan, installs=SIMDUTF_INSTALLED)
    step = _run(tmp_path, container)

    # ONE providers-first emerge, of the planned cpv only, with the stage's options
    (oneshot,) = container.oneshots()
    assert _targets(oneshot) == ["=dev-cpp/simdutf-9.2.1"]
    assert "--usepkg" in oneshot
    # before the stage emerge, and the stage emerge then excludes vte
    (stage,) = container.stage_emerges()
    assert container.emerges.index(oneshot) < container.emerges.index(stage)
    assert "--usepkg-exclude=x11-libs/vte" in stage
    # judged against the vdb as re-read after the providers: needs .so.34, offered .so.36
    (entry,) = step["soname"]
    assert entry["cpv"] == "x11-libs/vte-0.84.1" and str(entry["build_id"]) == "1"
    assert "libsimdutf.so.34" in str(entry["needs"])
    assert "libsimdutf.so.36" in str(entry["offered"])
    assert any("dev-cpp/simdutf-9.2.1" in str(p) for p in step["providers_first"])
    assert not any("dev-libs/foo" in str(p) for p in step["providers_first"])
    assert step.get("unresolved_sonames", []) == []
    assert [p.name for p in _quarantined(tmp_path)] == ["vte-0.84.1-1.gpkg.tar"]


def test_a_failed_providers_first_emerge_stops_the_stage_before_its_emerge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_OLD, VTE_STALE, CURL)
    container = _Factory(tmp_path, pkgdir, plan=[P_SIMDUTF_EBUILD, P_VTE_1], oneshot_fails=True)
    with pytest.raises(phases.FactoryError) as err:
        phases.run_phase(container, _recipe(), GNOME, emptytree=False)  # type: ignore[arg-type]
    assert err.value.phase == "gnome:stale-binpkgs"
    assert "simdutf: compile failed" in err.value.output
    assert len(container.oneshots()) == 1
    assert container.stage_emerges() == []
    assert _quarantined(tmp_path) == []
    assert (pkgdir / "x11-libs/vte/vte-0.84.1-1.gpkg.tar").is_file()


# --- R1.8: a soname nobody offers -------------------------------------------------------


def test_a_soname_no_package_offers_is_unresolved_and_excludes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vte needs libnobody.so.3: no planned, installed or indexed package offers
    libnobody.so at any version. Recorded, never stale, no providers first."""
    vte = _entry(
        "x11-libs/vte-0.84.1",
        1,
        slot="2.91",
        rdepend=f"{SIMDUTF}/34=",
        requires="x86_64: libc.so.6 libnobody.so.3 libsimdutf.so.36",
    )
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, vte, CURL)
    container = _Factory(tmp_path, pkgdir, plan=[P_SIMDUTF_BIN, P_VTE_1])
    step = _run(tmp_path, container)

    assert container.stage_emerges() == [_today()]
    assert container.oneshots() == []
    assert _quarantined(tmp_path) == []
    assert container.tools("emaint") == []
    assert step.get("soname", []) == []
    assert step.get("excluded", []) == []
    unresolved = json.dumps(step["unresolved_sonames"])
    assert "libnobody.so.3" in unresolved and "x11-libs/vte-0.84.1" in unresolved
    assert "libc.so.6" not in unresolved  # offered by the rootfs's glibc
    assert "libsimdutf.so.36" not in unresolved  # offered by the plan's simdutf


# --- the pretend carries the subslot pass's exclusions ----------------------------------


def test_the_stage_is_planned_with_the_subslot_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nodejs was built against icu 0/77, the tree ships 0/78: excluded by the subslot
    rule, so the plan the soname rule judges already compiles nodejs."""
    nodejs = _entry("net-libs/nodejs-26.9.0", 1, rdepend=f"{ICU}/77=")
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, VTE_FRESH, nodejs, CURL)
    tree = {**TREE, ICU: ["dev-libs/icu-78.1", "0/78"]}
    container = _Factory(tmp_path, pkgdir, plan=[P_SIMDUTF_BIN, P_VTE_2], tree=tree)
    _run(tmp_path, container)

    excluded = phases.phase_emerge_argv(
        GNOME, _recipe(), emptytree=False, usepkg_exclude=["net-libs/nodejs"]
    )
    assert container.pretends()[0] == [*excluded, "--pretend"]
    assert container.stage_emerges() == [excluded]
    assert container.oneshots() == []


# --- R1.4: nothing stale by either rule -------------------------------------------------


def test_nothing_stale_by_either_rule_emerges_as_today_with_no_extra_emerge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = _binhost(tmp_path, monkeypatch, SIMDUTF_BIN, VTE_FRESH, CURL)
    container = _Factory(tmp_path, pkgdir, plan=[P_SIMDUTF_BIN, P_VTE_2])
    step = _run(tmp_path, container)

    assert container.pretends()[0] == [*_today(), "--pretend"]
    assert container.stage_emerges() == [_today()]
    assert container.oneshots() == []
    assert _quarantined(tmp_path) == []
    assert container.tools("emaint") == []
    assert step["soname"] == []
    assert step["providers_first"] == []
    assert step["unresolved_sonames"] == []
