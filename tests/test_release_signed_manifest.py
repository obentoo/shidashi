"""``release.json``'s ``signed`` key and the unsigned summary (story 032, task 2.3; R1.4-R1.7).

Contract (design.md "Components & Interfaces" and "Data Models"):

- ``release.manifest(..., signed: bool)`` writes ``signed`` as a top-level JSON boolean --
  ``false`` for an unsigned release, ``true`` for a signed one (R1.4, R1.7); a rerun's
  earlier manifest never decides this run's value.
- ``release.summary(..., signed, unsigned)``: an unsigned release whose every variant built
  prints ``signing: skipped (unsigned release)`` and exits 0 (R1.5); a variant that did not
  build exits 1, the refused line still naming its successor PKGDIR (R1.6). The signed
  path's exit logic is unchanged.

Pure functions, driven directly with the builders of tests/test_release_core.py.
"""

import datetime
import json
from collections.abc import Sequence
from typing import Any

import pytest

from shidashi import release
from tests.test_release_core import (
    GEN,
    REFUSED,
    SUCCESSOR,
    UpstreamStage3,
    line_of,
    published,
    result,
    same_upstream,
)

SUMMARY_UNSIGNED_LINE = "signing: skipped (unsigned release)"
MANIFEST_STARTED = datetime.datetime(2026, 11, 1, 1, 0, 3, tzinfo=datetime.UTC)
MANIFEST_FINISHED = datetime.datetime(2026, 11, 1, 7, 12, 40, tzinfo=datetime.UTC)


def _manifest_pins() -> release.ReleasePins:
    return release.ReleasePins(
        stage3_snapshot=GEN,
        stage3={"systemd": (f"stage3-amd64-systemd-{GEN}.tar.xz", "ab" * 64)},
        gentoo={"snapshot": "2026.10.31", "sha512": "cd" * 64},
        overlays={"bentoo": "0" * 40},
    )


def _signed_manifest(
    results: Sequence[Any], *, signed: bool, earlier: dict[str, object] | None = None
) -> dict[str, object]:
    return release.manifest(
        results,
        pins=_manifest_pins(),
        upstream=[UpstreamStage3(init="systemd", pinned=GEN, latest=GEN, note="")],
        repo={"commit": "f" * 40, "dirty": False},
        started=MANIFEST_STARTED,
        finished=MANIFEST_FINISHED,
        earlier=earlier,
        signed=signed,
    )


def _unsigned_summary(results: Sequence[Any]) -> tuple[list[str], int]:
    lines, code = release.summary(
        results, same_upstream("systemd"), signed=False, verify_failures=[], unsigned=True
    )
    return list(lines), code


# --- manifest ---------------------------------------------------------------------------------


def test_manifest_of_an_unsigned_release_writes_signed_false_as_a_json_boolean() -> None:
    body = _signed_manifest([result("v3", "systemd", "ok", "skipped: unsigned")], signed=False)
    assert body.get("signed") is False
    assert json.loads(json.dumps(body)).get("signed") is False


def test_manifest_of_a_signed_release_writes_signed_true() -> None:
    body = _signed_manifest([result("v3", "systemd", "ok", published("v3"))], signed=True)
    assert body.get("signed") is True


@pytest.mark.parametrize(
    ("earlier_signed", "now_signed"),
    [
        # Hostile halves: a rerun's earlier manifest must not leak its signed state into
        # this run's, in either direction.
        pytest.param(True, False, id="signed-before-unsigned-now"),
        pytest.param(False, True, id="unsigned-before-signed-now"),
    ],
)
def test_manifest_signed_is_this_runs_never_the_earlier_manifests(
    earlier_signed: bool, now_signed: bool
) -> None:
    earlier: dict[str, object] = {
        "date": "2026-11-01",
        "started": "2026-11-01T00:00:00Z",
        "signed": earlier_signed,
        "variants": [],
    }
    publish_line = published("v3") if now_signed else "skipped: unsigned"
    body = _signed_manifest(
        [result("v3", "systemd", "ok", publish_line)], signed=now_signed, earlier=earlier
    )
    assert body.get("signed") is now_signed


# --- summary ----------------------------------------------------------------------------------


def test_summary_of_an_unsigned_release_that_built_says_signing_skipped_and_is_0() -> None:
    results = [result("v3", "systemd", "ok", "skipped: unsigned")]
    lines, code = _unsigned_summary(results)
    assert code == 0, lines
    assert SUMMARY_UNSIGNED_LINE in lines
    assert not any(line.startswith("signing: failed") for line in lines), lines
    assert not any(line.startswith("signing: verified") for line in lines), lines


def test_summary_of_an_unsigned_refusal_is_1_and_names_the_successor() -> None:
    results = [
        result("v3", "openrc", "ok", "skipped: unsigned"),
        result("v3", "systemd", "toolchain-refused", "not run: build failed", **REFUSED),
    ]
    lines, code = _unsigned_summary(results)
    assert code == 1, lines
    assert SUCCESSOR in line_of(lines, "v3", "systemd")
    assert SUCCESSOR not in line_of(lines, "v3", "openrc")


def test_summary_of_a_signed_release_whose_signing_failed_is_still_1() -> None:
    """Hostile half: a signed release left unattested must not pass for an unsigned one."""
    results = [result("v3", "systemd", "ok", published("v3"))]
    lines, code = release.summary(
        results, same_upstream("systemd"), signed=False, verify_failures=[], unsigned=False
    )
    assert code == 1, lines
    assert SUMMARY_UNSIGNED_LINE not in lines
    assert any(line.startswith("signing: failed") for line in lines), lines


def test_summary_of_a_signed_and_verified_release_is_unchanged_and_0() -> None:
    results = [result("v3", "systemd", "ok", published("v3"))]
    lines, code = release.summary(
        results, same_upstream("systemd"), signed=True, verify_failures=[], unsigned=False
    )
    assert code == 0, lines
    assert SUMMARY_UNSIGNED_LINE not in lines
    assert any(line.startswith("signing: verified") for line in lines), lines
