"""Regression tests for the Tech Review of the provisioning transaction (story 020,
sub-task 1.3, review round 1).

Each test pins one finding: a hostile xorriso startup file that turns a failed ``-map``
into exit 0 (the copy must carry the identity, whatever xorriso's exit says); a boot
report the parser does not recognise (an empty shape is no proof); El Torito image
options lost by the copy (GRUB's BIOS image needs ``boot-info-table``); a failed
move-aside of the old identity (the message must stay "nothing was changed"); a registry
entry written by someone else while xorriso runs (it must not be lost); an ISO path
xorriso would read as a stream (``-``); and an old identity that could not be removed
(it must be reported, never silently left with its private key).

``ssh-keygen`` and ``xorriso`` really run; the runner passes every command through,
except where a test answers or alters one. Requirements: R1.4, R1.5, R1.8, R4.2.
"""

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from shidashi import provision, workers

NAME = "bentoo-lab"
LABEL_OFFSET = 0x8028

needs_tools = pytest.mark.skipif(
    shutil.which("xorriso") is None or shutil.which("ssh-keygen") is None,
    reason="needs xorriso and ssh-keygen",
)

pytestmark = needs_tools


# --- the source ISO and the host ------------------------------------------------------


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A worker ISO shaped like GRUB's BIOS boot: El Torito with ``boot-info-table``."""
    base = tmp_path_factory.mktemp("source-iso")
    tree = base / "tree"
    (tree / "LiveOS").mkdir(parents=True)
    (tree / "LiveOS" / "squashfs.img").write_bytes(b"squash" * 1024)
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "boot" / "grub" / "eltorito.img").write_bytes(bytes(2048))
    iso = base / "worker.iso"
    subprocess.run(
        ["xorriso", "-no_rc", "-as", "mkisofs", "-quiet", "-R", "-V", "BENTOO_WORKER"]
        + ["-b", "boot/grub/eltorito.img", "-no-emul-boot", "-boot-load-size", "4"]
        + ["-boot-info-table", "--protective-msdos-label", "-o", str(iso), str(tree)],
        check=True,
        capture_output=True,
    )
    return iso


