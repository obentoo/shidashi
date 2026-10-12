"""vsock CID locks shared by every boot on the host (story 006, task 8.3; R4.1).

Contract (design.md v4, "Boot tests", "Host-wide CID locks", "Symlink-safe lock directory",
"Lock primitive" -- lock files outside a trusted tree):

- ``cid-<n>.lock`` lives in ``vm.cid_lock_dir()`` = ``$SHIDASHI_CID_LOCKS`` or
  ``/tmp/shidashi-vm-cids/`` -- never under scratch, never moved by ``--work-dir``: CIDs are
  host-global. Created when absent, then ``chmod``ed to 1777 (the umask would strip it);
  lock files 0666.
- ``vm test --cid N`` and ``vm start`` take ``cid-N.lock`` without waiting and exit 1 with
  ``vsock CID N is held by pid P``; ``vm start`` hands the lock's fd to QEMU (``pass_fds``)
  and rewrites the record with QEMU's pid from its pidfile. ``build --serial --boot-test``
  allocates from ``DEFAULT_CID`` (42) upward; ``assemble --boot-test`` skips a CID a
  ``vm test`` holds.
- Before any lock the directory is ``lstat``ed: a symlink, a non-directory, a world-writable
  directory without the sticky bit, or one owned by neither root nor the current user is a
  ``VmError`` naming it. Lock files are opened ``O_NOFOLLOW | O_CLOEXEC``
  (``locks.hold(..., mode=0o666, nofollow=True)``): a planted ``cid-N.lock`` symlink raises
  naming the path and its target is neither created nor written; with ``nofollow`` the lock
  is keyed by the resolved directory plus the literal name.

Every test redirects ``$SHIDASHI_CID_LOCKS`` into ``tmp_path``: the real
``/tmp/shidashi-vm-cids`` is never touched. Another user's ownership is simulated through
the stat calls (no privileges needed). No QEMU runs: ``boot_test`` and the session runner
are recorders; every other holder is a real second process, stopped by its fixture.
"""

import contextlib
import fcntl
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import shidashi
from shidashi import audit, cli, locks, ownership, vm
from shidashi.assembler import AssembleResult
from shidashi.cli import app

_REPO = Path(shidashi.__file__).resolve().parents[1]
runner = CliRunner()

_HOLD = """
import sys
from pathlib import Path
from shidashi import locks
with locks.hold(Path(sys.argv[1]), what="vsock CID"):
    print("held", flush=True)
    sys.stdin.readline()
"""

_ALLOCATE = """
import sys
from shidashi import vm
with vm.allocate_cid(int(sys.argv[1])) as cid:
    print(cid, flush=True)
    sys.stdin.readline()
"""


@pytest.fixture(autouse=True)
def cids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The host CID lock directory, redirected; absent until a test or the code makes it."""
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    path = tmp_path / "host" / "shidashi-vm-cids"
    monkeypatch.setenv("SHIDASHI_CID_LOCKS", str(path))
    return path


def _shared_dir(path: Path) -> Path:
    """The directory as a first user left it: 1777, owned by this user."""
    path.mkdir(parents=True)
    path.chmod(0o1777)
    return path


def _locked(path: Path) -> bool:
    """Whether any open file description holds ``path``'s flock (this process's too)."""
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextlib.contextmanager
def _elsewhere(script: str, *args: str, env: dict[str, str] | None = None) -> Iterator[Any]:
    """A real second process running ``script``; yields (proc, its first line)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, *args],
        cwd=_REPO,
        env={**os.environ, "PYTHONPATH": str(_REPO), **(env or {})},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        first = proc.stdout.readline().strip()
        if not first:
            proc.wait(timeout=10)
            pytest.fail(f"the other process never got its lock: {proc.stderr.read()}")
        yield proc, first
    finally:
        if proc.poll() is None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write("\n")
                proc.stdin.flush()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _flat(result: Any, capfd: pytest.CaptureFixture[str]) -> str:
    captured = capfd.readouterr()
    out = result.output + captured.out + captured.err
    for box in "│╭╮╰╯─":
        out = out.replace(box, " ")
    return " ".join(out.split())


def _report() -> dict[str, Any]:
    firmware = {"passed": True, "ssh_after_s": 1.0, "checks": [], "metrics": {}}
    return {"passed": True, "firmwares": {"bios": firmware}}


@pytest.fixture
def iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bentoo-x-kde.iso"
    path.write_bytes(b"ISO")
    monkeypatch.setattr(
        vm, "read_build_info", lambda _iso: {"arch": "v3", "flavor": "kde", "init": "systemd"}
    )
    monkeypatch.setattr(audit, "repo_state", lambda: {})
    return path


