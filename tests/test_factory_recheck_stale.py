"""ADDITIONS to tests/test_factory_recheck.py (story 019, task 4.1; R2.3): the
factory's ``check-binpkgs`` fails on a planned binpkg built against a stale subslot.
Self-contained so it runs alone; merge into the existing module when materialized.

The image's ``--pretend`` plan is canned; the provider resolver's script runs here
under bash against a fake ``portageq``. The PKGDIR is the host directory bound RW over
``/var/cache/binpkgs``, as the factory binds it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from shidashi import phases
from shidashi.container import CommandResult
from shidashi.recipe import Phase, ResolvedRecipe

pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

SIMDUTF = ">=dev-cpp/simdutf-6.2.0:0"
INDEX = f"""\
ARCH: amd64

BUILD_ID: 1
CPV: x11-libs/vte-0.84.1
PATH: x11-libs/vte/vte-0.84.1-1.gpkg.tar
RDEPEND: {SIMDUTF}/34=

BUILD_ID: 1
CPV: net-misc/curl-8.16.0
PATH: net-misc/curl/curl-8.16.0-1.gpkg.tar
RDEPEND: {SIMDUTF}/36=
"""

_FAKE_PORTAGEQ = """
import json, os, sys
answers = json.loads(os.environ["FAKE_PORTAGEQ"])
args = sys.argv[1:]
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
"""


class _Stage:
    """The shipped stage's container: the plan canned, the rest run here."""

    def __init__(self, tmp: Path, pkgdir: Path, plan: str, resolver_fails: bool = False) -> None:
        self.resolver_fails = resolver_fails
        self.resolver_runs = 0
        self.rootfs = tmp / "rootfs"
        self.rootfs.mkdir()
        self.binds: tuple[tuple[Path, Path], ...] = ()
        self.binds_rw = ((pkgdir, Path("/var/cache/binpkgs")),)
        self.plan = plan
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        tool = self.bindir / "portageq"
        tool.write_text(f"#!{sys.executable}\n{_FAKE_PORTAGEQ}")
        tool.chmod(0o755)

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        argv = list(argv)
        if argv[0] == "emerge":
            return CommandResult(0, "" if "--nodeps" in argv else self.plan, "")
        if any("portageq" in a for a in argv):
            self.resolver_runs += 1
            if self.resolver_fails:
                raise subprocess.CalledProcessError(1, argv, output="", stderr="portageq: boom\n")
        full = {
            **os.environ,
            **(env or {}),
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
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
        phases=(
            Phase(name="base", stage="base", sets=("base",), emptytree=True),
            Phase(name="gnome", stage="gnome", sets=("gnome",), ships=True),
        ),
        portage_layers=("base", "arch/v3", "flavor/gnome", "init/systemd"),
    )


def _stage(
    tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: str,
    *,
    index: bool = True,
    fails: bool = False,
) -> _Stage:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp / "cache"))
    pkgdir = tmp / "cache" / "binpkgs" / "v3" / "20260823T153057Z"
    pkgdir.mkdir(parents=True)
    if index:
        (pkgdir / "Packages").write_text(INDEX)
    return _Stage(tmp, pkgdir, plan, fails)


def test_hostile_a_stale_binpkg_outside_the_plan_does_not_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale vte belongs to another image: this plan installs only curl."""
    container = _stage(tmp_path, monkeypatch, "[binary  N ] net-misc/curl-8.16.0-1::gentoo\n")
    phases.check_binpkgs(container, _recipe(), "gnome")  # type: ignore[arg-type]


def test_a_planned_stale_binpkg_fails_the_check_naming_it_and_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = (
        "[binary   R    ] x11-libs/vte-0.84.1-1::gentoo\n"
        "[binary  N ] net-misc/curl-8.16.0-1::gentoo\n"
    )
    container = _stage(tmp_path, monkeypatch, plan)
    with pytest.raises(phases.FactoryError) as err:
        phases.check_binpkgs(container, _recipe(), "gnome")  # type: ignore[arg-type]
    message = str(err.value)
    assert err.value.phase == "gnome:binpkgs"
    assert "x11-libs/vte-0.84.1" in message and "dev-cpp/simdutf" in message
    assert "0/34 → 0/36" in message
    assert "shidashi factory v3 gnome systemd" in message
    assert "net-misc/curl" not in message  # fresh: not named


STALE_PLAN = "[binary   R    ] x11-libs/vte-0.84.1-1::gentoo\n"


def test_without_an_index_the_check_judges_nothing_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _stage(tmp_path, monkeypatch, STALE_PLAN, index=False)
    phases.check_binpkgs(container, _recipe(), "gnome")  # type: ignore[arg-type]
    assert container.resolver_runs == 0


def test_a_resolver_failure_fails_the_check_with_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _stage(tmp_path, monkeypatch, STALE_PLAN, fails=True)
    with pytest.raises(phases.FactoryError) as err:
        phases.check_binpkgs(container, _recipe(), "gnome")  # type: ignore[arg-type]
    assert err.value.phase == "gnome:binpkgs"
    assert "portageq: boom" in err.value.output
