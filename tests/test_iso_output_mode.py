"""The ISO and its output directory are published readable by everyone (story 031, task 2.3;
R1.3, R1.4, R4.5).

Under ``sudo`` with root's umask 077 the output directory comes out 0700 and the ISO 0600
(grub-mkrescue honours the umask): the maintainer's gpg, run as the invoking user, cannot
reach the directory or read what it signs. Two paths put an ISO there: the local build
(``Assembler.assemble``) and the worker path (``schedule._Mover``, which renames the
worker's files in and, across filesystems, copies them).
"""

import errno
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import build_outcome, schedule
from shidashi.assembler import AssemblerError
from tests.test_assembler import (  # noqa: F401 -- autouse fixtures of the module
    _no_tree_download,
    _system_config_stubbed,
    _toolbox_stubbed,
    _Wired,
)

#: Readable by everyone: a non-root ``sha256sum -c`` and a mirror keeping file modes.
ISO_MODE, DIR_MODE = 0o644, 0o755


@pytest.fixture
def strict_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- the local build -----------------------------------------------------------------------


@pytest.mark.usefixtures("strict_umask")
def test_a_local_build_creates_the_output_directory_0755_and_the_iso_0644(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Wired(tmp_path, monkeypatch).assemble()
    (iso,) = (tmp_path / "dist").glob("*.iso")
    assert _mode(tmp_path / "dist") == DIR_MODE, oct(_mode(tmp_path / "dist"))
    assert _mode(iso) == ISO_MODE, oct(_mode(iso))


@pytest.mark.usefixtures("strict_umask")
def test_a_local_build_widens_an_output_directory_an_earlier_run_left_0700(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dist").mkdir()
    _Wired(tmp_path, monkeypatch).assemble()
    assert _mode(tmp_path / "dist") == DIR_MODE


def test_a_stale_0600_iso_is_published_0644_under_the_usual_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover narrow ISO of an earlier run, rewritten in place, keeps its mode unless
    the mode is set after the ISO is built."""
    previous = os.umask(0o022)
    try:
        wired = _Wired(tmp_path, monkeypatch)
        wired.assemble()
        (iso,) = (tmp_path / "dist").glob("*.iso")
        iso.chmod(0o600)
        wired.assemble()
    finally:
        os.umask(previous)
    assert _mode(iso) == ISO_MODE


@pytest.mark.usefixtures("strict_umask")
def test_an_output_directory_that_cannot_be_prepared_is_an_assembler_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    real_chmod = Path.chmod

    def refuse_the_directory(self: Path, mode: int, **kwargs: bool) -> None:
        if self == dist:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        real_chmod(self, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", refuse_the_directory)
    with pytest.raises(AssemblerError) as raised:
        wired.assemble()
    assert str(dist) in str(raised.value)


@pytest.mark.usefixtures("strict_umask")
def test_an_iso_whose_mode_cannot_be_set_is_an_assembler_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(tmp_path, monkeypatch)
    real_chmod = Path.chmod

    def refuse_the_iso(self: Path, mode: int, **kwargs: bool) -> None:
        if self.suffix == ".iso":
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        real_chmod(self, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", refuse_the_iso)
    with pytest.raises(AssemblerError) as raised:
        wired.assemble()
    assert ".iso" in str(raised.value)


# --- the worker path: schedule._Mover ------------------------------------------------------


def _worker_iso(tmp_path: Path) -> tuple[Path, int]:
    iso_dir = tmp_path / "worker"
    iso_dir.mkdir()
    (iso_dir / "b.iso").write_bytes(b"ISO" * 100)  # 0600 under the strict umask
    return iso_dir, os.open(iso_dir, os.O_RDONLY | os.O_DIRECTORY)


@pytest.mark.usefixtures("strict_umask")
def test_a_worker_iso_renamed_into_place_is_0644_in_a_0755_directory(tmp_path: Path) -> None:
    iso_dir, iso_fd = _worker_iso(tmp_path)
    mover = schedule._Mover(tmp_path / "dist")
    try:
        mover.move(iso_dir, iso_fd, ["b.iso"], {})
    finally:
        mover.close()
        os.close(iso_fd)
    assert _mode(tmp_path / "dist" / "b.iso") == ISO_MODE
    assert _mode(tmp_path / "dist") == DIR_MODE


@pytest.mark.usefixtures("strict_umask")
def test_a_worker_iso_widens_an_output_directory_left_0700(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    iso_dir, iso_fd = _worker_iso(tmp_path)
    mover = schedule._Mover(tmp_path / "dist")
    try:
        mover.move(iso_dir, iso_fd, ["b.iso"], {})
    finally:
        mover.close()
        os.close(iso_fd)
    assert _mode(tmp_path / "dist") == DIR_MODE


@pytest.mark.usefixtures("strict_umask")
def test_a_worker_iso_copied_across_filesystems_stays_0644(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: ``_copy`` already creates 0644 and fchmods; a rename that raises EXDEV must
    not regress it."""
    iso_dir, iso_fd = _worker_iso(tmp_path)
    real_replace = os.replace

    def cross_device(src: str, dst: str, **kw: int) -> None:
        if kw.get("src_dir_fd") != kw.get("dst_dir_fd"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(src, dst, **kw)

    monkeypatch.setattr(os, "replace", cross_device)
    mover = schedule._Mover(tmp_path / "dist")
    try:
        mover.move(iso_dir, iso_fd, ["b.iso"], {})
    finally:
        mover.close()
        os.close(iso_fd)
    assert _mode(tmp_path / "dist" / "b.iso") == ISO_MODE


@pytest.mark.usefixtures("strict_umask")
def test_a_worker_iso_whose_mode_cannot_be_set_is_an_error_naming_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso_dir, iso_fd = _worker_iso(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "fchmod", refuse)
    mover = schedule._Mover(tmp_path / "dist")
    try:
        with pytest.raises(OSError, match="b.iso"):
            mover.move(iso_dir, iso_fd, ["b.iso"], {})
    finally:
        mover.close()
        os.close(iso_fd)


# --- not published ---------------------------------------------------------------------------


def test_the_build_outcome_stays_0600(tmp_path: Path) -> None:
    """R4.5: ``.outcome/*.json`` is not published; the widening must not reach it."""
    path = tmp_path / "x.json"
    outcome = build_outcome.BuildOutcome(
        arch="v3",
        init="systemd",
        status="ok",
        exit_code=0,
        isos=[],
        failed_images=[],
        message="",
        toolchain=None,
    )
    previous = os.umask(0o022)
    try:
        build_outcome.write(path, outcome)
    finally:
        os.umask(previous)
    assert _mode(path) == 0o600
