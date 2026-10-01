"""Light UNIT/INTEGRATION tests of the ``shidashi pretend`` CLI via Typer's ``CliRunner``.

Deterministic on the CI host: the (privileged) ``pretend_resolve`` orchestrator is
monkeypatched in the ``shidashi.cli`` namespace to return a synthetic
``PretendReport`` or raise ``SeedError``/``ResolveError`` — we exercise only the
CLI LAYER: pretty/json rendering, exit code mapping, flag propagation and the
presence of ``pretend`` in ``--help``. No stage3/nspawn is touched (mirrors the
pattern of ``tests/test_cli.py`` + the monkeypatched errors of the design's
§Testing Strategy).

Contract (design.md §cli, refined): ``pretend(arch, flavor, init,
--format=[pretty|json], --no-download, --keep)``; catches SeedError/ResolveError/
UnknownAxisError/RecipeConflictError → friendly message + Exit(1); success exit
0; ``pretend`` listed in ``shidashi --help``. When ``ResolveError`` carries
``raw_output`` (hard-conflict), the CLI prints that raw_output to stderr and exits 1.
``--keep`` propagates ``keep=True`` to the orchestrator.
"""

import json

import pytest
from typer.testing import CliRunner

from shidashi import cli
from shidashi.cli import app
from shidashi.resolve import CycleBreak, PretendReport, ResolveError
from shidashi.seed import SeedError

runner = CliRunner()


def _fake_report() -> PretendReport:
    return PretendReport(
        arch="v3",
        flavor="minimal",
        init="systemd",
        packages=("media-libs/libsdl2-2.30.5", "sys-apps/portage-3.0.66"),
        cycle_breaks=(
            CycleBreak(
                atom="media-libs/libsdl2-2.30.5",
                flag="pipewire",
                enable=False,
                raw_line="- media-libs/libsdl2-2.30.5 (Change USE: -pipewire)",
            ),
        ),
        raw_output="...emerge output...",
    )


# --- --help lists pretend (R1.4) ---------------------------------------------


def test_help_lists_pretend() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "pretend" in result.stdout


def test_pretend_help_shows_work_dir() -> None:
    result = runner.invoke(app, ["pretend", "--help"])
    assert result.exit_code == 0
    assert "--work-dir" in result.stdout


# --- success exit 0 + pretty (R1.1) ------------------------------------------


def test_pretend_success_pretty_exit0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "pretend_resolve", lambda *a, **k: _fake_report())
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    # the resolved package list shows up in the output
    assert "libsdl2" in result.stdout


# --- deterministic --format json (R1.2, R1.3) --------------------------------


def test_pretend_json_emits_packages_and_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "pretend_resolve", lambda *a, **k: _fake_report())
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert "media-libs/libsdl2-2.30.5" in data["packages"]
    # the cycle-break suggestions are in the JSON object
    assert data["cycle_breaks"][0]["flag"] == "pipewire"
    assert data["cycle_breaks"][0]["enable"] is False


# --- error mapping → friendly exit 1 (R5.4, R6.1) ----------------------------


def test_pretend_resolve_error_exit1_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> PretendReport:
        raise ResolveError("systemd-nspawn requires root; run as root")

    monkeypatch.setattr(cli, "pretend_resolve", _raise)
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "root" in combined.lower()


def test_pretend_seed_error_exit1_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> PretendReport:
        raise SeedError("sha256 mismatch for stage3 tarball")

    monkeypatch.setattr(cli, "pretend_resolve", _raise)
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "sha256" in combined.lower()


# --- hard-conflict: ResolveError.raw_output goes to stderr + exit 1 (R5.4) ---
#
# Refined contract §Error Handling: on a genuinely unsatisfiable resolution
# (a hard conflict, not a cycle) the orchestrator raises ResolveError carrying the
# emerge raw_output; the CLI prints that raw_output to stderr and exits 1.

_CONFLICT_RAW = (
    "!!! Multiple package instances within a single package slot have been\n"
    "!!! pulled into the dependency graph, resulting in a slot conflict:\n"
    "  ...emerge conflict...\n"
)


def test_pretend_hard_conflict_prints_raw_output_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_a: object, **_k: object) -> PretendReport:
        raise ResolveError("unsatisfiable resolution", raw_output=_CONFLICT_RAW)

    monkeypatch.setattr(cli, "pretend_resolve", _raise)
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    stderr = result.stderr or ""
    assert "Traceback" not in (result.stdout + stderr)
    # the emerge raw_output is surfaced on stderr (curation of the conflict)
    assert "...emerge conflict..." in stderr


# --- --no-download and --keep are accepted and propagated (R2.6) ------------


def test_pretend_accepts_no_download_and_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _spy(*a: object, **k: object) -> PretendReport:
        captured.update(k)
        return _fake_report()

    monkeypatch.setattr(cli, "pretend_resolve", _spy)
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd", "--no-download", "--keep"])
    assert result.exit_code == 0, result.stdout
    # --no-download propagates download=False to the orchestrator
    assert captured.get("download") is False
    # --keep propagates keep=True to the orchestrator (scratch rootfs kept)
    assert captured.get("keep") is True
