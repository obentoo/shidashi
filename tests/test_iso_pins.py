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
