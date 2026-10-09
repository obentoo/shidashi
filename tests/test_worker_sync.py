"""Cache sync (shidashi/worker.py push_plan, push, pull) against a fake worker.

Real rsync through a fake ssh (tests/_fake_worker.py); the worker's ``/mnt/work`` is
a temporary directory. ``SyncError`` is story 010's transport error (remote.py).
"""

import fnmatch
import grp
import os
import pwd
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, ownership, remote, worker
from tests._fake_worker import FakeWorker, fork_point_names, option_values, seed_host_cache


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    w.commit_recipes()  # the commit a sync resolves its fork-point keys at
    yield w
    w.close()


@pytest.fixture
def gen(fw: FakeWorker) -> str:
    return str(fw.config()["generation"])


@pytest.fixture
def host(fw: FakeWorker) -> dict[str, Path]:
    return seed_host_cache(fw)


def _no_deletes(fw: FakeWorker) -> None:
    for call in fw.rsync_calls():
        assert not any(a.startswith("--delete") for a in call["argv"]), call["argv"]


# --- push_plan (pure) ----------------------------------------------------------------


def test_push_plan_never_names_another_arch_or_generation(gen: str, host: dict[str, Path]) -> None:
    plan = worker.push_plan("v3", gen)
    sources = [h.rstrip("/") for h, _ in plan]
    assert not any("znver5" in s or "arrowlake" in s for s in sources)
    assert str(config.pkgdir("v3")) not in sources  # every generation at once
    assert not any("20250101T000000Z" in s for s in sources)
    assert not any(fnmatch.fnmatch(str(host["other_fork_point"]), s) for s in sources)


def test_push_plan_mirrors_the_arch_subset_under_mnt_work_cache(
    gen: str, host: dict[str, Path]
) -> None:
    plan = worker.push_plan("v3", gen)
    pairs = {h.rstrip("/"): w.rstrip("/") for h, w in plan}
    cache = config.cache_dir()
    for d in (
        config.pkgdir("v3", gen),
        config.distdir(),
        config.ccache_dir(),
        config.sccache_dir(),
        cache / "trees",
        cache / "repos",
    ):
        rel = d.relative_to(cache)
        assert pairs.get(str(d)) == f"/mnt/work/cache/{rel}", (d, plan)
    assert all(w.startswith("/mnt/work/cache") for _, w in plan)
    assert any(fnmatch.fnmatch(str(host["stage3"]), h.rstrip("/")) for h, _ in plan)
    last_host, last_worker = plan[-1]
    assert fnmatch.fnmatch(str(host["fork_point"]), last_host.rstrip("/"))  # fork points last
    assert last_worker.rstrip("/").startswith("/mnt/work/cache/fork-points")


# --- push ----------------------------------------------------------------------------


def test_push_copies_exactly_the_arch_subset(fw: FakeWorker, host: dict[str, Path]) -> None:
    sent = worker.push(fw.remote(), "v3", bwlimit=None)
    w = fw.work / "cache"
    for key in (
        *("binpkg", "distfile", "ccache", "sccache", "tree", "repo", "stage3"),
        *("fork_point", "phase_snapshot", "bootstrap"),
    ):
        rel = host[key].relative_to(fw.host_cache)
        assert (w / rel).is_file(), key
    for key in ("old_gen", "other_arch", "other_fork_point"):
        assert not (w / host[key].relative_to(fw.host_cache)).exists(), key
    assert sent >= 3 * 1024 * 1024  # the two big files went
    _no_deletes(fw)
    fw.assert_pinned()


def test_push_ships_the_venv_without_the_editable_install(
    fw: FakeWorker, host: dict[str, Path]
) -> None:
    worker.push(fw.remote(), "v3", bwlimit=None)
    venv = fw.work / "runtime" / "venv"
    assert (venv / "bin" / "python").is_symlink() or (venv / "bin" / "python").exists()
    shipped = {p.name for p in venv.rglob("*.pth")}
    assert "_virtualenv.pth" in shipped  # a third, unrelated .pth must survive
    for p in venv.rglob("*.pth"):
        assert str(fw.real_repo) not in p.read_text(), p  # nothing points at the host


