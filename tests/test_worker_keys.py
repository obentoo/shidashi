"""The restorable keys of an arch, here and at a commit (story 019, task 5.1; R3.1,
R3.5). A sync names an arch, not an init: the keys are the union over every init.
Expected keys are derived independently: ``load_pin_id`` + ``build_key`` of every
recipe ``shidashi factory`` builds, plus the bootstrap's (the arch × init's base).
``keys_at`` reads a real git repository holding copies of this checkout's
``variants/`` and ``seeds/``.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, phases, remote
from shidashi.tree import load_pin_id
from tests._pending import try_import

restorable_keys: Any = try_import("shidashi.phases", "restorable_keys")
keys_at: Any = try_import("shidashi.worker", "keys_at")


def _expected(arch: str) -> frozenset[str]:
    pins = load_pin_id(config.seeds_dir())
    keys = set()
    for init in config.available_names("init"):
        recipes = [config.load_recipe(arch, t, init) for t in config.factory_names()]
        recipes.append(config.load_recipe(arch, "base", init, any_stage=True))  # bootstrap
        keys |= {f"{pins}-{phases.build_key(r)}" for r in recipes}
    return frozenset(keys)


def _init_key(arch: str, init: str) -> str:
    pins = load_pin_id(config.seeds_dir())
    return f"{pins}-{phases.build_key(config.load_recipe(arch, 'base', init, any_stage=True))}"


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose HEAD holds this checkout's variants/ and seeds/."""
    repo = tmp_path / "repo"
    shutil.copytree(config.variants_dir(), repo / "variants")
    shutil.copytree(config.seeds_dir(), repo / "seeds")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "as the host")
    return repo


def _commit_edit(repo: Path, rel: str, edit: Any) -> str:
    path = repo / rel
    path.write_text(edit(path.read_text()))
    _git(repo, "commit", "-q", "-am", f"edit {rel}")
    return _git(repo, "rev-parse", "HEAD")


def test_restorable_keys_are_the_union_over_every_init_of_the_arch() -> None:
    keys = restorable_keys("v3")
    assert keys == _expected("v3")
    # hostile: one init's keys alone would skip the other init's valid fork points
    assert {_init_key("v3", "systemd"), _init_key("v3", "openrc")} <= keys
    # hostile: another arch compiles with other flags -- never the same key
    assert not keys & restorable_keys("znver5")


def test_keys_at_a_commit_with_the_hosts_tree_are_the_hosts_keys(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    assert keys_at(repo, head, "v3") == _expected("v3")


def test_hostile_keys_at_come_from_the_commits_tree_not_the_hosts(repo: Path) -> None:
    """The job built with ITS commit's flags and pins: the host checkout's say nothing."""
    host = _expected("v3")
    flags = _commit_edit(
        repo,
        "variants/arch/v3/portage/make.conf",
        lambda t: re.sub(r'COMMON_FLAGS="', 'COMMON_FLAGS="-fno-plt ', t, count=1),
    )
    by_flags = keys_at(repo, flags, "v3")
    assert by_flags and not by_flags & host
    assert {k.split("-")[0] for k in by_flags} == {k.split("-")[0] for k in host}  # same pins
    pins = _commit_edit(repo, "seeds/gentoo.toml", lambda t: re.sub(r"[0-9a-f]{128}", "0" * 128, t))
    by_pins = keys_at(repo, pins, "v3")
    assert not by_pins & by_flags and not by_pins & host


@pytest.mark.parametrize("before", [None, "/somewhere/variants"])
def test_keys_at_leaves_the_environment_as_it_found_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch, before: str | None
) -> None:
    for var in ("SHIDASHI_VARIANTS_DIR", "SHIDASHI_SEEDS_DIR"):
        if before is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, before)
    keys_at(repo, _git(repo, "rev-parse", "HEAD"), "v3")
    assert os.environ.get("SHIDASHI_VARIANTS_DIR") == before
    assert os.environ.get("SHIDASHI_SEEDS_DIR") == before


def test_keys_at_an_unknown_commit_is_a_sync_error_naming_it(repo: Path) -> None:
    with pytest.raises(remote.SyncError, match="deadbeef"):
        keys_at(repo, "deadbeef" * 5, "v3")
