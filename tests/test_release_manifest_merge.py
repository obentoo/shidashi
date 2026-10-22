"""``release.manifest`` with an earlier ``release.json`` (R9.3), ``read_earlier`` and the
failed-rename path of ``write_manifest`` (R5.3) -- story 030, task 4.2, test-after.

tests/test_release_core.py is the authored contract and stays frozen; these cover what it
does not: a rerun into an unsigned directory keeps the variants it did not release.
"""

import datetime
import json
import os
from pathlib import Path
from typing import Any

import pytest

from shidashi.build_outcome import BuildOutcome, ReleaseError, ToolchainRefusal
from shidashi.release import (
    MANIFEST_FILE,
    ReleasePins,
    Variant,
    VariantResult,
    manifest,
    read_earlier,
    write_manifest,
)
from shidashi.upstream import UpstreamStage3

GEN = "20260823T153057Z"
FIRST_START = datetime.datetime(2026, 10, 25, 0, 0, 3, tzinfo=datetime.UTC)
FIRST_END = datetime.datetime(2026, 10, 25, 9, 41, 10, tzinfo=datetime.UTC)
RERUN_START = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=datetime.UTC)
RERUN_END = datetime.datetime(2026, 10, 25, 14, 30, 0, tzinfo=datetime.UTC)
PINS = ReleasePins(
    stage3_snapshot=GEN,
    stage3={"systemd": ("stage3-systemd.tar.xz", "aa" * 64), "openrc": ("stage3-o.tar.xz", "bb")},
    gentoo={"snapshot": "20260820", "sha512": "cc" * 64},
    overlays={"bentoo": "31770bb"},
)
UPSTREAM = [UpstreamStage3(init="systemd", pinned=GEN, latest=None, note="--no-download")]
REPO = {"commit": "31770bb", "branch": "main", "dirty": False, "dirty_files": []}
TOOLCHAIN = {"gcc": "15.2.1", "glibc": "2.42", "binutils": "2.45"}


def merge_result(arch: str, init: str, status: str, publish_line: str) -> VariantResult:
    refusal = (
        ToolchainRefusal(differences=["gcc 15 -> 16"], pkgdir="/p", successor="/p-gcc16")
        if status == "toolchain-refused"
        else None
    )
    outcome = BuildOutcome(
        arch=arch,
        init=init,
        status=status,  # type: ignore[arg-type]
        exit_code=0 if status == "ok" else 1,
        isos=[f"bentoo-2026.10.25-minimal-{init}-{arch}.iso"] if status == "ok" else [],
        failed_images=[],
        message="",
        toolchain=refusal,
    )
    return VariantResult(
        variant=Variant(arch, init),
        outcome=outcome,
        publish=publish_line,
        generation=GEN if status == "ok" else None,
        toolchain=TOOLCHAIN if status == "ok" else None,
    )


def entry_of(body: dict[str, Any], arch: str, init: str) -> dict[str, Any]:
    found: list[dict[str, Any]] = [
        v for v in body["variants"] if (v["arch"], v["init"]) == (arch, init)
    ]
    assert len(found) == 1, body["variants"]
    return found[0]


def first_run() -> dict[str, Any]:
    return manifest(
        [
            merge_result("v3", "systemd", "ok", "published /pool/v3 (10 finals)"),
            merge_result("znver5", "openrc", "toolchain-refused", "not run: build failed"),
        ],
        pins=PINS,
        upstream=UPSTREAM,
        repo=REPO,
        started=FIRST_START,
        finished=FIRST_END,
    )


def test_manifest_without_earlier_has_the_data_model_keys_and_a_separate_refusal() -> None:
    body = first_run()
    assert set(body) == {
        "date",
        "started",
        "finished",
        "shidashi",
        "pins",
        "upstream_stage3",
        "variants",
    }
    assert body["date"] == "2026-10-25"
    assert body["started"] == "2026-10-25T00:00:03Z"
    assert body["finished"] == "2026-10-25T09:41:10Z"
    assert body["shidashi"] == {"commit": "31770bb", "dirty": False}
    assert body["pins"]["stage3"]["snapshot"] == GEN
    assert body["pins"]["stage3"]["systemd"] == {
        "filename": "stage3-systemd.tar.xz",
        "sha512": "aa" * 64,
    }
    assert body["upstream_stage3"] == {"systemd": {"latest": None, "note": "--no-download"}}
    ok = entry_of(body, "v3", "systemd")
    assert ok["toolchain"] == TOOLCHAIN and ok["generation"] == GEN
    assert "toolchain_refusal" not in ok
    refused = entry_of(body, "znver5", "openrc")
    assert refused["toolchain"] is None
    assert refused["toolchain_refusal"] == {
        "differences": ["gcc 15 -> 16"],
        "pkgdir": "/p",
        "successor": "/p-gcc16",
    }


def test_manifest_rerun_keeps_the_unreleased_variant_and_replaces_the_released_one() -> None:
    """R9.3: one variant built again; the other keeps its earlier entry, byte for byte."""
    earlier = first_run()
    kept_before = entry_of(earlier, "v3", "systemd")
    body: dict[str, Any] = manifest(
        [merge_result("znver5", "openrc", "ok", "published /pool/znver5 (12 finals)")],
        pins=PINS,
        upstream=UPSTREAM,
        repo=REPO,
        started=RERUN_START,
        finished=RERUN_END,
        earlier=json.loads(json.dumps(earlier)),
    )
    assert len(body["variants"]) == 2
    assert entry_of(body, "v3", "systemd") == kept_before
    again = entry_of(body, "znver5", "openrc")
    assert again["status"] == "ok"
    assert again["publish"] == "published /pool/znver5 (12 finals)"
    assert "toolchain_refusal" not in again
    assert body["started"] == "2026-10-25T00:00:03Z", "the release began with the first run"
    assert body["finished"] == "2026-10-25T14:30:00Z"


def test_read_earlier_round_trips_what_write_manifest_wrote(tmp_path: Path) -> None:
    assert read_earlier(tmp_path) is None
    body = first_run()
    write_manifest(tmp_path, body)
    assert read_earlier(tmp_path) == json.loads(json.dumps(body))


@pytest.mark.parametrize("text", ['{"date": "2026-10-25"', "[1, 2]", "\xff\xfe"])
def test_read_earlier_refuses_a_broken_release_json_and_leaves_it(
    tmp_path: Path, text: str
) -> None:
    path = tmp_path / MANIFEST_FILE
    raw = text.encode("latin-1")
    path.write_bytes(raw)
    with pytest.raises(ReleaseError, match=MANIFEST_FILE):
        read_earlier(tmp_path)
    assert path.read_bytes() == raw


def test_write_manifest_failed_rename_leaves_no_temp_file_and_the_old_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.3: the reader sees the whole old file; no ``.release.json.*.tmp`` is left."""
    old = b'{"date": "old"}\n'
    (tmp_path / MANIFEST_FILE).write_bytes(old)

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(ReleaseError, match=MANIFEST_FILE):
        write_manifest(tmp_path, first_run())
    assert sorted(p.name for p in tmp_path.iterdir()) == [MANIFEST_FILE]
    assert (tmp_path / MANIFEST_FILE).read_bytes() == old
