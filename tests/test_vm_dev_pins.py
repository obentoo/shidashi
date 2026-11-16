"""``vm start --dev --pins-json``: the ISO's pins handed in, for a host without git.

A build worker runs a shipped source tree with no ``.git``: for an ISO that does not
name its own pins, ``start_dev`` cannot read them there. The host resolves them and its
relay hands them over as the ``pins`` block of build.json. Checked here: the handed pins
are the trees that get mounted and git is never asked; an ISO that names its own must
agree; and a block that is not one is a usage error before anything boots. The block
itself, and how two of them are compared, is tests/test_iso_pins.py's.
"""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from shidashi import iso_pins, tree, vm, vm_dev
from shidashi.cli import app
from tests.test_vm_dev import _COMMIT, _PINS, FakeDevSession, FakeTrees, _info, _iso

_OTHER_COMMIT = "9" * 40


def _block(**changes: Any) -> dict[str, Any]:
    """The pins block of tests/test_vm_dev.py's ISO as the host would hand it in, with
    ``changes``. Its id is the true one of its pins: that fixture's is made up, which
    the ISO's own block is never checked for and a block handed in is."""
    block: dict[str, Any] = {**json.loads(json.dumps(_PINS)), **changes}
    if "id" not in changes:
        del block["id"]
        block = iso_pins.pins_block(iso_pins.handed_pins(block))
    return block


_HANDED = _block()


@pytest.fixture
def worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTrees:
    """A host with no git to ask: reading a commit's seeds fails the test."""
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_CID_LOCKS", str(tmp_path / "cids"))
    (tmp_path / "cids").mkdir(mode=0o700)
    monkeypatch.setattr(vm, "ensure_disk", lambda path, size: path)
    trees = FakeTrees(tmp_path / "cache" / "repos")
    monkeypatch.setattr(tree, "ensure_tree", trees.gentoo)
    monkeypatch.setattr(tree, "ensure_overlay", trees.overlay)

    def no_git(*_a: object, **_k: object) -> bytes:
        raise AssertionError("git was asked on a host that has none")

    monkeypatch.setattr(iso_pins, "_show", no_git)
    FakeDevSession.instances.clear()
    return trees


def _start(tmp_path: Path, **kw: Any) -> vm_dev.DevVm:
    return vm_dev.start_dev(
        name="dev",
        iso=_iso(tmp_path / "cache" / "releases", "2026.11.12"),
        init="systemd",
        arch="v3",
        directory=tmp_path / "session",
        session_factory=cast(Any, FakeDevSession),
        repo=tmp_path / "shipped-tree-without-git",
        **kw,
    )


def _older_iso(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ISO names only the commit that built it: no ``pins`` block."""
    info = _info(repository={"commit": "f" * 40})
    del info["pins"]
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: info)


def test_an_older_iso_boots_with_the_pins_handed_in_and_git_is_never_asked(
    tmp_path: Path, worker: FakeTrees, monkeypatch: pytest.MonkeyPatch
) -> None:
    _older_iso(monkeypatch)
    handed = iso_pins.handed_pins(_HANDED)
    dev = _start(tmp_path, pins=handed)
    assert dev.pin == handed.id == _HANDED["id"]
    assert worker.asked == ["gentoo-20261101", f"bentoo-{_COMMIT[:12]}"]
    assert [s.tag for s in FakeDevSession.instances[0].spec.shares] == [
        "repo-gentoo",
        "repo-bentoo",
    ]


def test_an_older_iso_without_handed_pins_still_asks_git(
    tmp_path: Path, worker: FakeTrees, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the control: without --pins-json this is the path that needs a checkout
    _older_iso(monkeypatch)
    with pytest.raises(AssertionError, match="git was asked"):
        _start(tmp_path)


def test_an_iso_naming_its_own_pins_accepts_the_same_ones_handed_in(
    tmp_path: Path, worker: FakeTrees, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: _info())
    dev = _start(tmp_path, pins=iso_pins.handed_pins(_HANDED))
    assert dev.pin == _HANDED["id"]
    assert len(FakeDevSession.instances) == 1


def test_handed_pins_that_are_not_the_iso_s_own_stop_the_start_and_say_so(
    tmp_path: Path, worker: FakeTrees, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: the relay hands in the pins of another ISO. Booting with either set
    would be a silent choice: the trees the image never saw, or not what was asked."""
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: _info())
    other = _block(overlays={"bentoo": {"url": "https://x/b.git", "commit": _OTHER_COMMIT}})
    handed = iso_pins.handed_pins(other)
    with pytest.raises(vm.VmError) as refused:
        _start(tmp_path, pins=handed)
    message = str(refused.value)
    assert "bentoo-2026.11.12-minimal-systemd-v3.iso" in message
    assert handed.id != _HANDED["id"]
    assert f"the pins handed in ({handed.id}) are not the ISO's own ({_HANDED['id']})" in message
    assert worker.asked == [] and FakeDevSession.instances == []  # nothing readied, nothing booted


