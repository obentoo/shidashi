"""Tests of ``shidashi.provision_reissue.reissue`` -- ``worker provision --keep-identity``:
a new generic worker ISO carrying the identity a worker already has.

End to end against real ISOs, as ``tests/test_provision_transaction.py`` builds them: a
worker is provisioned for real, then re-issued from ANOTHER source ISO (a new image).
The rule under test: the new medium holds the very same identity bytes, the host's pin
and registry do not move, and a refusal or a failed copy changes nothing at all.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import provision, provision_reissue, workers
from shidashi.cli import app
from tests.test_provision_transaction import (
    _Env,
    _env,
    _extract,
    _mkisofs,
    _snapshot,
    _tree,
)

pytestmark = pytest.mark.skipif(
    shutil.which("xorriso") is None or shutil.which("ssh-keygen") is None,
    reason="needs xorriso and ssh-keygen",
)


@pytest.fixture(scope="module")
def isos(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The first worker ISO and a non-worker one, built once."""
    return {
        "worker": _mkisofs(tmp_path_factory.mktemp("worker-iso"), "BENTOO_WORKER"),
        "kde": _mkisofs(tmp_path_factory.mktemp("kde-iso"), "BENTOO_KDE"),
    }


@pytest.fixture
def paired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isos: dict[str, Path]) -> _Env:
    """A host that provisioned ``lab`` for real from the first worker ISO."""
    env = _env(tmp_path, monkeypatch, isos["worker"])
    provision.provision(
        "lab", env.iso, workers_dir=env.workers_dir, replace=False, runner=subprocess.run
    )
    return env


@pytest.fixture
def newer(tmp_path: Path) -> Path:
    """Another worker ISO: the image a rebuild produced (its release file differs)."""
    base = tmp_path / "newer"
    tree = _tree(base)
    (tree / "bentoo" / "release").write_text("20261128T1224\n")
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "boot" / "grub" / "eltorito.img").write_bytes(bytes(2048))
    iso = base / "bentoo_worker.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-quiet", "-R", "-V", "BENTOO_WORKER"]
        + ["-b", "boot/grub/eltorito.img", "-no-emul-boot", "-boot-load-size", "4"]
        + ["--protective-msdos-label", "-o", str(iso), str(tree)],
        check=True,
        capture_output=True,
    )
    return iso


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_the_new_medium_carries_the_same_identity_and_the_pin_does_not_move(
    paired: _Env, newer: Path, tmp_path: Path
) -> None:
    wd = paired.workers_dir
    pinned = (wd / "known_hosts").read_bytes()
    registry = (wd / "workers.json").read_bytes()
    done = provision_reissue.reissue("lab", newer, workers_dir=wd, runner=subprocess.run)
    assert done.iso_path == wd / "lab" / "lab.iso"
    assert done.iso_path.stat().st_mode & 0o777 == 0o600
    got = _extract(done.iso_path, "/shidashi/identity", tmp_path / "got")
    assert _files(got) == _files(wd / "lab" / "identity")
    release = _extract(done.iso_path, "/bentoo/release", tmp_path / "release")
    assert release.read_text() == "20261128T1224\n"  # the new image, not the first one
    assert (wd / "known_hosts").read_bytes() == pinned
    assert (wd / "workers.json").read_bytes() == registry
    assert (
        done.fingerprint == workers.load_registry(wd / "workers.json")["lab"].host_key_fingerprint
    )
    assert not list((wd / "lab").glob(".lab.iso.tmp-*"))


@pytest.mark.parametrize(
    ("break_it", "says"),
    [
        (lambda wd: None, "not paired"),  # an unknown name: the call below uses "ghost"
        (lambda wd: (wd / "lab" / "identity" / "pairing.json").unlink(), "pairing.json"),
        (
            lambda wd: (wd / "lab" / "identity" / "ssh" / "ssh_host_ed25519_key").unlink(),
            "ssh_host_ed25519_key",
        ),
    ],
    ids=["unpaired", "no-record", "no-private-key"],
)
def test_hostile_a_worker_without_an_identity_to_keep_is_refused_and_nothing_changes(
    paired: _Env, newer: Path, break_it: Any, says: str
) -> None:
    wd = paired.workers_dir
    break_it(wd)
    name = "ghost" if says == "not paired" else "lab"
    before = _snapshot(wd)
    with pytest.raises(provision.ProvisionError, match=says):
        provision_reissue.reissue(name, newer, workers_dir=wd, runner=subprocess.run)
    assert _snapshot(wd) == before


def test_hostile_an_identity_whose_key_the_host_does_not_pin_is_refused(
    paired: _Env, newer: Path
) -> None:
    """Such a medium would boot a worker the host refuses to talk to: a lock-out."""
    wd = paired.workers_dir
    key = wd / "lab" / "identity" / "ssh" / "ssh_host_ed25519_key"
    key.unlink()
    Path(f"{key}.pub").unlink()
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lab", "-f", str(key)],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    before = _snapshot(wd)
    with pytest.raises(provision.ProvisionError, match="pins"):
        provision_reissue.reissue("lab", newer, workers_dir=wd, runner=subprocess.run)
    assert _snapshot(wd) == before


def test_hostile_an_iso_that_is_not_a_worker_iso_is_refused(
    paired: _Env, isos: dict[str, Path]
) -> None:
    wd = paired.workers_dir
    before = _snapshot(wd)
    with pytest.raises(provision.ProvisionError, match="not a worker ISO"):
        provision_reissue.reissue("lab", isos["kde"], workers_dir=wd, runner=subprocess.run)
    assert _snapshot(wd) == before


def test_hostile_a_failed_copy_leaves_the_old_medium_and_no_partial_file(
    paired: _Env, newer: Path
) -> None:
    wd = paired.workers_dir
    old = (wd / "lab" / "lab.iso").read_bytes()

    def _half_write(argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if argv[0] == "xorriso" and "-outdev" in argv:
            Path(argv[argv.index("-outdev") + 1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(argv, 5, "", "xorriso: write error\n")
        return subprocess.run(argv, *args, **kwargs)

    with pytest.raises(provision.ProvisionError, match="was not changed"):
        provision_reissue.reissue("lab", newer, workers_dir=wd, runner=_half_write)
    assert (wd / "lab" / "lab.iso").read_bytes() == old
    assert not list((wd / "lab").glob(".lab.iso.tmp-*"))


def test_the_command_refuses_keep_identity_with_replace_or_wifi(paired: _Env, newer: Path) -> None:
    for extra in (["--replace"], ["--wifi", "Home"]):
        result = CliRunner().invoke(
            app,
            ["worker", "provision", "lab", "--iso", str(newer), "--keep-identity", *extra],
        )
        assert result.exit_code == 2, result.output


def test_the_command_reissues_and_prints_the_dd_line(paired: _Env, newer: Path) -> None:
    result = CliRunner().invoke(
        app, ["worker", "provision", "lab", "--iso", str(newer), "--keep-identity"]
    )
    assert result.exit_code == 0, result.output
    iso = paired.workers_dir / "lab" / "lab.iso"
    assert str(iso) in result.output and "sudo dd" in result.output
