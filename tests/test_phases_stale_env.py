"""The stale-binpkgs reindex carries PKGDIR in its argv (story 019, task 8.3).

systemd-nspawn passes no host environment into the container: a ``run(..., env=)``
never reaches the command. The 3.3 harness merges ``env=`` into its child, so it
cannot tell the two apart; this double DROPS ``env=``, and its ``emaint`` reindexes
the stage's PKGDIR only when ``PKGDIR`` reaches it -- as a container whose own
default PKGDIR is somewhere else would.
"""

import sys
from pathlib import Path
from typing import Any

import pytest

from shidashi.container import CommandResult
from tests.test_phases_stale import (
    CURL,
    VTE_FRESH,
    VTE_STALE,
    _binhost,
    _Factory,
    _quarantined,
    _run,
)

PKGDIR_INSIDE = "/var/cache/binpkgs"
REINDEX = ["env", f"PKGDIR={PKGDIR_INSIDE}", "emaint", "binhost", "--fix"]

#: ``emaint`` in front of the harness's fake: without PKGDIR in its environment it
#: reindexes nothing here (the container's default PKGDIR is not the stage's bind).
_EMAINT_GATE = """
import json, os, sys
if not os.environ.get("PKGDIR"):
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"tool": "emaint", "argv": sys.argv[1:], "pkgdir": None}) + "\\n")
    sys.exit(0)
os.execv(REAL, [REAL, *sys.argv[1:]])
"""


class _EnvDroppingFactory(_Factory):
    """The 3.3 stage container, except ``env=`` never reaches the child (nspawn)."""

    def __init__(self, tmp: Path, pkgdir: Path) -> None:
        super().__init__(tmp, pkgdir)
        realbin = tmp / "realbin"
        realbin.mkdir()
        real = realbin / "emaint"
        real.symlink_to(self.bindir / "fake-tool")
        gate = self.bindir / "emaint"
        gate.unlink()
        gate.write_text(f"#!{sys.executable}\nREAL = {str(real)!r}\n{_EMAINT_GATE}")
        gate.chmod(0o755)
        self.calls: list[list[str]] = []

    def run(self, argv: Any, *, env: Any = None, check: bool = True) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        return super().run(argv, env=None, check=check)


@pytest.mark.parametrize(
    ("entries", "when"),
    [((VTE_STALE, VTE_FRESH), "before the emerge"), ((VTE_STALE, CURL), "after the rebuild")],
)
def test_the_reindex_reaches_emaint_with_pkgdir_through_an_env_dropping_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: tuple[str, ...], when: str
) -> None:
    """A quarantine (``when``) through a container that ignores ``env=`` still
    reindexes the stage's PKGDIR: the index no longer names the moved file."""
    monkeypatch.delenv("PKGDIR", raising=False)  # no host leak into the child
    pkgdir = _binhost(tmp_path, monkeypatch, *entries)
    container = _EnvDroppingFactory(tmp_path, pkgdir)
    _run(tmp_path, container)

    assert [p.name for p in _quarantined(tmp_path)] == ["vte-0.84.1-1.gpkg.tar"], when
    assert REINDEX in container.calls, [c for c in container.calls if "emaint" in c]
    emaints = container.tools("emaint")
    assert emaints, "the reindex never ran"
    assert all(e["pkgdir"] == PKGDIR_INSIDE for e in emaints), emaints
    index = (pkgdir / "Packages").read_text()
    assert "vte-0.84.1-1.gpkg.tar" not in index, index
    assert "vte-0.84.1-2.gpkg.tar" in index  # the fresh instance stays indexed
    assert (pkgdir / "x11-libs/vte/vte-0.84.1-2.gpkg.tar").is_file()
    if CURL in entries:
        assert "curl-8.16.0-1.gpkg.tar" in index
