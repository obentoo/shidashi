"""The worker sync honours the quarantine list (story 029, tasks 4.1 and 4.2; R4.1-R4.3).

A NEW file beside tests/test_worker_sync.py (same fake worker, same fixtures' shape) so the
story's tests stay apart from story 010's: ``-k exclude`` selects task 4.2's tests and
``-k "not exclude"`` task 4.1's (the module name itself says "quarantine", so ``-k
quarantine`` would select every test here).

Real rsync through the fake ssh of tests/_fake_worker.py: the worker's ``/mnt/work`` is a
temporary directory, its shell commands really run there, and its ``emaint`` indexes every
binpkg file it finds -- so a path still on the worker at reindex time is in its index.
"""

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import ownership, worker
from shidashi.remote import SyncError
from tests._fake_worker import FakeWorker, option_values, seed_host_cache

QUARANTINE = ".shidashi-quarantine.json"


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    w.commit_recipes()
    yield w
    w.close()


@pytest.fixture
def gen(fw: FakeWorker) -> str:
    return str(fw.config()["generation"])


@pytest.fixture
def owner(fw: FakeWorker) -> ownership.Owner:
    """The caller holds v3's owner lock, as a factory job does: the binhost comes back."""
    seed_host_cache(fw)
    held = ownership.Owner(
        arch="v3",
        worker=fw.name,
        job="kde",
        commit=fw.head,
        since="2026-10-20T14:02:31Z",
        host_pid=None,
    )
    return ownership.acquire("v3", held)


def _worker_pkgdir(fw: FakeWorker, gen: str) -> Path:
    return fw.work / "cache" / "binpkgs" / "v3" / gen


def _host_pkgdir(fw: FakeWorker, gen: str) -> Path:
    return fw.host_cache / "binpkgs" / "v3" / gen


def _put_binpkgs(fw: FakeWorker, gen: str, *rels: str) -> None:
    for rel in rels:
        fw.put(f"cache/binpkgs/v3/{gen}/{rel}", f"binpkg {rel}\n")


def _list(pkgdir: Path, *paths: str) -> None:
    pkgdir.mkdir(parents=True, exist_ok=True)
    (pkgdir / QUARANTINE).write_text(
        json.dumps({"version": 1, "paths": list(paths)}), encoding="utf-8"
    )


def _pull(fw: FakeWorker, owner: ownership.Owner, tmp_path: Path) -> Any:
    return worker.pull(fw.remote(), "v3", "kde", results=tmp_path / "results", owner=owner)


def _pkgdir_fetch_excludes(fw: FakeWorker) -> list[list[str]]:
    """The distinct exclude lists of the pull rsyncs that fetched the PKGDIR's binpkgs
    (the measuring dry run and the transfer use the same one)."""
    found: list[list[str]] = []
    for call in fw.rsync_calls():
        excludes = option_values(call["argv"], "--exclude")
        if "/Packages" in excludes and call.get("direction") == "pull" and excludes not in found:
            found.append(excludes)
    return found


# --- task 4.1: the worker moves the listed paths aside before its reindex ------------------

FOO_OLD = "dev-libs/foo/foo-1.0-1.gpkg.tar"
FOO_SIBLING = "dev-libs/foo/foo-1.0-2.gpkg.tar"  # same CPV, another BUILD_ID: not listed
ODD = "app-misc/odd/odd-1 $(echo x);'q' two.gpkg.tar"  # shell words, a quote, a space
NOT_THERE = "sys-apps/gone/gone-3-1.gpkg.tar"  # listed, but the worker never had it
NEW = "app-misc/new/new-2-1.gpkg.tar"


