"""Each read-only container run gets its own nspawn machine name (story 026, task 1.1).

On systemd 262 two read-only nspawns on one directory collide on the default machine
name (the directory's basename): the second fails with "Mount point
'/run/systemd/nspawn/<name>/unix-export' exists already, refusing." -- measured on the
bentoo-lab on 2026-10-14. A unique ``--machine`` per read-only run lets concurrent
assembles share the toolbox (story 006, R3.5); a writable container is unchanged.
"""

import re
import subprocess
from pathlib import Path

import pytest

from shidashi import container
from shidashi.container import Container, _nspawn_argv
from shidashi.toolbox import Toolbox, ToolboxError

_MACHINE = re.compile(r"^--machine=([A-Za-z0-9._-]{1,64})$")
_COLLISION = "Mount point '/run/systemd/nspawn/v3-systemd/unix-export' exists already, refusing."


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    cmds: list[list[str]] = []

    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(container, "inside_container", lambda: False)
    return cmds


def _machines(cmd: list[str]) -> list[str]:
    return [m.group(1) for a in cmd[: cmd.index("--")] if (m := _MACHINE.match(a))]


def test_the_argv_carries_the_machine_before_the_command() -> None:
    cmd = _nspawn_argv(Path("/t/v3-systemd"), ["true"], binds=(), ephemeral=False, machine="m-1")
    assert "--machine=m-1" in cmd[: cmd.index("--")]
    plain = _nspawn_argv(Path("/t/v3-systemd"), ["true"], binds=(), ephemeral=False)
    assert not any(a.startswith("--machine") for a in plain)


def test_two_runs_of_one_read_only_container_use_two_machines(
    tmp_path: Path, launched: list[list[str]]
) -> None:
    box = Container(tmp_path / "v3-systemd", ephemeral=False, read_only=True)
    box.run(["true"])
    box.run(["true"])
    (first,), (second,) = (_machines(c) for c in launched)
    assert first != second


def test_a_writable_container_names_no_machine(tmp_path: Path, launched: list[list[str]]) -> None:
    Container(tmp_path / "image", ephemeral=False).run(["true"])
    (cmd,) = launched
    assert _machines(cmd) == []
    assert not any(a.startswith("--machine") for a in cmd)


def test_an_odd_or_long_rootfs_name_gives_a_valid_machine(
    tmp_path: Path, launched: list[list[str]]
) -> None:
    Container(tmp_path / ("tool box:@" + "x" * 90), ephemeral=False, read_only=True).run(["true"])
    (cmd,) = launched
    assert len(_machines(cmd)) == 1  # the pattern bounds charset and length


def test_a_failed_probe_says_what_nspawn_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", _COLLISION + "\n")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(container, "inside_container", lambda: False)
    with pytest.raises(ToolboxError) as err:
        Toolbox(tmp_path / "v3-systemd").versions()
    assert "exists already, refusing" in str(err.value), str(err.value)
