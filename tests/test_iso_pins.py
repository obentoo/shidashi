"""Tests of shidashi.iso_pins -- which pinned trees an ISO was built from.

A dev VM mounts the ::gentoo tree and the overlays the ISO's own packages came from;
the ISO's ``bentoo/build.json`` says which. ISOs older than the ``pins`` block are read
through the Shidashi commit their build.json names.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from shidashi import iso_pins, tree

_SHA = "ab" * 64
_COMMIT = "1d98e5f4fc41ae67431e674b28b4acc238fac1f1"


def _seeds(directory: Path, *, date: str = "20261101", commit: str = _COMMIT) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "gentoo.toml").write_text(
        f'date = "{date}"\nbase_url = "https://distfiles.gentoo.org/snapshots"\nsha512 = "{_SHA}"\n'
    )
    (directory / "overlays.toml").write_text(
        f'[bentoo]\nurl = "https://github.com/obentoo/bentoo.git"\ncommit = "{commit}"\n'
    )
    return directory


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A Shidashi-shaped repository whose one commit pins 20260928."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _seeds(root / "seeds", date="20260928")
    _git(root, "add", "seeds")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pin")
    return root


def test_the_record_names_the_pin_id_the_tree_and_every_overlay(tmp_path: Path) -> None:
    seeds = _seeds(tmp_path / "seeds")
    assert iso_pins.pins_record(seeds) == {
        "id": tree.load_pin_id(seeds),
        "gentoo": {
            "date": "20261101",
            "base_url": "https://distfiles.gentoo.org/snapshots",
            "sha512": _SHA,
        },
        "overlays": {"bentoo": {"url": "https://github.com/obentoo/bentoo.git", "commit": _COMMIT}},
    }


def test_a_recorded_block_is_read_back_from_build_json(tmp_path: Path) -> None:
    info = {"pins": iso_pins.pins_record(_seeds(tmp_path / "seeds"))}
    pins = iso_pins.read_iso_pins(info, repo=tmp_path / "no-repo")
    assert pins.source == "build.json"
    assert pins.tree.date == "20261101" and pins.tree.sha512 == _SHA
    assert [(o.name, o.commit) for o in pins.overlays] == [("bentoo", _COMMIT)]
    assert pins.id == tree.load_pin_id(tmp_path / "seeds")


def test_an_older_iso_is_read_through_the_commit_that_built_it(repo: Path) -> None:
    info: dict[str, Any] = {"repository": {"commit": _git(repo, "rev-parse", "HEAD")}}
    pins = iso_pins.read_iso_pins(info, repo=repo)
    assert pins.source == "git"
    assert pins.tree.date == "20260928"
    assert [o.name for o in pins.overlays] == ["bentoo"]


def test_an_older_iso_whose_commit_is_unknown_here_is_refused(repo: Path) -> None:
    info = {"repository": {"commit": "f" * 40}}
    with pytest.raises(iso_pins.IsoPinsError, match="f{40}"):
        iso_pins.read_iso_pins(info, repo=repo)


def test_an_older_iso_built_with_edited_seeds_is_refused(repo: Path) -> None:
    # the commit's seeds are not what that build used
    commit = _git(repo, "rev-parse", "HEAD")
    info = {"repository": {"commit": commit, "dirty_files": [" M seeds/gentoo.toml"]}}
    with pytest.raises(iso_pins.IsoPinsError, match="seeds/gentoo.toml"):
        iso_pins.read_iso_pins(info, repo=repo)


def test_an_older_iso_built_with_other_edits_is_still_read(repo: Path) -> None:
    commit = _git(repo, "rev-parse", "HEAD")
    info = {"repository": {"commit": commit, "dirty_files": [" M shidashi/vm.py"]}}
    assert iso_pins.read_iso_pins(info, repo=repo).tree.date == "20260928"


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"repository": {"commit": None}},
        {"pins": {"gentoo": {"date": "20261101"}}},
        {"pins": "garbage"},
    ],
    ids=["nothing", "no-commit", "partial-block", "wrong-shape"],
)
def test_an_unreadable_build_json_is_refused_not_crashed(
    info: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(iso_pins.IsoPinsError):
        iso_pins.read_iso_pins(info, repo=tmp_path)


# --- pins handed in (vm start --dev --pins-json) -------------------------------------------


def test_a_block_round_trips_through_the_pins_it_names(tmp_path: Path) -> None:
    # the relay writes it on the host, the worker reads it: block -> IsoPins -> block
    block = iso_pins.pins_record(_seeds(tmp_path / "seeds"))
    pins = iso_pins.handed_pins(block)
    assert pins.source == "handed in"
    assert iso_pins.pins_block(pins) == block
    assert iso_pins.handed_pins(iso_pins.pins_block(pins)) == pins


def test_the_block_of_pins_read_with_git_is_the_one_an_assemble_would_record(repo: Path) -> None:
    info: dict[str, Any] = {"repository": {"commit": _git(repo, "rev-parse", "HEAD")}}
    pins = iso_pins.read_iso_pins(info, repo=repo)
    assert iso_pins.pins_block(pins) == iso_pins.pins_record(repo / "seeds")


def test_handed_pins_are_used_without_asking_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a worker's source tree has no .git: an ISO without the block has only these
    def no_git(*_a: object, **_k: object) -> bytes:
        raise AssertionError("git was asked")

    monkeypatch.setattr(iso_pins, "_show", no_git)
    handed = iso_pins.handed_pins(iso_pins.pins_record(_seeds(tmp_path / "seeds")))
    info = {"repository": {"commit": "f" * 40}}
    assert iso_pins.read_iso_pins(info, repo=tmp_path / "no-repo", handed=handed) is handed


def test_handed_pins_that_agree_with_the_iso_own_block_are_used(tmp_path: Path) -> None:
    block = iso_pins.pins_record(_seeds(tmp_path / "seeds"))
    handed = iso_pins.handed_pins(block)
    assert iso_pins.read_iso_pins({"pins": block}, repo=tmp_path, handed=handed) is handed


def _other_block(tmp_path: Path, part: str) -> dict[str, object]:
    """The block of ``tmp_path/seeds`` with one thing changed in ``part``."""
    if part == "snapshot":
        return iso_pins.pins_record(_seeds(tmp_path / "other", date="20261102"))
    if part == "commit":
        return iso_pins.pins_record(_seeds(tmp_path / "other", commit="0" * 40))
    block = iso_pins.pins_record(_seeds(tmp_path / "seeds"))
    # same id: it covers no URL, so only the comparison of the blocks can tell
    block["overlays"] = {"bentoo": {"url": "https://example.org/b.git", "commit": _COMMIT}}
    return block


@pytest.mark.parametrize(
    ("part", "differing"),
    [("snapshot", "gentoo"), ("commit", "overlays"), ("url", "overlays")],
)
def test_handed_pins_that_disagree_with_the_iso_own_block_are_refused(
    tmp_path: Path, part: str, differing: str
) -> None:
    """Hostile: the pins of another ISO are handed in; neither side is silently preferred."""
    own = iso_pins.pins_record(_seeds(tmp_path / "seeds"))
    handed = iso_pins.handed_pins(_other_block(tmp_path, part))
    with pytest.raises(iso_pins.IsoPinsError, match=f"not the ISO's own.*differ in {differing}$"):
        iso_pins.read_iso_pins({"pins": own}, repo=tmp_path, handed=handed)


def test_handed_pins_do_not_excuse_an_unreadable_block_of_the_iso(tmp_path: Path) -> None:
    handed = iso_pins.handed_pins(iso_pins.pins_record(_seeds(tmp_path / "seeds")))
    with pytest.raises(iso_pins.IsoPinsError, match="build.json has an unreadable pins block"):
        iso_pins.read_iso_pins({"pins": "garbage"}, repo=tmp_path, handed=handed)


_GENTOO = {"date": "20261101", "base_url": "u", "sha512": "s"}


@pytest.mark.parametrize(
    "block",
    [
        "garbage",
        [],
        {},
        {"gentoo": {"date": "20261101"}, "overlays": {}},
        {"gentoo": [], "overlays": {}},
        {"gentoo": {**_GENTOO, "date": 20261101}, "overlays": {}},
        {"gentoo": _GENTOO},
        {"gentoo": _GENTOO, "overlays": []},
        {"gentoo": _GENTOO, "overlays": {"bentoo": {"url": "u", "commit": "abc"}}},
    ],
    ids=[
        "a-string",
        "a-list",
        "empty",
        "partial-gentoo",
        "gentoo-a-list",
        "date-a-number",
        "no-overlays",
        "overlays-a-list",
        "short-commit",
    ],
)
def test_handed_pins_of_the_wrong_shape_are_refused_as_handed_in(block: object) -> None:
    with pytest.raises(iso_pins.IsoPinsError, match="the pins handed in are unreadable"):
        iso_pins.handed_pins(block)


def test_handed_pins_named_by_another_id_are_refused(tmp_path: Path) -> None:
    """Hostile: a block edited on its way keeps the id of the pins it was made from."""
    block = iso_pins.pins_record(_seeds(tmp_path / "seeds"))
    block["gentoo"] = {**_GENTOO, "sha512": "cd" * 64}
    with pytest.raises(iso_pins.IsoPinsError, match="are named 'p20261101"):
        iso_pins.handed_pins(block)
    del block["id"]  # the id is a check, not a requirement
    assert iso_pins.handed_pins(block).tree.sha512 == "cd" * 64
