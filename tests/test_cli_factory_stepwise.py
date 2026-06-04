"""UNIT da CLI ``shidashi factory`` stepwise via Typer ``CliRunner`` (story 004 7.1).

Determinista no host CI: ``Factory.build``/``build_stepwise`` (privilegiados) são
monkeypatched no namespace de ``shidashi.cli`` — exercitamos só a CAMADA CLI:

* guardas/usabilidade: ``--step`` sem TTY → exit 1 (monkeypatch ``isatty``);
  ``--step --format json`` → exit 1 (R2.6/R2.7);
* ``--help`` lista ``--step``/``--until``/``--reset``/``--force-resume`` (R8.3);
* propagação: ``--until <phase>`` chega a ``build_stepwise``; ``--step`` produz
  ``interactive=True``; ``--reset`` chega como flag (R1.1/R2.1/R6.3);
* mapeamento de exit: stop limpo → exit 0; FactoryError (abort) → exit 1 amigável
  (R1.5/R3.3/R8.1).

``--step``/``--until`` etc. ainda não existem na CLI (story 003 só tem o one-shot),
então estes casos ficam Red pelo motivo esperado (exit code/flag ausente).
"""

import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli
from shidashi.cli import app
from tests._pending import try_import

FactoryError: Any = try_import("shidashi.factory", "FactoryError")
FactoryResult: Any = try_import("shidashi.factory", "FactoryResult")

runner = CliRunner()

_BASE_YAML = """\
profile_base: default/linux/amd64/23.0/no-multilib
sets:
  - graphics
  - bentoo-apps
phases:
  - name: rebuild
  - name: graphics
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
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    return root


def _fake_result(**over: Any) -> Any:
    base: dict[str, Any] = dict(
        pkgdir=Path("/var/cache/shidashi/binpkgs/v3"),
        built_atoms=("media-libs/libsdl2-2.30.5",),
        phases=("rebuild",),
        fork_point=None,
        fork_point_reused=False,
        settle_atoms=(),
        stopped_at="rebuild",
        completed_phases=("rebuild",),
        phase_diffs=(),
    )
    base.update(over)
    return FactoryResult(**base)


class _FakeInstance:
    def __init__(self, step_fn: Any) -> None:
        self._step_fn = step_fn

    def build(self, **_k: object) -> Any:  # one-shot não usado aqui
        return _fake_result(stopped_at=None)

    def build_stepwise(self, **kwargs: object) -> Any:
        return self._step_fn(**kwargs)


def _fake_factory(step_fn: Any) -> Any:
    def _ctor(_recipe: object, _pkgdir: Path) -> _FakeInstance:
        return _FakeInstance(step_fn)

    return _ctor


def _force_tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    # patcha o stdin REAL (stdlib sys): a CLI consulta sys.stdin.isatty() para a
    # guarda de TTY do --step. Independe de como shidashi.cli importa sys.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: value, raising=False)


# --- R8.3 --help lista as novas flags ----------------------------------------


def test_factory_help_lists_new_flags() -> None:
    result = runner.invoke(app, ["factory", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "--step" in out
    assert "--until" in out
    assert "--reset" in out
    assert "--force-resume" in out


# --- R2.6 --step sem TTY → exit 1 --------------------------------------------


def test_step_without_tty_exits_1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    _force_tty(monkeypatch, False)
    monkeypatch.setattr(
        cli, "Factory", _fake_factory(lambda **_k: _fake_result()), raising=False
    )
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--step"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "--until" in combined  # mensagem direciona ao caminho scriptável


# --- R2.7 --step com --format json → exit 1 ----------------------------------


def test_step_with_json_format_exits_1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    _force_tty(monkeypatch, True)
    monkeypatch.setattr(
        cli, "Factory", _fake_factory(lambda **_k: _fake_result()), raising=False
    )
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--step", "--format", "json"]
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined


# --- R1.1 --until propaga para build_stepwise --------------------------------


def test_until_propagates_to_build_stepwise(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    captured: dict[str, object] = {}

    def _spy(**k: object) -> Any:
        captured.update(k)
        return _fake_result()

    monkeypatch.setattr(cli, "Factory", _fake_factory(_spy), raising=False)
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--until", "rebuild"]
    )
    assert result.exit_code == 0, result.stdout
    assert captured.get("until") == "rebuild"


def test_step_sets_interactive_true(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    _force_tty(monkeypatch, True)
    captured: dict[str, object] = {}

    def _spy(**k: object) -> Any:
        captured.update(k)
        return _fake_result()

    monkeypatch.setattr(cli, "Factory", _fake_factory(_spy), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--step"])
    assert result.exit_code == 0, result.stdout
    assert captured.get("interactive") is True


# --- R1.5 --until inválido → exit 1 (ValueError de plan_phase_run) -----------


def test_until_invalid_phase_exits_1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    def _raise(**_k: object) -> Any:
        raise ValueError("fase inválida 'bogus'; válidas: seed, rebuild, graphics, apps")

    monkeypatch.setattr(cli, "Factory", _fake_factory(_raise), raising=False)
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--until", "bogus"]
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "rebuild" in combined  # lista os nomes válidos


# --- R3.3/R8.1 stop limpo → exit 0; FactoryError (abort) → exit 1 ------------


def test_clean_stop_exits_0_and_reports_stopped_at(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    monkeypatch.setattr(
        cli, "Factory", _fake_factory(lambda **_k: _fake_result(stopped_at="rebuild")),
        raising=False,
    )
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--until", "rebuild"]
    )
    assert result.exit_code == 0, result.stdout
    assert "rebuild" in result.stdout


def test_abort_factory_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    def _raise(**_k: object) -> Any:
        raise FactoryError("abortado pelo usuário", phase="rebuild", output="!!! build break")

    monkeypatch.setattr(cli, "Factory", _fake_factory(_raise), raising=False)
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--until", "rebuild"]
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "rebuild" in combined
