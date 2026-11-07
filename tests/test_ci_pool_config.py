"""The host's CI pool config, ``ci.toml`` (story 034, task 4.2; R6.9, Q4).

Contract (design.md, "Config ``~/.config/shidashi/ci.toml``"): ``load_config(path) ->
PoolConfig`` -- ``worker``, ``token_file`` (``~`` expanded; a regular file of the caller's,
mode 0600), ``label`` (default ``bentoo-worker``), ``repos`` (non-empty, each
``OWNER/NAME``), ``source`` (``~`` expanded; a git work tree -- the checkout whose HEAD is
shipped to the worker -- by default the work tree of the running ``shidashi`` package) and
``[vm]`` (``memory_gib`` 8, ``disk_gib`` 64, ``timeout_min`` 360, ``min_free_gib`` 20,
``poll_s`` 30; positive integers; no ``cpus``: the worker derives them) as
``cfg.vm.<name>``. Anything else, an unknown ``[vm]`` key included: ``PoolConfigError``
naming the file and the field (R6.9).
"""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import shidashi
from tests._fake_ci_pool import LABEL, write_config
from tests._fake_github_api import mint_token
from tests._pending import try_import

load_config: Any = try_import("shidashi.ci_pool", "load_config")
PoolConfigError: Any = try_import("shidashi.ci_pool", "PoolConfigError")

REPO = "obentoo/private-app"
OTHER = "obentoo/private-lib"


