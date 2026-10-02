"""Tests of shidashi.publish -- the artifacts published beside an ISO."""

import datetime
import gzip
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from shidashi import config, publish
from shidashi.toolbox import HostTools

_WHEN = datetime.datetime(2026, 9, 30, 1, 2, tzinfo=datetime.UTC)


def test_release_name_and_title() -> None:
    kde = config.load_recipe("v3", "kde", "systemd")
    assert publish.release_name(kde, _WHEN) == "bentoo-2026.09.30-kde-systemd-v3"
    assert publish.title(kde, _WHEN) == "Bentoo 2026.09.30 KDE (systemd, x86-64-v3)"
    minimal = config.load_recipe("v3", "minimal", "openrc")
    assert publish.title(minimal, _WHEN) == "Bentoo 2026.09.30 Minimal (openrc, x86-64-v3)"


def test_digests_are_gentoos_format_and_match_the_tools(tmp_path: Path) -> None:
    iso = tmp_path / "b.iso"
    iso.write_bytes(b"ISO" * 1000)
    path, sha256 = publish.write_digests(iso)
    text = path.read_text()
    assert path.name == "b.iso.DIGESTS"
    assert text.startswith("# SHA512 HASH\n") and "# BLAKE2B HASH\n" in text
    assert f"{hashlib.sha512(iso.read_bytes()).hexdigest()}  b.iso" in text
    assert sha256 == hashlib.sha256(iso.read_bytes()).hexdigest()
    if shutil.which("b2sum"):  # BLAKE2B as b2sum prints it
        b2 = subprocess.run(["b2sum", str(iso)], capture_output=True, text=True).stdout.split()[0]
        assert f"{b2}  b.iso" in text


def test_sha256sums_update_their_own_lines_and_keep_the_others(tmp_path: Path) -> None:
    (tmp_path / "SHA256SUMS").write_text("aaa  other.iso\nbbb  b.iso\n")
    publish.update_sha256sums(tmp_path, {"b.iso": "ccc", "b.iso.packages": "ddd"})
    assert (tmp_path / "SHA256SUMS").read_text() == (
        "ccc  b.iso\nddd  b.iso.packages\naaa  other.iso\n"
    )


def test_packages_list_is_sorted_atoms(tmp_path: Path) -> None:
    dest = publish.write_packages(tmp_path / "p", [{"atom": "z/b-1"}, {"atom": "a/c-2"}])
    assert dest.read_text() == "a/c-2\nz/b-1\n"


@pytest.mark.skipif(shutil.which("mksquashfs") is None, reason="needs squashfs-tools")
@pytest.mark.parametrize("processors", [None, 2])
def test_contents_lists_every_path_of_the_live_root(tmp_path: Path, processors: int | None) -> None:
    root = tmp_path / "root"
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/sh").write_text("x")
    sq = tmp_path / "r.sq"
    subprocess.run(
        ["mksquashfs", str(root), str(sq), "-quiet", "-no-progress"],
        check=True,
        capture_output=True,
    )
    dest = publish.write_contents(sq, tmp_path / "c.gz", tools=HostTools(), processors=processors)
    with gzip.open(dest, "rt") as f:
        assert f.read().splitlines() == ["/", "/usr", "/usr/bin", "/usr/bin/sh"]


def test_stage4_keeps_xattrs_and_leaves_out_what_the_iso_does() -> None:
    argv = publish.stage4_argv(Path("/r"), Path("/o.tar.xz"), ["dev/*", "var/log/*.log"])
    assert argv[:6] == ["tar", "--create", "--file", "/o.tar.xz", "--directory", "/r"]
    assert {"--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner"} <= set(argv)
    assert "--use-compress-program=xz -9e -T0" in argv  # every CPU by default
    capped = publish.stage4_argv(Path("/r"), Path("/o.tar.xz"), [], threads=6)
    assert "--use-compress-program=xz -9e -T6" in capped
    # one path component per *, as in mksquashfs
    assert "--no-wildcards-match-slash" in argv
    assert "--exclude=./dev/*" in argv and "--exclude=./var/log/*.log" in argv
    assert argv[-1] == "."


@pytest.mark.skipif(shutil.which("xz") is None, reason="needs xz")
def test_stage4_excludes_content_and_keeps_mount_points(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for d in ("dev/pts", "var/log/cups", "usr/bin"):
        (root / d).mkdir(parents=True)
    (root / "dev/null.fake").write_text("")
    (root / "var/log/emerge.log").write_text("x")
    (root / "var/log/cups/access").write_text("x")
    (root / "usr/bin/sh").write_text("x")
    exclude = tmp_path / "ex"
    exclude.write_text("# comment\ndev/*\nvar/log/*.log\n")
    dest = publish.make_stage4(root, tmp_path / "s.tar.xz", exclude)
    names = subprocess.run(
        ["tar", "-tJf", str(dest)], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "./dev/" in names and "./dev/null.fake" not in names and "./dev/pts/" not in names
    assert "./var/log/emerge.log" not in names and "./var/log/cups/access" in names
    assert "./usr/bin/sh" in names


def test_latest_points_at_the_iso(tmp_path: Path) -> None:
    iso = tmp_path / "b.iso"
    iso.write_bytes(b"12345")
    path = publish.write_latest(tmp_path, "v3-kde-systemd", iso)
    assert path.name == "latest-v3-kde-systemd.txt"
    assert path.read_text().splitlines()[1] == "b.iso 5"


def test_bundle_run_packs_the_trail(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "20260930T010000Z-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}")
    dest = publish.bundle_run(run_dir, tmp_path / "b.build.tar.zst")
    names = subprocess.run(
        ["tar", "--zstd", "-tf", str(dest)], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "20260930T010000Z-abc/manifest.json" in names


def test_the_sbom_is_compressed_on_the_medium_and_round_trips(tmp_path: Path) -> None:
    from compression import zstd

    src = tmp_path / "sbom.spdx.json"
    src.write_text('{"spdxVersion": "SPDX-2.3", "packages": []}' * 1000)
    dest = publish.compress_sbom(src, tmp_path / "sbom.spdx.json.zst")
    assert dest.stat().st_size < src.stat().st_size / 10
    assert zstd.decompress(dest.read_bytes()) == src.read_bytes()
    assert publish.SBOM_ZSTD_LEVEL == 10
