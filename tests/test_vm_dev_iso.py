"""Tests of the ISO ``shidashi vm start --dev`` boots when none is named.

Two places are searched: the releases directory (``<cache>/releases/<date>/``, the
tool's own output) and the stash, where ISOs are kept flat -- or one directory down --
so that releases can be pruned. The newest by the date in the file name wins, a release
on a tie.

The stash's default is under /var/tmp, where anyone can create it first: it is searched
only when it is the invoking user's alone. Another owner is stood in for by the ``uid``
handed to the lookup (no test needs root); a loose mode and a symbolic link are real.
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, config, vm_dev


def _released(releases: Path, date: str, init: str = "systemd", arch: str = "v3") -> Path:
    path = releases / date / f"bentoo-{date}-minimal-{init}-{arch}.iso"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"iso")
    return path


def _stashed(directory: Path, date: str, init: str = "systemd", arch: str = "v3") -> Path:
    path = directory / f"bentoo-{date}-minimal-{init}-{arch}.iso"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_bytes(b"iso")
    return path


@pytest.fixture
def releases(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "releases"


@pytest.fixture
def stash(tmp_path: Path) -> Path:
    """An ISO stash that is the invoking user's alone."""
    directory = tmp_path / "isos"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)  # whatever the umask
    return directory


class SkipTrail(audit.Recorder):
    """The audit trail, as the events it was sent."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))


def _find(releases: Path, stash: Path | None = None, **kw: Any) -> vm_dev.FoundDevIso:
    return vm_dev.find_dev_iso(releases, stash, init="systemd", arch="v3", **kw)


# --- the releases directory --------------------------------------------------------------


def test_the_newest_minimal_iso_of_the_init_and_arch_is_chosen(releases: Path) -> None:
    _released(releases, "2026.11.10")
    newest = _released(releases, "2026.11.12")
    _released(releases, "2026.11.13", init="openrc")
    _released(releases, "2026.11.14", arch="znver5")
    (releases / "2026.11.15").mkdir()
    (releases / "2026.11.15" / "bentoo-2026.11.15-kde-systemd-v3.iso").write_bytes(b"")
    assert _find(releases) == vm_dev.FoundDevIso(newest)


def test_the_date_is_read_from_the_file_name_not_from_its_directory(releases: Path) -> None:
    """A release directory renamed, or holding an older ISO, does not make it newer."""
    misfiled = releases / "2026.12.31" / "bentoo-2026.11.01-minimal-systemd-v3.iso"
    misfiled.parent.mkdir(parents=True)
    misfiled.write_bytes(b"iso")
    newest = _released(releases, "2026.11.12")
    assert _find(releases).iso == newest


@pytest.mark.parametrize("name", ["bentoo-latest-minimal-systemd-v3.iso", "bentoo-minimal.iso"])
def test_a_file_with_no_date_in_its_name_is_no_candidate(
    releases: Path, stash: Path, name: str
) -> None:
    dated = _released(releases, "2026.11.10")
    (stash / name).write_bytes(b"iso")
    (releases / "2026.11.10" / name).write_bytes(b"iso")
    assert _find(releases, stash).iso == dated


def test_a_directory_named_like_an_iso_is_no_candidate(releases: Path, stash: Path) -> None:
    dated = _released(releases, "2026.11.10")
    (stash / "bentoo-2026.11.12-minimal-systemd-v3.iso").mkdir()
    assert _find(releases, stash).iso == dated


# --- the stash ---------------------------------------------------------------------------


def test_a_stashed_iso_newer_than_every_release_is_chosen(releases: Path, stash: Path) -> None:
    _released(releases, "2026.11.10")
    _stashed(stash, "2026.11.08")
    newest = _stashed(stash, "2026.11.12")
    _stashed(stash, "2026.11.13", init="openrc")
    _stashed(stash, "2026.11.14", arch="znver5")
    assert _find(releases, stash) == vm_dev.FoundDevIso(newest)


def test_a_release_newer_than_every_stashed_iso_is_chosen(releases: Path, stash: Path) -> None:
    newest = _released(releases, "2026.11.13")
    _stashed(stash, "2026.11.12")
    assert _find(releases, stash).iso == newest


def test_on_the_same_date_the_release_is_chosen(releases: Path, stash: Path) -> None:
    released = _released(releases, "2026.11.12")
    _stashed(stash, "2026.11.12")
    _stashed(stash / "2026.11.12", "2026.11.12")
    assert _find(releases, stash).iso == released


def test_the_stash_is_read_flat_and_one_directory_down(releases: Path, stash: Path) -> None:
    flat = _stashed(stash, "2026.11.10")
    assert _find(releases, stash).iso == flat  # no releases directory at all
    dated = _stashed(stash / "2026.11.12", "2026.11.12")
    assert _find(releases, stash).iso == dated
    # the directory's name says nothing: the file's date decides
    _stashed(stash / "zzz-old", "2026.11.01")
    assert _find(releases, stash).iso == dated
    flat_again = _stashed(stash, "2026.11.14")
    assert _find(releases, stash).iso == flat_again
    _stashed(stash / "a" / "b", "2026.11.20")  # two directories down: not looked at
    assert _find(releases, stash).iso == flat_again


@pytest.mark.parametrize("named", [False, True], ids=["no-stash-given", "absent-stash"])
def test_without_a_stash_the_releases_are_all_there_is(
    tmp_path: Path, releases: Path, named: bool
) -> None:
    newest = _released(releases, "2026.11.12")
    trail = SkipTrail()
    with audit.recording(trail):
        found = _find(releases, tmp_path / "absent" if named else None)
    assert found == vm_dev.FoundDevIso(newest)
    assert trail.events == []  # an absent stash is nothing to report


# --- a stash that is not the user's alone ------------------------------------------------


def _someone_elses(stash: Path) -> dict[str, Any]:
    return {"uid": stash.stat().st_uid + 1}


def _group_writable(stash: Path) -> dict[str, Any]:
    stash.chmod(0o775)
    return {}


def _world_writable(stash: Path) -> dict[str, Any]:
    stash.chmod(0o1777)  # as /var/tmp itself: sticky, and still anyone's to fill
    return {}


def _a_link(stash: Path) -> dict[str, Any]:
    real = stash.with_name("elsewhere")
    stash.rename(real)
    stash.symlink_to(real)
    return {}


def _a_file(stash: Path) -> dict[str, Any]:
    shutil.rmtree(stash)
    stash.write_bytes(b"")
    return {}


_UNSAFE = [
    (_someone_elses, "it belongs to uid "),
    (_group_writable, "group or others can write to it (mode 0775)"),
    (_world_writable, "group or others can write to it (mode 1777)"),
    (_a_link, "it is a symbolic link"),
    (_a_file, "it is not a directory"),
]


@pytest.mark.parametrize(("loosen", "reason"), _UNSAFE, ids=[fn.__name__ for fn, _ in _UNSAFE])
def test_a_stash_that_is_not_the_users_alone_is_skipped_and_said_so(
    releases: Path, stash: Path, loosen: Any, reason: str
) -> None:
    """Hostile: the stash holds the newest ISO, and it may be someone else's plant."""
    released = _released(releases, "2026.11.10")
    _stashed(stash, "2026.11.12")
    _stashed(stash / "2026.11.14", "2026.11.14")
    kw = loosen(stash)
    trail = SkipTrail()
    with audit.recording(trail):
        found = _find(releases, stash, **kw)

    assert found.iso == released
    (skip,) = found.skipped
    assert skip.directory == stash
    assert reason in skip.reason
    assert str(skip) == f"{stash} was not searched for an ISO: {skip.reason}"
    assert trail.events == [
        ("vm.dev.stash_skipped", {"directory": str(stash), "reason": skip.reason})
    ]