def test_push_refuses_without_a_work_disk_before_any_transfer(
    fw: FakeWorker, host: dict[str, Path]
) -> None:
    fw.set(mounted=False)
    with pytest.raises(remote.SyncError, match="/mnt/work"):
        worker.push(fw.remote(), "v3", bwlimit=None)
    assert fw.rsync_calls() == []
    assert list(fw.work.iterdir()) == []  # nothing landed on the RAM root


def test_push_caps_every_transfer_with_bwlimit(fw: FakeWorker, host: dict[str, Path]) -> None:
    worker.push(fw.remote(), "v3", bwlimit=800)
    calls = fw.rsync_calls()
    assert calls
    for call in calls:
        assert option_values(call["argv"], "--bwlimit") == ["800"], call["argv"]


def test_push_never_deletes_on_the_worker(fw: FakeWorker, host: dict[str, Path]) -> None:
    extra = fw.put("cache/distfiles/worker-only.tar", b"keep me")
    worker.push(fw.remote(), "v3", bwlimit=None)
    assert extra.read_bytes() == b"keep me"
    _no_deletes(fw)


def test_push_resumes_without_resending_complete_files(
    fw: FakeWorker, host: dict[str, Path]
) -> None:
    # the real transfer, not the dry run that measures it first (its cluster has -n)
    fw.fail(r"rsync --server -[^n ]+ .*fork-points", code=12, stderr="connection reset\n", times=1)
    with pytest.raises(remote.SyncError):
        worker.push(fw.remote(), "v3", bwlimit=None)
    big = fw.work / "cache" / host["distfile"].relative_to(fw.host_cache)
    assert big.is_file()
    inode = big.stat().st_ino
    worker.push(fw.remote(), "v3", bwlimit=None)
    assert big.stat().st_ino == inode  # not sent again
    assert (fw.work / "cache" / host["fork_point"].relative_to(fw.host_cache)).is_file()


# --- pull ----------------------------------------------------------------------------


@pytest.fixture
def results(tmp_path: Path) -> Path:
    return tmp_path / "results"


@pytest.fixture
def owner(fw: FakeWorker) -> ownership.Owner:
    """The caller holds v3's owner lock, as a factory job does (contract C5), at a
    commit the fake checkout has: the pull resolves its fork-point keys there."""
    held = ownership.Owner(
        arch="v3",
        worker=fw.name,
        job="kde",
        commit=fw.head,
        since="2026-10-05T14:02:31Z",
        host_pid=None,
    )
    return ownership.acquire("v3", held)


@pytest.fixture
def produced(fw: FakeWorker, gen: str, host: dict[str, Path]) -> dict[str, Path]:
    """What jobs left on the worker: two jobs whose names share a prefix. The fork
    points are kde's minimal stage, which the host does not have yet (its bootstrap
    checkpoint is the host's, rebuilt)."""
    put = fw.put
    job = {"flavor": "kde", "stage": "minimal"}
    v3, znver5 = fork_point_names(gen, **job), fork_point_names(gen, arch="znver5", **job)
    return {
        "same_binpkg": put(f"cache/binpkgs/v3/{gen}/app-misc/a-1.gpkg.tar", "binpkg\n"),
        "new_binpkg": put(f"cache/binpkgs/v3/{gen}/app-misc/new-2.gpkg.tar", "new\n"),
        "other_arch": put(f"cache/binpkgs/znver5/{gen}/zz-1.gpkg.tar", "z\n"),
        "fork_point": put(f"cache/fork-points/{v3['stage']}", "fp\n"),
        "phase_snapshot": put(f"cache/fork-points/{v3['phase_snapshot']}", "fp\n"),
        "bootstrap": put(f"cache/fork-points/{v3['bootstrap']}", "fp\n"),
        "other_fork_point": put(f"cache/fork-points/{znver5['stage']}", "fp\n"),
        "distfile": put("cache/distfiles/new-1.0.tar.gz", "src\n"),
        "ccache": put("cache/ccache/1/new-entry", "cc\n"),
        "run": put("out/runs/20261005T110000Z-abc123/events.jsonl", "{}\n"),
        "runs_list": put("out/jobs/kde.runs", "20261005T110000Z-abc123\n"),
        "older_run": put("out/runs/20261004T090000Z-old000/events.jsonl", "{}\n"),
        "iso": put("out/iso/kde/bentoo-kde.iso", "ISO kde\n"),
        "other_iso": put("out/iso/kde-v3/bentoo-kde-v3.iso", "ISO kde-v3\n"),
    }


