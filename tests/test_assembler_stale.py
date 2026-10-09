"""ADDITIONS to tests/test_assembler.py (story 019, task 4.2; R2.1, R2.2): the
assemble refuses a plan holding a stale-subslot binpkg before installing anything.

Runs alone by importing the module's fixtures (autouse) and fakes; when merged into
tests/test_assembler.py, drop that import. The ``--pretend`` plan is canned; the
provider resolver's script runs here under bash against a fake ``portageq``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import shidashi.assembler as asm
from shidashi.assembler import Assembler, AssemblerError
from shidashi.container import CommandResult
from tests.test_assembler import (  # noqa: F401 -- autouse fixtures of the module
    _FakeContainer,
    _no_tree_download,
    _pointer,
    _recipe,
    _system_config_stubbed,
    _toolbox_stubbed,
)

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


class _Installed(Exception):
    """The install emerge was reached."""


def _wire(
    tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: str,
    *,
    index: bool = True,
    fails: bool = False,
) -> list[list[str]]:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp / "cache"))
    monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: _pointer())
    monkeypatch.setattr(asm, "fetch_stage3", lambda pointer, *, cache_dir, download: tmp / "s.tar")

    def fake_extract(tarball: Path, rootfs: Path) -> None:
        (rootfs / "lib" / "modules" / "6.12.0").mkdir(parents=True)
        (rootfs / "boot").mkdir(parents=True)
        (rootfs / "etc" / "portage").mkdir(parents=True)

    monkeypatch.setattr(asm, "extract_stage3", fake_extract)
    monkeypatch.setattr(asm, "apply_portage", lambda rootfs, recipe, **_k: None)
    monkeypatch.setattr(asm, "apply_rootfs", lambda *_a, **_k: ())
    monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "portageq").write_text(f"#!{sys.executable}\n{_FAKE_PORTAGEQ}")
    (bindir / "portageq").chmod(0o755)
    cwd = tmp / "cwd"
    cwd.mkdir()
    runs: list[list[str]] = []

    class _Image(_FakeContainer):
        def run(self, argv: list[str], **kw: Any) -> object:
            argv = list(argv)
            runs.append(argv)
            if argv[0] == "emerge":
                if "--pretend" in argv:
                    return CommandResult(0, plan, "")
                raise _Installed(argv)
            if fails and any("portageq" in a for a in argv):
                raise subprocess.CalledProcessError(1, argv, output="", stderr="portageq: boom\n")
            env = kw.get("env") or {}
            full = {
                **os.environ,
                **env,
                "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_PORTAGEQ": json.dumps({SIMDUTF: ["dev-cpp/simdutf-9.2.1", "0/36"]}),
            }
            done = subprocess.run(argv, cwd=cwd, env=full, capture_output=True, text=True)
            if kw.get("check", True) and done.returncode != 0:
                raise subprocess.CalledProcessError(
                    done.returncode, argv, output=done.stdout, stderr=done.stderr
                )
            return CommandResult(done.returncode, done.stdout, done.stderr)

    monkeypatch.setattr(asm, "Container", _Image)
    binhost = tmp / "binhost"
    binhost.mkdir()
    if index:
        (binhost / "Packages").write_text(INDEX)
    return runs


def test_hostile_a_stale_binpkg_outside_the_images_plan_does_not_stop_the_assemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binhost is shared by every image of the arch: vte is another image's."""
    _wire(tmp_path, monkeypatch, "[binary  N ] net-misc/curl-8.16.0-1::gentoo\n")
    with pytest.raises(_Installed):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out")


def test_a_stale_binpkg_in_the_plan_stops_the_assemble_before_any_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = (
        "[binary   R    ] x11-libs/vte-0.84.1-1::gentoo\n"
        "[binary  N ] net-misc/curl-8.16.0-1::gentoo\n"
    )
    runs = _wire(tmp_path, monkeypatch, plan)
    with pytest.raises(AssemblerError) as err:
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out")
    message = str(err.value)
    assert "x11-libs/vte-0.84.1" in message and "dev-cpp/simdutf" in message
    assert "0/34 → 0/36" in message
    assert "shidashi factory znver5 kde systemd" in message
    assert "net-misc/curl" not in message
    installs = [a for a in runs if a[0] == "emerge" and "--pretend" not in a]
    assert installs == []  # nothing was installed


STALE_PLAN = "[binary   R    ] x11-libs/vte-0.84.1-1::gentoo\n"


def test_without_an_index_the_assemble_judges_nothing_and_installs_as_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _wire(tmp_path, monkeypatch, STALE_PLAN, index=False)
    with pytest.raises(_Installed):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out")
    assert not any(any("portageq" in a for a in argv) for argv in runs)


def test_a_resolver_failure_stops_the_assemble_with_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _wire(tmp_path, monkeypatch, STALE_PLAN, fails=True)
    with pytest.raises(AssemblerError, match="portageq: boom"):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out")
    assert [a for a in runs if a[0] == "emerge" and "--pretend" not in a] == []
