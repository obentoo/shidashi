"""UNIT da CLI ``kaji factory`` via Typer ``CliRunner`` (story 003 7.1).

Determinista no host CI: ``Factory.build`` (privilegiado) é monkeypatched no
namespace de ``kaji.cli`` p/ devolver um ``FactoryResult`` sintético ou levantar
``FactoryError``/``SeedError``/``ResolveError`` — exercitamos só a CAMADA CLI:
``--help`` lista ``factory`` como comando REAL (não mais o stub exit-2),
renderização pretty/json, mapeamento de exit codes e propagação de flags
(``--no-download``/``--keep``/``--no-emptytree``/``--pkgdir``) para ``build``.
Nenhum stage3/nspawn é tocado (espelha ``tests/test_cli_pretend.py``).

Contrato (design.md §cli): ``factory(arch, flavor, init, --format=[pretty|json]
(default pretty), --emptytree/--no-emptytree (default on), --pkgdir <path>,
--keep, --no-download)``; sucesso exit 0; FactoryError/SeedError/ResolveError/
UnknownAxisError → mensagem amigável + Exit(1); FactoryError imprime a phase que
falhou + ``output``.

``FactoryError``/``FactoryResult`` são importados de forma tolerante e
``cli.Factory`` é patchado com ``raising=False`` (ainda não existe). Até a CLI
substituir o stub ``factory`` (hoje exit 2), estes casos ficam Red pelo motivo
esperado.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kaji import cli
from kaji.cli import app
from kaji.resolve import ResolveError
from kaji.seed import SeedError
from tests._pending import try_import

FactoryError: Any = try_import("kaji.factory", "FactoryError")
FactoryResult: Any = try_import("kaji.factory", "FactoryResult")

runner = CliRunner()

# variants/ mínima reutilizada do estilo de test_cli.py -----------------------

_BASE_YAML = """\
profile_base: default/linux/amd64/23.0/no-multilib
sets:
  - graphics
  - bentoo-apps
phases:
  - name: rebuild
  - name: desktop
  - name: apps
"""
_ARCH_V3 = """\
arch: v3
common_flags: "-O2 -march=x86-64-v3 -pipe"
goamd64: v3
rustflags: "-C target-cpu=x86-64-v3"
cpu_flags_x86: [sse4_2, avx2]
tier: 1
runnable_on_build_host: true
"""
_FLAVOR_MINIMAL = "flavor: minimal\nsets: []\noverride_ok: true\n"
_INIT_SYSTEMD = "init: systemd\nprofile_suffix: systemd\nuse_prefer:\n  add: [systemd]\n"

_RECIPES = {
    ("flavor", "minimal"): _FLAVOR_MINIMAL,
    ("arch", "v3"): _ARCH_V3,
    ("init", "systemd"): _INIT_SYSTEMD,
}


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "variants"
    base = root / "base" / "base.yaml"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(_BASE_YAML, encoding="utf-8")
    for (axis, name), text in _RECIPES.items():
        recipe = root / axis / name / "recipe.yaml"
        recipe.parent.mkdir(parents=True, exist_ok=True)
        recipe.write_text(text, encoding="utf-8")
    monkeypatch.setenv("KAJI_VARIANTS_DIR", str(root))
    return root


def _fake_result() -> Any:
    return FactoryResult(
        pkgdir=Path("/var/cache/kaji/binpkgs/v3"),
        built_atoms=("media-libs/libsdl2-2.30.5", "sys-apps/portage-3.0.66"),
        phases=("rebuild", "graphics", "desktop"),
        fork_point=Path("/c/fork-points/v3-minimal-systemd-SNAP.tar"),
        fork_point_reused=False,
        settle_atoms=("media-video/ffmpeg-6.1.1",),
    )


# --- helpers: fake Factory ----------------------------------------------------


class _FakeInstance:
    def __init__(self, build_fn: Any) -> None:
        self._build_fn = build_fn

    def build(self, **kwargs: object) -> Any:
        return self._build_fn(**kwargs)


def _fake_factory(build_fn: Any) -> Any:
    def _ctor(_recipe: object, _pkgdir: Path) -> _FakeInstance:
        return _FakeInstance(build_fn)

    return _ctor


# --- --help lista factory como comando real (R1.4) ---------------------------


def test_help_lists_factory_as_real_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "factory" in result.stdout


def test_factory_help_shows_options() -> None:
    result = runner.invoke(app, ["factory", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "--format" in out
    assert "--no-download" in out
    assert "--keep" in out
    assert "--pkgdir" in out


# --- sucesso pretty exit 0 (R1.1) --------------------------------------------


def test_factory_success_pretty_exit0(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    monkeypatch.setattr(cli, "Factory", _fake_factory(lambda **_k: _fake_result()), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    assert "libsdl2" in result.stdout


# --- json shape (R1.2/R1.3) --------------------------------------------------


def test_factory_json_shape(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    monkeypatch.setattr(cli, "Factory", _fake_factory(lambda **_k: _fake_result()), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert "media-libs/libsdl2-2.30.5" in data["built_atoms"]
    assert data["phases"] == ["rebuild", "graphics", "desktop"]
    assert "fork_point" in data
    assert "media-video/ffmpeg-6.1.1" in data["settle_atoms"]


# --- FactoryError → exit 1 imprimindo phase + output (R8.3) ------------------


def test_factory_error_exit1_prints_phase_and_output(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    def _raise(**_k: object) -> Any:
        raise FactoryError("emerge failed", phase="graphics", output="!!! ffmpeg build error")

    monkeypatch.setattr(cli, "Factory", _fake_factory(_raise), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "graphics" in combined
    assert "ffmpeg build error" in combined


# --- SeedError / ResolveError → exit 1 amigável ------------------------------


def test_factory_seed_error_exit1(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    def _raise(**_k: object) -> Any:
        raise SeedError("sha256 mismatch for stage3 tarball")

    monkeypatch.setattr(cli, "Factory", _fake_factory(_raise), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "sha256" in combined.lower()


def test_factory_resolve_error_exit1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    def _raise(**_k: object) -> Any:
        raise ResolveError("repo 'bentoo' ausente; rode emerge --sync")

    monkeypatch.setattr(cli, "Factory", _fake_factory(_raise), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "bentoo" in combined


# --- flags propagam para build (R1.5) ----------------------------------------


def test_factory_flags_propagate_to_build(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    captured: dict[str, object] = {}

    def _spy(**k: object) -> Any:
        captured.update(k)
        return _fake_result()

    monkeypatch.setattr(cli, "Factory", _fake_factory(_spy), raising=False)
    result = runner.invoke(
        app,
        ["factory", "v3", "minimal", "systemd", "--no-download", "--keep", "--no-emptytree"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured.get("download") is False
    assert captured.get("keep") is True
    assert captured.get("emptytree") is False


def test_factory_pkgdir_override_used(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def _capture_init(_recipe: object, pkgdir: Path) -> _FakeInstance:
        seen["pkgdir"] = pkgdir
        return _FakeInstance(lambda **_k: _fake_result())

    monkeypatch.setattr(cli, "Factory", _capture_init, raising=False)
    override = tmp_path / "custom-pkgdir"
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--pkgdir", str(override)]
    )
    assert result.exit_code == 0, result.stdout
    assert seen["pkgdir"] == override
