"""Tests of shidashi.vm_dev_guest -- the script that turns a live medium into a build host.

The script runs as root in the guest; here it runs for real under a temporary root
(``SHIDASHI_GUEST_ROOT``) with the commands that need root or a guest stubbed on PATH:
``mount``, ``mkfs.ext4`` and ``chown`` record their arguments, ``portageq`` answers the
Bentoo paths, ``emaint`` writes an index and ``python3`` answers the binhost probe as
``FAKE_PROBE`` says.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shidashi import vm_dev_guest as guest

_STUBS = {
    "mount": 'echo "mount $*" >> "$STUB_LOG"',
    "mkfs.ext4": 'echo "mkfs $*" >> "$STUB_LOG"',
    "chown": 'echo "chown $*" >> "$STUB_LOG"',
    "portageq": (
        'case "$2" in DISTDIR) echo /var/cache/distfiles;; PKGDIR) echo /var/cache/binpkgs;; esac'
    ),
    "emaint": 'mkdir -p "$PKGDIR" && : > "$PKGDIR/Packages"',
    "python3": 'exit "${FAKE_PROBE:-0}"',
}

_ISO_BINREPO = (
    "[gentoo]\npriority = 1\n"
    "sync-uri = https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64\n"
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A guest root shaped like the live medium: Portage config, no repos mounted."""
    r = tmp_path / "guest"
    (r / "etc/portage/repos.conf").mkdir(parents=True)
    (r / "etc/portage/binrepos.conf").mkdir(parents=True)
    (r / "etc/portage/binrepos.conf/gentoobinhost.conf").write_text(_ISO_BINREPO)
    (r / "dev").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in _STUBS.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    return r


def _run(
    root: Path, script: str, *, probe: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    log = root.parent / "stub.log"
    env = dict(os.environ)
    env.update(
        PATH=f"{root.parent / 'bin'}:{env['PATH']}",
        SHIDASHI_GUEST_ROOT=str(root),
        STUB_LOG=str(log),
        FAKE_PROBE=str(probe),
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, check=False
    )
    return done, log.read_text().splitlines() if log.exists() else []


def test_the_work_disk_is_formatted_and_holds_every_build_directory(root: Path) -> None:
    done, calls = _run(root, guest.setup_script(("gentoo", "bentoo"), local_pool=False))
    assert done.returncode == 0, done.stderr
    assert "mkfs -q -F /dev/vda" in calls
    assert f"mount /dev/vda {root}/mnt/work" in calls
    for target in ("/var/tmp", "/var/cache/distfiles", "/var/cache/binpkgs", "/var/log/portage"):
        assert any(c.startswith("mount --bind") and c.endswith(f"{root}{target}") for c in calls)
    assert oct((root / "mnt/work/tmp").stat().st_mode & 0o7777) == "0o1777"


def test_every_pinned_repo_is_mounted_read_only_from_its_share(root: Path) -> None:
    _done, calls = _run(root, guest.setup_script(("gentoo", "bentoo"), local_pool=False))
    assert f"mount -t virtiofs -o ro repo-gentoo {root}/var/db/repos/gentoo" in calls
    assert f"mount -t virtiofs -o ro repo-bentoo {root}/var/db/repos/bentoo" in calls


def test_the_staged_repo_is_ready_above_the_pinned_ones(root: Path) -> None:
    _run(root, guest.setup_script(("gentoo", "bentoo"), local_pool=False))
    staged = root / "mnt/work/staged"
    assert (staged / "profiles/repo_name").read_text() == f"{guest.STAGED_REPO}\n"
    layout = (staged / "metadata/layout.conf").read_text()
    assert "masters = gentoo bentoo\n" in layout and "thin-manifests = true\n" in layout
    conf = (root / f"etc/portage/repos.conf/zz-{guest.STAGED_REPO}.conf").read_text()
    assert conf == (
        f"[{guest.STAGED_REPO}]\nlocation = {guest.STAGED_DIR}\n"
        f"priority = {guest.STAGED_PRIORITY}\n"
    )


def test_a_local_pool_comes_first_and_the_reachable_iso_binhost_stays(root: Path) -> None:
    done, calls = _run(root, guest.setup_script(("gentoo", "bentoo"), local_pool=True))
    assert done.returncode == 0, done.stderr
    assert f"mount -t virtiofs -o ro {guest.POOL_TAG} {root}{guest.POOL_MOUNT}" in calls
    local = (root / "etc/portage/binrepos.conf/bentoo-local.conf").read_text()
    assert f"sync-uri = file://{guest.POOL_MOUNT}\n" in local
    assert "verify-signature = false\n" in local  # the host's own pool, read-only
    assert (root / "etc/portage/binrepos.conf/gentoobinhost.conf").is_file()
    assert guest.parse_binhosts(done.stdout) == [
        {"name": "bentoo-local", "uri": f"file://{guest.POOL_MOUNT}"},
        {
            "name": "gentoo",
            "uri": "https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64",
        },
    ]


def test_an_unreachable_iso_binhost_is_set_aside(root: Path) -> None:
    done, _calls = _run(root, guest.setup_script(("gentoo", "bentoo"), local_pool=True), probe=1)
    assert not (root / "etc/portage/binrepos.conf/gentoobinhost.conf").exists()
    assert (root / f"{guest.ISO_BINREPOS_ASIDE}/gentoobinhost.conf").is_file()
    assert [b["name"] for b in guest.parse_binhosts(done.stdout)] == ["bentoo-local"]


def test_with_no_binhost_an_empty_pool_keeps_getbinpkg_quiet(root: Path) -> None:
    # emerge --getbinpkg with no binrepos prints a "!!!" line a build-log reader would chase
    done, _calls = _run(root, guest.setup_script(("gentoo", "bentoo"), local_pool=False), probe=1)
    assert done.returncode == 0, done.stderr
    empty = root / "mnt/work/empty-pool"
    assert (empty / "Packages").is_file()
    conf = (root / "etc/portage/binrepos.conf/none.conf").read_text()
    assert "sync-uri = file:///mnt/work/empty-pool\n" in conf
    assert guest.parse_binhosts(done.stdout) == [
        {"name": "none", "uri": "file:///mnt/work/empty-pool"}
    ]


def test_a_failing_step_stops_the_script(root: Path) -> None:
    (root.parent / "bin/mkfs.ext4").write_text("#!/bin/sh\nexit 1\n")
    done, _calls = _run(root, guest.setup_script(("gentoo", "bentoo"), local_pool=False))
    assert done.returncode != 0
    assert not (root / "mnt/work/staged").exists()


@pytest.mark.parametrize("bad", ["", "a b", "x;rm", "Gentoo", "../up"])
def test_a_repo_name_unfit_for_the_script_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="repository name"):
        guest.setup_script(("gentoo", bad), local_pool=False)


def test_the_first_repo_must_be_gentoo() -> None:
    with pytest.raises(ValueError, match="gentoo"):
        guest.setup_script(("bentoo",), local_pool=False)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_the_script_passes_shellcheck(tmp_path: Path) -> None:
    script = tmp_path / "setup.sh"
    script.write_text("#!/bin/bash\n" + guest.setup_script(("gentoo", "bentoo"), local_pool=True))
    done = subprocess.run(["shellcheck", str(script)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout
