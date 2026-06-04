"""UNIT/INTEGRAÇÃO leve da CLI ``shidashi pretend`` via Typer ``CliRunner``.

Determinista no host CI: o orquestrador ``pretend_resolve`` (privilegiado) é
monkeypatched no namespace de ``shidashi.cli`` para devolver um ``PretendReport``
sintético ou levantar ``SeedError``/``ResolveError`` — exercitamos só a CAMADA
CLI: renderização pretty/json, mapeamento de exit codes, propagação de flags e
a presença de ``pretend`` no ``--help``. Nenhum stage3/nspawn é tocado (espelha
o padrão de ``tests/test_cli.py`` + os erros monkeypatched do design §Testing
Strategy).

Contrato (design.md §cli, refinado): ``pretend(arch, flavor, init,
--format=[pretty|json], --no-download, --keep)``; captura SeedError/ResolveError/
UnknownAxisError/RecipeConflictError → mensagem amigável + Exit(1); sucesso exit
0; ``pretend`` listado em ``shidashi --help``. Quando ``ResolveError`` carrega
``raw_output`` (hard-conflict), a CLI imprime esse raw_output no stderr e sai 1.
``--keep`` propaga ``keep=True`` ao orquestrador.
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


# --- --help lista pretend (R1.4) ---------------------------------------------


def test_help_lists_pretend() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "pretend" in result.stdout


def test_pretend_help_shows_work_dir() -> None:
    result = runner.invoke(app, ["pretend", "--help"])
    assert result.exit_code == 0
    assert "--work-dir" in result.stdout


# --- sucesso exit 0 + pretty (R1.1) ------------------------------------------


def test_pretend_success_pretty_exit0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "pretend_resolve", lambda *a, **k: _fake_report())
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    # a lista de pacotes resolvida aparece na saída
    assert "libsdl2" in result.stdout


# --- --format json determinístico (R1.2, R1.3) -------------------------------


def test_pretend_json_emits_packages_and_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "pretend_resolve", lambda *a, **k: _fake_report())
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert "media-libs/libsdl2-2.30.5" in data["packages"]
    # as sugestões de quebra de ciclo estão no objeto JSON
    assert data["cycle_breaks"][0]["flag"] == "pipewire"
    assert data["cycle_breaks"][0]["enable"] is False


# --- mapeamento de erros → exit 1 amigável (R5.4, R6.1) ----------------------


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


# --- hard-conflict: ResolveError.raw_output vai pro stderr + exit 1 (R5.4) ---
#
# Contrato refinado §Error Handling: numa resolução genuinamente insatisfatível
# (hard conflict, não um ciclo) o orquestrador levanta ResolveError carregando o
# raw_output do emerge; a CLI imprime esse raw_output no stderr e sai 1.

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
    # o raw_output do emerge é surfaceado no stderr (curadoria do conflito)
    assert "...emerge conflict..." in stderr


# --- --no-download e --keep são aceitos e propagados (R2.6) ------------------


def test_pretend_accepts_no_download_and_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _spy(*a: object, **k: object) -> PretendReport:
        captured.update(k)
        return _fake_report()

    monkeypatch.setattr(cli, "pretend_resolve", _spy)
    result = runner.invoke(app, ["pretend", "v3", "minimal", "systemd", "--no-download", "--keep"])
    assert result.exit_code == 0, result.stdout
    # --no-download propaga download=False ao orquestrador
    assert captured.get("download") is False
    # --keep propaga keep=True ao orquestrador (rootfs scratch preservado)
    assert captured.get("keep") is True
