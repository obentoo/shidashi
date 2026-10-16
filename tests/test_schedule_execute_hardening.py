"""Hardening of shidashi.schedule.execute: the root parent touching user-owned files.

Under ``sudo`` the worker child runs as the invoking user, so everything under
``<scratch>/worker-results/<worker>/<job>/`` -- and the output directory -- is theirs to
rearrange while the root parent works on it (tech review of 4.1, findings 1-5 and 7):

- a source in ``results/iso/`` must be a regular file: a symlink is refused, never moved,
  chowned or hashed through;
- a destination already in the output directory that is a symlink or a directory is
  refused; across filesystems the copy never opens a planted name;
- a worker node name that is no host name gets no child and no directory;
- ``worker-results/`` and ``worker-results/<worker>/`` stay owned like the scratch
  directory; only the ``<job>`` leaf goes to the user, through a no-follow descriptor;
- the published SHA256SUMS line is the host's own hash; a worker line contradicting it
  fails the image;
- a worker job that ended on its own before the signal is not "left running".
"""

import dataclasses
import errno
import hashlib
import os
import pwd
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shidashi import parallel, schedule
from tests import test_schedule_execute as base
from tests.test_schedule_execute import (
    JOB,
    LAB,
    LAB2,
    RUN_ID,
    SPLIT,
    Lab,
    _estimates,
    _host_child,
    _is_worker_child,
    _iso_name,
    _options,
    _outcome,
    _plan,
    _runner,
    _simulate_worker_job,
    _specs,
)

lab = base.lab  # the fixture, shared: every test here runs in the same fake lab

FAKE_UID = 4242  # a user this test process cannot chown to: a recorded call, an EPERM


@dataclass(frozen=True)
class Chown:
    how: str  # "chown", "lchown" or "fchown"
    path: str  # the path named, or the descriptor's path
    target: str  # what the call reaches: the path with symlinks followed when they are
    uid: int
    gid: int


@pytest.fixture
def chowns(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> list[Chown]:
    """Every os.chown / os.lchown / os.fchown, with what it reached (passed through)."""
    calls: list[Chown] = []
    real_chown, real_lchown, real_fchown = os.chown, os.lchown, os.fchown

    def chown(path: Any, uid: int, gid: int, *a: Any, **k: Any) -> None:
        follow = k.get("follow_symlinks", True)
        target = os.path.realpath(path) if follow else os.path.abspath(path)
        calls.append(Chown("chown", str(path), target, uid, gid))
        real_chown(path, uid, gid, *a, **k)

    def lchown(path: Any, uid: int, gid: int) -> None:
        calls.append(Chown("lchown", str(path), os.path.abspath(path), uid, gid))
        real_lchown(path, uid, gid)

    def fchown(fd: int, uid: int, gid: int) -> None:
        where = os.readlink(f"/proc/self/fd/{fd}")
        calls.append(Chown("fchown", where, where, uid, gid))
        real_fchown(fd, uid, gid)

    monkeypatch.setattr(os, "chown", chown)
    monkeypatch.setattr(os, "lchown", lchown)
    monkeypatch.setattr(os, "fchown", fchown)
    return calls


def _results(lab: Lab, worker: str = LAB) -> Path:
    return lab.scratch / "worker-results" / worker / JOB


def _then(lab: Lab, after: Any) -> Any:
    """The fake runner, then ``after()`` -- what the user does once the job has ended."""
    inner = _runner(lab)

    def run(argvs: dict[str, list[str]], *a: object, **kw: Any) -> list[parallel.ChildResult]:
        results = inner(argvs, *a, **kw)
        after()
        return results

    return run


def _execute(
    lab: Lab, where: dict[str, str] = SPLIT, runner: Any = None, **over: Any
) -> list[schedule.Outcome]:
    return schedule.execute(
        _plan(where),
        _specs(),
        _estimates(where),
        options=_options(lab, **over),
        logs=lab.logs,
        run_id=RUN_ID,
        runner=runner if runner is not None else _runner(lab),
        host="dojo",
    )


def _fake_sudo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uid: int, gid: int) -> None:
    home = tmp_path / "home-alice"
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    real = pwd.getpwnam
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda name: (
            pwd.struct_passwd(("alice", "x", uid, gid, "", str(home), "/bin/bash"))
            if name == "alice"
            else real(name)
        ),
    )


# --- finding 1: a source in results/iso/ is a regular file or nothing -----------------------


