"""Every companion published beside an ISO is 0644 whatever the umask or the writing tool
(story 031, task 2.2; R1.2).

Under ``sudo`` with root's umask 077 the release of 2026.10.22 came out with its companions
0600: a non-root ``sha256sum -c SHA256SUMS`` cannot read them, nor can a mirror that keeps
file modes. Each writer runs its real tool here -- ``tar`` + ``xz`` for the stage4, ``tar
--zstd`` for the run bundle, ``syft`` for the SBOM, ``unsquashfs`` for the contents list --
because the mode a file ends with is decided by the tool that created it, and a fake tool
would only prove what the fake does.

Hostile halves, both directions of "0644 whatever": a strict umask (077) must not leave a
file narrower, a permissive one (000) must not leave it wider (0666 is as wrong as 0600),
and a stale narrow file from an earlier run, rewritten in place, must not keep its old mode
under an ordinary umask.
"""

import contextlib
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import publish
from shidashi.toolbox import HostTools

#: Every published file of a release directory is readable by everyone (story 030, 9.1).
COMPANION_FILE_MODE = 0o644
#: The umasks a maintainer's root could run with: strict (nothing for group or others),
#: permissive (nothing masked) and the usual one.
STRICT_UMASK, OPEN_UMASK, USUAL_UMASK = 0o077, 0o000, 0o022


@contextlib.contextmanager
def _companion_umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _companion_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _tiny_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "sh").write_text("x", encoding="utf-8")
    return root


def _exclude_file(tmp_path: Path) -> Path:
    exclude = tmp_path / "exclude"
    exclude.write_text("dev/*\n", encoding="utf-8")
    return exclude


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "20261024T000000Z-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    return run_dir


def _iso(tmp_path: Path) -> Path:
    iso = tmp_path / "bentoo-2026.10.24-kde-systemd-v3.iso"
    iso.write_bytes(b"ISO" * 100)
    return iso


# --- the stage4: tar + xz create it ---------------------------------------------------------


@pytest.mark.skipif(shutil.which("xz") is None, reason="needs xz")
@pytest.mark.parametrize("mask", [STRICT_UMASK, OPEN_UMASK], ids=["umask-077", "umask-000"])
def test_the_stage4_tar_writes_is_0644_under_any_umask(tmp_path: Path, mask: int) -> None:
    root, exclude = _tiny_root(tmp_path), _exclude_file(tmp_path)
    with _companion_umask(mask):
        dest = publish.make_stage4(root, tmp_path / "b.stage4.tar.xz", exclude, threads=1)
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))


@pytest.mark.skipif(shutil.which("xz") is None, reason="needs xz")
def test_a_stale_0600_stage4_rewritten_by_tar_ends_0644(tmp_path: Path) -> None:
    """tar truncates an existing archive and keeps its mode: a 0600 leftover of an earlier
    strict-umask run stays 0600 even under the usual umask unless it is set after tar."""
    root, exclude = _tiny_root(tmp_path), _exclude_file(tmp_path)
    stale = tmp_path / "b.stage4.tar.xz"
    stale.write_bytes(b"an earlier run's tarball")
    stale.chmod(0o600)
    with _companion_umask(USUAL_UMASK):
        dest = publish.make_stage4(root, stale, exclude, threads=1)
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))


