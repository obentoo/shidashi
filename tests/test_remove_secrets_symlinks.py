"""Story 007, sub-task 8.1 -- the secret removal never leaves the rootfs.

The assembler runs as root on the build host. A symlinked directory in the image
(`etc/bind -> /etc/bind`) resolves on the HOST, so a plain unlink through it
deletes the host's file. The removal refuses such a parent and removes nothing;
a secret that is itself a symlink is removed as the link, never its target.
"""

from pathlib import Path

import pytest

from shidashi import system

_KEY = "etc/bind/rndc.key"


def _outside(tmp_path: Path) -> Path:
    """A directory standing for the build host's, holding its own rndc.key."""
    host = tmp_path / "build-host" / "bind"
    host.mkdir(parents=True)
    (host / "rndc.key").write_text("the build host's own key\n")
    return host


@pytest.mark.parametrize("linked", ["etc/bind", "etc"])
def test_a_symlinked_parent_is_refused_and_the_outside_file_stays(
    tmp_path: Path, linked: str
) -> None:
    host = _outside(tmp_path)
    rootfs = tmp_path / "rootfs"
    if linked == "etc":
        rootfs.mkdir()
        (rootfs / "etc").symlink_to(host.parent)  # etc/bind -> build-host/bind
    else:
        (rootfs / "etc").mkdir(parents=True)
        (rootfs / "etc/bind").symlink_to(host)
    with pytest.raises(system.ConfigurationError) as caught:
        system.remove_generated_secrets(rootfs)
    assert f"/{linked}" in str(caught.value)
    assert (host / "rndc.key").read_text() == "the build host's own key\n"


def test_a_key_that_is_a_symlink_is_removed_as_the_link(tmp_path: Path) -> None:
    host = _outside(tmp_path)
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc/bind").mkdir(parents=True)
    (rootfs / _KEY).symlink_to(host / "rndc.key")
    assert system.remove_generated_secrets(rootfs) == [f"/{_KEY}"]
    assert not (rootfs / _KEY).is_symlink()
    assert (host / "rndc.key").is_file()


def test_a_real_key_in_a_real_directory_is_still_removed(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc/bind").mkdir(parents=True)
    (rootfs / _KEY).write_text("generated at build time\n")
    assert system.remove_generated_secrets(rootfs) == [f"/{_KEY}"]
    assert not (rootfs / _KEY).exists()
