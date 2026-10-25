"""``release.manifest(..., earlier=...)`` keeps the per-init entries of the inits a rerun did
not release (story 030, task 8.2; R9.3 as modified by refine 1).

Contract from design.md "``shidashi/release.py``": ``pins.stage3[init]`` and
``upstream_stage3[init]`` of every init this run did not release are kept from the earlier
``release.json``; the inits this run released get this run's entries; the shared
``pins.stage3.snapshot``, ``gentoo`` and ``overlays`` describe this run. A rerun's
``ReleasePins`` and upstream list hold only the inits it released (``release_cli`` builds
them per released init), so without the merge the unreleased init's entries vanish.

The earlier ``release.json`` is ``manifest``'s own output of the first run, round-tripped
through JSON as ``read_earlier`` would return it.
"""

import datetime
import json
from typing import Any

from shidashi.build_outcome import BuildOutcome
from shidashi.release import ReleasePins, Variant, VariantResult, manifest
from shidashi.upstream import UpstreamStage3

FIRST_GEN = "20260823T153057Z"
RERUN_GEN = "20261020T000000Z"
FIRST_START = datetime.datetime(2026, 10, 25, 0, 0, 3, tzinfo=datetime.UTC)
FIRST_END = datetime.datetime(2026, 10, 25, 9, 41, 10, tzinfo=datetime.UTC)
RERUN_START = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=datetime.UTC)
RERUN_END = datetime.datetime(2026, 10, 25, 14, 30, 0, tzinfo=datetime.UTC)
REPO = {"commit": "31770bb", "branch": "main", "dirty": False, "dirty_files": []}
TOOLCHAIN = {"gcc": "15.2.1", "glibc": "2.42", "binutils": "2.45"}

FIRST_PINS = ReleasePins(
    stage3_snapshot=FIRST_GEN,
    stage3={
        "systemd": ("stage3-amd64-systemd-20260823T153057Z.tar.xz", "aa" * 64),
        "openrc": ("stage3-amd64-openrc-20260823T153057Z.tar.xz", "bb" * 64),
    },
    gentoo={"snapshot": "20260820", "sha512": "cc" * 64},
    overlays={"bentoo": "31770bb"},
)
FIRST_UPSTREAM = [
    UpstreamStage3(init="systemd", pinned=FIRST_GEN, latest="20260901T000000Z", note=""),
    UpstreamStage3(init="openrc", pinned=FIRST_GEN, latest=None, note="--no-download"),
]

# The rerun releases openrc only, from newer pins: only openrc's stage3 is in its pins.
RERUN_PINS = ReleasePins(
    stage3_snapshot=RERUN_GEN,
    stage3={"openrc": ("stage3-amd64-openrc-20261020T000000Z.tar.xz", "dd" * 64)},
    gentoo={"snapshot": "20261019", "sha512": "ee" * 64},
    overlays={"bentoo": "0a1b2c3"},
)
RERUN_UPSTREAM = [
    UpstreamStage3(init="openrc", pinned=RERUN_GEN, latest="20261021T000000Z", note=""),
]


def ok_result(arch: str, init: str, generation: str) -> VariantResult:
    outcome = BuildOutcome(
        arch=arch,
        init=init,
        status="ok",
        exit_code=0,
        isos=[f"bentoo-2026.10.25-minimal-{init}-{arch}.iso"],
        failed_images=[],
        message="",
        toolchain=None,
    )
    return VariantResult(
        variant=Variant(arch, init),
        outcome=outcome,
        publish=f"published /pool/{arch} (10 finals)",
        generation=generation,
        toolchain=TOOLCHAIN,
    )


def first_release_json() -> dict[str, Any]:
    body = manifest(
        [ok_result("v3", "systemd", FIRST_GEN), ok_result("v3", "openrc", FIRST_GEN)],
        pins=FIRST_PINS,
        upstream=FIRST_UPSTREAM,
        repo=REPO,
        started=FIRST_START,
        finished=FIRST_END,
    )
    earlier: dict[str, Any] = json.loads(json.dumps(body))
    return earlier


