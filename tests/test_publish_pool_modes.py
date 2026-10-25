"""The signed publish pool is 0755/0644 whatever the umask or the build pool's modes
(story 031, task 3.1; R1.5).

``binhost publish`` runs under ``sudo``: with root's umask 077 the pool's directories came
out 0700 and every copy kept the build pool's mode, so the pool could not be served as is.
This runs the whole ``publish`` -- copy, ``gpkg-sign`` and ``emaint`` in the toolbox, the
swap into place -- through tests/test_publish_pool.py's lab: the fake toolbox's tools run
as subprocesses and inherit the umask, as the real ones do, and its ``emaint`` writes
``Packages`` with a plain ``write_text`` (umask-masked), as Portage does.

Hostile halves of "0755/0644 whatever": a narrow build pool under a strict umask must not
leave the pool narrower, a wide one (0777) under umask 000 must not leave it wider -- an
executable or world-writable binpkg is as wrong as an unreadable one; and the build pool is
only read (story 029 R5.6), so a fix that widened the source before copying it would publish
the right modes while breaking that rule: its modes are pinned unchanged.
"""

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_binhost_cli_show import FFMPEG, Lab
from tests.test_publish_pool import (
    CURL,
    ENTRIES,
    FPR,
    extra_socket,
    publish,
    publish_lab,
    toolbox_for,
)

#: What a servable pool is: every directory traversable and every file readable by everyone.
POOL_DIR_MODE, POOL_FILE_MODE = 0o755, 0o644


@pytest.fixture
def pool_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Lab]:
    with extra_socket() as agent:
        lab = publish_lab(tmp_path, monkeypatch, agent, *ENTRIES)
        lab.record(
            finals={"minimal": [(FFMPEG, 3)], "kde": [(CURL, 1)]},
            transients={"kde": [(FFMPEG, 3), (FFMPEG, 2)]},
        )
        yield lab


@contextlib.contextmanager
def _pool_umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _pool_mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _set_build_pool_modes(pkgdir: Path, *, dirs: int, files: int) -> dict[str, int]:
    """Every directory and file under ``pkgdir`` (itself excluded) set to ``dirs``/``files``;
    returns the modes as set, by relative path."""
    for path in sorted(pkgdir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        path.chmod(dirs if path.is_dir() else files)
    return {str(p.relative_to(pkgdir)): _pool_mode(p) for p in pkgdir.rglob("*")}


def _modes_under(root: Path) -> dict[str, int]:
    """``root`` itself (``.``) and every entry under it, by relative path, with its mode."""
    return {".": _pool_mode(root)} | {
        str(p.relative_to(root)): _pool_mode(p) for p in root.rglob("*")
    }


def _publish_under(lab: Lab, mask: int) -> Path:
    dest = lab.root / "publish" / "v3" / "20261024T000000Z"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _pool_umask(mask):
        publish(lab.pkgdir(), dest, key=FPR, toolbox_for=toolbox_for(lab))
    return dest


def _wrong_modes(dest: Path) -> dict[str, str]:
    wrong: dict[str, str] = {}
    for rel, mode in _modes_under(dest).items():
        want = POOL_DIR_MODE if (dest / rel).is_dir() else POOL_FILE_MODE
        if mode != want:
            wrong[rel] = f"{mode:o} (want {want:o})"
    return wrong


@pytest.mark.parametrize(
    ("mask", "dirs", "files"),
    [(0o077, 0o700, 0o600), (0o000, 0o777, 0o777)],
    ids=["narrow-pool-umask-077", "wide-pool-umask-000"],
)
def test_every_directory_of_the_published_pool_is_0755_and_every_file_0644(
    pool_lab: Lab, mask: int, dirs: int, files: int
) -> None:
    before = _set_build_pool_modes(pool_lab.pkgdir(), dirs=dirs, files=files)
    dest = _publish_under(pool_lab, mask)
    after = {
        str(p.relative_to(pool_lab.pkgdir())): _pool_mode(p) for p in pool_lab.pkgdir().rglob("*")
    }
    assert after == before, "publish changed the build pool's modes; it is only read"
    published: dict[str, Any] = _modes_under(dest)
    assert "Packages" in published and any(rel.endswith(".gpkg.tar") for rel in published)
    assert _wrong_modes(dest) == {}, published