@pytest.mark.parametrize("suffix", ["", ".DIGESTS"], ids=["iso", "companion"])
def test_a_symlinked_source_is_refused_and_its_target_left_alone(
    lab: Lab, chowns: list[Chown], tmp_path: Path, suffix: str
) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"VICTIM")
    before = victim.stat()
    name = _iso_name("gnome") + suffix

    def plant() -> None:
        link = _results(lab) / "iso" / name
        link.unlink()
        link.symlink_to(victim)

    outcomes = _execute(lab, runner=_then(lab, plant))
    gnome = _outcome(outcomes, "gnome")
    assert not gnome.ok and name in gnome.error, gnome.error
    assert not (lab.out / name).is_symlink() and not (lab.out / name).exists()
    assert victim.read_bytes() == b"VICTIM"
    after = victim.stat()
    assert (after.st_uid, after.st_gid, after.st_ino) == (
        before.st_uid,
        before.st_gid,
        before.st_ino,
    )
    assert not [c for c in chowns if c.target == str(victim)], chowns
    assert _outcome(outcomes, "wm").ok  # the sibling is not judged by gnome's files


def test_a_symlinked_iso_dir_is_not_followed(lab: Lab, tmp_path: Path) -> None:
    # the user owns results/<job>/: `iso` itself may be swapped for a link elsewhere
    elsewhere = tmp_path / "elsewhere"

    def plant() -> None:
        iso_dir = _results(lab) / "iso"
        iso_dir.rename(elsewhere)
        iso_dir.symlink_to(elsewhere)

    outcomes = _execute(lab, runner=_then(lab, plant))
    assert not _outcome(outcomes, "gnome").ok and not _outcome(outcomes, "wm").ok
    assert (elsewhere / _iso_name("gnome")).is_file()  # nothing was taken through the link
    assert not (lab.out / _iso_name("gnome")).exists()


# --- finding 2: the destination in the user's output directory -----------------------------


