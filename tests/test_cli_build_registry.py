"""`shidashi build` and the invoking user's worker registry (story 014, R6.2, R6.4).

R6.2 (v5, story 028's fail-closed read): a registry that EXISTS but cannot be read --
malformed, the wrong shape, a directory, unreadable -- or an invoking user that cannot
be resolved exits 1 before planning, naming the registry (or the user) and why. R6.4: a
registry that does not exist plans without workers and builds.

The two rules are converses, so each is pinned by its hostile half first:

- an unreadable registry must never collapse into "no registry" (planning host-only);
- an absent or empty-but-valid registry must never split into "an unreadable one"
  (refusing a build that should run);
- under sudo the registry is SUDO_USER's, never root's: a broken registry of the other
  user must neither refuse nor be taken for the invoking user's.

"Before planning" is observed as: discovery never called, no ``plan`` step in any build
run, nothing executed, no factory, no assemble child. Each case also runs under
``--local``, which plans on the host alone and must refuse the same way.

The fakes are tests/test_cli_build_schedule.py's ``lab`` fixture.
"""

import json
import os
import pwd
from pathlib import Path
from typing import Any

import pytest

from shidashi import config
from tests.test_cli_build_schedule import HOST, WORKER, Lab, _build, lab

__all__ = ["lab"]  # the fixture, reused

MODES = pytest.mark.parametrize("mode", [(), ("--local",)], ids=["planned", "local"])


def _registry() -> Path:
    return config.workers_dir() / "workers.json"


def _plan_steps(root: Path) -> list[dict[str, Any]]:
    """Every ``plan`` step recorded by any build run under ``root/runs``."""
    steps: list[dict[str, Any]] = []
    for path in sorted((root / "runs").glob("*/manifest.json")):
        manifest = json.loads(path.read_text())
        if manifest.get("command") != "build":
            continue
        steps += [
            s for s in manifest.get("steps", []) if str(s.get("step")).split("/")[-1] == "plan"
        ]
    return steps


def _refused_before_planning(lab: Lab, result: Any) -> None:
    assert result.exit_code == 1, result.output
    assert lab.discovered == [], "the build discovered nodes before refusing"
    assert _plan_steps(lab.root) == [], "the build recorded a plan before refusing"
    assert lab.executed == [], "the build executed a plan"
    assert lab.calls == [], f"the build ran {lab.calls}"
    assert lab.children == [], f"the build started assemble children {lab.children}"


def _planned_host_only(lab: Lab, result: Any) -> None:
    assert result.exit_code == 0, result.output
    assert lab.discovered, "the build never planned"
    ((plan, _specs, kw),) = lab.executed
    assert set(plan.assignments.values()) == {"dojo"}
    assert kw["options"].cache_owner is None


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- hostile half 1: an existing registry that cannot be read is not "no registry" -------


@MODES
@pytest.mark.parametrize(
    "content",
    ["{not json", "", '["bentoo-lab"]', '{"bentoo-lab": {"name": "bentoo-lab"}}'],
    ids=["malformed", "empty-file", "not-an-object", "entry-missing-fields"],
)
def test_an_unreadable_registry_exits_1_naming_it_before_planning(
    lab: Lab, mode: tuple[str, ...], content: str
) -> None:
    lab.nodes = [HOST, WORKER]
    path = _write(_registry(), content)
    result = _build(*mode)
    _refused_before_planning(lab, result)
    assert str(path) in result.output, result.output
    assert "registry" in result.output, result.output  # and why


@MODES
def test_a_directory_where_the_registry_should_be_exits_1_naming_it(
    lab: Lab, mode: tuple[str, ...]
) -> None:
    path = _registry()
    path.mkdir(parents=True)
    result = _build(*mode)
    _refused_before_planning(lab, result)
    assert str(path) in result.output, result.output


