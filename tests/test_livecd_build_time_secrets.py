"""Story 007, sub-task 1.1 -- the build-time secret deny list as data in livecd.yaml.

The deny list (``build_time_secrets``) and its allowlist (``build_time_secrets_allow``)
live beside ``squashfs_exclude`` and follow its pattern rules: relative to the
rootfs, ``*`` never crossing a ``/``. A pattern that is not relative to the rootfs
(absolute, or climbing out of it with a ``..`` segment) is refused when the file
loads, so a typo cannot silently match the BUILD HOST's files or nothing at all.
"""

from pathlib import Path

import pytest
import yaml

from shidashi import config, system
from shidashi.assembler import write_squashfs_exclude

#: The story's initial deny list (Constraints).
_INITIAL = (
    "etc/bind/rndc.key",
    "etc/ssh/ssh_host_*_key",
    "var/lib/systemd/random-seed",
    "var/lib/systemd/credential.secret",
)


def _livecd_with(tmp_path: Path, **lists: list[str]) -> Path:
    """A variants dir whose livecd.yaml is the shipped one plus ``lists``."""
    data = yaml.safe_load((config.variants_dir() / system.LIVECD_FILE).read_text())
    data.update(lists)
    tree = tmp_path / "variants"
    tree.mkdir(parents=True)
    (tree / system.LIVECD_FILE).write_text(yaml.safe_dump(data))
    return tree


def test_the_shipped_livecd_declares_the_initial_deny_list_and_an_empty_allowlist() -> None:
    livecd = system.load_livecd(config.variants_dir())
    assert set(_INITIAL) <= set(livecd.build_time_secrets)
    assert tuple(livecd.build_time_secrets_allow) == ()


def test_a_livecd_without_the_lists_loads_them_empty() -> None:
    livecd = system.LivecdFile.model_validate({"user": "u", "password": "p"})
    assert tuple(livecd.build_time_secrets) == ()
    assert tuple(livecd.build_time_secrets_allow) == ()


@pytest.mark.parametrize("key", ["build_time_secrets", "build_time_secrets_allow"])
@pytest.mark.parametrize("pattern", ["/etc/bind/rndc.key", "../etc/bind/rndc.key", "etc/../../x"])
def test_a_pattern_not_relative_to_the_rootfs_is_refused_at_load(
    tmp_path: Path, key: str, pattern: str
) -> None:
    # the key itself is accepted: what is refused is the pattern, not the field
    accepted = _livecd_with(tmp_path / "ok", **{key: ["etc/ok"]})
    assert tuple(getattr(system.load_livecd(accepted), key)) == ("etc/ok",)
    tree = _livecd_with(tmp_path / "bad", **{key: ["etc/ok", pattern]})
    with pytest.raises(system.ConfigurationError) as caught:
        system.load_livecd(tree)
    assert pattern in str(caught.value)  # the offending pattern is named


@pytest.mark.parametrize("key", ["build_time_secrets", "build_time_secrets_allow"])
def test_dots_inside_a_name_are_not_a_parent_reference(tmp_path: Path, key: str) -> None:
    """Hostile half of the rule above: only a ``..`` SEGMENT leaves the rootfs;
    a file name with two dots in it is an ordinary relative pattern."""
    patterns = ["etc/ssh/key..old", "var/lib/x/*..bak", "etc/.hidden"]
    tree = _livecd_with(tmp_path, **{key: patterns})
    assert tuple(getattr(system.load_livecd(tree), key)) == tuple(patterns)


def test_the_deny_list_never_reaches_the_squashfs_exclude_file(tmp_path: Path) -> None:
    """Unchanged: the squashfs still leaves out exactly squashfs_exclude. A secret
    is refused by verify-config, never quietly excluded from the image."""
    livecd = system.load_livecd(config.variants_dir())
    written = write_squashfs_exclude(livecd.squashfs_exclude, tmp_path / "exclude")
    lines = written.read_text().splitlines()
    assert lines == list(livecd.squashfs_exclude)
    assert not set(livecd.build_time_secrets) & set(lines)
