"""A replace keeps the arch caches the worker owns (found 2026-11-07).

``worker provision N --replace`` recorded a fresh entry for N, so the archs N owned
(``worker cache-owner``, story 028) were dropped: the host then built those archs itself
again, and pushed and pulled a cache that lives on the worker only.

What a replace carries over is ``cache_archs`` and nothing else: the address, the CPU
flags and the image are facts of the medium, filled in at first contact, and the key is
the new identity. The entry it reads is the one in the registry when the record is
written, not the one read before the slow copy.

``ssh-keygen`` and ``xorriso`` really run (skipped where they are absent).
"""

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shidashi import provision, workers

NAME = "bentoo-lab"

pytestmark = pytest.mark.skipif(
    shutil.which("xorriso") is None or shutil.which("ssh-keygen") is None,
    reason="needs xorriso and ssh-keygen",
)


@pytest.fixture(scope="module")
def worker_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small bootable ISO labelled as the worker's."""
    base = tmp_path_factory.mktemp("owner-iso")
    tree = base / "tree"
    (tree / "LiveOS").mkdir(parents=True)
    (tree / "LiveOS" / "squashfs.img").write_bytes(b"squash" * 512)
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


@pytest.fixture
def keyed_dir(tmp_path: Path) -> Path:
    """The host's workers directory, with its worker key."""
    wd = tmp_path / "xdg" / "shidashi" / "worker"
    wd.mkdir(parents=True, mode=0o700)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wd / "id_ed25519")],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    return wd


def _give(wd: Path, iso: Path, *, replace: bool = False, runner: Any = subprocess.run) -> Any:
    return provision.provision(NAME, iso, workers_dir=wd, replace=replace, runner=runner)


def _entry(wd: Path, name: str = NAME) -> workers.WorkerEntry:
    return workers.load_registry(wd / "workers.json")[name]


def _amend(wd: Path, name: str = NAME, **fields: Any) -> workers.WorkerEntry:
    """Change ``name``'s entry as the commands that follow a first contact do."""
    path = wd / "workers.json"
    registry = workers.load_registry(path)
    registry[name] = registry[name].model_copy(update=fields)
    workers.save_registry(path, registry)
    return registry[name]


def _while_copying(act: Callable[[], object]) -> Callable[..., Any]:
    """A runner that runs every tool for real, and ``act`` once the copy is written."""

    def runner(argv: Any, *args: Any, **kwargs: Any) -> Any:
        done = subprocess.run(argv, *args, **kwargs)
        if Path(str(argv[0])).name == "xorriso" and "-outdev" in argv:
            act()
        return done

    return runner


def test_a_first_provision_owns_no_arch_whoever_else_does(
    keyed_dir: Path, worker_source: Path
) -> None:
    spare = workers.WorkerEntry(
        name="spare",
        address="192.168.15.9",
        host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
        host_key_fingerprint="SHA256:spare",
        paired_at="2026-10-10T12:00:00+00:00",
        cache_archs=("znver5",),
    )
    workers.save_registry(keyed_dir / "workers.json", {"spare": spare})
    _give(keyed_dir, worker_source)
    assert _entry(keyed_dir).cache_archs == ()
    assert _entry(keyed_dir, "spare") == spare


def test_a_replace_keeps_the_archs_and_nothing_else_of_the_old_medium(
    keyed_dir: Path, worker_source: Path
) -> None:
    first = _give(keyed_dir, worker_source)
    old = _amend(
        keyed_dir,
        address="192.168.15.3",
        cpu_flags=("avx2", "bmi2"),
        image="20261107T1200",
        cache_archs=("v3", "znver5"),
    )
    second = _give(keyed_dir, worker_source, replace=True)

    new = _entry(keyed_dir)
    assert new.cache_archs == ("v3", "znver5")
    assert workers.cache_owner({NAME: new}, "v3") == NAME
    # the medium's own facts start over: the first contact fills them in (R3.5)
    assert (new.address, new.cpu_flags, new.image) == (None, (), "")
    assert new.provisioned is True
    assert new.host_key != old.host_key
    assert new.host_key_fingerprint == second.fingerprint != first.fingerprint


@pytest.mark.parametrize(
    ("before", "during"),
    [((), ("v3",)), (("v3",), ()), (("v3",), ("znver5",))],
    ids=["given", "withdrawn", "changed"],
)
def test_a_replace_keeps_the_archs_owned_when_the_record_is_written(
    keyed_dir: Path, worker_source: Path, before: tuple[str, ...], during: tuple[str, ...]
) -> None:
    """``worker cache-owner`` ran during the slow copy: its answer is the one kept."""
    _give(keyed_dir, worker_source)
    _amend(keyed_dir, cache_archs=before)
    runner = _while_copying(lambda: _amend(keyed_dir, cache_archs=during))
    _give(keyed_dir, worker_source, replace=True, runner=runner)
    assert _entry(keyed_dir).cache_archs == during


def _copy_fails(argv: Any, *args: Any, **kwargs: Any) -> Any:
    if Path(str(argv[0])).name == "xorriso" and "-outdev" in argv:
        return subprocess.CompletedProcess(argv, 32, "", "libburn : FAILURE : no space\n")
    return subprocess.run(argv, *args, **kwargs)


@pytest.mark.parametrize("failing", ["copy", "record"])
def test_a_failed_replace_leaves_the_old_entry_and_its_archs(
    keyed_dir: Path, worker_source: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """The copy fails before anything is installed; the record fails as the last write,
    after the new identity is in place and pinned. Either way the old worker is whole."""
    _give(keyed_dir, worker_source)
    old = _amend(keyed_dir, address="192.168.15.3", image="20261107T1200", cache_archs=("v3",))
    registry_before = (keyed_dir / "workers.json").read_bytes()
    pin_before = (keyed_dir / "known_hosts").read_bytes()
    key_before = (keyed_dir / NAME / "identity/ssh/ssh_host_ed25519_key").read_bytes()

    real_replace = os.replace

    def no_room_for_the_registry(src: Any, dst: Any, **kwargs: Any) -> None:
        if failing == "record" and Path(dst).name == "workers.json":
            raise OSError(28, "No space left on device", str(dst))
        real_replace(src, dst, **kwargs)

    runner: Any = _copy_fails if failing == "copy" else subprocess.run
    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", no_room_for_the_registry)
        with pytest.raises(provision.ProvisionError, match="nothing was changed"):
            _give(keyed_dir, worker_source, replace=True, runner=runner)

    assert (keyed_dir / "workers.json").read_bytes() == registry_before
    assert _entry(keyed_dir) == old
    assert (keyed_dir / "known_hosts").read_bytes() == pin_before
    assert (keyed_dir / NAME / "identity/ssh/ssh_host_ed25519_key").read_bytes() == key_before
    assert sorted(p.name for p in keyed_dir.iterdir()) == sorted(
        ["id_ed25519", "id_ed25519.pub", "known_hosts", "workers.json", NAME]
    )
