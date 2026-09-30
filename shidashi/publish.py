"""What is published beside an ISO -- the release artifacts of the major distributions.

For ``bentoo-<version>-<flavor>-<init>-<arch>.iso`` the output directory gets:

- ``<iso>.DIGESTS``: SHA512 and BLAKE2B of the ISO (Gentoo's format);
- ``SHA256SUMS``: one line per artifact, updated in place (Ubuntu, Arch);
- ``<iso>.packages``: every installed package with its version (Debian, Arch);
- ``<iso>.contents.gz``: every file of the live root (Gentoo, Debian);
- ``<iso>.spdx.json``: the SBOM, from syft (openSUSE);
- ``<name>.stage4.tar.xz``: the configured system as a tarball, for a manual
  install the Gentoo way -- optional, and made BEFORE the live user is added;
- ``latest-<flavor>-<init>-<arch>.txt``: which build is current (Gentoo).

Signing (a detached GPG signature of DIGESTS and SHA256SUMS) needs the
maintainer's key and is left to the maintainer.
"""

import datetime
import gzip
import hashlib
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from shidashi.recipe import ResolvedRecipe


class PublishError(Exception):
    """An artifact could not be made."""


def release_name(recipe: ResolvedRecipe, when: datetime.datetime) -> str:
    """``bentoo-2026.09.30-kde-systemd-v3``: the stem every artifact shares. Pure."""
    return f"bentoo-{when:%Y.%m.%d}-{recipe.flavor}-{recipe.init}-{recipe.arch}"


def title(recipe: ResolvedRecipe, when: datetime.datetime) -> str:
    """``Bentoo 2026.09.30 KDE (systemd, x86-64-v3)``: the boot menu's name. Pure."""
    arch = {"v3": "x86-64-v3", "v4": "x86-64-v4"}.get(recipe.arch, recipe.arch)
    flavor = recipe.flavor.upper() if len(recipe.flavor) <= 4 else recipe.flavor.title()
    return f"Bentoo {when:%Y.%m.%d} {flavor} ({recipe.init}, {arch})"


def _hashes(path: Path) -> tuple[str, str, str]:
    """SHA512, BLAKE2B (b2sum's 512-bit) and SHA256 in one read. I/O."""
    sha512, blake2b, sha256 = hashlib.sha512(), hashlib.blake2b(), hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            sha512.update(block)
            blake2b.update(block)
            sha256.update(block)
    return sha512.hexdigest(), blake2b.hexdigest(), sha256.hexdigest()


def write_digests(iso: Path) -> tuple[Path, str]:
    """``<iso>.DIGESTS`` in Gentoo's format; returns it and the ISO's SHA256."""
    sha512, blake2b, sha256 = _hashes(iso)
    path = iso.with_name(iso.name + ".DIGESTS")
    path.write_text(
        f"# SHA512 HASH\n{sha512}  {iso.name}\n# BLAKE2B HASH\n{blake2b}  {iso.name}\n",
        encoding="utf-8",
    )
    return path, sha256


def update_sha256sums(directory: Path, sums: Mapping[str, str]) -> Path:
    """Set the lines of ``sums`` (file name -> SHA256) in ``directory/SHA256SUMS``,
    keeping the other files' lines. I/O."""
    path = directory / "SHA256SUMS"
    lines: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            digest, _, name = line.partition("  ")
            if name:
                lines[name] = digest
    lines.update(sums)
    path.write_text("".join(f"{d}  {n}\n" for n, d in sorted(lines.items())), encoding="utf-8")
    return path


def sha256_of(path: Path) -> str:
    return _hashes(path)[2]


def write_packages(dest: Path, packages: Iterable[Mapping[str, Any]]) -> Path:
    """One ``category/package-version`` per line, sorted. I/O."""
    atoms = sorted(str(p["atom"]) for p in packages)
    dest.write_text("".join(f"{a}\n" for a in atoms), encoding="utf-8")
    return dest


