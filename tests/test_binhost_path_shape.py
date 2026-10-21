"""One strict PATH shape for index and quarantine entries (story 029, task 8.1; R3.11, R4.5).

Contract from design.md "shidashi/binhost.py": a PATH is not empty, not absolute, has no
empty, ``.`` or ``..`` component, and has at least two components. ``safe_path``,
``load_quarantined`` and ``add_quarantined`` apply that one rule, and the error names the
entry. A name that merely *contains* ``..`` (``a..b``) is a valid name. The quarantine list
feeds a ``mv`` script and rsync excludes, so an entry that is empty or names a directory
must never reach either: ``quarantine_script`` never emits a ``mv`` of the pool root.

Hostile halves first. The rule has two converses: it must not let a malformed spelling
through (trailing slash, ``a//b``, ``./x`` -- all of which ``PurePosixPath`` silently
normalises into a valid-looking path), and it must not refuse a valid PATH (a gpkg PATH of
three components, a ``..`` inside a name, a dotfile, exactly two components).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import

BinhostError: Any = try_import("shidashi.binhost", "BinhostError")
safe_path: Any = try_import("shidashi.binhost", "safe_path")
load_quarantined: Any = try_import("shidashi.binhost", "load_quarantined")
add_quarantined: Any = try_import("shidashi.binhost", "add_quarantined")
quarantine_script: Any = try_import("shidashi.binhost", "quarantine_script")

LIST = ".shidashi-quarantine.json"
GOOD = "dev-libs/foo/foo-1.0-1.gpkg.tar"

#: every spelling the rule forbids; the first column of the comment says which clause.
REFUSED = [
    "",  # empty
    "/",  # absolute
    "/dev-libs/foo/x",  # absolute
    "dev-libs",  # one component
    "dev-libs/",  # trailing slash = an empty last component (and a directory)
    "dev-libs/foo/",  # a directory, three-component look
    "dev-libs//foo",  # empty component
    "dev-libs//foo/x",  # empty component
    "./dev-libs/foo/x",  # '.' component
    "dev-libs/./foo/x",  # '.' component
    "dev-libs/foo/.",  # '.' last component
    "dev-libs/../x",  # '..' component
    "../dev-libs/foo",  # '..' first component
    "dev-libs/foo/..",  # '..' last component
    "..",  # only '..'
    ".",  # only '.'
]

#: valid names, including the ones a naive substring test on ".." would wrongly refuse.
ACCEPTED = [
    GOOD,
    "app-misc/a..b/a..b-1-1.gpkg.tar",  # '..' inside a name
    "app-misc/a..b",  # exactly two components, '..' inside a name
    "dev-libs/foo",  # exactly two components: the lower bound
    "dev-libs/.hidden/x",  # a dotfile is not '.'
    "dev-libs/.../x",  # three dots is a name, not '..'
    "dev-libs/foo/foo-1.0-1.gpkg.tar.part",
]


def _list(pkgdir: Path, *paths: str) -> Path:
    file = pkgdir / LIST
    file.write_text(json.dumps({"version": 1, "paths": list(paths)}), encoding="utf-8")
    return file


def _message(err: pytest.ExceptionInfo[BaseException]) -> str:
    return str(err.value)


def _names(message: str, entry: str) -> bool:
    """The message carries the entry (an empty one by its quotes)."""
    return (entry in message) if entry else ("''" in message or '""' in message)


# --- safe_path --------------------------------------------------------------------------


@pytest.mark.parametrize("rel", REFUSED)
def test_safe_path_refuses_a_malformed_path_and_names_it(tmp_path: Path, rel: str) -> None:
    with pytest.raises(BinhostError) as err:
        safe_path(tmp_path, rel)
    assert _names(_message(err), rel), _message(err)


@pytest.mark.parametrize("rel", ACCEPTED)
def test_safe_path_accepts_a_valid_path_unchanged(tmp_path: Path, rel: str) -> None:
    """The converse: the rule does not refuse what is plainly a valid PATH."""
    assert safe_path(tmp_path, rel) == tmp_path / rel


# --- load_quarantined -------------------------------------------------------------------


@pytest.mark.parametrize("rel", REFUSED)
def test_a_list_with_a_malformed_entry_is_damaged_and_names_the_entry(
    tmp_path: Path, rel: str
) -> None:
    """R4.5 on load: the whole list is refused (one bad entry beside a good one), naming it."""
    file = _list(tmp_path, GOOD, rel)
    before = file.read_bytes()
    with pytest.raises(BinhostError) as err:
        load_quarantined(tmp_path)
    assert _names(_message(err), rel), _message(err)
    assert file.read_bytes() == before, "a damaged list is never rewritten"


def test_a_list_of_valid_entries_loads_back_spelling_for_spelling(tmp_path: Path) -> None:
    """The converse on load: nothing valid is refused or normalised away."""
    _list(tmp_path, *ACCEPTED)
    assert load_quarantined(tmp_path) == frozenset(ACCEPTED)


def test_a_missing_list_is_still_empty(tmp_path: Path) -> None:
    assert load_quarantined(tmp_path) == frozenset()


# --- add_quarantined --------------------------------------------------------------------


@pytest.mark.parametrize("rel", REFUSED)
def test_an_append_of_a_malformed_entry_is_refused_and_leaves_the_list_untouched(
    tmp_path: Path, rel: str
) -> None:
    """R4.5 on append: BinhostError naming the entry; the list file is byte-identical
    afterwards, even when a valid entry travels in the same call."""
    file = _list(tmp_path, GOOD)
    before = file.read_bytes()
    with pytest.raises(BinhostError) as err:
        add_quarantined(tmp_path, ["app-misc/ok/ok-1-1.gpkg.tar", rel])
    assert _names(_message(err), rel), _message(err)
    assert file.read_bytes() == before


@pytest.mark.parametrize("rel", REFUSED)
def test_an_append_of_a_malformed_entry_creates_no_list(tmp_path: Path, rel: str) -> None:
    with pytest.raises(BinhostError):
        add_quarantined(tmp_path, [rel])
    assert not (tmp_path / LIST).exists()


def test_an_append_of_valid_entries_is_kept_spelling_for_spelling(tmp_path: Path) -> None:
    """The converse on append, including the entries a naive rule would wrongly refuse."""
    add_quarantined(tmp_path, ACCEPTED)
    assert load_quarantined(tmp_path) == frozenset(ACCEPTED)
    assert json.loads((tmp_path / LIST).read_text(encoding="utf-8"))["paths"] == sorted(ACCEPTED)


def test_a_trailing_slash_is_not_a_second_spelling_of_the_same_file(tmp_path: Path) -> None:
    """Wrongly-split half: ``a/b/c`` and ``a/b/c/`` would be two list entries for one path;
    the second is refused, so the list never carries both."""
    add_quarantined(tmp_path, [GOOD])
    with pytest.raises(BinhostError):
        add_quarantined(tmp_path, [GOOD + "/"])
    assert load_quarantined(tmp_path) == frozenset({GOOD})


# --- quarantine_script ------------------------------------------------------------------

POOL = "/var/cache/shidashi/binpkgs/v3/g1"
QROOT = "/var/cache/shidashi/quarantine/binpkgs/v3/g1"


@pytest.mark.parametrize("rel", REFUSED)
def test_quarantine_script_never_moves_the_pool_root_or_a_directory(rel: str) -> None:
    """For a refused entry the script either raises BinhostError or emits no ``mv`` at all;
    in no case a ``mv`` whose source is the pool itself."""
    try:
        script = quarantine_script(POOL, QROOT, [rel])
    except BinhostError:
        return
    assert "mv" not in script, script
    for line in script.splitlines():
        assert f"mv -- {POOL} " not in line and f"mv -- {POOL}/ " not in line, line


@pytest.mark.parametrize("rel", REFUSED)
def test_a_refused_entry_beside_a_valid_one_emits_no_move_at_all(rel: str) -> None:
    """Refused before the first move: no partial script with the valid entry's ``mv``."""
    with pytest.raises(BinhostError):
        quarantine_script(POOL, QROOT, [GOOD, rel])


@pytest.mark.parametrize("rel", ACCEPTED)
def test_quarantine_script_moves_a_valid_path_exactly_once(rel: str) -> None:
    script = quarantine_script(POOL, QROOT, [rel])
    assert script.count("mv -- ") == 1
    assert f"{POOL}/{rel}" in script
    assert f"{QROOT}/{rel}" in script
