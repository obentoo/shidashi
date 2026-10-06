"""Story 007, sub-task 2.1 -- verify(live=True) names every build-time secret.

A path is a build-time secret when a ``build_time_secrets`` pattern matches it and
no ``build_time_secrets_allow`` pattern does. Patterns are relative to the rootfs
and ``*`` never crosses a ``/`` (the rules of ``squashfs_exclude``). A path is the
image's own directory entry: a symlink is matched by its own path and never
followed (followed on the build host, it would read the HOST's files).

Per the hostile-half rule, every test plants the near-miss that must NOT be
reported beside the match that must be, so neither half passes vacuously.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from shidashi import config, system
from tests.test_system import _configure

#: The deny list these tests load: exact names, a `*` inside a name, a `*` as a
#: whole segment, and a pattern whose parent directory no image has.
_DENY = (
    "etc/bind/rndc.key",
    "etc/ssh/ssh_host_*_key",
    "var/lib/systemd/credential.secret",
    "etc/*.secret",
    "opt/absent/*.key",
)


def _cfg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deny: tuple[str, ...] = _DENY,
    allow: tuple[str, ...] = (),
) -> system.SystemConfig:
    """kde/systemd's configuration from a copy of variants/ whose livecd.yaml
    carries ``deny`` and ``allow``."""
    tree = tmp_path / "variants"
    shutil.copytree(config.variants_dir(), tree)
    livecd = tree / system.LIVECD_FILE
    data = yaml.safe_load(livecd.read_text())
    data["build_time_secrets"] = list(deny)
    data["build_time_secrets_allow"] = list(allow)
    livecd.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(tree))
    recipe = config.load_recipe("v3", "kde", "systemd")
    return system.load_system_config(recipe, variants_dir=tree)


def _plant(rootfs: Path, *paths: str) -> None:
    for rel in paths:
        path = rootfs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated at build time\n")


def _named(problems: list[str], path: str) -> int:
    """How many problems name ``path``."""
    return sum(path in p for p in problems)


def _image(tmp_path: Path, cfg: system.SystemConfig) -> Path:
    """A configured, clean live image (verify finds nothing), in ``tmp_path/img``."""
    image, _ = _configure(tmp_path / "img", cfg)
    assert system.verify(image.rootfs, cfg, init="systemd", live=True) == []
    return image.rootfs


def _verify(rootfs: Path, cfg: system.SystemConfig) -> list[str]:
    return system.verify(rootfs, cfg, init="systemd", live=True)


# --- the hostile halves first: what must NOT collapse into a match ---------------------


def test_a_star_never_crosses_a_slash_nor_reaches_a_public_neighbor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    rootfs = _image(tmp_path, cfg)
    near_misses = (
        "etc/ssh/ssh_host_ed25519_key.pub",  # the PUBLIC half of a host key
        "etc/ssh/ssh_host_rsa_key.pub",
        "etc/ssh/ssh_host_x/y_key",  # `ssh_host_*_key` only if `*` crossed the `/`
        "etc/sub/deep.secret",  # `etc/*.secret` only if `*` crossed the `/`
        "etc/bind/rndc.conf",  # rndc.key's public neighbor
    )
    _plant(rootfs, *near_misses, "etc/ssh/ssh_host_rsa_key")
    problems = _verify(rootfs, cfg)
    assert len(problems) == 1 and _named(problems, "etc/ssh/ssh_host_rsa_key") == 1
    for path in near_misses:
        assert _named(problems, path) == 0, f"{path} is not a build-time secret"


def test_an_allowed_path_does_not_exempt_its_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch, allow=("etc/ssh/ssh_host_ed25519_key",))
    rootfs = _image(tmp_path, cfg)
    _plant(rootfs, "etc/ssh/ssh_host_ed25519_key", "etc/ssh/ssh_host_rsa_key")
    problems = _verify(rootfs, cfg)
    assert _named(problems, "etc/ssh/ssh_host_ed25519_key") == 0  # allowed
    assert _named(problems, "etc/ssh/ssh_host_rsa_key") == 1  # its sibling is not
    assert len(problems) == 1


def test_an_allow_star_never_crosses_a_slash_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch, allow=("etc/*",))
    rootfs = _image(tmp_path, cfg)
    _plant(rootfs, "etc/top.secret", "etc/bind/rndc.key")
    problems = _verify(rootfs, cfg)
    assert _named(problems, "etc/top.secret") == 0  # `etc/*` names it
    assert _named(problems, "etc/bind/rndc.key") == 1  # `etc/*` does not reach etc/bind/
    assert len(problems) == 1


def test_a_symlinked_directory_is_never_followed_onto_the_build_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the image, etc/ssh -> /<host dir> points at nothing; followed on the
    build host it would find the host's own file and report it."""
    cfg = _cfg(tmp_path, monkeypatch)
    rootfs = _image(tmp_path, cfg)
    outside = tmp_path / "build-host-ssh"
    _plant(outside, "ssh_host_rsa_key")
    (rootfs / "etc/ssh").symlink_to(outside)
    _plant(rootfs, "etc/bind/rndc.key")
    problems = _verify(rootfs, cfg)
    assert _named(problems, "ssh_host_rsa_key") == 0
    assert _named(problems, "etc/bind/rndc.key") == 1 and len(problems) == 1


