"""UNIT da CLI ``shidashi assemble`` via Typer ``CliRunner`` (Fase 1).

Determinista no host CI: ``Assembler.assemble`` (privilegiado) é monkeypatched no
namespace de ``shidashi.cli`` p/ devolver um ``Path`` de ISO sintético ou levantar
``AssemblerError``/``ImageError``/``SeedError``/``ResolveError``/``CalledProcessError``
— exercitamos só a CAMADA CLI: ``--help``, render pretty/json, mapeamento de exit
codes e propagação de flags (``--output``/``--no-download``/``--keep``/``--binhost``).
Nenhum stage3/nspawn/mksquashfs é tocado (espelha tests/test_cli_factory.py).
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli
from shidashi.assembler import AssemblerError
from shidashi.cli import app
from shidashi.image import ImageError
from shidashi.resolve import ResolveError
from shidashi.seed import SeedError
from tests._variants_tree import write_variants

runner = CliRunner()

@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared stage-format tree (tests/_variants_tree.py), pointed at by the env."""
    root = write_variants(tmp_path / "variants")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    # cache/scratch under tmp so --binhost's default (config.pkgdir) never touches /var.
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    return root


# --- fake Assembler -----------------------------------------------------------


class _FakeInstance:
    def __init__(self, assemble_fn: Any, recipe: object, binhost: Path) -> None:
        self._assemble_fn = assemble_fn
        self.recipe = recipe
        self.binhost = binhost

    def assemble(self, output: Path, **kwargs: object) -> Any:
        return self._assemble_fn(output, **kwargs)


def _fake_assembler(assemble_fn: Any, sink: dict[str, Any] | None = None) -> Any:
    def _ctor(recipe: object, binhost: Path) -> _FakeInstance:
        inst = _FakeInstance(assemble_fn, recipe, binhost)
        if sink is not None:
            sink["instance"] = inst
        return inst

    return _ctor


# --- --help -------------------------------------------------------------------


def test_help_lists_assemble() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "assemble" in result.stdout


def test_assemble_help_shows_options() -> None:
    result = runner.invoke(app, ["assemble", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "--format" in out
    assert "--output" in out
    assert "--binhost" in out
    assert "--no-download" in out
    assert "--keep" in out


# --- sucesso pretty/json ------------------------------------------------------


def test_assemble_success_pretty_exit0(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    iso = Path("/dist/bentoo-minimal-systemd-v3.iso")
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda _o, **_k: iso), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    assert "bentoo-minimal-systemd-v3.iso" in result.stdout


def test_assemble_json_shape(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    iso = Path("/dist/x.iso")
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda _o, **_k: iso), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["iso"] == "/dist/x.iso"
    assert data["arch"] == "v3"
    assert data["flavor"] == "minimal"
    assert data["init"] == "systemd"
    assert "binhost" in data


# --- propagação de flags ------------------------------------------------------


def test_assemble_passes_output_and_flags(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    captured: dict[str, Any] = {}

    def _capture(output: Path, **kwargs: object) -> Path:
        captured["output"] = output
        captured["kwargs"] = kwargs
        return output

    monkeypatch.setattr(cli, "Assembler", _fake_assembler(_capture), raising=False)
    result = runner.invoke(
        app,
        [
            "assemble",
            "v3",
            "minimal",
            "systemd",
            "-o",
            "/tmp/custom.iso",
            "--no-download",
            "--keep",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["output"] == Path("/tmp/custom.iso")
    assert captured["kwargs"] == {"download": False, "keep": True}


def test_assemble_default_output_name(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        cli,
        "Assembler",
        _fake_assembler(lambda o, **_k: captured.update(output=o) or o),
        raising=False,
    )
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    assert captured["output"] == Path("bentoo-minimal-systemd-v3.iso")


def test_assemble_binhost_default_is_per_arch(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda o, **_k: o, sink), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    # binhost default particionado por arch (config.pkgdir): .../binpkgs/v3
    assert sink["instance"].binhost.parts[-2:] == ("binpkgs", "v3")


# --- mapeamento de erros → exit 1 amigável -----------------------------------


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (AssemblerError("nenhum vmlinuz encontrado"), "vmlinuz"),
        (ImageError("mksquashfs ausente no host"), "mksquashfs"),
        (SeedError("sha512 mismatch for stage3 tarball"), "sha512"),
        (ResolveError("repo 'bentoo' ausente; rode emerge --sync"), "bentoo"),
    ],
)
def test_assemble_known_errors_exit1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path, exc: Exception, needle: str
) -> None:
    def _raise(_o: Path, **_k: object) -> Any:
        raise exc

    monkeypatch.setattr(cli, "Assembler", _fake_assembler(_raise), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert needle.lower() in combined.lower()


def test_assemble_emerge_failure_exit1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    def _raise(_o: Path, **_k: object) -> Any:
        raise subprocess.CalledProcessError(1, ["emerge"], stderr="!!! sem binpkg para @kde")

    monkeypatch.setattr(cli, "Assembler", _fake_assembler(_raise), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "emerge/dracut" in combined


def test_assemble_unknown_axis_exit1(variants_tree: Path) -> None:
    # eixo desconhecido falha no _resolve, antes de instanciar o Assembler.
    result = runner.invoke(app, ["assemble", "v3", "naoexiste", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined


def test_assemble_is_not_a_stub(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    # regressão: assemble deixou de ser o stub exit-2 "Fase 0".
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda o, **_k: o), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0
    assert "Fase 0" not in result.stdout