def test_pull_moves_the_quarantined_paths_aside_on_the_worker_before_its_reindex(
    fw: FakeWorker, gen: str, owner: ownership.Owner, tmp_path: Path
) -> None:
    """R4.1. Hostile halves: the sibling instance of the same CPV stays; a name full of
    shell syntax is moved as the one literal file it is; a listed path the worker does
    not have is no error."""
    _put_binpkgs(fw, gen, FOO_OLD, FOO_SIBLING, ODD, NEW)
    for pkgdir in (_host_pkgdir(fw, gen), _worker_pkgdir(fw, gen)):  # pushed, so on both
        _list(pkgdir, FOO_OLD, ODD, NOT_THERE)

    _pull(fw, owner, tmp_path)

    wpkg = _worker_pkgdir(fw, gen)
    assert not (wpkg / FOO_OLD).exists()
    assert not (wpkg / ODD).exists()
    assert (wpkg / FOO_SIBLING).is_file() and (wpkg / NEW).is_file()
    aside = fw.work / "cache" / "quarantine" / "binpkgs" / "v3" / gen
    names = {p.name for p in aside.rglob("*") if p.is_file()}
    assert {Path(FOO_OLD).name, Path(ODD).name} <= names
    # moved BEFORE the worker's emaint: its index -- now the host's -- never names them
    for index in (wpkg / "Packages", _host_pkgdir(fw, gen) / "Packages"):
        text = index.read_text(encoding="utf-8")
        assert FOO_OLD not in text and Path(ODD).name not in text, index
        assert FOO_SIBLING in text
    assert not (_host_pkgdir(fw, gen) / FOO_OLD).exists()
    assert (_host_pkgdir(fw, gen) / FOO_SIBLING).is_file()


def test_a_failed_quarantine_move_on_the_worker_stops_the_pull_before_any_transfer(
    fw: FakeWorker, gen: str, owner: ownership.Owner, tmp_path: Path
) -> None:
    """A move the worker could not make is a SyncError, before the reindex and the
    transfers: an index naming a file the next pull would bring back is worse."""
    _put_binpkgs(fw, gen, FOO_OLD, NEW)
    for pkgdir in (_host_pkgdir(fw, gen), _worker_pkgdir(fw, gen)):
        _list(pkgdir, FOO_OLD)
    fw.fail(r"quarantine|mv ", code=1, stderr="mv: cannot move: Read-only file system\n")
    host_index = _host_pkgdir(fw, gen) / "Packages"
    before = host_index.read_bytes()
    with pytest.raises(Exception) as err:
        _pull(fw, owner, tmp_path)
    assert "quarantine" in str(err.value).lower() or "Read-only" in str(err.value)
    assert fw.calls("emaint") == []
    assert host_index.read_bytes() == before


def test_without_a_quarantine_list_the_pull_is_unchanged_and_with_one_it_is_not(
    fw: FakeWorker, gen: str, owner: ownership.Owner, tmp_path: Path
) -> None:
    """R4.3: no list, no extra command on the worker and the same excludes as before
    this story. Then the same pull with a list does move (the converse half)."""
    _put_binpkgs(fw, gen, FOO_OLD, NEW)
    _pull(fw, owner, tmp_path)
    assert (_worker_pkgdir(fw, gen) / FOO_OLD).is_file()
    assert not any("quarantine" in c for c in fw.ssh_commands())
    assert _pkgdir_fetch_excludes(fw) == [["/Packages"]]

    for pkgdir in (_host_pkgdir(fw, gen), _worker_pkgdir(fw, gen)):
        _list(pkgdir, FOO_OLD)
    _pull(fw, owner, tmp_path)
    assert not (_worker_pkgdir(fw, gen) / FOO_OLD).exists()


