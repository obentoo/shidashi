"""``shidashi binhost show <arch>`` (story 029, task 3.1; R2.1-R2.8).

Driven through the real CLI (typer ``CliRunner``) over a real lab on disk: the pinned
``seeds/`` (stage3 pointer, ::gentoo pin, one overlay pin), the pinned repositories already
extracted in the cache (so nothing is downloaded), and a build pool with a string
``Packages`` index and its files. Nothing is faked but the host.

The ``Lab`` helper is shared by every story-029 CLI test.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli, config
from shidashi.cli import app
from tests.test_binhost_refs import binpkg_path, entry, index

runner = CliRunner()

GEN = "20260823T153057Z"
OTHER_GEN = "20250101T000000Z"
TREE_DATE = "20260901"  # older than the 7-day cooldown, whatever day the suite runs
OVERLAY_COMMIT = "a" * 40
RECORD = ".shidashi-pool.json"
QUARANTINE = ".shidashi-quarantine.json"
FFMPEG = "media-video/ffmpeg-8.0"


@dataclass
class Lab:
    root: Path
    cache: Path
    seeds: Path
    gentoo: Path
    overlay: Path

    def pkgdir(self, generation: str = GEN, arch: str = "v3") -> Path:
        return self.cache / "binpkgs" / arch / generation

    def ebuild(self, cpv: str, *, repo: str = "gentoo") -> None:
        category, pf = cpv.split("/")
        pn = binpkg_path(cpv, 1).split("/")[1]
        where = self.gentoo if repo == "gentoo" else self.overlay
        path = where / category / pn / f"{pf}.ebuild"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("EAPI=8\n", encoding="utf-8")

    def pool(self, *entries: str, generation: str = GEN, arch: str = "v3") -> Path:
        """A build pool: the index and one file per entry, each with its own bytes."""
        pkgdir = self.pkgdir(generation, arch)
        pkgdir.mkdir(parents=True, exist_ok=True)
        for block in entries:
            rel = dict(line.split(": ", 1) for line in block.splitlines())["PATH"]
            target = pkgdir / rel
            if not rel.startswith("/") and ".." not in rel.split("/"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f"binpkg {rel}\n".encode())
        (pkgdir / "Packages").write_text(index(*entries), encoding="utf-8")
        return pkgdir

    def record(
        self,
        finals: dict[str, list[tuple[str, int]]],
        transients: dict[str, list[tuple[str, int]]],
        generation: str = GEN,
    ) -> None:
        stages = {
            stage: {
                "finals": [list(p) for p in finals.get(stage, [])],
                "transients": [list(p) for p in transients.get(stage, [])],
            }
            for stage in {*finals, *transients}
        }
        path = self.pkgdir(generation) / RECORD
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "stages": stages}), encoding="utf-8")


def make_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    seeds, cache = tmp_path / "seeds", tmp_path / "cache"
    seeds.mkdir()
    (seeds / "stage3.toml").write_text(
        f'snapshot = "{GEN}"\nbase_url = "https://example.invalid/autobuilds"\n\n'
        f'[systemd]\nfilename = "stage3-systemd-{GEN}.tar.xz"\nsha512 = "{"0" * 128}"\n\n'
        f'[openrc]\nfilename = "stage3-openrc-{GEN}.tar.xz"\nsha512 = "{"1" * 128}"\n',
        encoding="utf-8",
    )
    (seeds / "gentoo.toml").write_text(
        f'date = "{TREE_DATE}"\nbase_url = "https://example.invalid/snapshots"\n'
        f'sha512 = "{"2" * 128}"\n',
        encoding="utf-8",
    )
    (seeds / "overlays.toml").write_text(
        f'[bentoo]\nurl = "https://example.invalid/bentoo.git"\ncommit = "{OVERLAY_COMMIT}"\n',
        encoding="utf-8",
    )
    gentoo = cache / "repos" / f"gentoo-{TREE_DATE}"
    overlay = cache / "repos" / f"bentoo-{OVERLAY_COMMIT[:12]}"
    for repo in (gentoo, overlay):
        repo.mkdir(parents=True)
        repo.chmod(0o755)
    monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(seeds))
    monkeypatch.setenv("SHIDASHI_CACHE", str(cache))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.chdir(tmp_path)
    return Lab(tmp_path, cache, seeds, gentoo, overlay)


def binhost(*args: str) -> Result:
    return runner.invoke(app, ["binhost", *args])


KINDS = ("final", "transient", "old", "unrecorded")


def kind_of(output: str, path: str) -> str | None:
    """The kind printed on the one line that names ``path`` (one line per instance)."""
    lines = [line.split() for line in output.splitlines() if path in line.split()]
    if len(lines) != 1:
        return None
    kinds = {w.strip(":,()[]") for w in lines[0]} & set(KINDS)
    return kinds.pop() if len(kinds) == 1 else None


def count_of(output: str, kind: str) -> int | None:
    """The count printed beside ``kind`` (``final 2``, ``final: 2`` or ``2 final``) on
    the summary -- the lines that name no instance."""
    summary = "\n".join(line for line in output.splitlines() if "/" not in line)
    for pattern in (rf"\b{kind}\b[:=]?\s*(\d+)\b", rf"\b(\d+)\s+{kind}\b"):
        numbers = {int(n) for n in re.findall(pattern, summary)}
        if numbers:
            return numbers.pop() if len(numbers) == 1 else None
    return None


# --- the lab of this file -------------------------------------------------------------------

ENTRIES = {
    # FINAL for minimal and TRANSIENT for kde: FINAL (R2.2)
    "ffmpeg3": entry(FFMPEG, 3, use="sdl x264", build_time=300),
    "ffmpeg2": entry(FFMPEG, 2, use="x264", build_time=200),  # TRANSIENT only (R2.3)
    "ffmpeg4": entry(FFMPEG, 4, use="sdl x264", build_time=400),  # same CPV, nobody's
    "curl": entry("net-misc/curl-8.16.0", 1, build_time=100),  # kde's FINAL
    "old10": entry("dev-libs/old-1.0", 1, build_time=10),  # no ebuild anywhere: OLD
    "old11": entry("dev-libs/old-1.1", 1, build_time=20),  # no ebuild anywhere: OLD
    "gone": entry("sys-apps/gone-3", 1, build_time=30),  # no ebuild anywhere: OLD
    "thing": entry("bentoo-base/thing-2", 1, build_time=40),  # ebuild in the overlay only
    "plain1": entry("app-misc/plain-1-r1", 1, build_time=50),  # a revision's ebuild
    "plain2": entry("app-misc/plain-1-r1", 2, build_time=60),
}
EXPECTED = {
    "ffmpeg3": "final",
    "ffmpeg2": "transient",
    "ffmpeg4": "unrecorded",
    "curl": "final",
    "old10": "old",
    "old11": "old",
    "gone": "old",
    "thing": "unrecorded",
    "plain1": "unrecorded",
    "plain2": "unrecorded",
}


def _path(key: str) -> str:
    return dict(line.split(": ", 1) for line in ENTRIES[key].splitlines())["PATH"]


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    lab = make_lab(tmp_path, monkeypatch)
    for cpv in (FFMPEG, "net-misc/curl-8.16.0", "app-misc/plain-1-r1"):
        lab.ebuild(cpv)
    lab.ebuild("bentoo-base/thing-2", repo="overlay")
    # near misses: another version of old, the same pn-ver in another category
    lab.ebuild("dev-libs/old-2.0")
    lab.ebuild("dev-python/old-1.0")
    lab.pool(*ENTRIES.values())
    lab.record(
        finals={"minimal": [(FFMPEG, 3)], "kde": [("net-misc/curl-8.16.0", 1)]},
        transients={"kde": [(FFMPEG, 3), (FFMPEG, 2)]},
    )
    return lab


def test_show_prints_every_instance_with_its_kind(lab: Lab) -> None:
    """R2.1-R2.5 in one pool. Hostile halves: build 3 is final AND a transient (final
    wins), build 4 shares the final's CPV (not final), an overlay-only ebuild is
    available (not old), another version or category of ``old`` does not save it."""
    result = binhost("show", "v3")
    assert result.exit_code == 0, result.output
    got = {key: kind_of(result.output, _path(key)) for key in ENTRIES}
    assert got == EXPECTED


def test_show_prints_the_count_of_each_kind(lab: Lab) -> None:
    result = binhost("show", "v3")
    assert result.exit_code == 0, result.output
    assert {k: count_of(result.output, k) for k in ("final", "transient", "old", "unrecorded")} == {
        "final": 2,
        "transient": 1,
        "old": 3,
        "unrecorded": 4,
    }


def test_show_reads_the_generation_factory_would_build_unless_told_another(lab: Lab) -> None:
    """R2.6: without --generation, the pinned stage3's snapshot -- not the other
    generation directory, even when it is the only other one there."""
    lab.pool(entry("app-misc/elsewhere-1", 1), generation=OTHER_GEN)
    default = binhost("show", "v3")
    assert default.exit_code == 0, default.output
    assert _path("curl") in default.output
    assert "app-misc/elsewhere" not in default.output
    other = binhost("show", "v3", "--generation", OTHER_GEN)
    assert other.exit_code == 0, other.output
    assert "app-misc/elsewhere/elsewhere-1-1.gpkg.tar" in other.output
    assert _path("curl") not in other.output


def per_init_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """systemd and openrc on DIFFERENT generations (``cli._generation_pkgdir`` is where the
    PKGDIR of an arch and init comes from), so the init a command used shows."""

    def per_init(arch: str, init: str) -> Path:
        return config.pkgdir(arch, GEN if init == "systemd" else OTHER_GEN)

    monkeypatch.setattr(cli, "_generation_pkgdir", per_init)


def test_show_reads_the_pkgdir_of_its_init_and_systemd_by_default(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.8, R2.6: ``--init`` picks the PKGDIR; without it, systemd's."""
    per_init_generation(monkeypatch)
    lab.pool(entry("app-misc/openrc-only-1", 1), generation=OTHER_GEN)
    default = binhost("show", "v3")
    assert default.exit_code == 0, default.output
    assert _path("curl") in default.output
    assert "openrc-only" not in default.output
    openrc = binhost("show", "v3", "--init", "openrc")
    assert openrc.exit_code == 0, openrc.output
    assert "app-misc/openrc-only/openrc-only-1-1.gpkg.tar" in openrc.output
    assert _path("curl") not in openrc.output


