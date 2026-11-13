"""Which pinned trees an ISO was built from -- the ``pins`` block of ``bentoo/build.json``.

The live medium ships with ``/var/db/repos/{gentoo,bentoo}`` empty, so a VM that builds
ebuilds against it (``shidashi vm start --dev``) must mount the trees itself -- the SAME
ones the ISO's packages came from, or Portage resolves against ebuilds the image never
saw. The assemble records them (:func:`pins_record`) and the dev VM reads them back
(:func:`read_iso_pins`).

ISOs assembled before the block existed (2026-11-13) name only the Shidashi commit that
built them: their pins are that commit's ``seeds/``, read with git -- unless the build
ran with edited seeds, when the commit is not what the build used and nothing is
guessed.
"""

import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pydantic

from shidashi.tree import (
    OverlayPin,
    TreeError,
    TreePin,
    load_overlay_pins,
    load_tree_pin,
    pin_id,
)

_PIN_FILES = ("gentoo.toml", "overlays.toml")


class IsoPinsError(Exception):
    """An ISO's pins cannot be told; the message says why and what was read."""


@dataclass(frozen=True)
class IsoPins:
    """The ::gentoo snapshot and overlay commits of one ISO, and where they were read."""

    tree: TreePin
    overlays: tuple[OverlayPin, ...]
    source: Literal["build.json", "git"]

    @property
    def id(self) -> str:
        return pin_id(self.tree, self.overlays)


def pins_record(seeds_dir: Path) -> dict[str, object]:
    """The ``pins`` block of build.json for the pins in ``seeds_dir``. Reads the files only."""
    tree_pin = load_tree_pin(seeds_dir)
    overlays = load_overlay_pins(seeds_dir)
    return {
        "id": pin_id(tree_pin, overlays),
        "gentoo": {"date": tree_pin.date, "base_url": tree_pin.base_url, "sha512": tree_pin.sha512},
        "overlays": {o.name: {"url": o.url, "commit": o.commit} for o in overlays},
    }


def read_iso_pins(info: Mapping[str, object], *, repo: Path) -> IsoPins:
    """The pins of the ISO whose build.json is ``info``; :class:`IsoPinsError` when unknown.

    The ``pins`` block when present; otherwise ``seeds/`` at the build's commit in the
    Shidashi repository ``repo``.
    """
    block = info.get("pins")
    if block is not None:
        return _from_block(block)
    return _from_commit(info.get("repository"), repo)


def _from_block(block: object) -> IsoPins:
    try:
        if not isinstance(block, Mapping):
            raise TypeError(f"expected a table, got {type(block).__name__}")
        gentoo = block["gentoo"]
        tree_pin = TreePin(
            date=gentoo["date"],
            base_url=gentoo["base_url"],
            sha512=gentoo["sha512"],
        )
        overlays = tuple(
            OverlayPin(name=name, url=pin["url"], commit=pin["commit"])
            for name, pin in block["overlays"].items()
        )
    except (KeyError, TypeError, AttributeError, pydantic.ValidationError) as err:
        raise IsoPinsError(f"the ISO's build.json has an unreadable pins block: {err}") from err
    return IsoPins(tree_pin, overlays, "build.json")


def _from_commit(repository: object, repo: Path) -> IsoPins:
    if not isinstance(repository, Mapping):
        repository = {}
    commit = repository.get("commit")
    if not isinstance(commit, str) or not commit:
        raise IsoPinsError(
            "the ISO's build.json names neither its pins nor the commit that built it; "
            "assemble a new ISO"
        )
    edited = _edited_seeds(repository)
    if edited:
        raise IsoPinsError(
            f"the ISO was built from {commit} with edited {', '.join(edited)}: that commit's "
            "pins are not the ones it used; assemble a new ISO"
        )
    with tempfile.TemporaryDirectory(prefix="shidashi-iso-pins-") as tmp:
        seeds = Path(tmp)
        for name in _PIN_FILES:
            (seeds / name).write_bytes(_show(repo, commit, f"seeds/{name}"))
        try:
            return IsoPins(load_tree_pin(seeds), load_overlay_pins(seeds), "git")
        except (TreeError, OSError, ValueError) as err:
            raise IsoPinsError(f"the pins of commit {commit} do not read: {err}") from err


def _edited_seeds(repository: Mapping[object, object]) -> list[str]:
    """The ``seeds/`` pin files among a build's uncommitted changes (``git status`` lines)."""
    lines = repository.get("dirty_files")
    if not isinstance(lines, Sequence) or isinstance(lines, str):
        return []
    return [
        f"seeds/{name}"
        for name in _PIN_FILES
        if any(isinstance(line, str) and line.endswith(f"seeds/{name}") for line in lines)
    ]


def _show(repo: Path, commit: str, path: str) -> bytes:
    done = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        raise IsoPinsError(
            f"cannot read {path} at commit {commit} in {repo} (fetch it, or assemble a new "
            f"ISO): {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout
