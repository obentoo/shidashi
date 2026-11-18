"""Translated where a person reads it, English where a program or a paste does.

Each test loads a small catalog of its own (``write_test_catalog``) so it states exactly
which words change, then checks the copy that is kept -- a status file, an audit trail,
an error -- stays English.
"""

import io
import json
import time
from pathlib import Path

import pytest
from rich.console import Console

from shidashi import doctor, i18n, parallel, progress, release
from tests._i18n_catalog import loaded_catalog, write_test_catalog
from tests.test_release_cli import release_lab
from tests.test_release_core import result, same_upstream

PT_BR = "pt_BR.UTF-8"


def test_the_grid_translates_state_words_and_the_status_file_stays_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    localedir = write_test_catalog(
        tmp_path / "locale", "pt_BR", {"done": "concluído", "starting": "iniciando"}
    )
    kde, gnome = tmp_path / "kde.status", tmp_path / "gnome.status"
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    with loaded_catalog(monkeypatch, localedir, PT_BR):
        progress.write_status(kde, "done")  # what a child writes when its assemble ends
        gnome.write_text(json.dumps({"step": "seed", "since": time.time()}) + "\n")
        with parallel.watch({"kde": kde, "gnome": gnome}, console=console, started={}):
            pass
        assert parallel.shown_step(None) == "iniciando"
    assert json.loads(kde.read_text(encoding="utf-8"))["step"] == "done"
    lines = out.getvalue().splitlines()
    assert any(line.startswith("kde  concluído  ") for line in lines), lines
    # an audit step's name is the run's own vocabulary: shown as written
    assert any(line.startswith("gnome  seed  ") for line in lines), lines


def test_an_ended_image_line_is_translated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    localedir = write_test_catalog(tmp_path, "pt_BR", {"failed": "falhou"})
    ended = parallel.ChildResult(
        image="kde",
        exit_code=1,
        run_id=None,
        log=tmp_path / "kde.log",
        duration_s=65.0,
        isos=(),
        error="no result file",
    )
    assert parallel.ended_line(ended).startswith("kde  failed  ")
    with loaded_catalog(monkeypatch, localedir, PT_BR):
        assert parallel.ended_line(ended).startswith("kde  falhou  ")


def _no_git(name: str) -> str | None:
    return None if name == "git" else f"/usr/bin/{name}"


def test_doctor_details_are_translated_and_its_error_stays_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    localedir = write_test_catalog(
        tmp_path / "locale",
        "pt_BR",
        {"missing: {why}": "ausente: {why}", "fetches the pinned overlays": "busca os overlays"},
    )
    monkeypatch.undo()  # conftest stubs require_build_host; this test uses the real one
    real_checks = doctor.checks
    monkeypatch.setattr(
        doctor, "checks", lambda work_dir: real_checks(work_dir, which=_no_git, run=lambda _a: "")
    )
    with loaded_catalog(monkeypatch, localedir, PT_BR):
        (git,) = [c for c in doctor.checks(tmp_path) if c.name == "git"]
        assert git.detail == "ausente: busca os overlays"
        with pytest.raises(doctor.DoctorError) as raised:
            doctor.require_build_host(tmp_path)
    assert "git (missing: fetches the pinned overlays)" in str(raised.value)
    assert "ausente" not in str(raised.value)


def test_the_release_summary_is_translated_unless_built_in_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    localedir = write_test_catalog(
        tmp_path,
        "pt_BR",
        {
            "signing: skipped (unsigned release)": "assinatura: pulada (release não assinada)",
            "{variant}: {status} — {isos}; publish: {publish}": (
                "{variant}: {status} — {isos}; publicação: {publish}"
            ),
        },
    )
    results = [result("v3", "systemd", "ok", "skipped: unsigned")]

    def summary() -> list[str]:
        return release.summary(
            results, same_upstream("systemd"), signed=False, verify_failures=[], unsigned=True
        )[0]

    with loaded_catalog(monkeypatch, localedir, PT_BR):
        shown = summary()
        with i18n.english():
            kept = summary()
    assert shown[-1] == "assinatura: pulada (release não assinada)"
    assert "; publicação: skipped: unsigned" in shown[0]  # the manifest's word stays
    assert kept == summary()
    assert kept[-1] == "signing: skipped (unsigned release)"


def test_a_release_run_prints_its_summary_translated_and_records_it_in_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = release_lab(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    monkeypatch.setenv("SHIDASHI_RUNS", str(runs))
    localedir = write_test_catalog(
        tmp_path / "locale",
        "pt_BR",
        {"signing: skipped (unsigned release)": "assinatura: pulada (release não assinada)"},
    )
    with loaded_catalog(monkeypatch, localedir, PT_BR):
        done = lab.release("v3", "systemd", "--unsigned")
    assert done.exit_code == 0, done.output
    assert "assinatura: pulada (release não assinada)" in done.output
    assert f"release: {lab.the_release()}" in done.output  # read by the weekly workflow
    trails = [p.read_text(encoding="utf-8") for p in runs.rglob("*.json")]
    recorded = [text for text in trails if "signing: skipped (unsigned release)" in text]
    assert recorded, "the summary is not in the audit trail"
    assert not any("assinatura" in text for text in trails)


@pytest.mark.parametrize("locale", ["pt_BR.UTF-8", "ru_RU.UTF-8", "zh_CN.UTF-8"])
def test_world_lines_keep_one_column_in_every_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    import shutil

    from rich.cells import cell_len
    from typer.testing import CliRunner

    from shidashi import config
    from shidashi.cli import app
    from tests._i18n_catalog import compile_repo_catalogs

    tree = tmp_path / "variants"
    shutil.copytree(config.variants_dir(), tree)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(tree))
    (tree / "minimal/world.systemd").write_text("# edited by hand\n")
    localedir = compile_repo_catalogs(tmp_path / "locale")
    with loaded_catalog(monkeypatch, localedir, locale):
        done = CliRunner().invoke(app, ["world", "--check"])
        stale = i18n.t("stale")
    assert done.exit_code == 1
    lines = [line for line in done.stdout.splitlines() if "variants/" in line]
    assert any(line.startswith(stale) for line in lines), lines
    assert len({cell_len(line[: line.index("variants/")]) for line in lines}) == 1, lines
