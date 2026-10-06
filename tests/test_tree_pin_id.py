"""Unit tests of the pin id (story 016, task 3.1, R8.1, R8.12).

``pin_id(tree_pin, overlays)`` is ``p<gentoo date>.<8 hex>`` over the tree's
date and sha512 and every overlay's name and commit; ``load_pin_id(seeds_dir)``
reads both pin files, with no network and no cooldown.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from shidashi import tree
from shidashi.tree import OverlayPin, TreeError, TreePin, load_pin_id, pin_id

C1, C2, C3 = "a" * 40, "b" * 40, "c" * 40


def _tree(date: str = "20260928", sha: str = "1" * 128, url: str = "https://m/s") -> TreePin:
    return TreePin(date=date, base_url=url, sha512=sha)


def _ov(name: str, commit: str, url: str = "https://g/x.git") -> OverlayPin:
    return OverlayPin(name=name, url=url, commit=commit)


def _seeds(root: Path, *, overlays: str | None = None, gentoo: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "gentoo.toml").write_text(
        gentoo
        if gentoo is not None
        else f'date = "20260928"\nbase_url = "https://m/s"\nsha512 = "{"1" * 128}"\n',
        encoding="utf-8",
    )
    if overlays is not None:
        (root / "overlays.toml").write_text(overlays, encoding="utf-8")
    return root


# --- wrong collapse: different pins, one id ---------------------------------------------


@pytest.mark.parametrize(
    "other",
    [
        (_tree(), (_ov("bentoo", C2),)),  # another overlay commit
        (_tree(sha="2" * 128), (_ov("bentoo", C1),)),  # another tree digest, same date
        (_tree(date="20260927"), (_ov("bentoo", C1),)),  # another date
        (_tree(), (_ov("bentoo", C1), _ov("guru", C3))),  # an added overlay
        (_tree(), (_ov("bentoo2", C1),)),  # a renamed overlay
        (_tree(), ()),  # no overlay at all
    ],
)
def test_any_pinned_input_changes_the_id(other: tuple[TreePin, tuple[OverlayPin, ...]]) -> None:
    assert pin_id(_tree(), (_ov("bentoo", C1),)) != pin_id(*other)


def test_two_overlays_swapping_their_commits_change_the_id() -> None:
    """Hostile third element: the same set of names and the same set of commits."""
    one = pin_id(_tree(), (_ov("a", C1), _ov("b", C2)))
    other = pin_id(_tree(), (_ov("a", C2), _ov("b", C1)))
    assert one != other


# --- wrong split: the same pins, two ids -------------------------------------------------


def test_overlay_order_and_urls_do_not_change_the_id() -> None:
    base = pin_id(_tree(), (_ov("a", C1), _ov("b", C2)))
    assert pin_id(_tree(), (_ov("b", C2), _ov("a", C1))) == base
    assert pin_id(_tree(url="https://other/mirror"), (_ov("a", C1), _ov("b", C2))) == base
    assert pin_id(_tree(), (_ov("a", C1, "https://elsewhere"), _ov("b", C2))) == base


def test_the_same_pins_give_the_same_id() -> None:
    assert pin_id(_tree(), (_ov("bentoo", C1),)) == pin_id(_tree(), (_ov("bentoo", C1),))


# --- the shape and the formula (D7) ------------------------------------------------------


def test_the_id_is_the_date_and_8_hex_with_no_dash() -> None:
    value = pin_id(_tree(), (_ov("bentoo", C1),))
    assert re.fullmatch(r"p20260928\.[0-9a-f]{8}", value)
    assert "-" not in value
    payload = json.dumps(["1" * 128, sorted([("bentoo", C1)])])
    assert value == f"p20260928.{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:8]}"


# --- load_pin_id (R8.12) -------------------------------------------------------------------


def test_load_pin_id_reads_both_pin_files(tmp_path: Path) -> None:
    seeds = _seeds(
        tmp_path / "seeds", overlays=f'[bentoo]\nurl = "https://g/b.git"\ncommit = "{C1}"\n'
    )
    assert load_pin_id(seeds) == pin_id(_tree(), (_ov("bentoo", C1),))


def test_load_pin_id_without_an_overlays_file_still_gives_an_id(tmp_path: Path) -> None:
    assert load_pin_id(_seeds(tmp_path / "seeds")) == pin_id(_tree(), ())


def test_load_pin_id_needs_no_network_and_no_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_a: object, **_k: object) -> object:
        raise AssertionError("load_pin_id must not touch the network")

    monkeypatch.setattr(tree, "_download", no_network)
    monkeypatch.setattr(tree, "_git", no_network)
    future = f'date = "20991231"\nbase_url = "https://m/s"\nsha512 = "{"1" * 128}"\n'
    assert load_pin_id(_seeds(tmp_path / "seeds", gentoo=future)).startswith("p20991231.")


@pytest.mark.parametrize(
    ("gentoo", "overlays", "named"),
    [
        ("date = ", None, "gentoo.toml"),
        ('date = "20260928"\n', None, "gentoo.toml"),
        (None, '[bentoo]\nurl = "u"\ncommit = "short"\n', "overlays.toml"),
        (None, "[bentoo\n", "overlays.toml"),
    ],
)
def test_an_unreadable_pin_file_is_a_tree_error_naming_it(
    tmp_path: Path, gentoo: str | None, overlays: str | None, named: str
) -> None:
    seeds = _seeds(tmp_path / "seeds", gentoo=gentoo, overlays=overlays)
    with pytest.raises(TreeError) as err:
        load_pin_id(seeds)
    assert str(seeds / named) in str(err.value)


def test_a_missing_gentoo_pin_is_a_tree_error(tmp_path: Path) -> None:
    (tmp_path / "seeds").mkdir()
    with pytest.raises(TreeError, match="gentoo.toml"):
        load_pin_id(tmp_path / "seeds")