@dataclass
class _Host:
    workers_dir: Path
    dist: Path
    iso: Path
    home: Path


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path) -> _Host:
    """The host's workers directory with its key, the source ISO in an output directory,
    and a HOME of its own (where a test may plant a ``.xorrisorc``)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    wd = tmp_path / "xdg" / "shidashi" / "worker"
    wd.mkdir(parents=True, mode=0o700)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "shidashi-worker-key"]
        + ["-f", str(wd / "id_ed25519")],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    iso = dist / "bentoo-2026.10.10-worker-systemd-v3.iso"
    shutil.copyfile(source, iso)
    return _Host(wd, dist, iso, home)


def _snapshot(root: Path) -> dict[str, tuple[str, int, bytes]]:
    """Every path under ``root``: its kind, mode and bytes (a link: its target)."""
    out: dict[str, tuple[str, int, bytes]] = {}
    for path in sorted([root, *root.rglob("*")]):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = ("link", 0, os.readlink(path).encode())
        elif path.is_dir():
            out[rel] = ("dir", stat.S_IMODE(path.stat().st_mode), b"")
        else:
            out[rel] = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    return out


def _listing(iso: Path) -> list[str]:
    done = subprocess.run(
        ["xorriso", "-no_rc", "-indev", str(iso), "-find", "/"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [p.strip("'") for p in done.stdout.split()]


# --- the runner -----------------------------------------------------------------------

Before = Callable[[list[str]], "subprocess.CompletedProcess[str] | None"]
After = Callable[[list[str], "subprocess.CompletedProcess[Any]"], None]


@dataclass
class _Runner:
    """``subprocess.run`` for real, recording argv; ``before`` may answer a command
    instead of running it, ``after`` may alter what a real run left behind."""

    before: Before | None = None
    after: After | None = None
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        answer = self.before(argv) if self.before else None
        if answer is not None:
            return answer
        done: subprocess.CompletedProcess[Any] = subprocess.run(argv, *args, **kwargs)
        if self.after:
            self.after(argv, done)
        return done


def _is_xorriso(argv: list[str]) -> bool:
    return Path(argv[0]).name == "xorriso"


def _writes_copy(argv: list[str]) -> bool:
    return _is_xorriso(argv) and "-outdev" in argv


def _reports(argv: list[str]) -> bool:
    return _is_xorriso(argv) and "-report_el_torito" in argv


def _indev(argv: list[str]) -> Path:
    return Path(argv[argv.index("-indev") + 1])


def _run(host: _Host, runner: Any, *, replace: bool = False, iso: Path | None = None) -> Any:
    return provision.provision(
        NAME, iso or host.iso, workers_dir=host.workers_dir, replace=replace, runner=runner
    )


def _assert_nothing_changed(
    caught: pytest.ExceptionInfo[provision.ProvisionError],
    before: dict[str, Any],
    root: Path,
) -> str:
    message = str(caught.value)
    assert "provision failed at" in message
    assert message.endswith("nothing was changed"), message
    assert _snapshot(root) == before
    return message


# ===================================================================================
# finding 1: startup files, and a copy that does not carry the identity
# ===================================================================================


def test_every_xorriso_run_ignores_the_startup_files(host: _Host) -> None:
    """``-no_rc`` only works as xorriso's FIRST argument (xorriso(1), FILES)."""
    runner = _Runner()
    _run(host, runner)
    runs = [argv for argv in runner.calls if _is_xorriso(argv)]
    assert len(runs) >= 3  # the copy and the two reports
    for argv in runs:
        assert argv[1] == "-no_rc", argv


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-0 directory")
def test_a_hostile_xorrisorc_cannot_turn_an_unreadable_identity_into_a_success(
    tmp_path: Path, host: _Host
) -> None:
    """Reviewer evidence: with ``-abort_on NEVER`` / ``-return_with FATAL 32`` in
    ``~/.xorrisorc`` an unreadable ``-map`` source exited 0 and a medium WITHOUT
    ``/shidashi`` was pinned and recorded."""
    (host.home / ".xorrisorc").write_text("-abort_on NEVER\n-return_with FATAL 32\n")

    def unreadable_identity(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        if not _writes_copy(argv):
            return None
        identity = Path(argv[argv.index("-map") + 1])
        identity.chmod(0)
        try:
            return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        finally:
            identity.chmod(0o700)

    before = _snapshot(tmp_path)
    with pytest.raises(provision.ProvisionError) as caught:
        _run(host, _Runner(before=unreadable_identity))
    _assert_nothing_changed(caught, before, tmp_path)


def test_a_copy_written_without_the_identity_is_refused_and_removed(
    tmp_path: Path, host: _Host
) -> None:
    """xorriso exits 0 but the identity never reached the medium: the worker would boot
    unpaired, while the host pinned a key no medium holds."""

    def drops_the_map(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        if not _writes_copy(argv):
            return None
        at = argv.index("-map")
        trimmed = argv[:at] + argv[at + 3 :]
        return subprocess.run(trimmed, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    before = _snapshot(tmp_path)
    with pytest.raises(provision.ProvisionError) as caught:
        _run(host, _Runner(before=drops_the_map))
    message = _assert_nothing_changed(caught, before, tmp_path)
    assert "/shidashi/identity" in message


# ===================================================================================
# finding 2: an empty boot shape is no proof
# ===================================================================================


@pytest.mark.parametrize("key", ["El Torito", "System area summary"])
def test_a_boot_report_the_parser_cannot_read_is_refused(
    tmp_path: Path, host: _Host, key: str
) -> None:
    """A renamed report key leaves both shapes empty -- and two empty shapes are equal."""

    def renames(argv: list[str], done: subprocess.CompletedProcess[Any]) -> None:
        if _reports(argv) and isinstance(done.stdout, str):
            done.stdout = done.stdout.replace(key, key.replace(" ", "_"))

    before = _snapshot(tmp_path)
    with pytest.raises(provision.ProvisionError) as caught:
        _run(host, _Runner(after=renames))
    message = _assert_nothing_changed(caught, before, tmp_path)
    assert "cannot read the boot shape of" in message
    assert host.iso.name in message


# ===================================================================================
# finding 3: El Torito image options
# ===================================================================================


def test_a_copy_that_lost_its_el_torito_image_options_is_refused(
    tmp_path: Path, host: _Host
) -> None:
    """GRUB's BIOS ``eltorito.img`` needs ``boot-info-table`` (and ``grub2-boot-info``)."""
    source = host.iso.resolve()
    seen: list[str] = []

    def drops_opts(argv: list[str], done: subprocess.CompletedProcess[Any]) -> None:
        if not _reports(argv) or not isinstance(done.stdout, str):
            return
        if _indev(argv).resolve() == source:
            seen.append(done.stdout)
            return
        kept = [ln for ln in done.stdout.splitlines() if not ln.startswith("El Torito img opts")]
        done.stdout = "\n".join(kept) + "\n"

    before = _snapshot(tmp_path)
    with pytest.raises(provision.ProvisionError) as caught:
        _run(host, _Runner(after=drops_opts))
    assert seen and "boot-info-table" in seen[0]  # the fixture has the option to lose
    message = _assert_nothing_changed(caught, before, tmp_path)
    assert "boot-info-table" in message


# ===================================================================================
# finding 4: a failed move-aside of the old identity
# ===================================================================================


def test_a_failed_move_aside_of_the_old_identity_says_nothing_was_changed(
    tmp_path: Path, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move itself failed, so there is no old dir to restore: the rollback must not
    report one as left behind."""
    _run(host, _Runner())
    before = _snapshot(tmp_path)
    real_replace = os.replace

    def replace(src: Any, dst: Any, **kwargs: Any) -> None:
        if Path(dst).name.startswith(f".{NAME}.old-"):
            raise PermissionError(13, "Permission denied", str(dst))
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(provision.ProvisionError) as caught:
        _run(host, _Runner(), replace=True)
    message = _assert_nothing_changed(caught, before, tmp_path)
    assert "Permission denied" in message
    assert "rollback left" not in message


# ===================================================================================
# finding 5: a registry entry written during the copy
# ===================================================================================


def test_an_entry_recorded_while_xorriso_runs_is_not_lost(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another command pairs ``spare`` during the slow copy; the registry is re-read
    after the pin and saved last."""
    registry = host.workers_dir / "workers.json"
    spare_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
    spare = workers.WorkerEntry(
        name="spare",
        address="192.168.15.9",
        host_key=spare_key,
        host_key_fingerprint="SHA256:spare",
        paired_at="2026-10-10T12:00:00+00:00",
    )

    def pairs_spare(argv: list[str], done: subprocess.CompletedProcess[Any]) -> None:
        if _writes_copy(argv):
            workers.save_registry(registry, {**workers.load_registry(registry), "spare": spare})

    order: list[str] = []
    for fn in ("pin", "load_registry", "save_registry"):
        real = getattr(workers, fn)

        def recorded(*args: Any, _fn: str = fn, _real: Any = real, **kwargs: Any) -> Any:
            order.append(_fn)
            return _real(*args, **kwargs)

        monkeypatch.setattr(workers, fn, recorded)

    result = _run(host, _Runner(after=pairs_spare))
    calls = list(order)  # before this test's own reads below

    saved = workers.load_registry(registry)
    assert set(saved) == {NAME, "spare"}
    assert saved["spare"] == spare
    assert saved[NAME].host_key_fingerprint == result.fingerprint
    # the registry is re-read after the pin, and its save is the last write
    assert calls[-3:] == ["pin", "load_registry", "save_registry"], calls


# ===================================================================================
# finding 6: an ISO path xorriso would read as a stream
# ===================================================================================


def test_an_iso_named_dash_is_read_as_the_file_not_as_stdin(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``iso_volume_id`` reads the file ``-``; xorriso must be handed the same file."""
    shutil.copyfile(host.iso, host.dist / "-")
    monkeypatch.chdir(host.dist)
    runner = _Runner()

    result = _run(host, runner, iso=Path("-"))

    for argv in runner.calls:
        if _is_xorriso(argv):
            assert _indev(argv).is_absolute(), argv
    assert "/shidashi/identity/pairing.json" in _listing(Path(result.iso_path))


# ===================================================================================
# informational: an old identity that could not be removed is reported
# ===================================================================================


def test_a_provision_reports_no_leftover_when_all_was_removed(host: _Host) -> None:
    _run(host, _Runner())
    assert _run(host, _Runner(), replace=True).leftover is None


def test_an_old_identity_that_cannot_be_removed_is_reported_as_leftover(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library does not log: the CLI must be able to WARN that the previous private
    key still sits at that path."""
    _run(host, _Runner())
    old_pub = (host.workers_dir / NAME / "identity/ssh/ssh_host_ed25519_key.pub").read_text()
    real_rmtree = shutil.rmtree

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name.startswith(f".{NAME}.old-"):
            return  # a removal that silently did nothing (e.g. ignore_errors on EACCES)
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    result = _run(host, _Runner(), replace=True)

    leftover = result.leftover
    assert isinstance(leftover, Path)
    assert leftover.parent == host.workers_dir
    assert leftover.name.startswith(f".{NAME}.old-")
    assert (leftover / "identity/ssh/ssh_host_ed25519_key.pub").read_text() == old_pub
    assert (leftover / "identity/ssh/ssh_host_ed25519_key").is_file()
    entry = workers.load_registry(host.workers_dir / "workers.json")[NAME]
    assert entry.host_key_fingerprint == result.fingerprint  # the new identity is in place