def test_a_planted_symlink_to_a_dir_at_the_destination_is_refused(
    lab: Lab, chowns: list[Chown], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    name = _iso_name("gnome")
    (lab.out / name).symlink_to(elsewhere)
    outcomes = _execute(lab)
    gnome = _outcome(outcomes, "gnome")
    assert not gnome.ok and name in gnome.error, gnome.error
    assert list(elsewhere.iterdir()) == []
    assert not [c for c in chowns if c.target.startswith(str(elsewhere))], chowns
    assert (_results(lab) / "iso" / name).is_file()  # left where the worker put it


def test_a_planted_directory_at_the_destination_is_refused(lab: Lab) -> None:
    name = _iso_name("gnome") + ".packages"
    (lab.out / name).mkdir()
    gnome = _outcome(_execute(lab), "gnome")
    assert not gnome.ok and name in gnome.error, gnome.error
    assert list((lab.out / name).iterdir()) == []


def _cross_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every rename between two directories fails with EXDEV, as across filesystems."""
    real_replace = os.replace

    def exdev(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

    def replace(src: Any, dst: Any, *, src_dir_fd: Any = None, dst_dir_fd: Any = None) -> None:
        if src_dir_fd != dst_dir_fd or (
            src_dir_fd is None and Path(src).parent != Path(dst).parent
        ):
            exdev()
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", exdev)
    monkeypatch.setattr(os, "replace", replace)


def test_across_filesystems_a_planted_file_symlink_is_never_written_through(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL\n")
    name = _iso_name("gnome") + ".packages"
    (lab.out / name).symlink_to(victim)
    _cross_device(monkeypatch)
    gnome = _outcome(_execute(lab), "gnome")
    assert victim.read_text() == "ORIGINAL\n"
    assert not gnome.ok and name in gnome.error, gnome.error


def test_across_filesystems_results_are_copied_owned_and_summed(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, chowns: list[Chown]
) -> None:
    _cross_device(monkeypatch)
    outcomes = _execute(lab)
    assert all(o.ok for o in outcomes), [(o.image, o.error) for o in outcomes]
    iso = lab.out / _iso_name("gnome")
    assert iso.read_bytes() == f"{LAB}-gnome".encode()
    assert stat.S_IMODE(iso.stat().st_mode) == 0o644
    want = (lab.out.stat().st_uid, lab.out.stat().st_gid)
    assert (iso.stat().st_uid, iso.stat().st_gid) == want
    assert not (_results(lab) / "iso" / iso.name).exists()  # the source is gone
    assert not [p.name for p in lab.out.iterdir() if p.name.endswith(".tmp")]
    digest = hashlib.sha256(iso.read_bytes()).hexdigest()
    assert f"{digest}  {iso.name}" in (lab.out / "SHA256SUMS").read_text().splitlines()
    assert all(c.how == "fchown" for c in chowns), chowns


# --- finding 3: worker node names --------------------------------------------------------------


@pytest.mark.parametrize("evil", ["../../escaped", "-x"])
def test_an_invalid_worker_name_gets_no_child_and_no_directory(
    lab: Lab, tmp_path: Path, evil: str
) -> None:
    where = {**SPLIT, "gnome": evil, "wm": evil}
    outcomes = _execute(lab, where)
    for name in ("gnome", "wm"):
        got = _outcome(outcomes, name)
        assert not got.ok and repr(evil) in got.error, got.error
    assert lab.worker_argvs() == {}
    assert not (tmp_path / "escaped").exists()
    assert not (lab.scratch / "worker-results" / evil).exists()
    assert _outcome(outcomes, "kde").ok


# --- finding 4: who owns worker-results/ ---------------------------------------------------------


def test_under_sudo_only_the_job_leaf_goes_to_the_user_through_its_descriptor(
    lab: Lab, chowns: list[Chown], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_sudo(monkeypatch, tmp_path, FAKE_UID, FAKE_UID)
    _execute(lab)
    to_user = [c for c in chowns if FAKE_UID in (c.uid, c.gid)]
    assert to_user, "the job's results directory goes to the user"
    assert all(c.how == "fchown" and Path(c.path) == _results(lab) for c in to_user), to_user
    assert lab.worker_argvs()  # an EPERM on the leaf (not root here) does not stop the job


def test_under_sudo_an_open_worker_dir_is_closed_to_others(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    node = lab.scratch / "worker-results" / LAB
    node.mkdir(parents=True)
    node.chmod(0o777)
    _fake_sudo(monkeypatch, tmp_path, os.getuid(), os.getgid())
    _execute(lab)
    assert stat.S_IMODE(node.stat().st_mode) == 0o755
    assert stat.S_IMODE(node.parent.stat().st_mode) == 0o755


@pytest.mark.parametrize("planted", ["leaf", "node"])
def test_a_planted_symlink_in_the_results_path_is_not_followed(
    lab: Lab, tmp_path: Path, planted: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{JOB}.rc").write_text("0\n")
    (outside / JOB).mkdir()
    (outside / JOB / f"{JOB}.rc").write_text("0\n")
    node = lab.scratch / "worker-results" / LAB
    if planted == "leaf":
        node.mkdir(parents=True)
        (node / JOB).symlink_to(outside)
    else:
        node.parent.mkdir(parents=True)
        node.symlink_to(outside)
    outcomes = _execute(lab)
    assert (outside / f"{JOB}.rc").is_file() and (outside / JOB / f"{JOB}.rc").is_file()
    assert lab.worker_argvs() == {}
    for name in ("gnome", "wm"):
        got = _outcome(outcomes, name)
        assert not got.ok and "not started" in got.error, got.error


# --- finding 5: the published digest is the host's own -------------------------------------------


def test_a_worker_digest_contradicting_the_file_fails_the_image(lab: Lab) -> None:
    name = _iso_name("gnome")

    def lie() -> None:
        sums = _results(lab) / "iso" / "SHA256SUMS"
        lines = [
            f"{'f' * 64}  {name}" if line.endswith(f"  {name}") else line
            for line in sums.read_text().splitlines()
        ]
        sums.write_text("".join(f"{line}\n" for line in lines))

    outcomes = _execute(lab, runner=_then(lab, lie))
    gnome = _outcome(outcomes, "gnome")
    assert not gnome.ok and name in gnome.error and "SHA256" in gnome.error, gnome.error
    published = (lab.out / "SHA256SUMS").read_text()
    assert not any(line.endswith(f"  {name}") for line in published.splitlines())
    assert not (lab.out / name).exists()  # nothing of the image is published
    wm = _outcome(outcomes, "wm")
    assert wm.ok and f"  {_iso_name('wm')}" in published


# --- finding 7: a worker job that ended before the signal ---------------------------------


def test_an_interrupt_lists_only_the_worker_jobs_still_running(lab: Lab) -> None:
    where = {"minimal": "dojo", "kde": LAB2, "gnome": LAB, "wm": LAB}

    def run(argvs: dict[str, list[str]], *_a: object, **_kw: Any) -> list[parallel.ChildResult]:
        lab.calls.append({str(k): [str(a) for a in v] for k, v in argvs.items()})
        results = [
            _simulate_worker_job(lab, str(k), [str(a) for a in v])
            if _is_worker_child([str(a) for a in v])
            else _host_child(lab, str(k), [str(a) for a in v])
            for k, v in argvs.items()
        ]
        # LAB's job had ended on its own; LAB2's and the host's were stopped by the signal
        stopped = [
            r if r.image == f"@{LAB}" else dataclasses.replace(r, error="interrupted")
            for r in results
        ]
        raise parallel.Interrupted(stopped, signum=signal.SIGTERM)

    with pytest.raises(schedule.Interrupted) as caught:
        _execute(lab, where, runner=run)
    err = caught.value
    assert [j.worker for j in err.jobs] == [LAB2]
    for name in ("gnome", "wm"):
        got = _outcome(list(err.outcomes), name)
        assert got.ok, got.error
        assert (lab.out / _iso_name(name)).is_file()
    assert f"  {_iso_name('gnome')}" in (lab.out / "SHA256SUMS").read_text()
