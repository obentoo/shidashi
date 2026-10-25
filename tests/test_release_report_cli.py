"""``shidashi release report <dir> [--markdown]`` (story 032, task 4.2; R3.1, R3.4, Q15).

Contract (story.md R3, design.md "shidashi release report"): the command reads
``<dir>/release.json`` and prints the report; it exits 0 when it rendered and 1 when
``release.json`` is missing or malformed, naming the file; it reads only and never writes.
``shidashi release <arch> <init>`` keeps working beside it.

Driven through the real CLI (typer ``CliRunner``); the release directories are real ones
under ``tmp_path``.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli
from tests.test_release_cli import Lab, release_lab
from tests.test_release_core import FPR

report_runner = CliRunner()

ISO_KDE = "bentoo-2026.11.01-kde-systemd-v3.iso"
ISO_GNOME = "bentoo-2026.11.01-gnome-systemd-v3.iso"
REPORT_BODY = {
    "date": "2026-11-01",
    "started": "2026-11-01T01:00:03Z",
    "finished": "2026-11-01T07:12:40Z",
    "signed": False,
    "variants": [
        {
            "arch": "v3",
            "init": "systemd",
            "status": "ok",
            "isos": [ISO_KDE, ISO_GNOME],
            "generation": "20260823T153057Z",
            "toolchain": {"gcc": "15.2.1", "glibc": "2.42", "binutils": "2.45"},
            "publish": "skipped: unsigned",
        }
    ],
}


def run_report(*args: str) -> Result:
    return report_runner.invoke(cli.app, ["release", "report", *args])


def release_directory(tmp_path: Path, content: str | None) -> Path:
    directory = tmp_path / "releases" / "2026.11.01"
    directory.mkdir(parents=True)
    (directory / "SHA256SUMS").write_text("0" * 64 + f"  {ISO_KDE}\n", encoding="utf-8")
    if content is not None:
        (directory / "release.json").write_text(content, encoding="utf-8")
    return directory


def tree_of(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under ``root`` with its mode and, for a file, its bytes."""
    return {
        str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def names_path(output: str, path: Path) -> bool:
    """Whether ``output`` names ``path``, a long path wrapped over lines included."""
    return "".join(str(path).split()) in "".join(output.split())


@pytest.mark.parametrize("markdown", [["--markdown"], []], ids=["markdown", "plain"])
def test_report_prints_every_variant_and_writes_nothing(
    tmp_path: Path, markdown: list[str]
) -> None:
    directory = release_directory(tmp_path, json.dumps(REPORT_BODY))
    before = tree_of(tmp_path)
    result = run_report(str(directory), *markdown)
    assert result.exit_code == 0, result.output
    assert ISO_KDE in result.stdout and ISO_GNOME in result.stdout
    assert "unsigned" in result.stdout.lower()
    assert tree_of(tmp_path) == before


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param('{"date": "2026-11-01", "variants": [', id="truncated"),
        pytest.param("", id="empty"),
        pytest.param("[1, 2]", id="not-an-object"),
        pytest.param('{"date": "2026-11-01"}', id="no-variants"),
        pytest.param("release: yes\n", id="not-json"),
    ],
)
def test_report_of_a_missing_or_malformed_release_json_exits_1_naming_it_and_writes_nothing(
    tmp_path: Path, content: str | None
) -> None:
    directory = release_directory(tmp_path, content)
    before = tree_of(tmp_path)
    result = run_report(str(directory), "--markdown")
    assert result.exit_code == 1, result.output
    assert names_path(result.output, directory / "release.json"), result.output
    assert tree_of(tmp_path) == before
    assert ISO_KDE not in result.stdout


def test_report_of_a_directory_that_does_not_exist_exits_1_and_creates_nothing(
    tmp_path: Path,
) -> None:
    gone = tmp_path / "releases" / "2026.11.08"
    result = run_report(str(gone), "--markdown")
    assert result.exit_code == 1, result.output
    assert "2026.11.08" in result.output
    assert not gone.exists()


def test_release_report_help_exits_0() -> None:
    result = run_report("--help")
    assert result.exit_code == 0, result.output
    assert "--markdown" in result.output


@pytest.fixture
def report_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


def test_release_arch_init_still_releases_beside_the_report_command(report_lab: Lab) -> None:
    """Hostile half: ``report`` must not swallow the positional form -- ``release v3
    systemd`` is still a release of v3/systemd."""
    result = report_lab.release("v3", "systemd", "--key", FPR)
    assert [(c.arch, c.init) for c in report_lab.builds.calls] == [("v3", "systemd")], result.output
    help_result = report_runner.invoke(cli.app, ["release", "--help"])
    assert help_result.exit_code == 0, help_result.output
