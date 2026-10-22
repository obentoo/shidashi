"""``release.json`` is published readable by everyone (story 030, task 9.1).

The validation of 2026-10-22 found it 0600 in bentoo-lab's release: the temporary file it is
renamed from is created 0600, so a non-root ``sha256sum -c SHA256SUMS`` or a mirror that keeps
file modes could not read it. Every other published file of the directory is 0644.
"""

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi.release import MANIFEST_FILE, write_manifest


@pytest.fixture
def strict_umask() -> Iterator[None]:
    # the most restrictive umask a maintainer could run with: nothing for group or others
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.usefixtures("strict_umask")
def test_release_json_is_0644_after_the_first_write(tmp_path: Path) -> None:
    write_manifest(tmp_path, {"date": "2026-10-22"})
    assert _mode(tmp_path / MANIFEST_FILE) == 0o644


@pytest.mark.usefixtures("strict_umask")
def test_release_json_stays_0644_after_a_rewrite(tmp_path: Path) -> None:
    write_manifest(tmp_path, {"date": "2026-10-22"})
    write_manifest(tmp_path, {"date": "2026-10-22", "variants": []})
    assert _mode(tmp_path / MANIFEST_FILE) == 0o644