def rerun_openrc_only(earlier: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = manifest(
        [ok_result("v3", "openrc", RERUN_GEN)],
        pins=RERUN_PINS,
        upstream=RERUN_UPSTREAM,
        repo=REPO,
        started=RERUN_START,
        finished=RERUN_END,
        earlier=earlier,
    )
    return body


# --- hostile halves first: what the merge must NOT keep from the earlier run -------------


def test_the_rerun_init_gets_this_runs_pin_and_upstream_never_the_earlier_ones() -> None:
    """Wrong collapse: openrc is in both runs; the earlier openrc entries must not survive."""
    earlier = first_release_json()
    body = rerun_openrc_only(earlier)
    assert body["pins"]["stage3"]["openrc"] == {
        "filename": "stage3-amd64-openrc-20261020T000000Z.tar.xz",
        "sha512": "dd" * 64,
    }
    assert body["upstream_stage3"]["openrc"] == {"latest": "20261021T000000Z", "note": ""}


def test_the_shared_snapshot_gentoo_and_overlays_describe_this_run_not_the_earlier() -> None:
    """``pins.stage3.snapshot`` sits beside the init keys but is not an init: a merge that
    keeps every earlier key this run did not set would keep the earlier snapshot."""
    earlier = first_release_json()
    body = rerun_openrc_only(earlier)
    assert body["pins"]["stage3"]["snapshot"] == RERUN_GEN
    assert body["pins"]["gentoo"] == {"snapshot": "20261019", "sha512": "ee" * 64}
    assert body["pins"]["overlays"] == {"bentoo": "0a1b2c3"}


# --- what the merge must keep: the init this run did not release (R9.3) -------------------


def test_the_init_the_rerun_did_not_release_keeps_its_pin_and_upstream_entry() -> None:
    """Wrong split: systemd is absent from the rerun's pins and upstream, yet its variant
    stays in release.json -- its pin and upstream entry stay with it."""
    earlier = first_release_json()
    body = rerun_openrc_only(earlier)
    stage3, upstream = body["pins"]["stage3"], body["upstream_stage3"]
    assert stage3.get("systemd") == earlier["pins"]["stage3"]["systemd"], stage3
    assert stage3.get("systemd") == {
        "filename": "stage3-amd64-systemd-20260823T153057Z.tar.xz",
        "sha512": "aa" * 64,
    }
    assert upstream.get("systemd") == {"latest": "20260901T000000Z", "note": ""}, upstream
    kept = [v for v in body["variants"] if v["init"] == "systemd"]
    assert len(kept) == 1, body["variants"]


def test_after_the_rerun_every_init_with_a_variant_has_exactly_one_pin_and_upstream() -> None:
    earlier = first_release_json()
    body = rerun_openrc_only(earlier)
    inits = {v["init"] for v in body["variants"]}
    assert inits == {"systemd", "openrc"}
    assert set(body["pins"]["stage3"]) == {"snapshot", "systemd", "openrc"}
    assert set(body["upstream_stage3"]) == {"systemd", "openrc"}


# --- no earlier release.json: the shape is unchanged ---------------------------------------


def test_without_earlier_only_this_runs_inits_appear() -> None:
    body = rerun_openrc_only(None)
    assert body["pins"]["stage3"] == {
        "snapshot": RERUN_GEN,
        "openrc": {
            "filename": "stage3-amd64-openrc-20261020T000000Z.tar.xz",
            "sha512": "dd" * 64,
        },
    }
    assert body["upstream_stage3"] == {"openrc": {"latest": "20261021T000000Z", "note": ""}}
    assert set(body) == {
        "date",
        "started",
        "finished",
        "signed",
        "shidashi",
        "pins",
        "upstream_stage3",
        "variants",
    }