@pytest.mark.parametrize(
    ("content", "named"),
    [
        (json.dumps({"version": 1, "paths": ["../../escape/evil-1.gpkg.tar"]}), "evil-1"),
        (json.dumps({"version": 1, "paths": ["/etc/passwd"]}), "/etc/passwd"),
        ("{not json", QUARANTINE),
    ],
    ids=["climbing", "absolute", "corrupt"],
)
def test_an_unsafe_or_corrupt_quarantine_list_stops_the_pull_before_any_transfer(
    fw: FakeWorker, gen: str, owner: ownership.Owner, tmp_path: Path, content: str, named: str
) -> None:
    """R4.4, R7.3: a ``SyncError("quarantine: ...")`` naming the entry (or the file),
    before the worker moves, reindexes or sends anything -- the pull's usual failure."""
    _put_binpkgs(fw, gen, FOO_OLD, NEW)
    host = _host_pkgdir(fw, gen)
    host.mkdir(parents=True, exist_ok=True)
    (host / QUARANTINE).write_text(content, encoding="utf-8")
    index_before = (host / "Packages").read_bytes()
    with pytest.raises(SyncError) as err:
        _pull(fw, owner, tmp_path)
    assert "quarantine" in str(err.value) and named in str(err.value), str(err.value)
    assert fw.rsync_calls() == []
    assert fw.calls("emaint") == []
    assert (_worker_pkgdir(fw, gen) / FOO_OLD).is_file()
    assert (host / "Packages").read_bytes() == index_before
    assert (host / QUARANTINE).read_text(encoding="utf-8") == content


# --- task 4.2: the pull never brings a quarantined path back -------------------------------

BRACKET = "app-misc/ab/a[b]-1.gpkg.tar"  # as an rsync pattern, [b] matches "b"...
BRACKET_TWIN = "app-misc/ab/ab-1.gpkg.tar"  # ...so this innocent file would be excluded
STAR = "app-misc/st/st*-1.gpkg.tar"
STAR_TWIN = "app-misc/st/st-x-1.gpkg.tar"


def _replay(excludes: list[str], root: Path, rels: list[str]) -> set[str]:
    """What a real rsync with ``excludes`` copies out of a tree holding ``rels``."""
    src, dst = root / "src", root / "dst"
    for rel in rels:
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel, encoding="utf-8")
    dst.mkdir(parents=True)
    argv = ["rsync", "-r", *[f"--exclude={e}" for e in excludes], f"{src}/", f"{dst}/"]
    subprocess.run(argv, check=True, capture_output=True)
    return {str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file()}


@pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")
def test_pull_excludes_every_listed_path_literally(
    fw: FakeWorker, gen: str, owner: ownership.Owner, tmp_path: Path
) -> None:
    """R4.2. The excludes the pull really used, replayed with a real rsync: each listed
    PATH is left out as the literal path it is. Hostile halves: a name with rsync glob
    characters must not exclude its innocent look-alike, and must itself stay out."""
    _list(_host_pkgdir(fw, gen), FOO_OLD, BRACKET, STAR)
    _put_binpkgs(fw, gen, NEW, BRACKET_TWIN, STAR_TWIN)
    _pull(fw, owner, tmp_path)

    (excludes,) = _pkgdir_fetch_excludes(fw) or [[]]
    assert "/Packages" in excludes
    copied = _replay(
        excludes,
        tmp_path / "replay",
        [FOO_OLD, BRACKET, STAR, BRACKET_TWIN, STAR_TWIN, NEW, "Packages", "x/Packages"],
    )
    assert copied == {BRACKET_TWIN, STAR_TWIN, NEW, "x/Packages"}
    # end to end: the look-alikes reached the host
    host = _host_pkgdir(fw, gen)
    assert (host / BRACKET_TWIN).is_file() and (host / STAR_TWIN).is_file()


def test_pull_excludes_a_path_the_host_moved_aside(
    fw: FakeWorker, gen: str, owner: ownership.Owner, tmp_path: Path
) -> None:
    """R4.2 end to end: a gc on the host moved FOO_OLD aside; the worker still has it
    (its list was never pushed). The pull must not bring it back."""
    _list(_host_pkgdir(fw, gen), FOO_OLD)
    _put_binpkgs(fw, gen, FOO_OLD, NEW)
    _pull(fw, owner, tmp_path)
    host = _host_pkgdir(fw, gen)
    assert (host / NEW).is_file()
    assert not (host / FOO_OLD).exists()
    excludes = _pkgdir_fetch_excludes(fw)
    assert excludes and any(FOO_OLD in e for e in excludes[0]), excludes
