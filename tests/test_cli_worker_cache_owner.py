"""Story 028: ``shidashi worker cache-owner NAME [--arch ARCH] [--off]``."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from shidashi import config, ownership, workers
from shidashi.cli import app
from shidashi.workers import WorkerEntry

cli = CliRunner()


def _entry(name: str, *archs: str) -> WorkerEntry:
    return WorkerEntry(
        name=name,
        host_key="ssh-ed25519 AAAA",
        host_key_fingerprint=f"SHA256:{name}",
        paired_at="2026-10-17T00:00:00Z",
        cache_archs=archs,
    )


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    path = config.workers_dir() / "workers.json"
    path.parent.mkdir(parents=True)
    workers.save_registry(path, {"bentoo-lab": _entry("bentoo-lab"), "other": _entry("other")})
    yield path


def _run(*args: str) -> Result:
    return cli.invoke(app, ["worker", "cache-owner", *args])


def _owned(path: Path, name: str) -> tuple[str, ...]:
    return workers.load_registry(path)[name].cache_archs


def test_declare_records_the_arch_and_says_so(registry: Path) -> None:
    result = _run("bentoo-lab", "--arch", "v3")
    assert result.exit_code == 0, result.output
    assert _owned(registry, "bentoo-lab") == ("v3",)
    assert "v3" in result.output and "bentoo-lab" in result.output


def test_declaring_twice_changes_nothing(registry: Path) -> None:
    _run("bentoo-lab", "--arch", "v3")
    result = _run("bentoo-lab", "--arch", "v3")
    assert result.exit_code == 0, result.output
    assert _owned(registry, "bentoo-lab") == ("v3",)


def test_without_arch_it_lists_what_the_worker_owns(registry: Path) -> None:
    _run("bentoo-lab", "--arch", "v3")
    result = _run("bentoo-lab")
    assert result.exit_code == 0, result.output
    assert "v3" in result.output
    assert "no arch" in _run("other").output


def test_off_withdraws_the_arch(registry: Path) -> None:
    _run("bentoo-lab", "--arch", "v3")
    result = _run("bentoo-lab", "--arch", "v3", "--off")
    assert result.exit_code == 0, result.output
    assert _owned(registry, "bentoo-lab") == ()


def test_an_arch_owned_by_another_worker_is_refused_naming_it(registry: Path) -> None:
    _run("other", "--arch", "v3")
    result = _run("bentoo-lab", "--arch", "v3")
    assert result.exit_code == 1
    assert "other" in result.output
    assert _owned(registry, "bentoo-lab") == ()


def test_a_held_owner_lock_refuses_any_change(registry: Path) -> None:
    ownership.acquire(
        "v3",
        ownership.Owner(arch="v3", worker="bentoo-lab", job="fac-v3", commit="abc123", since="now"),
    )
    result = _run("bentoo-lab", "--arch", "v3")
    assert result.exit_code == 1
    assert "lock" in result.output
    assert _owned(registry, "bentoo-lab") == ()


def test_an_unknown_arch_is_a_usage_error(registry: Path) -> None:
    result = _run("bentoo-lab", "--arch", "pentium4")
    assert result.exit_code == 2
    assert "pentium4" in result.output and "v3" in result.output  # names the known archs
    assert _owned(registry, "bentoo-lab") == ()


def test_off_without_arch_is_a_usage_error(registry: Path) -> None:
    result = _run("bentoo-lab", "--off")
    assert result.exit_code == 2
    assert "--off needs --arch" in result.output


def test_an_unpaired_worker_is_refused(registry: Path) -> None:
    result = _run("ghost", "--arch", "v3")
    assert result.exit_code == 1
    assert "ghost" in result.output
