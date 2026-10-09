"""The factory's ``check-binpkgs`` fails on a soname-stale plan (story 019, task 4.3;
R2.3, R1.8).

The real case of 2026-10-08: vte-0.84.1 build 1 REQUIRES ``libsimdutf.so.34``; the
pinned simdutf-9.2.1 keeps ``SLOT 0/34`` while it PROVIDES ``libsimdutf.so.36``. The
resolver here answers ``0/34`` for simdutf, so the SUBSLOT rule sees 0/34 on both sides
and judges vte fresh: in these fixtures only the SONAME rule can fail the check.

The check runs with ``--emptytree``: the plan is the whole root, so the pool is the
``provides`` of the plan's own instances -- never the rest of the index. Planned
instances are matched to index entries by cpv AND build id.

Same harness as ``tests/test_factory_recheck_stale.py`` (task 4.1): the ``--pretend``
plan is canned; the resolver's script runs here under bash against a fake
``portageq``; the PKGDIR is the host directory bound RW over ``/var/cache/binpkgs``.
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
#: what the pinned tree answers: simdutf-9.2.1 is SLOT 0/34 (the overlay's bug), so the
#: subslot rule finds every ``:0/34=`` dep below fresh
TREE = {SIMDUTF: ["dev-cpp/simdutf-9.2.1", "0/34"]}
HEADER = "ARCH: amd64\nVERSION: 0\n"


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


def _index(*entries: str) -> str:
    return "\n".join((HEADER, *entries))


# The index entries, as read on 2026-10-08 (build ids and sonames are the real ones).
VTE_STALE = _entry(
    "x11-libs/vte-0.84.1",
    1,
    slot="2.91",
    rdepend=f"{SIMDUTF}/34=",
    requires="x86_64: libc.so.6 libsimdutf.so.34",
    provides="x86_64: libvte-2.91.so.0",
)
#: the rebuild against simdutf-9.2.1: same cpv, still records 0/34, needs .so.36
VTE_FRESH = _entry(
    "x11-libs/vte-0.84.1",
    2,
    slot="2.91",
    rdepend=f"{SIMDUTF}/34=",
    requires="x86_64: libc.so.6 libsimdutf.so.36",
    provides="x86_64: libvte-2.91.so.0",
)
SIMDUTF_BIN = _entry("dev-cpp/simdutf-9.2.1", 1, slot="0/34", provides="x86_64: libsimdutf.so.36")
#: the binpkg vte build 1 was built against (binhost of 2026-09-19): in the index only
SIMDUTF_OLD = _entry("dev-cpp/simdutf-9.0.0", 1, slot="0/34", provides="x86_64: libsimdutf.so.34")
GLIBC = _entry("sys-libs/glibc-2.42", 1, slot="2.2", provides="x86_64: libc.so.6")
CURL = _entry("net-misc/curl-8.16.0", 1, requires="x86_64: libc.so.6")
#: a library nobody, anywhere, offers at any version
NEEDS_GONE = _entry("app-misc/needs-1", 1, requires="x86_64: libc.so.6 libgone.so.3")

#: plan lines as ``emerge --verbose --pretend`` prints them (slot, repo and USE in the line)
P_SIMDUTF = "[binary   N    ] dev-cpp/simdutf-9.2.1-1::bentoo  0 KiB"
P_VTE_1 = '[binary   N    ] x11-libs/vte-0.84.1-1:2.91::gentoo  USE="-vala" 0 KiB'
P_VTE_2 = '[binary   N    ] x11-libs/vte-0.84.1-2:2.91::gentoo  USE="-vala" 0 KiB'
P_GLIBC = "[binary   N    ] sys-libs/glibc-2.42-1:2.2::gentoo  0 KiB"
P_CURL = "[binary  N ] net-misc/curl-8.16.0-1::gentoo  0 KiB"
P_NEEDS_GONE = "[binary   N    ] app-misc/needs-1-1::gentoo  0 KiB"

#: the refusal's soname line (describe, D1)
VTE_LINE = "x11-libs/vte-0.84.1 (build 1): needs libsimdutf.so.34 — offered libsimdutf.so.36"
REBUILD = "shidashi factory v3 gnome systemd"

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
    """The shipped stage's container: the plan canned, the resolver run here."""

    def __init__(self, tmp: Path, pkgdir: Path, plan: str) -> None:
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
        full = {
            **os.environ,
            **(env or {}),
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PORTAGEQ": json.dumps(TREE),
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


def _stage(tmp: Path, monkeypatch: pytest.MonkeyPatch, index: str, *plan: str) -> _Stage:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp / "cache"))
    pkgdir = tmp / "cache" / "binpkgs" / "v3" / "20260823T153057Z"
    pkgdir.mkdir(parents=True)
    (pkgdir / "Packages").write_text(index)
    return _Stage(tmp, pkgdir, "".join(f"{line}\n" for line in plan))


