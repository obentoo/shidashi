"""Spike 012 (2026-10-06): nspawn nested in an OCI container needs ``--keep-unit``.

Even with ``--register=no``, systemd-nspawn asks systemd over D-Bus for a transient
scope unit to run the container in; inside a container there is no bus, and it
fails with "Failed to open system bus". ``--keep-unit`` keeps the container in the
caller's own unit and needs no bus. It is added only inside a container: on the
host, the builder VM and the worker, the command line is unchanged.
"""

from pathlib import Path

import pytest

from shidashi import container
from shidashi.container import _nspawn_argv, _nspawn_shell_argv, inside_container


def test_the_command_line_gains_keep_unit_only_when_asked() -> None:
    plain = _nspawn_argv(Path("/r"), ["true"], binds=[], ephemeral=False)
    nested = _nspawn_argv(Path("/r"), ["true"], binds=[], ephemeral=False, keep_unit=True)
    assert "--keep-unit" not in plain
    assert "--keep-unit" in nested
    assert nested.index("--keep-unit") < nested.index("--")  # an nspawn option, not the command
    assert [a for a in nested if a != "--keep-unit"] == plain


def test_the_shell_gains_keep_unit_only_when_asked() -> None:
    assert "--keep-unit" not in _nspawn_shell_argv(Path("/r"))
    assert "--keep-unit" in _nspawn_shell_argv(Path("/r"), keep_unit=True)


@pytest.fixture
def no_marks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv("container", raising=False)
    monkeypatch.setattr(container, "_CONTAINER_MARKS", (tmp_path / "containerenv",))
    return tmp_path / "containerenv"


def test_a_host_is_not_a_container(no_marks: Path) -> None:
    assert inside_container() is False


@pytest.mark.parametrize("value", ["podman", "docker", "systemd-nspawn", "oci"])
def test_the_container_variable_marks_a_container(
    no_marks: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("container", value)
    assert inside_container() is True


def test_an_empty_container_variable_is_not_a_mark(
    no_marks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("container", "")
    assert inside_container() is False


def test_a_runtime_marker_file_marks_a_container(no_marks: Path) -> None:
    no_marks.write_text("")  # podman's /run/.containerenv, docker's /.dockerenv
    assert inside_container() is True
