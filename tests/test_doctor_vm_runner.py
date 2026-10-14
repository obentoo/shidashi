"""The doctor's vm checks on a node without QEMU (story 024, task 3.3; R3.2, R3.3).

Contract (design.md, Doctor): when ``qemu-system-x86_64`` is missing but podman and the
runner tarball (the v3-systemd toolbox) are present, the ``qemu-system-x86_64`` and
``xorriso`` checks of scope ``vm`` read ok with the detail ``missing here: boot tests run
in the v3-systemd toolbox container``. Without podman or without the tarball they stay
failed, the detail naming the fix. A node with QEMU is reported as before. vm checks
never block a build.

The host is described through the probes, as in tests/test_doctor.py; the runner tarball
is a real file (or none) under a temporary cache, at the path the toolbox key gives it.
"""

import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shidashi import config, doctor, toolbox
from shidashi.cli import app
from shidashi.doctor import Check, checks, missing
from shidashi.recipe import TOOLBOX_STAGE
from shidashi.seed import load_pointer
from shidashi.tree import load_pin_id

runner = CliRunner()

_BUILD_TOOLS = {"systemd-nspawn", "tar", "gpg", "git", "openssl", "objdump", "gcc", "findmnt"}
_CONTAINER = "v3-systemd toolbox container"


class _Node:
    """A host with ``tools`` on PATH and both VM devices."""

    def __init__(self, tools: set[str]) -> None:
        self.tools = tools

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.tools else None

    def run(self, argv: Sequence[str]) -> str | None:
        answers = {
            "systemd-nspawn": "systemd 262 (262)\n",
            "tar --version": "tar (GNU tar) 1.35\n",
            "tar --help": "  --acls\n  --xattrs\n",
            "findmnt": "btrfs\n",
            "gcc": "  -march=                     \t\tznver3\n",
        }
        if argv[0] not in self.tools:
            return None
        return answers.get(" ".join(argv[:2]) if argv[0] == "tar" else argv[0])

    def access(self, path: str, mode: int) -> bool:
        if str(path).startswith("/dev/"):
            return True
        return os.access(path, mode)


def _runner_tarball() -> Path:
    recipe = config.load_recipe("v3", TOOLBOX_STAGE, "systemd")
    return toolbox.tarball_path(
        recipe,
        snapshot=load_pointer("systemd", seeds_dir=config.seeds_dir()).snapshot,
        pins=load_pin_id(config.seeds_dir()),
        fork_points_dir=config.fork_points_dir(),
    )


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    return tmp_path / "cache"


def _with_tarball() -> Path:
    tarball = _runner_tarball()
    tarball.parent.mkdir(parents=True, exist_ok=True)
    tarball.write_bytes(b"toolbox")
    return tarball


def _checks(node: _Node) -> dict[str, Check]:
    found = checks(Path("/work"), which=node.which, run=node.run, access=node.access, euid=0)
    assert missing(found, "build") == []  # vm checks never block a build
    return {c.name: c for c in found}


# --- hostile half -------------------------------------------------------------------------


def test_a_node_with_qemu_is_reported_as_before(cache: Path) -> None:
    """Wrong collapse: podman and the tarball on a QEMU host change nothing."""
    _with_tarball()
    found = _checks(_Node(_BUILD_TOOLS | {"qemu-system-x86_64", "xorriso", "podman"}))
    assert found["qemu-system-x86_64"].ok
    assert found["qemu-system-x86_64"].detail == "/usr/bin/qemu-system-x86_64"
    assert found["xorriso"].detail == "/usr/bin/xorriso"


def test_podman_without_the_tarball_is_not_a_runner(cache: Path) -> None:
    """R3.3, wrong collapse: podman alone serves no boot test."""
    found = _checks(_Node(_BUILD_TOOLS | {"podman"}))
    for name in ("qemu-system-x86_64", "xorriso"):
        assert not found[name].ok, name
        assert "shidashi factory v3 toolbox systemd" in found[name].detail
        assert _CONTAINER not in found[name].detail


def test_the_tarball_without_podman_is_not_a_runner(cache: Path) -> None:
    """R3.3: the tarball alone serves no boot test; the detail names podman."""
    _with_tarball()
    found = _checks(_Node(set(_BUILD_TOOLS)))
    for name in ("qemu-system-x86_64", "xorriso"):
        assert not found[name].ok, name
        assert "podman" in found[name].detail


def test_an_unresolvable_toolbox_is_reported_not_raised(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.3: no pins to key the toolbox -- the doctor still answers, the checks failed."""
    empty = tmp_path / "no-seeds"
    empty.mkdir()
    monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(empty))
    found = _checks(_Node(_BUILD_TOOLS | {"podman"}))
    assert not found["qemu-system-x86_64"].ok
    assert not found["xorriso"].ok


# --- R3.2: the plain case -----------------------------------------------------------------


def test_podman_and_the_tarball_satisfy_the_boot_test_tools(cache: Path) -> None:
    _with_tarball()
    found = _checks(_Node(_BUILD_TOOLS | {"podman"}))
    for name in ("qemu-system-x86_64", "xorriso"):
        assert found[name].ok, found[name]
        assert found[name].scope == "vm"
        assert found[name].detail == f"missing here: boot tests run in the {_CONTAINER}"
    vm_missing = {c.name for c in missing(list(found.values()), "vm")}
    assert not vm_missing & {"qemu-system-x86_64", "xorriso"}


def test_the_doctor_command_does_not_ask_for_qemu_on_such_a_node(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_tarball()
    monkeypatch.setenv("COLUMNS", "500")
    node = _Node(_BUILD_TOOLS | {"podman"})
    monkeypatch.setattr(
        doctor,
        "checks",
        lambda work_dir: checks(
            work_dir, which=node.which, run=node.run, access=node.access, euid=0
        ),
    )
    done = runner.invoke(app, ["doctor"])
    assert done.exit_code == 0, done.output
    assert "also needs: qemu-system-x86_64" not in done.output
    assert _CONTAINER in done.output
