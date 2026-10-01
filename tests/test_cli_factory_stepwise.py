"""UNIT tests of the stepwise ``shidashi factory`` CLI via Typer's ``CliRunner`` (story 004 7.1).

Deterministic on the CI host: ``Factory.build``/``build_stepwise`` (privileged) are
monkeypatched in the ``shidashi.cli`` namespace — we exercise only the CLI LAYER:

* guards/usability: ``--step`` without a TTY → exit 1 (monkeypatch ``isatty``);
  ``--step --format json`` → exit 1 (R2.6/R2.7);
* ``--help`` lists ``--step``/``--until``/``--reset``/``--force-resume`` (R8.3);
* propagation: ``--until <phase>`` reaches ``build_stepwise``; ``--step`` yields
  ``interactive=True``; ``--reset`` arrives as a flag (R1.1/R2.1/R6.3);
* exit mapping: clean stop → exit 0; FactoryError (abort) → friendly exit 1
  (R1.5/R3.3/R8.1).

``--step``/``--until`` etc. do not exist in the CLI yet (story 003 only has the
one-shot), so these cases stay Red for the expected reason (exit code/missing flag).
"""

import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli
from shidashi.cli import app
from tests._pending import try_import
from tests._variants_tree import write_variants

FactoryError: Any = try_import("shidashi.factory", "FactoryError")
FactoryResult: Any = try_import("shidashi.factory", "FactoryResult")

runner = CliRunner()


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared stage-format tree (tests/_variants_tree.py), pointed at by the env."""
    root = write_variants(tmp_path / "variants")
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

    def build(self, **_k: object) -> Any:  # one-shot not used here
        return _fake_result(stopped_at=None)

    def build_stepwise(self, **kwargs: object) -> Any:
        return self._step_fn(**kwargs)


def _fake_factory(step_fn: Any) -> Any:
    def _ctor(_recipe: object, _pkgdir: Path) -> _FakeInstance:
        return _FakeInstance(step_fn)

    return _ctor


def _force_tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    # patches the REAL stdin (stdlib sys): the CLI checks sys.stdin.isatty() for the
    # --step TTY guard. Independent of how shidashi.cli imports sys.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: value, raising=False)


# --- R8.3 --help lists the new flags -----------------------------------------


def test_factory_help_lists_new_flags() -> None:
    result = runner.invoke(app, ["factory", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "--step" in out
    assert "--until" in out
    assert "--reset" in out
    assert "--force-resume" in out


# --- R2.6 --step without a TTY → exit 1 --------------------------------------


def test_step_without_tty_exits_1(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    _force_tty(monkeypatch, False)
    monkeypatch.setattr(cli, "Factory", _fake_factory(lambda **_k: _fake_result()), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--step"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "--until" in combined  # the message points to the scriptable path


# --- R2.7 --step with --format json → exit 1 ---------------------------------


def test_step_with_json_format_exits_1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    _force_tty(monkeypatch, True)
    monkeypatch.setattr(cli, "Factory", _fake_factory(lambda **_k: _fake_result()), raising=False)
    result = runner.invoke(
        app, ["factory", "v3", "minimal", "systemd", "--step", "--format", "json"]
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined


# --- R1.1 --until propagates to build_stepwise -------------------------------


def test_until_propagates_to_build_stepwise(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    captured: dict[str, object] = {}

    def _spy(**k: object) -> Any:
        captured.update(k)
        return _fake_result()

    monkeypatch.setattr(cli, "Factory", _fake_factory(_spy), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--until", "rebuild"])
    assert result.exit_code == 0, result.stdout
    assert captured.get("until") == "rebuild"


def test_step_sets_interactive_true(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    _force_tty(monkeypatch, True)
    captured: dict[str, object] = {}

    def _spy(**k: object) -> Any:
        captured.update(k)
        return _fake_result()

    monkeypatch.setattr(cli, "Factory", _fake_factory(_spy), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--step"])
    assert result.exit_code == 0, result.stdout
    assert captured.get("interactive") is True


# --- R1.5 invalid --until → exit 1 (ValueError from plan_phase_run) ---------


def test_until_invalid_phase_exits_1(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    def _raise(**_k: object) -> Any:
        raise ValueError("invalid phase 'bogus'; valid: seed, rebuild, graphics, apps")

    monkeypatch.setattr(cli, "Factory", _fake_factory(_raise), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--until", "bogus"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "rebuild" in combined  # lists the valid names


# --- R3.3/R8.1 clean stop → exit 0; FactoryError (abort) → exit 1 -----------


def test_clean_stop_exits_0_and_reports_stopped_at(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    monkeypatch.setattr(
        cli,
        "Factory",
        _fake_factory(lambda **_k: _fake_result(stopped_at="rebuild")),
        raising=False,
    )
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--until", "rebuild"])
    assert result.exit_code == 0, result.stdout
    assert "rebuild" in result.stdout


def test_abort_factory_error_exits_1(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    def _raise(**_k: object) -> Any:
        raise FactoryError("aborted by the user", phase="rebuild", output="!!! build break")

    monkeypatch.setattr(cli, "Factory", _fake_factory(_raise), raising=False)
    result = runner.invoke(app, ["factory", "v3", "minimal", "systemd", "--until", "rebuild"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "rebuild" in combined