@MODES
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_registry_without_read_permission_exits_1_naming_it(
    lab: Lab, mode: tuple[str, ...]
) -> None:
    path = _write(_registry(), "{}")
    path.chmod(0)
    try:
        result = _build(*mode)
    finally:
        path.chmod(0o600)
    _refused_before_planning(lab, result)
    assert str(path) in result.output, result.output


# --- hostile half 2: an absent or empty registry is not an unreadable one ----------------


@MODES
def test_an_absent_registry_plans_host_only_and_builds(lab: Lab, mode: tuple[str, ...]) -> None:
    assert not _registry().parent.exists()  # not even the workers directory
    result = _build(*mode)
    _planned_host_only(lab, result)
    assert "error" not in result.output.lower(), result.output


@MODES
def test_a_workers_directory_without_a_registry_plans_host_only(
    lab: Lab, mode: tuple[str, ...]
) -> None:
    _registry().parent.mkdir(parents=True)  # the worker key's home, no workers.json yet
    result = _build(*mode)
    _planned_host_only(lab, result)


@MODES
def test_an_empty_but_valid_registry_plans_host_only(lab: Lab, mode: tuple[str, ...]) -> None:
    _write(_registry(), "{}")
    result = _build(*mode)
    _planned_host_only(lab, result)


# --- the invoking user under sudo (R6.1 + R6.2) --------------------------------------------


def _sudo_as(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user: str, home: Path | None) -> Path:
    """Run as root under ``sudo -u``-style env for ``user``; ``home`` None = no such user.

    Root's own registry lives under ``$HOME``; SUDO_USER's under their passwd home.
    Returns root's registry path."""
    root_home = tmp_path / "root-home"
    monkeypatch.setenv("SUDO_USER", user)
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1000")
    monkeypatch.setenv("HOME", str(root_home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    real = pwd.getpwnam

    def getpwnam(name: str) -> pwd.struct_passwd:
        if name == user:
            if home is None:
                raise KeyError(f"getpwnam(): name not found: {name!r}")
            return pwd.struct_passwd((user, "x", 1000, 1000, "", str(home), "/bin/bash"))
        return real(name)

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    return root_home / ".local" / "share" / "shidashi" / "worker" / "workers.json"


def _user_registry(home: Path) -> Path:
    return home / ".local" / "share" / "shidashi" / "worker" / "workers.json"


@MODES
def test_an_unknown_sudo_user_exits_1_naming_the_user_before_planning(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: tuple[str, ...]
) -> None:
    root_registry = _sudo_as(monkeypatch, tmp_path, "ghost-user-014", None)
    _write(root_registry, "{}")  # root's registry is readable: it must not stand in
    result = _build("--plan", *mode)
    _refused_before_planning(lab, result)
    assert "ghost-user-014" in result.output, result.output


@MODES
def test_under_sudo_the_invoking_users_broken_registry_refuses_though_roots_is_fine(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: tuple[str, ...]
) -> None:
    home = tmp_path / "home-alice"
    root_registry = _sudo_as(monkeypatch, tmp_path, "alice", home)
    _write(root_registry, "{}")
    broken = _write(_user_registry(home), "{not json")
    result = _build("--plan", *mode)
    _refused_before_planning(lab, result)
    assert str(broken) in result.output, result.output
    assert str(root_registry) not in result.output, result.output


@MODES
def test_under_sudo_roots_broken_registry_does_not_refuse_the_invoking_users_build(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: tuple[str, ...]
) -> None:
    home = tmp_path / "home-alice"
    root_registry = _sudo_as(monkeypatch, tmp_path, "alice", home)
    _write(root_registry, "{not json")  # never read: the registry is alice's
    assert not _user_registry(home).exists()  # alice paired nobody
    result = _build("--plan", *mode)
    assert result.exit_code == 0, result.output
    assert lab.discovered, "the build never planned"
    assert str(root_registry) not in result.output, result.output
    steps = _plan_steps(lab.root)
    assert steps and set(steps[0].get("assignments", {}).values()) == {"dojo"}, steps