# --- then what must NOT split: every differently-shaped match is reported ----------------


def test_every_match_is_reported_once_each_naming_its_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    rootfs = _image(tmp_path, cfg)
    secrets = (
        "etc/bind/rndc.key",
        "etc/ssh/ssh_host_rsa_key",
        "etc/ssh/ssh_host_ecdsa_key",
        "etc/ssh/ssh_host_ed25519_key",
        "var/lib/systemd/credential.secret",
        "etc/top.secret",
    )
    _plant(rootfs, *secrets)
    problems = _verify(rootfs, cfg)
    assert len(problems) == len(secrets)
    for path in secrets:
        assert _named(problems, path) == 1, f"{path} named {_named(problems, path)} times"


def test_a_dangling_symlink_at_a_denied_path_is_reported_by_its_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    rootfs = _image(tmp_path, cfg)
    (rootfs / "etc/bind").mkdir(parents=True)
    (rootfs / "etc/bind/rndc.key").symlink_to("../../run/nowhere/rndc.key")
    problems = _verify(rootfs, cfg)
    assert len(problems) == 1 and _named(problems, "etc/bind/rndc.key") == 1


def test_an_allow_pattern_with_a_star_exempts_every_path_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch, allow=("etc/ssh/ssh_host_*_key",))
    rootfs = _image(tmp_path, cfg)
    _plant(rootfs, "etc/ssh/ssh_host_rsa_key", "etc/ssh/ssh_host_ed25519_key")
    _plant(rootfs, "etc/bind/rndc.key")
    problems = _verify(rootfs, cfg)
    assert len(problems) == 1 and _named(problems, "etc/bind/rndc.key") == 1


# --- the benign cases ---------------------------------------------------------------------


def test_a_clean_image_and_a_missing_parent_directory_are_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`opt/absent/*.key` names a directory the image does not have: no match, no
    error. A clean image has nothing to report."""
    cfg = _cfg(tmp_path, monkeypatch)
    rootfs = _image(tmp_path, cfg)
    assert not (rootfs / "opt").exists()
    assert _verify(rootfs, cfg) == []
    _plant(rootfs, "etc/bind/rndc.key")
    assert _named(_verify(rootfs, cfg), "etc/bind/rndc.key") == 1


def test_the_existing_checks_keep_their_messages_beside_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged: every check verify ran before still runs, with the same message."""
    cfg = _cfg(tmp_path, monkeypatch)
    rootfs = _image(tmp_path, cfg)
    (rootfs / "etc/hostname").write_text("gentoo\n")
    _plant(rootfs, "etc/bind/rndc.key")
    problems = _verify(rootfs, cfg)
    assert "/etc/hostname is 'gentoo', not 'bentoo'" in problems
    assert _named(problems, "etc/bind/rndc.key") == 1
    assert len(problems) == 2
