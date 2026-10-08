"""The host's own builds honour the owner lock (story 010, R5.2).

Only commands that WRITE a PKGDIR are writers: ``factory`` and ``build``. An
``assemble`` only reads the binhost: it never consults the lock.
"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, ownership
from shidashi.assembler import AssembleResult
from shidashi.cli import app

runner = CliRunner()


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    built: list[str] = []

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            built.append(f"factory:{recipe.arch}")
            self.recipe = recipe

        def build(self, **_k: object) -> None:
            raise SystemExit(0)

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            built.append(f"assembler:{recipe.arch}")
            self.recipe = recipe

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            iso = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    monkeypatch.setattr(cli, "Factory", FakeFactory)
    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    return built


def _hold(arch: str) -> None:
    ownership.acquire(
        arch,
        ownership.Owner(
            arch=arch,
            worker="bentoo-lab",
            job="fac-" + arch,
            commit="a" * 40,
            since="2026-10-05T14:02:31Z",
            host_pid=None,
        ),
    )


# hostile halves first: a lock must not refuse what it does not guard


def test_a_lock_on_another_arch_does_not_refuse_the_host_factory(lab: list[str]) -> None:
    _hold("znver5")
    runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    assert lab == ["factory:v3"]


def test_an_assemble_is_not_a_writer_and_ignores_the_lock(lab: list[str]) -> None:
    _hold("v3")
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.output
    assert "assembler:v3" in lab


def test_a_host_factory_for_an_owned_arch_is_refused_before_the_factory_exists(
    lab: list[str], tmp_path: Path
) -> None:
    _hold("v3")
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    for part in ("bentoo-lab", "fac-v3", "shidashi worker unlock v3"):
        assert part in result.output
    assert lab == []
    assert not (tmp_path / "cache" / "binpkgs" / "v3").exists()


def test_a_host_build_for_a_worker_owned_arch_skips_its_factory_and_still_assembles(
    lab: list[str],
) -> None:
    _hold("v3")  # a worker (bentoo-lab) owns v3
    result = runner.invoke(app, ["build", "v3", "systemd", "--images", "minimal"])
    assert not any(b.startswith("factory:") for b in lab)  # the factory is skipped
    assert "assembler:v3" in lab  # the assembles still run (R5.8, story 014 R3.3)
    skipped = [line for line in result.output.splitlines() if "bentoo-lab" in line]
    assert len(skipped) == 1 and "factory" in skipped[0].lower()  # one line naming the owner
    owner = ownership.current("v3")
    assert owner is not None and owner.worker == "bentoo-lab"  # no lock taken over it


# --- the host's writers hold the lock themselves (R5.7, contract C5) -------------------


def _factory_that_records(
    monkeypatch: pytest.MonkeyPatch, seen: list[Any], *, fail: bool = False
) -> None:
    class RecordingFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            self.recipe = recipe

        def build(self, **_k: object) -> None:
            seen.append(ownership.current(self.recipe.arch))
            if fail:
                raise RuntimeError("emerge failed")
            raise SystemExit(0)

    monkeypatch.setattr(cli, "Factory", RecordingFactory)


def test_a_host_factory_holds_the_lock_with_its_pid_while_it_builds(
    lab: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    seen: list[Any] = []
    _factory_that_records(monkeypatch, seen)
    runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    (held,) = seen
    assert held is not None and held.host_pid == os.getpid()
    assert held.worker.startswith("host:")
    assert ownership.current("v3") is None  # released at the end


def test_a_failed_host_factory_releases_the_lock(
    lab: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    _factory_that_records(monkeypatch, seen, fail=True)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    assert result.exit_code != 0
    assert seen and seen[0] is not None
    assert ownership.current("v3") is None


def test_a_build_that_skips_the_factory_neither_takes_nor_is_refused_by_the_lock(
    lab: list[str],
) -> None:
    _hold("v3")
    result = runner.invoke(app, ["build", "v3", "systemd", "--images", "minimal", "--skip-factory"])
    assert "shidashi worker unlock v3" not in result.output
    assert not any(b.startswith("factory:") for b in lab)
    owner = ownership.current("v3")
    assert owner is not None and owner.job == "fac-v3"


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root writes anywhere")
def test_an_unwritable_locks_directory_exits_1_naming_it(lab: list[str], tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    cache.chmod(0o555)
    try:
        result = runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    finally:
        cache.chmod(0o755)
    assert result.exit_code == 1
    assert str(cache) in result.output
    assert lab == []
