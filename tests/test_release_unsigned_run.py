"""``shidashi release --unsigned`` end to end (story 032, task 2.2; R1.1, R1.3-R1.6).

Contract (story.md R1, design.md "shidashi release --unsigned", "Data Models"): an unsigned
release builds and records every variant without reading, checking or asking for a key;
it skips the per-arch binhost publish and records each variant's publish as the literal
``skipped: unsigned``; ``release.json`` carries ``"signed": false``; the release directory
holds no ``SHA256SUMS.asc``, no ``.asc`` and no ``.signing/``; every variant built -> it
prints ``signing: skipped (unsigned release)`` and exits 0; a toolchain refusal is recorded
with its successor PKGDIR, the other variants still finish, and the release exits 1.

Driven through the real CLI with story 030's release lab: ``FakeChildBuilds`` for the child
builds, ``FakePublisher`` for ``publish_pool.publish`` and ``FakeMaintainerGpg`` for the
maintainer's gpg -- the unsigned path must leave all three of the last untouched.
"""

import json
from pathlib import Path

import pytest

from tests.test_release_cli import Lab, all_pairs, line_of, release_lab
from tests.test_release_core import listed

RUN_UNSIGNED_LINE = "signing: skipped (unsigned release)"


@pytest.fixture
def unsigned_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


def _no_gpg_was_touched(lab: Lab) -> None:
    assert lab.maintainer.checked == [], "a key was checked"
    assert lab.maintainer.signed == [], "something was signed"
    assert lab.maintainer.exported == [], "a public key was exported"


def _no_signature_material(directory: Path) -> None:
    assert not (directory / "SHA256SUMS.asc").exists()
    assert sorted(p.name for p in directory.rglob("*.asc")) == []
    assert not (directory / ".signing").exists()


def test_an_unsigned_release_touches_no_gpg_publishes_nothing_and_exits_0(
    unsigned_lab: Lab,
) -> None:
    result = unsigned_lab.release("v3", "systemd", "--unsigned")
    assert result.exit_code == 0, result.output
    _no_gpg_was_touched(unsigned_lab)
    assert unsigned_lab.publisher.calls == [], "the binhost publish signs: it must not run"
    entry = unsigned_lab.variant("v3", "systemd")
    assert entry["status"] == "ok"
    assert entry["publish"] == "skipped: unsigned"
    assert len(entry["isos"]) == 2
    assert unsigned_lab.manifest().get("signed") is False
    _no_signature_material(unsigned_lab.the_release())
    assert RUN_UNSIGNED_LINE in result.output
    # Hostile half: an unsigned success is not a signing that failed, nor one that verified
    assert "signing: failed" not in result.output
    assert "signing: verified" not in result.output


def test_an_unsigned_release_still_records_release_json_in_sha256sums(unsigned_lab: Lab) -> None:
    result = unsigned_lab.release("v3", "systemd", "--unsigned")
    assert result.exit_code == 0, result.output
    directory = unsigned_lab.the_release()
    sums = listed(directory)
    assert "release.json" in sums
    for iso in unsigned_lab.variant("v3", "systemd")["isos"]:
        assert iso in sums and f"{iso}.DIGESTS" in sums


def test_an_unsigned_release_never_passes_a_key_to_a_child_build(unsigned_lab: Lab) -> None:
    unsigned_lab.release("v3", "systemd", "--unsigned")
    assert unsigned_lab.builds.calls, "no child build ran"
    for call in unsigned_lab.builds.calls:
        assert "--key" not in call.argv and "--unsigned" not in call.argv, call.argv


def test_an_unsigned_refusal_records_the_successor_finishes_the_others_and_exits_1(
    unsigned_lab: Lab,
) -> None:
    pairs = sorted(all_pairs())
    assert len(pairs) >= 2, "the recipe axes have a single variant"
    refused = pairs[0]
    unsigned_lab.builds.script[refused] = "refused"
    successor = f"{unsigned_lab.pkgdir(refused[0], refused[1])}-gcc16"

    result = unsigned_lab.release("--all", "--unsigned")

    assert result.exit_code == 1, result.output
    assert sorted((c.arch, c.init) for c in unsigned_lab.builds.calls) == pairs
    entry = unsigned_lab.variant(*refused)
    assert entry["status"] == "toolchain-refused"
    assert successor in json.dumps(entry), entry
    for other in pairs[1:]:
        built = unsigned_lab.variant(*other)
        assert built["status"] == "ok", built
        assert built["publish"] == "skipped: unsigned", built
    assert unsigned_lab.manifest().get("signed") is False
    assert successor in line_of(result.output, *refused)
    _no_gpg_was_touched(unsigned_lab)
    assert unsigned_lab.publisher.calls == []
    _no_signature_material(unsigned_lab.the_release())
