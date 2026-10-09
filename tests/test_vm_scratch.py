"""ADDITIONS to tests/test_vm.py (story 019, task 6.1; R4.1, R4.2): an unwritable
VM scratch is an error naming the directory and the two ways out, never a traceback.
Self-contained so it runs alone; merge when materialized.

The host tools ``Session.start`` checks first (qemu, systemd-ssh-proxy) are made
present; the runner records instead of running ssh-keygen. Unwritable directories are
real (mode 0555): the tests skip as root, which writes anywhere.
"""

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import audit, vm
from shidashi.cli import app

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")

WAYS_OUT = ("--work-dir", "SHIDASHI_SCRATCH")


class _KeygenReached(Exception):
    """The start went past the session directory."""


def _runner(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
    raise _KeygenReached(argv)


@pytest.fixture
def host_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = tmp_path / "systemd-ssh-proxy"
    proxy.write_text("")
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda tool: "/usr/bin/" + tool if "qemu" in tool else real_which(tool)
    )
    monkeypatch.setattr(vm, "_SSH_PROXY", str(proxy))


@pytest.fixture
def read_only(tmp_path: Path) -> Iterator[Path]:
    """A root-owned-like scratch: it exists, this user cannot write into it."""
    scratch = tmp_path / "scratch"
    (scratch / "vm" / "kept").mkdir(parents=True)
    (scratch / "vm" / "kept" / "qemu.pid").write_text("1\n")  # a stale file of a last run
    (scratch / "vm" / "owned").mkdir()  # root-owned and empty: mkdir(exist_ok) succeeds
    dirs = (scratch / "vm" / "kept", scratch / "vm" / "owned", scratch / "vm", scratch)
    for d in dirs:
        d.chmod(0o555)
    yield scratch
    for d in reversed(dirs):
        d.chmod(0o755)


@pytest.mark.usefixtures("host_tools")
def test_hostile_a_writable_session_directory_starts_as_before(tmp_path: Path) -> None:
    session = vm.Session(vm.VmSpec(Path("/b.iso")), tmp_path / "ok" / "vm" / "x", runner=_runner)
    with pytest.raises(_KeygenReached):
        session.start()


@pytest.mark.usefixtures("host_tools")
@pytest.mark.parametrize("name", ["new", "kept", "owned"])
def test_an_unwritable_session_directory_is_a_vm_error_naming_it(
    read_only: Path, name: str
) -> None:
    """``new``: it cannot be created. ``kept``: it exists, its stale files cannot go.
    ``owned``: it exists and is empty -- the 2026-10-08 case: only a write probe
    catches it before QEMU (``_runner`` would raise ``_KeygenReached``)."""
    directory = read_only / "vm" / name
    session = vm.Session(vm.VmSpec(Path("/b.iso")), directory, runner=_runner)
    with pytest.raises(vm.VmError) as err:
        session.start()
    message = str(err.value)
    assert str(directory) in message
    assert all(way in message for way in WAYS_OUT), message
    assert isinstance(err.value.__cause__, OSError)  # chained, not swallowed


@pytest.mark.usefixtures("host_tools")
@pytest.mark.parametrize("command", ["test", "start"])
def test_vm_test_and_start_report_an_unwritable_scratch_in_one_error(
    read_only: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(read_only))
    monkeypatch.setattr(
        vm, "read_build_info", lambda iso: {"arch": "v3", "flavor": "kde", "init": "systemd"}
    )
    monkeypatch.setattr(audit, "repo_state", lambda: {})
    iso = tmp_path / "b.iso"
    iso.write_bytes(b"ISO")
    args = ["vm", "test", str(iso), "--firmware", "bios"]
    if command == "start":
        args = ["vm", "start", str(iso), "--no-wait"]
    result = CliRunner().invoke(app, args)
    captured = capfd.readouterr()
    out = result.output + captured.out + captured.err
    assert result.exit_code == 1, out
    assert isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in out
    assert out.count("error:") == 1, out
    flat = " ".join(out.split())  # the console may wrap a long line
    assert str(read_only / "vm") in flat.replace(" ", "") or str(read_only) in flat
    assert all(way in flat for way in WAYS_OUT), out
