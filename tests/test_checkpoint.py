"""Tests of shidashi.checkpoint — the assemble's btrfs checkpoints.

The pure parts (fingerprints, command lines) directly; the store's logic over a
directory-copy backend (tests/_fake_btrfs.py), without root and without btrfs.
"""

import json
from pathlib import Path

import pytest

from shidashi import checkpoint
from shidashi.checkpoint import (
    INSTALL,
    PACKAGES,
    PARTIAL,
    Fingerprints,
    Store,
    fingerprint,
    has_resume_list,
    resume_point,
    snapshot_argv,
    subvolume_create_argv,
    subvolume_delete_argv,
    tree_digest,
)
from tests._fake_btrfs import FakeBackend

# --- fingerprints (PURE / small I/O) ------------------------------------------


def test_a_fingerprint_is_stable_and_order_insensitive_for_mappings() -> None:
    assert fingerprint("a", {"x": 1, "y": 2}) == fingerprint("a", {"y": 2, "x": 1})
    assert fingerprint("a", [1, 2]) != fingerprint("a", [2, 1])
    assert fingerprint("install", "x") != fingerprint("packages", "x")


def test_tree_digest_sees_content_mode_and_names_but_skips_what_it_is_told(
    tmp_path: Path,
) -> None:
    layer = tmp_path / "layer"
    (layer / "portage").mkdir(parents=True)
    conf = layer / "portage" / "make.conf"
    conf.write_text('USE="a"\n')
    (layer / "system.yaml").write_text("hostname: one\n")
    skip = frozenset({"system.yaml"})
    first = tree_digest([layer], skip=skip)

    (layer / "system.yaml").write_text("hostname: two\n")
    assert tree_digest([layer], skip=skip) == first  # skipped: no effect

    conf.write_text('USE="b"\n')
    changed = tree_digest([layer], skip=skip)
    assert changed != first  # content

    conf.chmod(0o600)
    assert tree_digest([layer], skip=skip) != changed  # mode


def test_tree_digest_ignores_the_roots_name_but_not_their_order(tmp_path: Path) -> None:
    # a rendered configuration lands in a temporary directory with a random name
    for name in ("one", "two"):
        (tmp_path / name / "etc").mkdir(parents=True)
        (tmp_path / name / "etc" / "f").write_text("same")
    assert tree_digest([tmp_path / "one"]) == tree_digest([tmp_path / "two"])
    (tmp_path / "two" / "etc" / "g").write_text("more")
    one, two = tmp_path / "one", tmp_path / "two"
    assert tree_digest([one, two]) != tree_digest([two, one])
    assert tree_digest([tmp_path / "absent"]) == tree_digest([tmp_path / "missing"])


def test_the_btrfs_command_lines() -> None:
    assert subvolume_create_argv(Path("/s/r")) == ["btrfs", "-q", "subvolume", "create", "/s/r"]
    assert snapshot_argv(Path("/s/r"), Path("/s/c/install"), readonly=True) == [
        "btrfs",
        "-q",
        "subvolume",
        "snapshot",
        "-r",
        "/s/r",
        "/s/c/install",
    ]
    assert "-r" not in snapshot_argv(Path("/a"), Path("/b"), readonly=False)
    assert subvolume_delete_argv(Path("/s/r")) == ["btrfs", "-q", "subvolume", "delete", "/s/r"]


def _resume_list(rootfs: Path, mergelist: list[object]) -> None:
    edb = rootfs / "var" / "cache" / "edb"
    edb.mkdir(parents=True, exist_ok=True)
    (edb / "mtimedb").write_text(json.dumps({"resume": {"mergelist": mergelist}}))


def test_has_resume_list_needs_a_non_empty_mergelist(tmp_path: Path) -> None:
    assert not has_resume_list(tmp_path)
    _resume_list(tmp_path, [])
    assert not has_resume_list(tmp_path)
    _resume_list(tmp_path, [["binary", "/", "app-misc/b-1", "merge"]])
    assert has_resume_list(tmp_path)


def test_open_store_is_off_outside_btrfs(tmp_path: Path) -> None:
    # tests/conftest.py makes every filesystem "tmpfs"
    assert checkpoint.open_store(tmp_path / "ckpt") is None
    store = checkpoint.open_store(tmp_path / "ckpt", backend=FakeBackend())
    assert store is not None and store.root.is_dir()


# --- the store over the fake backend -------------------------------------------


def _rootfs(tmp_path: Path, marker: str) -> Path:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir(exist_ok=True)
    (rootfs / "marker").write_text(marker)
    return rootfs


