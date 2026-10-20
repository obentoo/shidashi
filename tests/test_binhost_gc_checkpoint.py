"""A gc leaves every assemble checkpoint valid (story 029, task 3.4; R3.6, R3.2).

``checkpoint.binhost_slice(index, cpvs)`` hashes every index entry of ``cpvs``
(CPV|BUILD_ID|BUILD_TIME|SHA1|MD5|USE). An image can only select a CPV whose ebuild is in
the pinned repositories, so for every such set of CPVs the slice must hash the same before
and after a ``gc`` -- including a CPV with a second, older BUILD_ID, and after the
toolbox's ``emaint`` rewrote the whole index.
"""

from itertools import combinations
from pathlib import Path

import pytest

from shidashi import checkpoint
from tests.test_binhost_cli_gc import install_fake_toolbox, pool_files
from tests.test_binhost_cli_show import Lab, binhost, make_lab
from tests.test_binhost_refs import entry

AVAILABLE = {
    "dev-libs/foo-2.0": [
        entry("dev-libs/foo-2.0", 1, use="a b", build_time=500, sha1="1" * 40),
        entry("dev-libs/foo-2.0", 2, use="a", build_time=50, sha1="2" * 40),  # older instance
    ],
    "media-video/ffmpeg-8.0": [
        entry("media-video/ffmpeg-8.0", 2, use="x264", build_time=200, sha1="3" * 40),
        entry("media-video/ffmpeg-8.0", 3, use="sdl x264", build_time=300, sha1="4" * 40),
    ],
    "app-misc/plain-1-r1": [entry("app-misc/plain-1-r1", 1, build_time=10, sha1="5" * 40)],
}
GONE = [entry(f"dev-libs/foo-1.{n}", 1, build_time=100 + n, sha1=f"{n + 6}" * 40) for n in range(4)]


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    lab = make_lab(tmp_path, monkeypatch)
    for cpv in AVAILABLE:
        lab.ebuild(cpv)
    lab.pool(*[e for es in AVAILABLE.values() for e in es], *GONE)
    install_fake_toolbox(tmp_path, monkeypatch)
    return lab


def _slices(index: Path) -> dict[tuple[str, ...], str | None]:
    """The slice of every non-empty set of available CPVs (what any image could plan)."""
    cpvs = sorted(AVAILABLE)
    return {
        combo: checkpoint.binhost_slice(index, combo)
        for n in range(1, len(cpvs) + 1)
        for combo in combinations(cpvs, n)
    }


def test_the_slice_of_every_set_of_available_cpvs_survives_a_gc(lab: Lab) -> None:
    index = lab.pkgdir() / "Packages"
    before = _slices(index)
    gone_before = checkpoint.binhost_slice(index, ["dev-libs/foo-1.0"])
    files_before = pool_files(lab.pkgdir())

    result = binhost("gc", "v3", "--keep", "1")
    assert result.exit_code == 0, result.output
    # not vacuous: the gc moved something and the index was rewritten without it
    assert pool_files(lab.pkgdir()) < files_before
    assert checkpoint.binhost_slice(index, ["dev-libs/foo-1.0"]) != gone_before
    assert _slices(index) == before


def test_keep_zero_still_leaves_every_available_slice(lab: Lab) -> None:
    """R3.2: whatever the keep, an available CPV's instances -- all of them -- stay."""
    index = lab.pkgdir() / "Packages"
    before = _slices(index)
    result = binhost("gc", "v3", "--keep", "0")
    assert result.exit_code == 0, result.output
    assert not any("dev-libs/foo/foo-1." in p for p in pool_files(lab.pkgdir()))
    assert _slices(index) == before
