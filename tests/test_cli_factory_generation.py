"""Contract check: what ``shidashi factory`` shows of a generation failure
(story 016, task 4.4, R4.3, R5.2, R5.7, R7.4).

The errors are the real ones -- ``check_or_record`` and ``run_update`` raise them
-- and only ``Factory`` is faked, so the test covers the message from the
comparison to the terminal, through both unchanged handlers.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli
from shidashi.cli import app
from shidashi.generation import FINGERPRINT_FILE, GenerationFingerprint, check_or_record
from shidashi.update import run_update
from tests._variants_tree import write_variants
from tests.test_update_abi import GCC_MAJOR, FakeContainer, _recipe

runner = CliRunner()
PKGDIR = Path("binpkgs/v3/20260823T153057Z")


def _fp(gcc: str) -> GenerationFingerprint:
    return GenerationFingerprint(
        arch="v3",
        profile="p",
        common_flags="-O2",
        chost="x86_64-pc-linux-gnu",
        llvm_slot="22",
        gcc=gcc,
        binutils="2.46.1",
        glibc="2.43",
    )


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(write_variants(tmp_path / "variants")))
    monkeypatch.chdir(tmp_path)  # a short, relative PKGDIR: the message is not wrapped mid-path


class _Fake:
    def __init__(self, _recipe: object, _pkgdir: Path) -> None:
        pass

    def build(self, **_k: object) -> Any:
        check_or_record(PKGDIR, _fp("17.1.0"), after_phase="base")

    build_stepwise = build

    def update(self, **_k: object) -> Any:
        return run_update(FakeContainer(GCC_MAJOR), _recipe(), current=_fp("16.2.0"))  # type: ignore[arg-type]


def _flat(result: Any) -> str:
    return "".join(result.output.split())


@pytest.mark.parametrize("extra", [[], ["--until", "base"]])
def test_a_refused_recheck_reaches_the_user_with_the_successor(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    check_or_record(PKGDIR, _fp("16.2.0"))
    monkeypatch.setattr(cli, "Factory", _Fake)
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--pkgdir", str(PKGDIR), *extra]
    )
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    out = _flat(result)
    assert "generation" in out and "'base'" in out
    assert "gcc:'16.2.0'->'17.1.0'" in out
    assert "--pkgdirbinpkgs/v3/20260823T153057Z-gcc17" in out


def test_an_unreadable_fingerprint_reaches_the_user_naming_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    PKGDIR.mkdir(parents=True)
    (PKGDIR / FINGERPRINT_FILE).write_text(json.dumps({"arch": "v3"}), encoding="utf-8")
    monkeypatch.setattr(cli, "Factory", _Fake)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--pkgdir", str(PKGDIR)])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert FINGERPRINT_FILE in _flat(result)


def test_an_update_crossing_the_gcc_major_reaches_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Factory", _Fake)
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--update", "--no-download"]
    )
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    out = _flat(result)
    assert "gcc" in out and "16.2.0" in out and "17.1.0" in out


def test_a_long_successor_path_is_printed_unbroken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rich folds at 80 columns when stderr is not a terminal; the handlers print with
    soft_wrap=True so the path the user must copy stays on one line."""
    deep = tmp_path / ("d" * 60) / "cache" / "binpkgs" / "v3" / "20260823T153057Z"
    successor = deep.parent / "20260823T153057Z-gcc17"
    assert len(f"--pkgdir {successor}") > 80
    check_or_record(deep, _fp("16.2.0"))

    class _Deep(_Fake):
        def build(self, **_k: object) -> Any:
            check_or_record(deep, _fp("17.1.0"), after_phase="base")

    monkeypatch.setattr(cli, "Factory", _Deep)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--pkgdir", str(deep)])
    assert result.exit_code == 1
    assert f"--pkgdir {successor}" in result.output


def test_a_refused_update_recheck_reaches_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    check_or_record(PKGDIR, _fp("16.2.0"))

    class _Update(_Fake):
        def update(self, **_k: object) -> Any:
            check_or_record(PKGDIR, _fp("17.1.0"), after_phase="update")

    monkeypatch.setattr(cli, "Factory", _Update)
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--update", "--no-download"]
    )
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    out = _flat(result)
    assert "'update'" in out and "-gcc17" in out