# --- the CLI -------------------------------------------------------------------------------


@pytest.fixture
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``vm start --dev`` with ``start_dev`` stood in for; what it was called with."""
    captured: dict[str, Any] = {}

    def fake_start(**kw: Any) -> vm_dev.DevVm:
        captured.update(kw)
        return vm_dev.DevVm("dev", 43, Path("/i.iso"), "p1", [], Path("/s"))

    monkeypatch.setattr(vm_dev, "start_dev", fake_start)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SHIDASHI_SCRATCH", raising=False)
    return captured


def test_cli_pins_json_reaches_the_start_as_pins(started: dict[str, Any]) -> None:
    args = ["vm", "start", "--dev", "--pins-json", json.dumps(_HANDED)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    pins = started["pins"]
    assert isinstance(pins, iso_pins.IsoPins) and pins.source == "handed in"
    assert iso_pins.pins_block(pins) == _HANDED
    assert len(result.stdout.splitlines()) == 1


def test_cli_without_pins_json_hands_nothing_in(started: dict[str, Any]) -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--dev"])
    assert result.exit_code == 0, result.output
    assert started["pins"] is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "not json",
        "null",
        '"p20261101.28ad071a"',
        "[]",
        "{}",
        json.dumps({"gentoo": _HANDED["gentoo"]}),
        json.dumps({**_HANDED, "overlays": {"bentoo": {"url": "u", "commit": "abc123"}}}),
        json.dumps(_block(id="p20261101.00000000")),
    ],
    ids=[
        "empty",
        "cut-short",
        "not-json",
        "null",
        "only-the-id",
        "a-list",
        "no-keys",
        "no-overlays",
        "short-commit",
        "another-id",
    ],
)
def test_cli_refuses_a_pins_json_that_is_no_pins_block_before_anything_boots(
    started: dict[str, Any], text: str
) -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--dev", f"--pins-json={text}"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error: --pins-json: ")
    assert started == {}


def test_cli_refuses_pins_json_without_dev(tmp_path: Path, started: dict[str, Any]) -> None:
    iso = tmp_path / "b.iso"
    iso.write_bytes(b"iso")
    args = ["vm", "start", str(iso), "--pins-json", json.dumps(_HANDED)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert "--pins-json needs --dev" in result.stderr
    assert started == {}


def test_cli_a_disagreement_is_a_failed_start_not_a_usage_error(
    tmp_path: Path, worker: FakeTrees, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the block is well-formed: only the ISO, read at start, can contradict it -- exit 1
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: _info())
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(vm_dev, "sweep_dead_dev_sessions", lambda _root, *, keep: [])
    other = _block(gentoo={**_HANDED["gentoo"], "date": "20261102"})
    iso = _iso(tmp_path / "cache" / "releases", "2026.11.12")
    args = ["vm", "start", "--dev", "--iso", str(iso), "--pins-json", json.dumps(other)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "are not the ISO's own" in result.stderr and "differ in gentoo" in result.stderr
    assert FakeDevSession.instances == []