def _check(container: _Stage) -> None:
    phases.check_binpkgs(container, _recipe(), "gnome")  # type: ignore[arg-type]


FULL_INDEX = _index(VTE_STALE, VTE_FRESH, SIMDUTF_BIN, GLIBC, CURL)


# --- hostile: what is NOT in the plan is never judged ------------------------------------


def test_hostile_a_soname_stale_binpkg_outside_the_plan_does_not_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vte build 1 is soname-stale, but this image installs only simdutf, glibc and curl."""
    container = _stage(tmp_path, monkeypatch, FULL_INDEX, P_SIMDUTF, P_GLIBC, P_CURL)
    _check(container)


def test_hostile_the_stale_build_of_a_planned_cpv_is_not_judged_for_its_fresh_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same cpv, two builds: the plan takes build 2 (needs .so.36, offered). Matching the
    plan by cpv alone would judge build 1 too and fail a plan that holds no stale binpkg."""
    container = _stage(tmp_path, monkeypatch, FULL_INDEX, P_SIMDUTF, P_VTE_2, P_GLIBC)
    _check(container)


# --- R1.8: a soname offered at no version in the plan never fails -----------------------


def test_a_soname_nobody_offers_does_not_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``libgone.so.3`` is offered by no package at any version: unresolved, not stale."""
    index = _index(NEEDS_GONE, GLIBC)
    container = _stage(tmp_path, monkeypatch, index, P_NEEDS_GONE, P_GLIBC)
    _check(container)


def test_hostile_a_soname_offered_only_outside_the_plan_does_not_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vte build 1 is planned, simdutf is not: the plan offers ``libsimdutf.so`` at no
    version, so the requirement is unresolved. A pool built from the whole index would see
    simdutf-9.2.1's ``.so.36`` there and fail the check wrongly."""
    container = _stage(tmp_path, monkeypatch, FULL_INDEX, P_VTE_1, P_GLIBC, P_CURL)
    _check(container)


def test_hostile_an_unplanned_provider_of_the_needed_soname_does_not_rescue_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index still holds simdutf-9.0.0 offering ``.so.34``, but the plan installs
    simdutf-9.2.1 (``.so.36``): vte build 1 is stale. A pool built from the whole index
    would see ``.so.34`` offered and let the plan through."""
    index = _index(VTE_STALE, VTE_FRESH, SIMDUTF_OLD, SIMDUTF_BIN, GLIBC, CURL)
    container = _stage(tmp_path, monkeypatch, index, P_SIMDUTF, P_VTE_1, P_GLIBC, P_CURL)
    with pytest.raises(phases.FactoryError) as err:
        _check(container)
    assert err.value.phase == "gnome:binpkgs"
    assert VTE_LINE in str(err.value)


# --- the refusal ------------------------------------------------------------------------


def test_a_planned_soname_stale_binpkg_fails_the_check_by_the_soname_rule_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vte build 1 needs ``.so.34`` beside the planned simdutf-9.2.1 offering ``.so.36``.

    The plan token carries slot, repository and USE (``vte-0.84.1-1:2.91::gentoo``): it
    must still reach the index entry ``CPV: x11-libs/vte-0.84.1`` + ``BUILD_ID: 1``.
    """
    container = _stage(tmp_path, monkeypatch, FULL_INDEX, P_SIMDUTF, P_VTE_1, P_GLIBC, P_CURL)
    with pytest.raises(phases.FactoryError) as err:
        _check(container)
    message = str(err.value)
    assert err.value.phase == "gnome:binpkgs"
    assert "x11-libs/vte-0.84.1" in message
    assert "libsimdutf.so.34" in message and "libsimdutf.so.36" in message
    assert VTE_LINE in message
    assert REBUILD in message
    # the subslot rule was asked (one resolver run) and saw 0/34 on both sides: it adds
    # no line, so the failure is the soname rule's alone
    assert container.resolver_runs == 1
    assert "→" not in message
    assert f"{SIMDUTF}/34=" not in message
    # fresh instances are not named
    assert "x11-libs/vte-0.84.1 (build 2)" not in message
    assert "net-misc/curl" not in message
    assert "dev-cpp/simdutf-9.2.1" not in message
