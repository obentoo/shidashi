"""A corrupt owner lock is reported, and ``unlock --force`` clears it (story 018).

Found by story 010's audit: a truncated or hand-edited ``<arch>.owner.json`` made
``ownership.current()`` raise pydantic's ValidationError, so the release command itself
ended in a traceback.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, ownership
from shidashi.cli import app

runner = CliRunner()


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "cache"
    monkeypatch.setenv("SHIDASHI_CACHE", str(root))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    return root


def _corrupt(text: str) -> Path:
    path = ownership.locks_dir() / "v3.owner.json"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text",
    [
        '{"arch": "v3", "worker": "bentoo-lab", "job": "fac',  # truncated mid-write
        "not json at all\n",
        '{"arch": "v3"}\n',  # JSON, but not an Owner
    ],
)
def test_current_on_a_corrupt_lock_raises_corrupt_lock_naming_the_file(
    cache: Path, text: str
) -> None:
    path = _corrupt(text)
    with pytest.raises(ownership.CorruptLock) as err:
        ownership.current("v3")
    assert isinstance(err.value, ownership.LockError)
    assert str(path) in str(err.value)
    assert "shidashi worker unlock v3 --force" in str(err.value)


def test_unlock_without_force_on_a_corrupt_lock_exits_1_and_keeps_it(cache: Path) -> None:
    path = _corrupt("garbage")
    result = runner.invoke(app, ["worker", "unlock", "v3"])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert str(path) in result.output and "--force" in result.output
    assert path.exists()


def test_unlock_force_removes_a_corrupt_lock(cache: Path) -> None:
    path = _corrupt('{"arch": "v3", "wor')
    result = runner.invoke(app, ["worker", "unlock", "v3", "--force"])
    assert result.exit_code == 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not path.exists()
    assert "v3" in result.output
    ownership.acquire(  # the arch is usable again
        "v3",
        ownership.Owner(
            arch="v3", worker="host:x", job="factory", commit="a" * 40, since="t", host_pid=None
        ),
    )


def test_a_host_factory_on_a_corrupt_lock_exits_1_without_a_traceback(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            built.append(recipe.arch)

        def build(self, **_k: object) -> None:
            raise SystemExit(0)

    monkeypatch.setattr(cli, "Factory", FakeFactory)
    path = _corrupt("garbage")
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert str(path) in result.output
    assert built == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_discard_on_an_unwritable_locks_dir_raises_lock_error(cache: Path) -> None:
    _corrupt("garbage")
    locks = ownership.locks_dir()
    locks.chmod(0o555)
    try:
        with pytest.raises(ownership.LockError):
            ownership.discard("v3")
    finally:
        locks.chmod(0o2775)


def test_a_lock_file_that_is_not_utf8_is_a_corrupt_lock(cache: Path) -> None:
    path = ownership.locks_dir() / "v3.owner.json"
    path.write_bytes(b'{"arch": "v3", "worker": "\xff\xfe\x80"}')
    with pytest.raises(ownership.CorruptLock):
        ownership.current("v3")
    result = runner.invoke(app, ["worker", "unlock", "v3", "--force"])
    assert result.exit_code == 0, result.output
    assert not path.exists()