# --- the locks live in the host directory ---------------------------------------------------


def test_the_locks_live_in_the_host_dir_not_in_scratch(tmp_path: Path, cids: Path) -> None:
    _shared_dir(cids)
    with vm.allocate_cid(42) as cid:
        assert cid == 42
        assert _locked(cids / "cid-42.lock"), "CID 42 is not held in the host directory"
    assert not (tmp_path / "scratch" / "vm-cids").exists(), "a CID lock went under scratch"


def test_two_scratch_roots_share_one_cid_namespace(tmp_path: Path, cids: Path) -> None:
    """Wrongly split: a boot test under another scratch (another --work-dir, root's
    assemble vs the user's vm test) uses the same host CIDs -- 42 is taken for both."""
    _shared_dir(cids)
    other = {"SHIDASHI_SCRATCH": str(tmp_path / "other-scratch")}
    with _elsewhere(_ALLOCATE, "42", env=other) as (_proc, first):
        assert first == "42"
        with vm.allocate_cid(42) as cid:
            assert cid == 43, "two scratch roots handed out the same vsock CID"


def test_an_absent_directory_is_created_1777_under_umask_077_files_0666(cids: Path) -> None:
    old = os.umask(0o077)
    try:
        with vm.allocate_cid(42):
            pass
    finally:
        os.umask(old)
    assert cids.is_dir(), f"the host CID directory {cids} was not created"
    assert stat.S_IMODE(cids.lstat().st_mode) == 0o1777
    assert stat.S_IMODE((cids / "cid-42.lock").lstat().st_mode) == 0o666


