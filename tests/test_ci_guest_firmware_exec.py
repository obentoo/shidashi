"""The two toolbox paths story 034 left to the host proof alone (audit, ``untested``):

- ``Toolbox.firmware`` on a node with no OVMF of its own (the slim worker): the toolbox
  copies its firmware through the hidden ``ci-guest firmware DEST``, once per toolbox image;
- the hidden ``ci-guest exec``: it runs one tool of ``ci_guest_vm.TOOLS`` and refuses any
  other, so the toolbox container is no general way to run a command as root.

The toolbox image and podman are stood in for: ``FakePodman`` answers ``podman rm`` and
plays the inner ``ci-guest firmware`` by copying a fake OVMF, as the real one would.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import ci_guest_vm, vm, vmrunner
from shidashi.ci_guest import CiGuestError
from shidashi.cli import app

TAG = "localhost/shidashi-vm-runner:0123456789abcdef"


def _ovmf(where: Path) -> tuple[str, str, str]:
    where.mkdir(parents=True, exist_ok=True)
    code, vars_ = where / "OVMF_CODE_4M.qcow2", where / "OVMF_VARS_4M.qcow2"
    code.write_bytes(b"code")
    vars_.write_bytes(b"vars")
    return str(code), str(vars_), "qcow2"


class FakePodman:
    """Every podman call the toolbox makes; the inner ``ci-guest firmware DEST`` copies
    ``source`` into DEST as the toolbox would, or fails with ``rc``."""

    def __init__(self, source: Path, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.source = source
        self.rc = rc

    def __call__(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[:2] == ["fake-podman-run", "firmware"]:
            if self.rc:
                return subprocess.CompletedProcess(argv, self.rc, "", "no OVMF in the image\n")
            code, vars_, fmt = _ovmf(self.source)
            ci_guest_vm.write_firmware(Path(argv[2]), Path(code), Path(vars_), fmt)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def firmware_runs(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["fake-podman-run", "firmware"]]


def _toolbox(
    root: Path, podman: FakePodman, monkeypatch: pytest.MonkeyPatch
) -> ci_guest_vm.Toolbox:
    monkeypatch.setattr(vmrunner, "runner_tarball", lambda: root / "toolbox.tar")
    monkeypatch.setattr(vmrunner, "ensure_image", lambda _tarball, runner: TAG)
    toolbox = ci_guest_vm.Toolbox(root, podman)
    # the podman run line itself is vmrunner.run_argv's, tested there
    monkeypatch.setattr(toolbox, "argv", lambda _name, inner, scratch: ["fake-podman-run", *inner])
    return toolbox


def _no_host_ovmf(monkeypatch: pytest.MonkeyPatch) -> None:
    def none() -> tuple[str, str, str]:
        raise vm.VmError("no OVMF firmware on the host")

    monkeypatch.setattr(vm, "ovmf", none)


def test_a_node_without_ovmf_copies_the_toolboxs_once_per_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_host_ovmf(monkeypatch)
    podman = FakePodman(tmp_path / "in-the-toolbox")
    toolbox = _toolbox(tmp_path / "root", podman, monkeypatch)

    code, vars_, fmt = toolbox.firmware(tmp_path / "scratch")

    dest = tmp_path / "root" / "firmware" / TAG.rsplit(":", 1)[-1]
    assert (code, vars_, fmt) == (dest / "OVMF_CODE_4M.qcow2", dest / "OVMF_VARS_4M.qcow2", "qcow2")
    assert code.read_bytes() == b"code"
    assert podman.firmware_runs() == [["fake-podman-run", "firmware", str(dest)]]
    # a container left under the name by a killed run would refuse the new one
    assert ["podman", "rm", "-f", "-i", "--time", "0", "shidashi-vm-ci-firmware"] in podman.calls

    toolbox.firmware(tmp_path / "scratch")
    assert len(podman.firmware_runs()) == 1, "the toolbox's OVMF was copied a second time"


def test_a_toolbox_that_cannot_copy_its_ovmf_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_host_ovmf(monkeypatch)
    toolbox = _toolbox(tmp_path / "root", FakePodman(tmp_path / "src", rc=1), monkeypatch)
    with pytest.raises(CiGuestError, match="OVMF firmware failed: no OVMF in the image"):
        toolbox.firmware(tmp_path / "scratch")


def test_a_node_with_ovmf_copies_its_own_and_runs_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ovmf(tmp_path / "host-ovmf")
    monkeypatch.setattr(vm, "ovmf", lambda: host)
    podman = FakePodman(tmp_path / "src")
    toolbox = _toolbox(tmp_path / "root", podman, monkeypatch)

    code, _vars, _fmt = toolbox.firmware(tmp_path / "scratch")

    assert code.read_bytes() == b"code"
    assert podman.firmware_runs() == []


# --- the hidden commands, as root inside the toolbox ---------------------------------------


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Root, and an os.execvp that records instead of replacing the test process."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    execs: list[list[str]] = []

    def execvp(file: str, args: list[str]) -> None:
        execs.append([file, *args])
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", execvp)
    return execs


@pytest.mark.parametrize("tool", ci_guest_vm.TOOLS)
def test_exec_runs_an_allowed_tool_with_its_arguments(as_root: list[list[str]], tool: str) -> None:
    done = CliRunner().invoke(app, ["ci-guest", "exec", tool, "--version", "-x"])
    assert done.exit_code == 0, done.output
    assert as_root == [[tool, tool, "--version", "-x"]]


@pytest.mark.parametrize(
    "args",
    [["sh", "-c", "id"], ["/usr/bin/qemu-img", "info"], ["qemu-img-evil"], []],
    ids=["shell", "a-path", "a-lookalike", "nothing"],
)
def test_exec_refuses_anything_else(as_root: list[list[str]], args: list[str]) -> None:
    done = CliRunner().invoke(app, ["ci-guest", "exec", *args])
    assert done.exit_code == 1, done.output
    assert "exec runs only qemu-img, qemu-system-x86_64, xorriso" in done.output
    assert as_root == [], "a tool outside the allowlist was run"


def test_exec_refuses_a_caller_that_is_not_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    done = CliRunner().invoke(app, ["ci-guest", "exec", "qemu-img", "info"])
    assert done.exit_code == 1 and "runs as root" in done.output


def test_firmware_copies_the_toolboxs_ovmf_and_records_it(
    as_root: list[list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _ovmf(tmp_path / "usr-share-edk2")
    monkeypatch.setattr(vm, "ovmf", lambda: source)
    dest = tmp_path / "root" / "firmware" / "tag"

    done = CliRunner().invoke(app, ["ci-guest", "firmware", str(dest)])

    assert done.exit_code == 0, done.output
    record = json.loads((dest / "ovmf.json").read_text())
    assert record == {"code": "OVMF_CODE_4M.qcow2", "vars": "OVMF_VARS_4M.qcow2", "format": "qcow2"}
    assert (dest / "OVMF_VARS_4M.qcow2").read_bytes() == b"vars"


def test_firmware_in_a_toolbox_without_ovmf_fails_and_writes_no_record(
    as_root: list[list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_host_ovmf(monkeypatch)
    dest = tmp_path / "dest"
    done = CliRunner().invoke(app, ["ci-guest", "firmware", str(dest)])
    assert done.exit_code == 1, done.output
    assert "cannot copy the toolbox's OVMF" in done.output
    assert not (dest / "ovmf.json").exists()
