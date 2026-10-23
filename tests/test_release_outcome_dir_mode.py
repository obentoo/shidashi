"""Directories a release creates beyond the release directory itself keep their mode under
any umask (story 030, task 10.2, step 5).

``mkdir(parents=True)`` ignores ``mode`` for the parents it creates, and the umask masks it
for the leaf: under ``sudo`` with umask 077 a fresh ``--output-root`` would be left 0700 and
the maintainer's gpg (run as ``SUDO_USER``, R6.4) could not reach the release below it.
"""

import os
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from shidashi import audit, release_cli
from shidashi import release as release_core
from shidashi.release_sign import RELEASE_DIR_MODE, ensure_release_dir

#: What an operator's own directory above the release root may be; it is not ours to widen.
OPERATOR_DIR_MODE = 0o711


@pytest.fixture
def strict_umask() -> Iterator[None]:
    # the most restrictive umask root could run with under sudo: nothing for group or others
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.usefixtures("strict_umask")
def test_every_parent_the_release_creates_gets_the_release_dir_mode(tmp_path: Path) -> None:
    operator = tmp_path / "srv"
    operator.mkdir()
    operator.chmod(OPERATOR_DIR_MODE)
    dest = operator / "shidashi" / "releases" / "2026.10.22"

    ensure_release_dir(dest)

    assert _mode(operator / "shidashi") == RELEASE_DIR_MODE
    assert _mode(operator / "shidashi" / "releases") == RELEASE_DIR_MODE
    assert _mode(dest) == RELEASE_DIR_MODE
    assert _mode(operator) == OPERATOR_DIR_MODE


@pytest.mark.usefixtures("strict_umask")
def test_the_outcome_directory_gets_the_release_dir_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, int] = {}

    def observing_build(argv: Sequence[str]) -> int:
        del argv
        seen["outcome"] = _mode(tmp_path / ".outcome")
        return 1  # no outcome written: the variant ends as an error, which is not under test

    monkeypatch.setattr(release_core, "run_build", observing_build)
    (tmp_path / ".outcome").mkdir()
    (tmp_path / ".outcome").chmod(0o700)  # what an earlier run under umask 077 left

    release_cli._build(audit.Recorder(), release_core.Variant("amd64", "openrc"), tmp_path, [])

    assert seen["outcome"] == RELEASE_DIR_MODE