@pytest.mark.skipif(shutil.which("xz") is None, reason="needs xz")
def test_a_stage4_whose_mode_cannot_be_set_is_a_publish_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constraint of story 031: a file an external tool wrote gets an explicit mode after
    the tool returns, and an ``OSError`` from it is the module's domain error naming the
    file -- never a bare traceback after hours of building."""
    root, exclude = _tiny_root(tmp_path), _exclude_file(tmp_path)
    dest = tmp_path / "b.stage4.tar.xz"

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted", str(dest))

    monkeypatch.setattr(os, "chmod", refuse)
    monkeypatch.setattr(os, "fchmod", refuse)
    raised: BaseException | None = None
    try:
        publish.make_stage4(root, dest, exclude, threads=1)
    except BaseException as err:  # what it raised is the assertion below, not a crash here
        raised = err
    assert isinstance(raised, publish.PublishError), f"expected a PublishError, got {raised!r}"
    assert dest.name in str(raised), str(raised)


# --- the run bundle: tar --zstd creates it --------------------------------------------------


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs zstd")
@pytest.mark.parametrize("mask", [STRICT_UMASK, OPEN_UMASK], ids=["umask-077", "umask-000"])
def test_the_run_bundle_tar_writes_is_0644_under_any_umask(tmp_path: Path, mask: int) -> None:
    run_dir = _run_dir(tmp_path)
    with _companion_umask(mask):
        dest = publish.bundle_run(run_dir, tmp_path / "out" / "b.build.tar.zst")
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))
    names = subprocess.run(
        ["tar", "--zstd", "-tf", str(dest)], capture_output=True, text=True, check=True
    ).stdout.split()
    assert f"{run_dir.name}/manifest.json" in names  # the content is unchanged by the mode


# --- the SBOM: syft creates it (0644 & ~umask, measured 2026-10-24) -------------------------


@pytest.mark.skipif(shutil.which("syft") is None, reason="needs syft")
def test_the_sbom_syft_writes_is_0644_under_a_strict_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYFT_CHECK_FOR_APP_UPDATE", "false")  # no network from a test
    root = _tiny_root(tmp_path)
    with _companion_umask(STRICT_UMASK):
        dest = publish.write_sbom(root, tmp_path / "b.iso.spdx.json")
    assert dest is not None
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))


# --- the contents list: gzip over unsquashfs's listing --------------------------------------


@pytest.mark.skipif(shutil.which("mksquashfs") is None, reason="needs squashfs-tools")
def test_the_contents_list_is_0644_under_a_strict_umask(tmp_path: Path) -> None:
    squashfs = tmp_path / "root.sq"
    subprocess.run(
        ["mksquashfs", str(_tiny_root(tmp_path)), str(squashfs), "-quiet", "-no-progress"],
        check=True,
        capture_output=True,
    )
    with _companion_umask(STRICT_UMASK):
        dest = publish.write_contents(squashfs, tmp_path / "b.iso.contents.gz", tools=HostTools())
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))


# --- the files Python writes: .packages and latest-*.txt ------------------------------------


@pytest.mark.parametrize("mask", [STRICT_UMASK, OPEN_UMASK], ids=["umask-077", "umask-000"])
def test_the_packages_list_is_0644_under_any_umask(tmp_path: Path, mask: int) -> None:
    with _companion_umask(mask):
        dest = publish.write_packages(tmp_path / "b.iso.packages", [{"atom": "a/b-1"}])
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))
    assert dest.read_text(encoding="utf-8") == "a/b-1\n"


def test_a_stale_0600_packages_list_rewritten_ends_0644(tmp_path: Path) -> None:
    stale = tmp_path / "b.iso.packages"
    stale.write_text("old/atom-0\n", encoding="utf-8")
    stale.chmod(0o600)
    with _companion_umask(USUAL_UMASK):
        dest = publish.write_packages(stale, [{"atom": "a/b-1"}])
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))


@pytest.mark.parametrize("mask", [STRICT_UMASK, OPEN_UMASK], ids=["umask-077", "umask-000"])
def test_the_latest_pointer_is_0644_under_any_umask(tmp_path: Path, mask: int) -> None:
    iso = _iso(tmp_path)
    with _companion_umask(mask):
        dest = publish.write_latest(tmp_path, "v3-kde-systemd", iso)
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))
    assert dest.read_text(encoding="utf-8").splitlines()[1] == f"{iso.name} 300"


def test_a_stale_0600_latest_pointer_rewritten_ends_0644(tmp_path: Path) -> None:
    iso = _iso(tmp_path)
    stale = tmp_path / "latest-v3-kde-systemd.txt"
    stale.write_text("# an earlier build\n", encoding="utf-8")
    stale.chmod(0o600)
    with _companion_umask(USUAL_UMASK):
        dest = publish.write_latest(tmp_path, "v3-kde-systemd", iso)
    assert _companion_mode(dest) == COMPANION_FILE_MODE, oct(_companion_mode(dest))