def git(*args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", *args], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_config_loads_the_fields_and_the_designs_defaults(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    assert cfg.worker == "bentoo-lab"
    assert Path(cfg.token_file) == tmp_path / "github-ci.token"
    assert cfg.label == LABEL
    assert list(cfg.repos) == [REPO, OTHER]
    vm = cfg.vm
    assert (vm.memory_gib, vm.disk_gib, vm.timeout_min) == (8, 64, 360)
    assert (vm.min_free_gib, vm.poll_s) == (20, 30)
    assert not hasattr(vm, "cpus"), "the worker derives the vCPUs; the host sets none"


def test_config_expands_the_token_file_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    home_token = tmp_path / ".config" / "shidashi" / "github-ci.token"
    home_token.parent.mkdir(parents=True)
    home_token.write_text(mint_token() + "\n", encoding="utf-8")
    home_token.chmod(0o600)
    path = write_config(tmp_path / "cfg", token_file="~/.config/shidashi/github-ci.token")
    assert Path(load_config(path).token_file) == home_token


@pytest.mark.parametrize(
    ("changes", "token_mode", "field"),
    [
        pytest.param({}, 0o644, "token_file", id="token-0644"),
        pytest.param({}, 0o640, "token_file", id="token-0640"),
        pytest.param({"token_file": "/nonexistent/token"}, 0o600, "token_file", id="token-missing"),
        pytest.param({"token_file": None}, 0o600, "token_file", id="token-unset"),
        pytest.param({"worker": None}, 0o600, "worker", id="worker-unset"),
        pytest.param({"repos": []}, 0o600, "repos", id="repos-empty"),
        pytest.param({"repos": ["obentoo"]}, 0o600, "repos", id="repo-no-name"),
        pytest.param({"repos": ["obentoo/app/x"]}, 0o600, "repos", id="repo-extra"),
        pytest.param({"repos": ["obentoo/app "]}, 0o600, "repos", id="repo-space"),
        pytest.param({"repos": ["obentoo/app?x=1"]}, 0o600, "repos", id="repo-query"),
        pytest.param({"repos": "obentoo/app"}, 0o600, "repos", id="repos-not-list"),
        pytest.param({"label": "bentoo worker"}, 0o600, "label", id="label-space"),
        pytest.param({"label": ""}, 0o600, "label", id="label-empty"),
        pytest.param({"vm": {"memory_gib": 0}}, 0o600, "memory_gib", id="memory-0"),
        pytest.param({"vm": {"memory_gib": -1}}, 0o600, "memory_gib", id="memory-negative"),
        pytest.param({"vm": {"memory_gib": "8"}}, 0o600, "memory_gib", id="memory-string"),
        # hostile: TOML's true is a Python bool, and a bool is an int
        pytest.param({"vm": {"disk_gib": True}}, 0o600, "disk_gib", id="disk-bool"),
        pytest.param({"vm": {"timeout_min": 0}}, 0o600, "timeout_min", id="timeout-0"),
        pytest.param({"vm": {"poll_s": 0}}, 0o600, "poll_s", id="poll-0"),
    ],
)
def test_config_a_bad_value_names_the_file_and_the_field(
    tmp_path: Path, changes: dict[str, Any], token_mode: int, field: str
) -> None:
    """R6.9."""
    path = write_config(tmp_path, token_mode=token_mode, **changes)
    with pytest.raises(PoolConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message and field in message, message


@pytest.mark.parametrize(
    "raw", [pytest.param(None, id="missing"), pytest.param("worker = [\n", id="not-toml")]
)
def test_config_a_missing_or_unreadable_file_is_named(tmp_path: Path, raw: str | None) -> None:
    path = write_config(tmp_path, raw=raw or "")
    if raw is None:
        path.unlink()
    with pytest.raises(PoolConfigError) as caught:
        load_config(path)
    assert str(path) in str(caught.value)


def test_config_accepts_owner_names_github_allows(tmp_path: Path) -> None:
    """Hostile split of the repo rule: dots, underscores, dashes."""
    cfg = load_config(write_config(tmp_path, repos=["my.org_1/re_po-2.x", "a/b"]))
    assert list(cfg.repos) == ["my.org_1/re_po-2.x", "a/b"]


def test_config_source_defaults_to_the_work_tree_of_the_running_package(tmp_path: Path) -> None:
    """The checkout whose HEAD is shipped to the worker: by default this very one."""
    cfg = load_config(write_config(tmp_path))
    assert str(cfg.source) and Path(cfg.source).is_absolute(), cfg.source
    here = git("-C", str(Path(shidashi.__file__).resolve().parent), "rev-parse", "--show-toplevel")
    assert Path(cfg.source).resolve() == Path(here).resolve()


def test_config_source_names_another_work_tree_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    checkout = tmp_path / "Projetos" / "shidashi"
    git("init", "-q", str(checkout))
    cfg = load_config(write_config(tmp_path / "cfg", source="~/Projetos/shidashi"))
    assert Path(cfg.source).resolve() == checkout.resolve()


@pytest.mark.parametrize(
    "spoil",
    [
        # hostile: a directory that only LOOKS like a checkout
        pytest.param("empty-dot-git", id="empty-dot-git"),
        pytest.param("plain-dir", id="plain-dir"),
        pytest.param("missing", id="missing"),
        pytest.param("file", id="file"),
    ],
)
def test_config_a_source_that_is_not_a_git_work_tree_is_named(tmp_path: Path, spoil: str) -> None:
    """R6.9: refused before any GitHub call, naming the file and ``source``."""
    source = tmp_path / "not-a-checkout"
    if spoil == "empty-dot-git":
        (source / ".git").mkdir(parents=True)
    elif spoil == "plain-dir":
        source.mkdir()
    elif spoil == "file":
        source.write_text("x", encoding="utf-8")
    path = write_config(tmp_path / "cfg", source=str(source))
    with pytest.raises(PoolConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message and "source" in message, message


def test_config_a_cpus_key_is_refused_as_unknown(tmp_path: Path) -> None:
    """The vCPUs are the worker's to derive (half its CPUs, R7.1): a ``[vm] cpus`` left from
    an older ci.toml must not look honoured."""
    assert load_config(write_config(tmp_path / "ok")).worker == "bentoo-lab"
    path = write_config(tmp_path / "cfg", vm={"cpus": 4})
    with pytest.raises(PoolConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message and "cpus" in message, message