def write_contents(squashfs: Path, dest: Path) -> Path:
    """Every path of the live root, directories included (``unsquashfs -l``;
    ``-lc`` would list files only), gzipped. I/O."""
    try:
        done = subprocess.run(
            ["unsquashfs", "-l", str(squashfs)], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as err:
        raise PublishError(f"unsquashfs -l {squashfs}: {err}") from err
    paths = [
        line.removeprefix("squashfs-root") or "/"
        for line in done.stdout.splitlines()
        if line.startswith("squashfs-root")
    ]
    with gzip.open(dest, "wt", encoding="utf-8") as out:
        out.write("".join(f"{p}\n" for p in sorted(paths)))
    return dest


def write_sbom(rootfs: Path, dest: Path) -> Path | None:
    """The SBOM of the live root (``syft``, SPDX JSON); ``None`` without syft. I/O."""
    if shutil.which("syft") is None:
        return None
    try:
        subprocess.run(
            ["syft", "scan", f"dir:{rootfs}", "--quiet", "-o", f"spdx-json={dest}"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as err:
        raise PublishError(f"syft {rootfs}: {err.stderr}") from err
    return dest


#: zstd level of the SBOM inside the medium. Measured on the kde SBOM (236 MiB,
#: 2026-09-30): level 10 -> 32 MiB in 2 s, level 19 -> 28 MiB in 76 s.
SBOM_ZSTD_LEVEL = 10


def compress_sbom(src: Path, dest: Path, *, level: int = SBOM_ZSTD_LEVEL) -> Path:
    """``src`` zstd-compressed into ``dest`` (streamed; stdlib ``compression.zstd``).

    The medium carries the SBOM compressed -- uncompressed it cost 247 MB of the
    ISO; the published copy beside the ISO stays plain, as the scanners read it.
    """
    from compression import zstd

    with src.open("rb") as plain, zstd.open(dest, "wb", level=level) as packed:
        shutil.copyfileobj(plain, packed, length=1 << 20)
    return dest


def tar_excludes(squashfs_patterns: Sequence[str]) -> list[str]:
    """The ISO's exclude patterns as tar ``--exclude`` options. Pure.

    tar sees ``./path``; ``--no-wildcards-match-slash`` keeps ``*`` inside one
    path component, as in mksquashfs, so both artifacts leave out the same files.
    """
    out = ["--no-wildcards-match-slash"]
    for pattern in squashfs_patterns:
        out.append(f"--exclude=./{pattern}")
    return out


def read_patterns(exclude_file: Path) -> list[str]:
    lines = exclude_file.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def stage4_argv(rootfs: Path, dest: Path, patterns: Sequence[str]) -> list[str]:
    """``tar`` of the configured root, xattrs and ACLs kept, ``xz -9e`` on every
    CPU (the author's own stage4 recipe). Pure."""
    return [
        "tar", "--create", "--file", str(dest), "--directory", str(rootfs),
        "--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner",
        "--use-compress-program=xz -9e -T0", *tar_excludes(patterns), ".",
    ]


def make_stage4(rootfs: Path, dest: Path, exclude_file: Path) -> Path:
    """The stage4 tarball of ``rootfs``. I/O (long: xz -9e)."""
    try:
        subprocess.run(stage4_argv(rootfs, dest, read_patterns(exclude_file)),
                       capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as err:
        dest.unlink(missing_ok=True)
        raise PublishError(f"stage4 {dest.name}: {err.stderr}") from err
    return dest


def write_latest(directory: Path, key: str, iso: Path) -> Path:
    """``latest-<key>.txt``: the current build's ISO, its size and date. I/O."""
    path = directory / f"latest-{key}.txt"
    when = datetime.datetime.fromtimestamp(iso.stat().st_mtime, datetime.UTC)
    path.write_text(
        f"# Latest Bentoo {key} build, {when:%Y-%m-%dT%H:%M:%SZ}\n"
        f"{iso.name} {iso.stat().st_size}\n",
        encoding="utf-8",
    )
    return path


def bundle_run(run_dir: Path, dest: Path) -> Path:
    """The run's audit trail (events, manifest, report, packages) as a tar.zst. I/O."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["tar", "--create", "--zstd", "--file", str(dest), "--directory", str(run_dir.parent),
             run_dir.name],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as err:
        raise PublishError(f"bundling {run_dir}: {err}") from err
    return dest
