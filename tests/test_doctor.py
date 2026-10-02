"""Tests of shidashi.doctor -- what the build host must provide.

Each test describes a host through the probes (``which``, ``run``, ``access``):
nothing depends on the machine the tests run on.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shidashi import doctor
from shidashi.cli import app
from shidashi.doctor import Check, DoctorError, checks, missing

runner = CliRunner()

_TOOLS = {
    "systemd-nspawn",
    "tar",
    "gpg",
    "git",
    "openssl",
    "objdump",
    "qemu-system-x86_64",
    "xorriso",
    "gcc",
    "syft",
    "findmnt",
}


class _Host:
    """A host with ``tools`` installed; nspawn and tar answer as configured."""

    def __init__(
        self,
        tools: set[str] = _TOOLS,
        *,
        nspawn: str = "systemd 262 (262)\n+PAM +AUDIT",
        tar_version: str = "tar (GNU tar) 1.35\n",
        tar_help: str = "  --acls\n  --xattrs\n",
        fstype: str = "btrfs",
        devices: bool = True,
    ) -> None:
        self.tools = tools
        self.answers = {
            "systemd-nspawn": nspawn,
            "tar --version": tar_version,
            "tar --help": tar_help,
            "findmnt": f"{fstype}\n",
        }
        self.devices = devices

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.tools else None

    def run(self, argv: Sequence[str]) -> str | None:
        if argv[0] not in self.tools:
            return None
        key = " ".join(argv[:2]) if argv[0] == "tar" else argv[0]
        return self.answers.get(key)

    def access(self, _path: str, _mode: int) -> bool:
        return self.devices


def _checks(host: _Host, *, python: tuple[int, int] = (3, 14)) -> list[Check]:
    return checks(
        Path("/work"),
        which=host.which,
        run=host.run,
        access=host.access,
        euid=1000,
        python=python,
    )


def _by_name(found: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in found}


def test_a_complete_host_builds_and_boots() -> None:
    found = _checks(_Host())
    assert missing(found, "build") == []
    assert missing(found, "vm") == []
    assert _by_name(found)["systemd-nspawn"].detail == "systemd 262"
    assert "reflinks" in _by_name(found)["work filesystem"].detail


def test_no_gentoo_tool_is_required() -> None:
    """The point of the toolbox: none of the ISO tools is a host requirement."""
    names = {c.name for c in _checks(_Host())}
    assert not names & {"emerge", "portageq", "grub-mkrescue", "mksquashfs", "mformat"}


def test_a_host_without_systemd_cannot_build() -> None:
    found = _checks(_Host(_TOOLS - {"systemd-nspawn"}))
    (lacking,) = missing(found, "build")
    assert lacking.name == "systemd-nspawn" and "systemd-container" in lacking.detail


def test_an_old_nspawn_is_refused() -> None:
    lacking = missing(_checks(_Host(nspawn="systemd 239 (239)")), "build")
    assert [c.name for c in lacking] == ["systemd-nspawn"]
    assert f"needs {doctor.MIN_NSPAWN}" in lacking[0].detail


def test_busybox_tar_is_refused() -> None:
    lacking = missing(_checks(_Host(tar_version="tar (busybox) 1.36.1\n")), "build")
    assert [c.name for c in lacking] == ["tar"] and "GNU tar" in lacking[0].detail


def test_a_tar_without_xattrs_is_refused() -> None:
    lacking = missing(_checks(_Host(tar_help="  --acls\n")), "build")
    assert lacking[0].detail == "GNU tar without --xattrs"


def test_an_old_python_is_refused() -> None:
    lacking = missing(_checks(_Host(), python=(3, 12)), "build")
    assert [c.name for c in lacking] == ["python"] and "uv python install" in lacking[0].detail


def test_vm_and_optional_tools_never_block_a_build() -> None:
    host = _Host(_TOOLS - {"qemu-system-x86_64", "xorriso", "syft", "gcc"}, devices=False)
    found = _checks(host)
    assert missing(found, "build") == []
    assert {c.name for c in missing(found, "vm")} == {
        "qemu-system-x86_64",
        "xorriso",
        "/dev/kvm",
        "/dev/vhost-vsock",
    }
    assert {c.name for c in missing(found, "optional")} == {"gcc", "syft"}


def test_root_and_the_filesystem_are_information_only() -> None:
    found = _checks(_Host(fstype="ext4"))
    by = _by_name(found)
    assert by["root"].scope == "info" and not by["root"].ok
    assert "btrfs makes" in by["work filesystem"].detail
    assert missing(found, "build") == []


def test_require_build_host_names_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest stubs require_build_host for every test; this one uses the real function
    monkeypatch.undo()
    _fake_checks(monkeypatch, _Host(_TOOLS - {"gpg", "git"}))
    with pytest.raises(DoctorError, match=r"gpg \(missing.*git \(missing.*shidashi doctor"):
        doctor.require_build_host(Path("/work"))


# --- the CLI ---------------------------------------------------------------------


def _fake_checks(monkeypatch: pytest.MonkeyPatch, host: _Host) -> None:
    monkeypatch.setattr(
        doctor,
        "checks",
        lambda work_dir: checks(
            work_dir, which=host.which, run=host.run, access=host.access, euid=0
        ),
    )


def test_doctor_command_passes_on_a_complete_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_checks(monkeypatch, _Host())
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "builds and `shidashi vm`: ok" in result.output


def test_doctor_command_fails_without_a_build_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_checks(monkeypatch, _Host(_TOOLS - {"systemd-nspawn"}))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "this host cannot build" in result.output and "systemd-nspawn" in result.output


def test_doctor_command_only_warns_about_vm_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_checks(monkeypatch, _Host(_TOOLS - {"qemu-system-x86_64"}))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "`shidashi vm` also needs: qemu-system-x86_64" in result.output


@pytest.mark.parametrize("command", ["factory", "assemble", "pretend"])
def test_builds_refuse_to_start_on_a_host_that_cannot_build(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    def refuse(_work_dir: Path) -> None:
        raise DoctorError("this host cannot build: systemd-nspawn (missing)")

    monkeypatch.setattr(doctor, "require_build_host", refuse)
    result = runner.invoke(app, [command, "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    assert "this host cannot build" in result.output


def test_build_refuses_to_start_on_a_host_that_cannot_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_work_dir: Path) -> None:
        raise DoctorError("this host cannot build: tar (not GNU tar)")

    monkeypatch.setattr(doctor, "require_build_host", refuse)
    result = runner.invoke(app, ["build", "v3", "systemd", "--images", "minimal"])
    assert result.exit_code == 1
    assert "this host cannot build" in result.output