def _store(tmp_path: Path) -> tuple[Store, FakeBackend]:
    backend = FakeBackend()
    return Store(tmp_path / "ckpt", backend), backend


def test_save_find_restore_round_trip(tmp_path: Path) -> None:
    store, backend = _store(tmp_path)
    rootfs = _rootfs(tmp_path, "installed")
    saved = store.save(rootfs, INSTALL, "fp1", image="minimal", run_id="run-a", data={"since": 7})

    assert saved.path in backend.readonly  # frozen read-only
    assert store.find(INSTALL, "other") is None  # another fingerprint: never reused
    mark = store.find(INSTALL, "fp1")
    assert mark is not None and mark.run_id == "run-a" and mark.data["since"] == 7
    assert mark.images == ("minimal",)

    (rootfs / "marker").write_text("broken later")
    store.restore(mark, rootfs)
    assert (rootfs / "marker").read_text() == "installed"
    assert rootfs in backend.subvolumes and rootfs not in backend.readonly  # writable copy


def test_a_snapshot_without_its_manifest_is_never_trusted(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    mark = store.save(_rootfs(tmp_path, "x"), INSTALL, "fp", image="a", run_id=None)
    mark.path.with_name(mark.path.name + ".json").unlink()
    assert store.find(INSTALL, "fp") is None


def test_an_older_format_is_never_reused(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    mark = store.save(_rootfs(tmp_path, "x"), INSTALL, "fp", image="a", run_id=None)
    manifest = mark.path.with_name(mark.path.name + ".json")
    raw = json.loads(manifest.read_text())
    raw["format"] = checkpoint.FORMAT - 1
    manifest.write_text(json.dumps(raw))
    assert store.find(INSTALL, "fp") is None


def test_two_images_with_the_same_inputs_share_one_snapshot(tmp_path: Path) -> None:
    store, backend = _store(tmp_path)
    rootfs = _rootfs(tmp_path, "same")
    store.save(rootfs, INSTALL, "fp", image="minimal", run_id="r1")
    shared = store.save(rootfs, INSTALL, "fp", image="worker", run_id="r2")
    assert shared.images == ("minimal", "worker")
    assert shared.run_id == "r1"  # claimed, not frozen twice
    assert [c for c in backend.calls if c[0] == "snapshot-ro"] == [("snapshot-ro", shared.path)]


def test_an_image_replaces_only_its_own_older_checkpoint(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    rootfs = _rootfs(tmp_path, "r")
    store.save(rootfs, INSTALL, "old", image="minimal", run_id=None)
    store.save(rootfs, INSTALL, "old", image="worker", run_id=None)

    store.save(rootfs, INSTALL, "new", image="minimal", run_id=None)
    old = store.find(INSTALL, "old")
    assert old is not None and old.images == ("worker",)  # still used by worker

    store.save(rootfs, INSTALL, "new", image="worker", run_id=None)
    assert store.find(INSTALL, "old") is None  # no image uses it: gone
    new = store.find(INSTALL, "new")
    assert new is not None and new.images == ("minimal", "worker")


def test_release_drops_an_image_everywhere(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    rootfs = _rootfs(tmp_path, "r")
    store.save(rootfs, INSTALL, "i", image="kde", run_id=None)
    store.save(rootfs, PACKAGES, "p", image="kde", run_id=None)
    store.save(rootfs, PACKAGES, "p", image="gnome", run_id=None)
    store.release("kde")
    assert store.find(INSTALL, "i") is None
    shared = store.find(PACKAGES, "p")
    assert shared is not None and shared.images == ("gnome",)
    store.release("gnome")
    assert store.marks() == []
    assert sorted(p.name for p in (tmp_path / "ckpt").iterdir()) == []


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ((INSTALL,), INSTALL),
        ((INSTALL, PACKAGES), PACKAGES),  # the deepest wins
        ((), None),
    ],
)
def test_resume_point_prefers_the_deepest_valid_checkpoint(
    tmp_path: Path, saved: tuple[str, ...], expected: str | None
) -> None:
    store, _ = _store(tmp_path)
    fps = Fingerprints(install="i", packages="p", partial="x")
    rootfs = _rootfs(tmp_path, "r")
    for step in saved:
        fp = fps.install if step == INSTALL else fps.packages
        store.save(rootfs, step, fp, image="a", run_id=None)
    mark = resume_point(store, fps)
    assert (mark.step if mark else None) == expected


def test_a_stale_install_falls_through_to_nothing(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.save(_rootfs(tmp_path, "r"), INSTALL, "old", image="a", run_id=None)
    assert resume_point(store, Fingerprints(install="new", packages="p", partial="x")) is None


def test_a_partial_install_counts_only_with_portages_resume_list(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    fps = Fingerprints(install="i", packages="p", partial="x")
    rootfs = _rootfs(tmp_path, "half")
    store.save(rootfs, PARTIAL, "x", image="a", run_id=None)
    assert resume_point(store, fps) is None  # nothing was merged: start over

    store.release("a")
    _resume_list(rootfs, [["binary", "/", "app-misc/b-1", "merge"]])
    store.save(rootfs, PARTIAL, "x", image="a", run_id=None)
    mark = resume_point(store, fps)
    assert mark is not None and mark.step == PARTIAL


def test_retain_lets_go_of_what_is_no_longer_current(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    rootfs = _rootfs(tmp_path, "r")
    store.save(rootfs, INSTALL, "old", image="minimal", run_id=None)
    store.save(rootfs, PACKAGES, "shared", image="worker", run_id=None)
    store.save(rootfs, PACKAGES, "shared", image="minimal", run_id=None)
    store.save(rootfs, INSTALL, "kept-by-worker", image="worker", run_id=None)
    store.save(rootfs, INSTALL, "kept-by-worker", image="minimal", run_id=None)

    store.retain("minimal", ["shared"])
    assert store.find(INSTALL, "old") is None  # minimal's own, stale: deleted
    survivor = store.find(INSTALL, "kept-by-worker")
    assert survivor is not None and survivor.images == ("worker",)  # worker still uses it
    shared = store.find(PACKAGES, "shared")
    assert shared is not None and shared.images == ("worker", "minimal")


# --- the resolver's plan and the binhost slice ---------------------------------


def test_plan_tokens_keep_the_binpkg_identity_only() -> None:
    output = (
        ">>> Verifying ebuild manifests\n"
        "[binary     N    ] acct-group/tss-0-r3::gentoo  0 KiB\n"
        '[binary   R    ] dev-lang/python-3.14.7-1:3.14::gentoo  USE="-bluetooth*" 0 KiB\n'
        "[ebuild  N     ] app-misc/built-1::gentoo\n"
    )
    assert checkpoint.plan_tokens(output) == (
        "acct-group/tss-0-r3::gentoo",
        "dev-lang/python-3.14.7-1:3.14::gentoo",
    )
    assert checkpoint.token_cpv("dev-lang/python-3.14.7-1:3.14::gentoo") == "dev-lang/python-3.14.7"
    assert checkpoint.token_cpv("acct-group/tss-0-r3::gentoo") == "acct-group/tss-0-r3"


def test_binhost_slice_sees_only_the_packages_asked_for(tmp_path: Path) -> None:
    index = tmp_path / "Packages"

    def write(python_build: str, other: str) -> None:
        index.write_text(
            "PACKAGES: 2\n\n"
            f"BUILD_ID: {python_build}\nCPV: dev-lang/python-3.14.7\nSHA1: x{python_build}\n\n"
            f"CPV: kde-plasma/plasma-desktop-6.5\nSHA1: {other}\n"
        )

    write("1", "a")
    first = checkpoint.binhost_slice(index, {"dev-lang/python-3.14.7"})
    write("1", "b")  # an unrelated package changed
    assert checkpoint.binhost_slice(index, {"dev-lang/python-3.14.7"}) == first
    write("2", "b")  # python rebuilt
    assert checkpoint.binhost_slice(index, {"dev-lang/python-3.14.7"}) != first
    assert checkpoint.binhost_slice(tmp_path / "absent", {"x/y-1"}) is None


def test_prune_removes_other_formats_and_orphan_snapshots(tmp_path: Path) -> None:
    store, backend = _store(tmp_path)
    rootfs = _rootfs(tmp_path, "r")
    kept = store.save(rootfs, INSTALL, "current", image="a", run_id=None)
    old = store.save(rootfs, PACKAGES, "old", image="a", run_id=None)
    manifest = old.path.with_name(old.path.name + ".json")
    raw = json.loads(manifest.read_text())
    raw["format"] = checkpoint.FORMAT - 1
    manifest.write_text(json.dumps(raw))
    orphan = tmp_path / "ckpt" / "install-deadbeef"
    backend.snapshot(rootfs, orphan, readonly=True)  # a save interrupted mid-way

    removed = store.prune()
    assert sorted(removed) == sorted([old.path.name, orphan.name])
    assert not old.path.exists() and not orphan.exists()
    assert store.find(INSTALL, "current") == kept