def test_another_owner_is_named_with_the_user_it_should_be(releases: Path, stash: Path) -> None:
    _released(releases, "2026.11.10")
    owner = stash.stat().st_uid
    (skip,) = _find(releases, stash, uid=owner + 1).skipped
    assert skip.reason == f"it belongs to uid {owner}, not to uid {owner + 1}"
    assert _find(releases, stash, uid=owner).skipped == ()
    assert owner == os.getuid()  # the default: the user running the lookup
    assert _find(releases, stash).skipped == ()


def test_a_loose_sub_directory_is_skipped_and_the_rest_of_the_stash_read(
    releases: Path, stash: Path
) -> None:
    flat = _stashed(stash, "2026.11.10")
    _stashed(stash / "open", "2026.11.14")
    (stash / "open").chmod(0o777)
    (stash / "linked").symlink_to(stash / "open")
    (stash / "empty-and-open").mkdir(mode=0o777)
    (stash / "empty-and-open").chmod(0o777)
    found = _find(releases, stash)
    assert found.iso == flat
    assert [(s.directory.name, s.reason) for s in found.skipped] == [
        ("linked", "it is a symbolic link"),
        ("open", "group or others can write to it (mode 0777)"),
    ]  # the open directory with no ISO in it: nothing was passed over there


# --- nothing to boot ---------------------------------------------------------------------