def test_vm_test_work_dir_does_not_move_the_cid_locks(
    tmp_path: Path, cids: Path, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shared_dir(cids)
    seen: list[tuple[object, bool]] = []

    def boot_test(_iso: Path, **kw: Any) -> dict[str, Any]:
        seen.append((kw.get("cid"), _locked(cids / "cid-44.lock")))
        return _report()

    monkeypatch.setattr(vm, "boot_test", boot_test)
    work = tmp_path / "work"
    done = runner.invoke(
        app, ["vm", "test", str(iso), "--firmware", "bios", "--cid", "44", "--work-dir", str(work)]
    )
    assert done.exit_code == 0, done.output
    assert seen == [(44, True)], "vm test did not hold its CID in the host directory"
    assert not list(work.rglob("cid-*.lock"))


# --- vm test / vm start refuse a held CID ---------------------------------------------------


@pytest.mark.parametrize("command", ["test", "start"])
def test_a_held_cid_is_refused_naming_its_holder(
    cids: Path,
    iso: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    command: str,
) -> None:
    _shared_dir(cids)
    booted: list[str] = []
    monkeypatch.setattr(vm, "boot_test", lambda *_a, **_k: booted.append("boot") or _report())

    class _Session:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def start(self) -> None:
            booted.append("start")

    monkeypatch.setattr(vm, "Session", _Session)
    args = ["vm", "test", str(iso), "--firmware", "bios", "--cid", "42"]
    if command == "start":
        args = ["vm", "start", str(iso), "--no-wait", "--cid", "42"]
    with _elsewhere(_HOLD, str(cids / "cid-42.lock")) as (holder, _first):
        done = runner.invoke(app, args)
        out = _flat(done, capfd)
    assert done.exit_code == 1, out
    assert f"vsock CID 42 is held by pid {holder.pid}" in out, out
    assert "Traceback" not in out
    assert booted == [], "a VM was started on a held CID"


def test_a_held_cid_never_blocks_another(
    cids: Path, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly collapsed: 42 held by another boot leaves ``vm test --cid 43`` free."""
    _shared_dir(cids)
    seen: list[tuple[object, bool]] = []

    def boot_test(_iso: Path, **kw: Any) -> dict[str, Any]:
        seen.append((kw.get("cid"), _locked(cids / "cid-43.lock")))
        return _report()

    monkeypatch.setattr(vm, "boot_test", boot_test)
    with _elsewhere(_HOLD, str(cids / "cid-42.lock")):
        done = runner.invoke(app, ["vm", "test", str(iso), "--firmware", "bios", "--cid", "43"])
    assert done.exit_code == 0, done.output
    assert seen == [(43, True)]


def test_vm_start_passes_the_lock_fd_to_qemu_and_records_its_pid(
    tmp_path: Path, cids: Path, iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QEMU (faked) inherits the lock's open description, so the lock outlives ``vm start``
    for as long as the VM runs; the record names QEMU, read from its pidfile."""
    _shared_dir(cids)
    lock = cids / "cid-42.lock"
    qemu_pid = os.getppid()  # a live process that is not this one
    inherited: list[int] = []
    passed: list[object] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text("private")
            key.with_suffix(".pub").write_text("ssh-ed25519 AAAA test\n")
        elif argv[0] == "qemu-system-x86_64":
            fds = tuple(kw.get("pass_fds") or ())
            passed.append(fds)
            ino = lock.stat().st_ino if lock.exists() else None
            for fd in fds:
                if os.fstat(fd).st_ino == ino:
                    inherited.append(os.dup(fd))  # QEMU keeps it open
            Path(argv[argv.index("-pidfile") + 1]).write_text(f"{qemu_pid}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    proxy = tmp_path / "systemd-ssh-proxy"
    proxy.write_text("")
    monkeypatch.setattr(vm, "_SSH_PROXY", str(proxy))
    real_which = vm.shutil.which
    monkeypatch.setattr(
        vm.shutil, "which", lambda t: "/usr/bin/" + t if "qemu" in t else real_which(t)
    )
    real_session = vm.Session
    monkeypatch.setattr(
        vm, "Session", lambda spec, directory, **_k: real_session(spec, directory, runner=run)
    )
    try:
        done = runner.invoke(app, ["vm", "start", str(iso), "--no-wait", "--name", "fd"])
        assert done.exit_code == 0, done.output
        assert inherited, f"QEMU was not handed the CID lock's fd (pass_fds={passed})"
        assert _locked(lock), "the CID lock did not outlive vm start while QEMU runs"
        assert locks.holder_pid(lock) == qemu_pid, "the record does not name QEMU"
    finally:
        for fd in inherited:
            os.close(fd)


# --- build and assemble allocate upward on the host CIDs ------------------------------------


def test_an_assemble_boot_test_skips_a_cid_vm_test_holds(
    tmp_path: Path, cids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shared_dir(cids)

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            out = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"ISO")
            return AssembleResult(name=out.stem, isos=(out,), artifacts=())

    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    booted: list[object] = []
    monkeypatch.setattr(vm, "boot_test", lambda _i, **kw: booted.append(kw.get("cid")) or _report())
    argv = ["assemble", "v3", "minimal", "systemd", "-o", str(tmp_path / "out")]
    with _elsewhere(_HOLD, str(cids / "cid-44.lock")):
        done = runner.invoke(app, [*argv, "--boot-test", "--cid", "44"])
    assert done.exit_code == 0, done.output
    assert booted == [45], f"the assemble booted on a CID vm test holds: {booted}"


def test_serial_boot_test_allocates_from_42_upward(
    tmp_path: Path, cids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shared_dir(cids)

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            out = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            return AssembleResult(name=out.stem, isos=(out,), artifacts=())

    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    monkeypatch.setattr(ownership, "current", lambda _arch: None)
    seen: list[tuple[object, bool]] = []

    def boot_test(_iso: Path, **kw: Any) -> dict[str, Any]:
        cid = kw.get("cid")
        seen.append((cid, _locked(cids / f"cid-{cid}.lock")))
        return _report()

    monkeypatch.setattr(vm, "boot_test", boot_test)
    argv = ["build", "v3", "systemd", "--images", "minimal", "--skip-factory", "--serial"]
    with _elsewhere(_HOLD, str(cids / "cid-42.lock")):
        done = runner.invoke(app, [*argv, "--boot-test"])
    assert done.exit_code == 0, done.output
    assert seen == [(43, True)], f"--serial did not allocate from 42 upward: {seen}"


# --- hostile: an unsafe lock directory or lock file ----------------------------------------


def _refused(path: Path) -> str:
    with pytest.raises(vm.VmError) as err, vm.allocate_cid(42):
        pytest.fail("a CID was allocated in an unsafe lock directory")
    message = str(err.value)
    assert str(path) in message, message
    return message


def test_a_lock_directory_that_is_a_symlink_is_refused_naming_it(
    tmp_path: Path, cids: Path
) -> None:
    planted = _shared_dir(tmp_path / "planted")
    cids.parent.mkdir(parents=True)
    cids.symlink_to(planted)
    _refused(cids)
    assert list(planted.iterdir()) == [], "a lock was created through the symlink"


def test_a_regular_file_at_the_directory_path_is_refused(cids: Path) -> None:
    cids.parent.mkdir(parents=True)
    cids.write_text("not a directory\n")
    _refused(cids)
    assert cids.read_text() == "not a directory\n"


def test_a_world_writable_directory_without_the_sticky_bit_is_refused(cids: Path) -> None:
    cids.mkdir(parents=True)
    cids.chmod(0o777)
    _refused(cids)
    assert list(cids.iterdir()) == []


class _Stat:
    """A stat result reporting another owner; everything else is the real one."""

    def __init__(self, real: os.stat_result, uid: int) -> None:
        self._real = real
        self.st_uid = uid

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __getitem__(self, index: int) -> Any:
        return self.st_uid if index == stat.ST_UID else self._real[index]

    def __len__(self) -> int:
        return len(self._real)


def _owned_by(monkeypatch: pytest.MonkeyPatch, path: Path, uid: int) -> None:
    """Every stat of ``path``'s inode (lstat, stat, fstat) reports owner ``uid``."""
    real = path.lstat()
    key = (real.st_dev, real.st_ino)

    def wrap(fn: Callable[..., os.stat_result]) -> Callable[..., Any]:
        def stat_of(*args: Any, **kw: Any) -> Any:
            got = fn(*args, **kw)
            return _Stat(got, uid) if (got.st_dev, got.st_ino) == key else got

        return stat_of

    for name in ("lstat", "stat", "fstat"):
        monkeypatch.setattr(os, name, wrap(getattr(os, name)))


def test_a_directory_owned_by_another_user_is_refused(
    cids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shared_dir(cids)
    foreign = max(os.getuid(), 0) + 4242  # neither root nor the caller
    with monkeypatch.context() as patched:
        _owned_by(patched, cids, foreign)
        _refused(cids)
    assert list(cids.iterdir()) == []


def test_a_root_owned_shared_directory_is_accepted(
    cids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly split: root's assemble created it, the user's vm test must use it too."""
    _shared_dir(cids)
    with monkeypatch.context() as patched:
        _owned_by(patched, cids, 0)
        with vm.allocate_cid(42) as cid:
            held = _locked(cids / "cid-42.lock")
    assert cid == 42 and held


@pytest.mark.parametrize("target_exists", [False, True])
def test_a_planted_lock_file_symlink_raises_and_its_target_is_untouched(
    tmp_path: Path, cids: Path, target_exists: bool
) -> None:
    _shared_dir(cids)
    target = tmp_path / "victim"
    if target_exists:
        target.write_text("precious\n")
    planted = cids / "cid-42.lock"
    planted.symlink_to(target)
    with pytest.raises((vm.VmError, locks.LockError)) as err, vm.allocate_cid(42):
        pytest.fail("a CID was allocated through a planted lock-file symlink")
    assert str(planted) in str(err.value), str(err.value)
    if target_exists:
        assert target.read_text() == "precious\n", "the symlink's target was written"
    else:
        assert not target.exists(), "the symlink's target was created"


def test_locks_hold_nofollow_refuses_a_symlink_but_keys_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    """Wrongly collapsed: a symlink as the FINAL component is refused (ELOOP -> LockError
    naming it). Wrongly split: the same lock reached through a symlinked DIRECTORY is one
    lock -- a nested hold takes no second flock."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    target = tmp_path / "victim"
    target.write_text("precious\n")
    (real / "cid-7.lock").symlink_to(target)
    with pytest.raises(locks.LockError) as err:  # noqa: SIM117
        with locks.hold(real / "cid-7.lock", what="vsock CID 7", mode=0o666, nofollow=True):
            pytest.fail("held through a symlink")
    assert str(real / "cid-7.lock") in str(err.value)
    assert target.read_text() == "precious\n"

    lock = real / "cid-8.lock"
    with (
        locks.hold(lock, what="vsock CID 8", mode=0o666, nofollow=True),
        locks.hold(alias / "cid-8.lock", what="vsock CID 8", wait=False, nofollow=True),
    ):
        assert _locked(lock)
    assert not _locked(lock)


def test_a_lock_fd_is_not_inherited_by_a_plain_child(cids: Path) -> None:
    _shared_dir(cids)
    lister = (
        "import os\n"
        "for fd in os.listdir('/proc/self/fd'):\n"
        "    try: print(os.readlink(f'/proc/self/fd/{fd}'))\n"
        "    except OSError: pass\n"
    )
    with vm.allocate_cid(42):
        child = subprocess.run(
            [sys.executable, "-c", lister],
            close_fds=False,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    leaked = [line for line in child.stdout.splitlines() if line.endswith("cid-42.lock")]
    assert leaked == [], f"a plain child inherited the CID lock: {leaked}"