def test_pull_brings_only_this_jobs_isos(
    fw: FakeWorker, produced: dict[str, Path], results: Path
) -> None:
    worker.pull(fw.remote(), "v3", "kde", results=results)
    names = {p.name for p in results.rglob("*") if p.is_file()}
    assert "bentoo-kde.iso" in names
    assert "bentoo-kde-v3.iso" not in names


def test_pull_brings_only_this_archs_binpkgs_and_fork_points(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    assert not (fw.host_cache / "binpkgs" / "znver5" / gen / "zz-1.gpkg.tar").exists()
    assert not (fw.host_cache / "fork-points" / produced["other_fork_point"].name).exists()


def test_pull_brings_back_binpkgs_fork_points_caches_runs_and_isos(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    received = worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    c = fw.host_cache
    assert (c / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").is_file()
    for key in ("fork_point", "phase_snapshot", "bootstrap"):
        assert (c / "fork-points" / produced[key].name).read_text() == "fp\n", key
    assert (c / "distfiles" / "new-1.0.tar.gz").is_file()
    assert (c / "ccache" / "1" / "new-entry").is_file()
    assert (config.runs_dir() / "20261005T110000Z-abc123" / "events.jsonl").is_file()
    assert any(p.name == "bentoo-kde.iso" for p in results.rglob("*"))
    assert received.bytes > 0 and received.binhost  # it held the lock
    fw.assert_pinned()


def test_pull_regenerates_the_index_on_the_worker_before_any_transfer(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    calls = fw.calls()
    emaint = [i for i, c in enumerate(calls) if c["kind"] == "emaint"]
    assert emaint, "the index was not regenerated on the worker"
    assert calls[emaint[0]]["pkgdir"].rstrip("/") == str(fw.work / "cache/binpkgs/v3" / gen)
    first_rsync = min(i for i, c in enumerate(calls) if c["kind"] == "rsync")
    assert emaint[0] < first_rsync


def test_pull_replaces_the_hosts_index_atomically_with_the_workers(
    fw: FakeWorker,
    produced: dict[str, Path],
    results: Path,
    owner: ownership.Owner,
    gen: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = fw.host_cache / "binpkgs" / "v3" / gen / "Packages"
    new = index.parent / "app-misc" / "new-2.gpkg.tar"
    replaced: list[tuple[str, str, bool]] = []
    real = os.replace

    def spy(src: Any, dst: Any, **kw: Any) -> None:
        replaced.append((str(src), str(dst), new.exists()))
        real(src, dst, **kw)

    monkeypatch.setattr(os, "replace", spy)
    worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    onto_index = [r for r in replaced if r[1] == str(index)]
    assert onto_index, replaced
    src, _, binpkgs_there = onto_index[-1]
    assert Path(src).parent == index.parent and Path(src) != index  # a temp sibling
    assert binpkgs_there  # after the binpkgs came
    worker_index = (fw.work / "cache/binpkgs/v3" / gen / "Packages").read_text()
    assert index.read_text() == worker_index  # replaced, never merged
    assert "host-only-entry" not in index.read_text()


def test_pull_never_deletes_on_the_host(
    fw: FakeWorker, produced: dict[str, Path], results: Path
) -> None:
    keep = fw.host_cache / "distfiles" / "host-only.tar"
    keep.write_text("keep")
    worker.pull(fw.remote(), "v3", "kde", results=results)
    assert keep.read_text() == "keep"
    _no_deletes(fw)


def test_pull_into_a_missing_but_creatable_pkgdir_is_not_refused(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    import shutil

    shutil.rmtree(fw.host_cache / "binpkgs" / "v3" / gen)
    worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    assert (fw.host_cache / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").is_file()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_pull_refuses_an_unwritable_host_directory_before_any_transfer(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    pkgdir = fw.host_cache / "binpkgs" / "v3" / gen
    pkgdir.chmod(0o555)
    try:
        with pytest.raises(remote.SyncError) as err:
            worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    finally:
        pkgdir.chmod(0o755)
    text = str(err.value)
    assert str(pkgdir) in text
    assert pwd.getpwuid(pkgdir.stat().st_uid).pw_name in text
    assert "chgrp" in text or "chmod" in text
    assert fw.rsync_calls() == []


def test_pull_leaves_the_hosts_index_untouched_when_the_worker_index_fails(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    fw.fail(r"emaint", code=1, stderr="emaint: corrupt binpkg\n")
    index = fw.host_cache / "binpkgs" / "v3" / gen / "Packages"
    before = index.read_text()
    with pytest.raises(remote.SyncError):
        worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    assert index.read_text() == before
    assert not (index.parent / "app-misc" / "new-2.gpkg.tar").exists()


def test_pull_leaves_the_hosts_index_untouched_when_a_transfer_fails(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    fw.fail(r"rsync --server --sender", code=12, stderr="connection reset\n")
    index = fw.host_cache / "binpkgs" / "v3" / gen / "Packages"
    before = index.read_text()
    with pytest.raises(remote.SyncError):
        worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    assert index.read_text() == before


# --- the pull without the lock (contract C5, R6.7, R6.8) -----------------------------


def test_pull_without_the_lock_leaves_the_binhost_alone_and_brings_the_rest(
    fw: FakeWorker, produced: dict[str, Path], results: Path, gen: str
) -> None:
    fw.put("out/jobs/kde.log", "the job's log\n")
    fw.put("out/jobs/kde.rc", "0\n")
    index = fw.host_cache / "binpkgs" / "v3" / gen / "Packages"
    before = index.read_text()
    got = worker.pull(fw.remote(), "v3", "kde", results=results)
    assert got.binhost is False  # said so
    assert index.read_text() == before  # never replaced without the lock
    c = fw.host_cache
    assert not (c / "binpkgs" / "v3" / gen / "app-misc" / "new-2.gpkg.tar").exists()
    assert not (c / "fork-points" / produced["fork_point"].name).exists()
    assert not fw.calls("emaint")  # the worker's index is not even regenerated
    assert (c / "distfiles" / "new-1.0.tar.gz").is_file()  # the caches still come
    assert (config.runs_dir() / "20261005T110000Z-abc123" / "events.jsonl").is_file()
    assert any(p.name == "bentoo-kde.iso" for p in got.isos)
    assert got.log is not None and got.log.read_text() == "the job's log\n"
    assert got.run_ids == ("20261005T110000Z-abc123",)  # exactly this job's runs
    assert not (config.runs_dir() / "20261004T090000Z-old000").exists()  # not an older one


def test_pull_as_an_owner_that_does_not_hold_the_lock_is_refused(
    fw: FakeWorker, produced: dict[str, Path], results: Path, gen: str
) -> None:
    other = ownership.Owner(
        arch="v3",
        worker="other-box",
        job="fac-v3",
        commit="d" * 40,
        since="2026-10-05T14:02:31Z",
        host_pid=None,
    )
    ownership.acquire("v3", other)
    claimed = other.model_copy(update={"worker": fw.name, "job": "kde"})
    index = fw.host_cache / "binpkgs" / "v3" / gen / "Packages"
    before = index.read_text()
    with pytest.raises(ownership.OwnedElsewhere):
        worker.pull(fw.remote(), "v3", "kde", results=results, owner=claimed)
    assert index.read_text() == before


def test_push_refuses_a_venv_whose_interpreter_the_worker_lacks(
    fw: FakeWorker, host: dict[str, Path]
) -> None:
    (fw.venv / "pyvenv.cfg").write_text("home = /nonexistent/python-home\nversion_info = 3.14\n")
    with pytest.raises(remote.SyncError) as err:
        worker.push(fw.remote(), "v3", bwlimit=None)
    assert "/nonexistent/python-home" in str(err.value)
    assert not any("runtime/venv" in " ".join(c["argv"]) for c in fw.rsync_calls())


# --- regressions from the 4.1/4.2 review ---------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_pull_refuses_a_read_only_leaf_inside_a_cache_before_any_transfer(
    fw: FakeWorker, produced: dict[str, Path], results: Path
) -> None:
    """A root build leaves ccache buckets 2755: rsync's temp file fails there midway."""
    leaf = fw.host_cache / "ccache" / "f"
    leaf.mkdir(parents=True)
    leaf.chmod(0o555)
    try:
        with pytest.raises(remote.SyncError) as err:
            worker.pull(fw.remote(), "v3", "kde", results=results)
    finally:
        leaf.chmod(0o755)
    text = str(err.value)
    assert str(leaf) in text
    assert grp.getgrgid(leaf.stat().st_gid).gr_name in text  # the directory's own group
    assert f"chmod -R u+w {fw.host_cache / 'ccache'}" in text
    assert fw.rsync_calls() == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
@pytest.mark.parametrize("member", [True, False])
def test_pull_refusal_keeps_the_directorys_group_when_the_user_is_in_it(
    fw: FakeWorker,
    produced: dict[str, Path],
    results: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: bool,
) -> None:
    leaf = fw.host_cache / "ccache" / "f"
    leaf.mkdir(parents=True)
    leaf.chmod(0o555)
    st = leaf.stat()
    group = grp.getgrgid(st.st_gid).gr_name
    # someone else owns the leaf; this user is, or is not, in its group
    monkeypatch.setattr(os, "getuid", lambda: st.st_uid + 1)
    monkeypatch.setattr(os, "getgroups", lambda: [st.st_gid] if member else [])
    monkeypatch.setattr(os, "getegid", lambda: st.st_gid if member else st.st_gid + 1)
    monkeypatch.setattr(os, "getgid", lambda: st.st_gid if member else st.st_gid + 1)
    try:
        with pytest.raises(remote.SyncError) as err:
            worker.pull(fw.remote(), "v3", "kde", results=results)
    finally:
        monkeypatch.undo()
        leaf.chmod(0o755)
    text = str(err.value)
    assert f"chmod -R g+w {fw.host_cache / 'ccache'}" in text
    assert f":{group}," in text
    assert ("chgrp" in text) is (not member)  # chgrp only when not in that group


def test_pull_sets_no_attributes_on_host_directories(
    fw: FakeWorker, produced: dict[str, Path], results: Path, owner: ownership.Owner
) -> None:
    """Directories of the host cache belong to root's builds: a pull adds files to
    them and never sets their times, permissions or group."""
    ccache = fw.host_cache / "ccache"
    ccache.chmod(0o2775)
    (fw.work / "cache" / "ccache").chmod(0o700)
    worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    assert stat.S_IMODE(ccache.stat().st_mode) == 0o2775
    calls = fw.rsync_calls()
    assert calls
    for call in calls:
        argv = call["argv"]
        assert {"--omit-dir-times", "--no-perms", "--no-group"} <= set(argv), argv
        assert "--partial-dir=.rsync-partial" in argv and "--partial" not in argv, argv


def test_an_interrupted_pull_leaves_no_truncated_file_under_its_final_name(
    fw: FakeWorker, tmp_path: Path
) -> None:
    """The host trusts a fork point by its existence: an interrupted pull must keep
    the partial file aside, never under the final name."""
    fw.put("cache/fork-points/big.tar", os.urandom(4 * 1024 * 1024))
    dest = tmp_path / "fork-points"
    argv = remote.rsync_argv(
        fw.remote(), ["/mnt/work/cache/fork-points/"], f"{dest}/", push=False, bwlimit=64
    )
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if dest.is_dir() and any(p.name.startswith(".big.tar") for p in dest.iterdir()):
                break
            time.sleep(0.05)
        else:
            pytest.fail("the transfer never started")
        time.sleep(0.3)  # some bytes land in the temp file
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    assert not (dest / "big.tar").exists()
    assert (dest / ".rsync-partial" / "big.tar").is_file()


def test_pull_of_a_factory_that_built_nothing_succeeds_without_the_binhost(
    fw: FakeWorker, host: dict[str, Path], results: Path, owner: ownership.Owner, gen: str
) -> None:
    """A fresh generation and a factory that built nothing: no PKGDIR on the worker.
    The pull must still succeed (so the lock can be released), saying why."""
    fork_point = fork_point_names(gen)["stage"]
    fw.put(f"cache/fork-points/{fork_point}", "fp\n")
    fw.put("out/jobs/kde.rc", "0\n")
    index = fw.host_cache / "binpkgs" / "v3" / gen / "Packages"
    before = index.read_text()
    got = worker.pull(fw.remote(), "v3", "kde", results=results, owner=owner)
    assert got.binhost is False
    assert got.binhost_reason and f"binpkgs/v3/{gen}" in got.binhost_reason
    assert not fw.calls("emaint")
    assert index.read_text() == before
    assert (fw.host_cache / "fork-points" / fork_point).is_file()
    assert (results / "kde.rc").read_text() == "0\n"


def test_pull_without_the_lock_says_why_the_binhost_stayed(
    fw: FakeWorker, produced: dict[str, Path], results: Path
) -> None:
    got = worker.pull(fw.remote(), "v3", "kde", results=results)
    assert got.binhost is False and got.binhost_reason and "lock" in got.binhost_reason


def test_pull_lists_only_the_isos_the_worker_has_now(
    fw: FakeWorker, produced: dict[str, Path], results: Path
) -> None:
    stale = results / "iso" / "bentoo-kde-yesterday.iso"
    stale.parent.mkdir(parents=True)
    stale.write_text("an earlier run's ISO")
    leftover = results / "iso" / ".rsync-partial" / "bentoo-kde.iso"
    leftover.parent.mkdir()
    leftover.write_text("ISO")  # an interrupted earlier pull
    got = worker.pull(fw.remote(), "v3", "kde", results=results)
    assert [p.name for p in got.isos] == ["bentoo-kde.iso"]
    assert got.isos[0] == results / "iso" / "bentoo-kde.iso"
    assert got.isos[0].read_text() == "ISO kde\n"


@pytest.mark.parametrize("arch", ["*", "v3*", "../v3", "nope"])
def test_push_and_pull_refuse_an_unknown_arch_before_any_contact(
    fw: FakeWorker, host: dict[str, Path], results: Path, arch: str
) -> None:
    with pytest.raises(ValueError, match="arch"):
        worker.push(fw.remote(), arch, bwlimit=None)
    with pytest.raises(ValueError, match="arch"):
        worker.pull(fw.remote(), arch, "kde", results=results)
    assert fw.ssh_commands() == [] and fw.rsync_calls() == []
