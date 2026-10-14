"""The runner image, imported once per toolbox (story 024, task 2.2; R2.1, R2.2, R2.4).

Contract (design.md): ``ensure_image(tarball, *, runner=subprocess.run) -> str`` asks
``podman image exists <tag>``; when absent it takes the exclusive lock
``<scratch>/vm-runner.lock``, asks again, and only then runs ``podman import --change
CMD=/bin/bash <tarball> <tag>``. It returns ``image_tag(tarball)``. A failed import
raises ``VmRunnerError`` naming the tarball and podman's stderr, with no retry. No
registry is ever contacted.

podman is a fake ``runner`` holding the set of images it has; the lock is the real one,
checked from a second open file description (``flock`` is per description).
"""

import errno
import fcntl
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from shidashi import vmrunner


class _Podman:
    """``podman image exists`` / ``podman import`` over an in-memory image set."""

    def __init__(self, lock: Path, *, images: set[str] | None = None) -> None:
        self.images: set[str] = set() if images is None else images
        self.calls: list[list[str]] = []
        self.lock = lock
        self.locked_at: list[tuple[str, bool]] = []
        self.import_rc = 0
        self.import_stderr = ""
        #: images that appear after the first ``exists`` (another process imported them)
        self.appear_after_first_check: set[str] = set()

    def lock_held(self) -> bool:
        if not self.lock.exists():
            return False
        fd = os.open(self.lock, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            raise
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def __call__(self, argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        assert argv[0] == "podman", argv
        if argv[1:3] == ["image", "exists"]:
            self.locked_at.append(("exists", self.lock_held()))
            present = argv[3] in self.images
            self.images |= self.appear_after_first_check
            self.appear_after_first_check = set()
            return subprocess.CompletedProcess(argv, 0 if present else 1, "", "")
        if argv[1] == "import":
            self.locked_at.append(("import", self.lock_held()))
            if self.import_rc != 0:
                return subprocess.CompletedProcess(argv, self.import_rc, "", self.import_stderr)
            self.images.add(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "sha256:feed\n", "")
        raise AssertionError(f"unexpected podman call: {argv}")

    def imports(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "import"]


@pytest.fixture
def tarball(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    path = (
        tmp_path
        / "cache"
        / "fork-points"
        / "v3-systemd-20260823T153057Z-p20260928.60cfe9d6-b392fdb91-toolbox.tar"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"toolbox")
    return path


@pytest.fixture
def podman(tmp_path: Path) -> _Podman:
    return _Podman(tmp_path / "scratch" / "vm-runner.lock")


# --- hostile half: what must never be reused, what must never be imported twice -----------


def test_the_image_of_an_old_toolbox_does_not_serve_a_new_one(
    tarball: Path, podman: _Podman, tmp_path: Path
) -> None:
    """R2.3, wrong collapse: an image exists, but of the previous toolbox."""
    old = tarball.with_name("v3-systemd-20260801T000000Z-p20260928.60cfe9d6-b392fdb91-toolbox.tar")
    old.write_bytes(b"toolbox")  # the tag stats the tarball (task 2.4)
    podman.images.add(vmrunner.image_tag(old))
    tag = vmrunner.ensure_image(tarball, runner=podman)
    assert tag == vmrunner.image_tag(tarball) != vmrunner.image_tag(old)
    assert len(podman.imports()) == 1
    assert tag in podman.images


def test_an_image_another_process_imported_meanwhile_is_not_imported_again(
    tarball: Path, podman: _Podman
) -> None:
    """R2.2, wrong split: absent at the first look, present once the lock is ours (the
    other boot test imported it while we waited) -- one import for both, not two."""
    podman.appear_after_first_check = {vmrunner.image_tag(tarball)}
    tag = vmrunner.ensure_image(tarball, runner=podman)
    assert tag == vmrunner.image_tag(tarball)
    assert podman.imports() == []
    exists = [c for c in podman.calls if c[1:3] == ["image", "exists"]]
    assert len(exists) == 2  # looked again under the lock


def test_the_second_look_and_the_import_happen_under_the_lock(
    tarball: Path, podman: _Podman
) -> None:
    vmrunner.ensure_image(tarball, runner=podman)
    assert podman.locked_at[0] == ("exists", False)  # the cheap first look takes no lock
    assert podman.locked_at[1:] == [("exists", True), ("import", True)]
    assert not podman.lock_held()  # released afterwards


# --- R2.1: the plain cases --------------------------------------------------------------


def test_an_existing_image_is_used_as_it_is(tarball: Path, podman: _Podman) -> None:
    podman.images.add(vmrunner.image_tag(tarball))
    assert vmrunner.ensure_image(tarball, runner=podman) == vmrunner.image_tag(tarball)
    assert podman.imports() == []


def test_an_absent_image_is_imported_from_the_tarball(tarball: Path, podman: _Podman) -> None:
    tag = vmrunner.ensure_image(tarball, runner=podman)
    assert tag == vmrunner.image_tag(tarball)
    (argv,) = podman.imports()
    assert argv[:2] == ["podman", "import"]
    assert argv[-2:] == [str(tarball), tag]
    changes = [argv[i + 1] for i, a in enumerate(argv) if a in ("--change", "-c")] + [
        a.split("=", 1)[1] for a in argv if a.startswith("--change=")
    ]
    assert changes == ["CMD=/bin/bash"]
    # the side effect: the image now exists
    assert tag in podman.images


def test_no_registry_is_contacted(tarball: Path, podman: _Podman) -> None:
    vmrunner.ensure_image(tarball, runner=podman)
    verbs = {tuple(c[1:3]) if c[1] == "image" else (c[1],) for c in podman.calls}
    assert verbs <= {("image", "exists"), ("import",)}
    assert not any("pull" in a or "login" in a or "push" in a for c in podman.calls for a in c)


# --- R2.4: a failed import --------------------------------------------------------------


def test_a_failed_import_names_the_tarball_and_podmans_reason(
    tarball: Path, podman: _Podman
) -> None:
    podman.import_rc = 125
    podman.import_stderr = "Error: writing blob: archive/tar: invalid tar header\n"
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.ensure_image(tarball, runner=podman)
    message = str(raised.value)
    assert str(tarball) in message
    assert "archive/tar: invalid tar header" in message
    assert len(podman.imports()) == 1  # no retry
    assert vmrunner.image_tag(tarball) not in podman.images
    assert not podman.lock_held()
