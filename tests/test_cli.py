"""Testes de INTEGRAÇÃO da CLI do Kaji (kaji.cli) via Typer ``CliRunner``.

Constrói uma árvore ``variants/`` mínima e VÁLIDA em ``tmp_path`` e aponta a CLI
para ela com ``KAJI_VARIANTS_DIR``. A fixture permite que ``v3 × minimal ×
systemd`` e ``v3 × kde × systemd`` fundam-se sem conflito; ``init/badinit`` (que
derruba ``qt6``) força um :class:`RecipeConflictError` sintético contra a flavor
``kde`` (``override_ok: false``).

Requisitos exercitados: R1.4, R4.1, R4.2, R4.3, R5.1, R5.2, R6.1, R6.2, R6.3.
"""

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from kaji.cli import app

runner = CliRunner()

# --- conteúdo da fixture variants/ -------------------------------------------

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
cpu_flags_x86:
  - sse4_2
  - avx2
tier: 1
runnable_on_build_host: true
"""

_FLAVOR_MINIMAL = """\
flavor: minimal
sets: []
override_ok: true
"""

_FLAVOR_KDE = """\
flavor: kde
use_prefer:
  add: [qt6, kde, wayland]
  drop: [gtk, gnome, webkit]
sets: [kde]
override_ok: false
"""

_INIT_SYSTEMD = """\
init: systemd
profile_suffix: systemd
use_prefer:
  add: [systemd]
"""

_INIT_OPENRC = """\
init: openrc
profile_suffix: ""
use_prefer:
  add: [elogind, udev]
  drop: [systemd]
phases_prepend:
  - name: seat
"""

# badinit derruba qt6, que a flavor kde ADICIONA (sinal oposto). Como kde tem
# override_ok=false, o merge levanta RecipeConflictError → validate sai com 1.
_INIT_BADINIT = """\
init: badinit
profile_suffix: bad
use_prefer:
  drop: [qt6]
"""

_RECIPES = {
    ("flavor", "minimal"): _FLAVOR_MINIMAL,
    ("flavor", "kde"): _FLAVOR_KDE,
    ("arch", "v3"): _ARCH_V3,
    ("init", "systemd"): _INIT_SYSTEMD,
    ("init", "openrc"): _INIT_OPENRC,
    ("init", "badinit"): _INIT_BADINIT,
}


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Monta a árvore variants/ mínima e aponta KAJI_VARIANTS_DIR para ela."""
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


# --- recipe show -------------------------------------------------------------


def test_show_default_yaml_parses(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "show", "v3", "minimal", "systemd"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.stdout)
    assert isinstance(data, dict)
    assert data["arch"] == "v3"
    assert data["flavor"] == "minimal"
    assert data["init"] == "systemd"


def test_show_json_parses(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "show", "v3", "minimal", "systemd", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["arch"] == "v3"
    assert data["profile"] == "default/linux/amd64/23.0/no-multilib/systemd"


def test_show_pretty_runs(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "show", "v3", "kde", "systemd", "--format", "pretty"])
    assert result.exit_code == 0
    assert "v3" in result.stdout


def test_show_unknown_axis_exit1_no_traceback(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "show", "nope", "kde", "systemd"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "v3" in combined  # lista os arch disponíveis


# --- recipe validate ---------------------------------------------------------


def test_validate_clean_merge_exit0(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "validate", "v3", "kde", "systemd"])
    assert result.exit_code == 0
    assert "v3" in result.stdout


def test_validate_synthetic_conflict_exit1_friendly(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "validate", "v3", "kde", "badinit"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "qt6" in combined  # a flag em conflito aparece na mensagem amigável


def test_validate_unknown_arch_lists_available(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "validate", "bogus", "kde", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "v3" in combined  # os nomes de arch disponíveis são listados


# --- recipe list -------------------------------------------------------------


def test_list_shows_axis_names(variants_tree: Path) -> None:
    result = runner.invoke(app, ["recipe", "list"])
    assert result.exit_code == 0
    out = result.stdout
    assert "v3" in out
    assert "kde" in out
    assert "minimal" in out
    assert "systemd" in out
    assert "openrc" in out


# --- stubs (R6.2) ------------------------------------------------------------


def test_stub_factory_exit2(variants_tree: Path) -> None:
    result = runner.invoke(app, ["factory", "v3", "kde", "systemd"])
    assert result.exit_code == 2
    assert "Fase 0" in result.stdout


def test_stub_assemble_exit2(variants_tree: Path) -> None:
    result = runner.invoke(app, ["assemble", "v3", "kde", "systemd"])
    assert result.exit_code == 2
    assert "Fase 0" in result.stdout


def test_stub_release_exit2(variants_tree: Path) -> None:
    result = runner.invoke(app, ["release", "--all"])
    assert result.exit_code == 2
    assert "Fase 0" in result.stdout


# --- no-args mostra ajuda (R6.3) ---------------------------------------------


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code in (0, 2)
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Usage" in result.stdout or "Commands" in result.stdout
