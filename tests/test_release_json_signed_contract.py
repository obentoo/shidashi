"""``release.json``: what the release writes is what the report reads (story 032, task 9.1;
R1.4, R1.6, R1.7, R3.2, R3.3, R3.5).

Contract (design.md "Data Models"): ``release.json`` gains one key, ``signed`` -- ``false``
for an unsigned release, ``true`` for a signed one; ``release report`` (and story 033's
``release sign``) read a missing key as ``true``. Nothing else is renamed, so the report
reads the variants exactly as story 030's ``release.manifest`` writes them -- the status,
the ISOs, the generation, and a toolchain refusal's successor PKGDIR.

The producer is the real one: ``shidashi release`` run end to end in story 030's release
lab, then ``shidashi release report`` on the directory it made (``FakeChildBuilds``,
``FakePublisher`` and ``FakeMaintainerGpg`` as in tests/test_release_cli.py).
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli
from tests.test_release_cli import Lab, line_of, release_lab
from tests.test_release_core import FPR

contract_runner = CliRunner()


@pytest.fixture
def contract_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


def report_of(directory: Path) -> Result:
    return contract_runner.invoke(cli.app, ["release", "report", str(directory), "--markdown"])


def test_an_unsigned_release_json_is_reported_unsigned_with_every_iso(contract_lab: Lab) -> None:
    made = contract_lab.release("v3", "systemd", "--unsigned")
    assert made.exit_code == 0, made.output
    body = contract_lab.manifest()
    assert body.get("signed") is False
    report = report_of(contract_lab.the_release())
    assert report.exit_code == 0, report.output
    assert "unsigned" in report.stdout.lower() and "host" in report.stdout.lower()
    row = line_of(report.stdout, "v3", "systemd")
    for iso in contract_lab.variant("v3", "systemd")["isos"]:
        assert iso in row, (iso, row)
    generation = contract_lab.variant("v3", "systemd")["generation"]
    assert generation and generation in row, (generation, row)


def test_a_signed_release_json_says_signed_true_and_is_reported_signed(contract_lab: Lab) -> None:
    made = contract_lab.release("v3", "systemd", "--key", FPR)
    assert made.exit_code == 0, made.output
    assert contract_lab.manifest().get("signed") is True
    report = report_of(contract_lab.the_release())
    assert report.exit_code == 0, report.output
    assert "unsigned" not in report.stdout.lower()


def test_a_release_json_from_before_this_story_is_reported_as_signed(contract_lab: Lab) -> None:
    """R3.5: a manifest without ``signed`` (story 030's) reads as signed -- the same report
    as the identical manifest with ``"signed": true``."""
    made = contract_lab.release("v3", "systemd", "--key", FPR)
    assert made.exit_code == 0, made.output
    directory = contract_lab.the_release()
    body = contract_lab.manifest()
    body.pop("signed", None)
    (directory / "release.json").write_text(json.dumps(body), encoding="utf-8")
    without = report_of(directory)
    body["signed"] = True
    (directory / "release.json").write_text(json.dumps(body), encoding="utf-8")
    with_true = report_of(directory)
    assert without.exit_code == 0 and with_true.exit_code == 0, without.output
    assert without.stdout == with_true.stdout
    assert "unsigned" not in without.stdout.lower()
    for iso in contract_lab.variant("v3", "systemd")["isos"]:
        assert iso in without.stdout, without.stdout


def test_a_refused_variant_written_by_the_release_reports_its_successor(
    contract_lab: Lab,
) -> None:
    """R3.2 against the real producer: the report finds the successor where the release
    wrote it, whatever key holds it."""
    contract_lab.builds.script[("v3", "systemd")] = "refused"
    made = contract_lab.release("v3", "systemd", "--unsigned")
    assert made.exit_code == 1, made.output
    successor = f"{contract_lab.pkgdir('v3', 'systemd')}-gcc16"
    assert successor in json.dumps(contract_lab.variant("v3", "systemd"))
    report = report_of(contract_lab.the_release())
    assert report.exit_code == 0, report.output
    assert successor in "".join(report.stdout.split()), report.stdout
    assert "toolchain-refused" in line_of(report.stdout, "v3", "systemd")