@pytest.mark.parametrize("make", [False, True], ids=["no-directories", "no-match"])
def test_no_minimal_iso_names_both_places_and_how_to_make_one(
    tmp_path: Path, releases: Path, make: bool
) -> None:
    stash = tmp_path / "isos"
    if make:
        _released(releases, "2026.11.12", init="openrc")
        _stashed(stash, "2026.11.12", arch="znver5")
        stash.chmod(0o755)
    with pytest.raises(vm_dev.NoDevIsoError) as caught:
        _find(releases, stash)
    message = str(caught.value)
    assert f"no minimal ISO for v3/systemd under {releases} or {stash}: " in message
    assert "shidashi build v3 minimal systemd" in message
    assert "--iso" in message
    assert "\n" not in message


def test_no_minimal_iso_without_a_stash_names_the_releases_alone(releases: Path) -> None:
    with pytest.raises(vm_dev.NoDevIsoError) as caught:
        _find(releases)
    assert f"under {releases}: build one" in str(caught.value)


def test_no_minimal_iso_says_which_stash_was_skipped_and_why(releases: Path, stash: Path) -> None:
    _stashed(stash, "2026.11.12")
    stash.chmod(0o777)
    with pytest.raises(vm_dev.NoDevIsoError) as caught:
        _find(releases, stash)
    message = str(caught.value)
    assert (
        f"under {releases}; {stash} was not searched for an ISO: group or others can "
        "write to it (mode 0777): build one with"
    ) in message
    assert f"or {stash}" not in message  # it was not searched: not listed as if it were


# --- where the stash is ------------------------------------------------------------------


def test_the_stash_is_under_var_tmp_unless_the_environment_names_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SHIDASHI_DEV_ISO_DIR")  # set by conftest: no test reads the real one
    assert config.dev_iso_dir() == Path("/var/tmp/shidashi-isos")
    monkeypatch.setenv("SHIDASHI_DEV_ISO_DIR", "")
    assert config.dev_iso_dir() == Path("/var/tmp/shidashi-isos")
    monkeypatch.setenv("SHIDASHI_DEV_ISO_DIR", str(tmp_path / "a"))
    assert config.dev_iso_dir() == tmp_path / "a"
    monkeypatch.setenv("SHIDASHI_DEV_ISO_DIR", str(tmp_path / "b"))  # read on every call
    assert config.dev_iso_dir() == tmp_path / "b"


def test_no_test_reads_the_invoking_users_stash() -> None:
    assert config.dev_iso_dir() != Path("/var/tmp/shidashi-isos")
    assert not config.dev_iso_dir().exists()