def test_show_without_an_index_names_the_pkgdir_and_the_factory(lab: Lab) -> None:
    """R2.7."""
    (lab.pkgdir() / "Packages").unlink()
    result = binhost("show", "v3")
    assert result.exit_code == 1
    flat = "".join(result.output.split())
    assert "".join(str(lab.pkgdir()).split()) in flat
    assert "shidashifactory" in flat


def test_show_of_a_damaged_record_names_it_and_leaves_it(lab: Lab) -> None:
    """R7.3, R7.4 seen from the command."""
    path = lab.pkgdir() / RECORD
    path.write_text('{"version": 1, "stages": []}', encoding="utf-8")
    result = binhost("show", "v3")
    assert result.exit_code == 1
    assert RECORD in "".join(result.output.split())
    assert path.read_text(encoding="utf-8") == '{"version": 1, "stages": []}'


def test_show_of_a_generation_with_no_record_has_no_final(lab: Lab) -> None:
    """Constraints: an existing generation has no record -- old or unrecorded only."""
    (lab.pkgdir() / RECORD).unlink()
    result = binhost("show", "v3")
    assert result.exit_code == 0, result.output
    assert kind_of(result.output, _path("ffmpeg3")) == "unrecorded"
    assert kind_of(result.output, _path("old10")) == "old"
    assert count_of(result.output, "final") == 0
