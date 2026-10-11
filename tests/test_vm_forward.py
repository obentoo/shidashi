"""Tests of ``shidashi vm start --forward HOST:GUEST`` (story 021, R4).

A guest TCP port reachable on the host's loopback: ``VmSpec.forwards`` rendered by
``qemu_argv`` as one ``hostfwd`` per pair on the user NIC, bound to 127.0.0.1 only;
the pairs kept in the session and read back (an older session file has none); the
CLI refusing a malformed value or a taken host port before QEMU starts.

No QEMU is spawned: the session's runner records the argv it is handed (and writes
the key ssh-keygen would), and the host tools ``Session.start`` looks for are made
present. A taken port is a real socket listening on 127.0.0.1.

Requirements exercised: R4.1, R4.2, R4.3, R4.4, R4.5, R4.6.
"""

import json
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import audit, vm
from shidashi.cli import app

BASE_NIC = "user,model=virtio-net-pci"


def _argv(tmp_path: Path, spec: vm.VmSpec) -> list[str]:
    return vm.qemu_argv(
        spec,
        pubkey="ssh-ed25519 AAAA test",
        qmp=tmp_path / "qmp.sock",
        serial=tmp_path / "serial.log",
        pidfile=tmp_path / "qemu.pid",
    )


def _nic(argv: list[str]) -> str:
    at = [i for i, a in enumerate(argv) if a == "-nic"]
    assert len(at) == 1, f"expected one -nic, got {len(at)}"
    return argv[at[0] + 1]


def _hostfwds(nic: str) -> list[str]:
    return [part for part in nic.split(",") if part.startswith("hostfwd=")]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# --- qemu_argv (R4.1, R4.2) ---------------------------------------------------------


def test_without_forwards_the_nic_is_unchanged(tmp_path: Path) -> None:
    spec = vm.VmSpec(iso=tmp_path / "x.iso")
    assert spec.forwards == ()
    assert _nic(_argv(tmp_path, spec)) == BASE_NIC


def test_one_forward_is_a_hostfwd_on_the_loopback(tmp_path: Path) -> None:
    spec = vm.VmSpec(iso=tmp_path / "x.iso", forwards=((2222, 22),))
    assert _nic(_argv(tmp_path, spec)) == f"{BASE_NIC},hostfwd=tcp:127.0.0.1:2222-:22"


def test_hostile_two_forwards_render_two_hostfwds_in_order(tmp_path: Path) -> None:
    """Wrongly collapse: the second pair must not replace or swallow the first."""
    spec = vm.VmSpec(iso=tmp_path / "x.iso", forwards=((2222, 22), (8080, 80)))
    nic = _nic(_argv(tmp_path, spec))
    assert nic.startswith(BASE_NIC + ",")
    assert _hostfwds(nic) == [
        "hostfwd=tcp:127.0.0.1:2222-:22",
        "hostfwd=tcp:127.0.0.1:8080-:80",
    ]


def test_hostile_two_host_ports_to_one_guest_port_stay_two(tmp_path: Path) -> None:
    """Wrongly collapse: pairs sharing a guest port are still distinct forwards."""
    spec = vm.VmSpec(iso=tmp_path / "x.iso", forwards=((2222, 22), (2223, 22)))
    assert _hostfwds(_nic(_argv(tmp_path, spec))) == [
        "hostfwd=tcp:127.0.0.1:2222-:22",
        "hostfwd=tcp:127.0.0.1:2223-:22",
    ]


def test_hostile_the_host_side_is_never_every_interface(tmp_path: Path) -> None:
    """``tcp::H-:G`` and ``tcp:0.0.0.0:H-:G`` would expose the guest's sshd to the LAN."""
    spec = vm.VmSpec(iso=tmp_path / "x.iso", forwards=((2222, 22), (8080, 80)))
    argv = _argv(tmp_path, spec)
    joined = " ".join(argv)
    assert "0.0.0.0" not in joined
    assert "tcp::" not in joined
    assert all(f.startswith("hostfwd=tcp:127.0.0.1:") for f in _hostfwds(_nic(argv)))


# --- the session keeps its forwards (R4.5, R4.6) ------------------------------------


class _Spawn:
    """A runner that records each argv; ssh-keygen's key is written, QEMU is not run."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(a) for a in argv])
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text("private")
            key.with_suffix(".pub").write_text("ssh-ed25519 AAAA test\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def qemu(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == "qemu-system-x86_64"]


@pytest.fixture
def host_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = tmp_path / "systemd-ssh-proxy"
    proxy.write_text("")
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda tool: "/usr/bin/" + tool if "qemu" in tool else real_which(tool)
    )
    monkeypatch.setattr(vm, "_SSH_PROXY", str(proxy))
    monkeypatch.setattr(audit, "repo_state", lambda: {})


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "scratch"
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(path))
    return path


@pytest.mark.usefixtures("host_tools", "scratch")
def test_a_started_session_reads_its_forwards_back(tmp_path: Path) -> None:
    forwards = ((_free_port(), 22), (_free_port(), 80))
    spawn = _Spawn()
    spec = vm.VmSpec(iso=tmp_path / "x.iso", forwards=forwards)
    vm.Session(spec, vm.session_dir("fw"), runner=spawn).start()
    assert len(spawn.qemu) == 1
    assert vm.load_session("fw").spec.forwards == forwards


@pytest.mark.usefixtures("host_tools", "scratch")
def test_a_session_started_without_forwards_reads_back_none(tmp_path: Path) -> None:
    spawn = _Spawn()
    vm.Session(vm.VmSpec(iso=tmp_path / "x.iso"), vm.session_dir("plain"), runner=spawn).start()
    assert vm.load_session("plain").spec.forwards == ()


def test_an_old_session_file_has_no_forwards(scratch: Path) -> None:
    directory = vm.session_dir("old")
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(
        json.dumps(
            {
                "iso": "/x.iso",
                "uefi": False,
                "cid": 42,
                "memory": "8G",
                "cpus": 8,
                "disks": ["/w.qcow2"],
                "shares": [],
            }
        )
    )
    spec = vm.load_session("old").spec
    assert spec.forwards == ()
    assert spec.disks == (Path("/w.qcow2"),)


# --- the CLI (R4.1-R4.4) ------------------------------------------------------------


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch, host_tools: None, scratch: Path) -> _Spawn:
    """``vm start``'s session runs on a recording runner: nothing is spawned."""
    recorder = _Spawn()
    real = vm.Session

    def session(spec: vm.VmSpec, directory: Path, **_k: Any) -> vm.Session:
        return real(spec, directory, runner=recorder)

    monkeypatch.setattr(vm, "Session", session)
    return recorder


@pytest.fixture
def iso(tmp_path: Path) -> Path:
    path = tmp_path / "w.iso"
    path.write_bytes(b"ISO")
    return path


@pytest.fixture
def busy_port() -> Iterator[int]:
    """A host port something already listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        yield s.getsockname()[1]


def _start(iso: Path, *forwards: str, name: str = "fw") -> Any:
    args = ["vm", "start", str(iso), "--no-wait", "--name", name]
    for value in forwards:
        args.append(f"--forward={value}")
    return CliRunner().invoke(app, args)


def _flat(result: Any, capfd: pytest.CaptureFixture[str]) -> str:
    captured = capfd.readouterr()
    out = result.output + captured.out + captured.err
    for box in "│╭╮╰╯─":
        out = out.replace(box, " ")
    return "".join(out.split())


def test_vm_start_help_lists_forward() -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--help"])
    assert result.exit_code == 0
    assert "--forward" in result.output


def test_vm_start_forwards_every_pair_on_the_loopback(
    spawn: _Spawn, iso: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    ssh, web = _free_port(), _free_port()
    result = _start(iso, f"{ssh}:22", f"{web}:80")
    assert result.exit_code == 0, _flat(result, capfd)
    assert len(spawn.qemu) == 1
    assert _hostfwds(_nic(spawn.qemu[0])) == [
        f"hostfwd=tcp:127.0.0.1:{ssh}-:22",
        f"hostfwd=tcp:127.0.0.1:{web}-:80",
    ]
    assert vm.load_session("fw").spec.forwards == ((ssh, 22), (web, 80))


def test_hostile_the_port_check_releases_the_port_for_qemu(spawn: _Spawn, iso: Path) -> None:
    """A check that kept its probe socket open would make QEMU's own bind fail."""
    port = _free_port()
    assert _start(iso, f"{port}:22").exit_code == 0
    assert _bindable(port)


@pytest.mark.parametrize("guest", [1, 65535])
def test_the_port_range_bounds_are_accepted(spawn: _Spawn, iso: Path, guest: int) -> None:
    """Wrongly split: 1 and 65535 are ports, not off-by-one refusals."""
    port = _free_port()
    assert _start(iso, f"{port}:{guest}").exit_code == 0
    assert _hostfwds(_nic(spawn.qemu[0])) == [f"hostfwd=tcp:127.0.0.1:{port}-:{guest}"]


@pytest.mark.parametrize(
    "template",
    [
        "22",
        "a:b",
        "{free}:ssh",
        ":22",
        "{free}:",
        "0:22",
        "65536:22",
        "-1:22",
        "{free}:0",
        "{free}:65536",
        "{free}:22:1",
        "{free}-22",
    ],
)
def test_a_malformed_forward_is_refused_before_the_vm_starts(
    spawn: _Spawn, iso: Path, scratch: Path, capfd: pytest.CaptureFixture[str], template: str
) -> None:
    value = template.format(free=_free_port())
    result = _start(iso, value)
    out = _flat(result, capfd)
    assert result.exit_code != 0, out
    assert "Traceback" not in out
    assert value.replace(" ", "") in out, out
    assert spawn.calls == [], "nothing may run for a refused --forward"
    assert not (scratch / "vm" / "fw" / "session.json").exists()


def test_a_taken_host_port_is_refused_naming_it(
    spawn: _Spawn, iso: Path, busy_port: int, scratch: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    result = _start(iso, f"{busy_port}:22")
    out = _flat(result, capfd)
    assert result.exit_code != 0, out
    assert "Traceback" not in out
    assert str(busy_port) in out, out
    assert spawn.qemu == [], "QEMU must not start on a taken host port"
    assert not (scratch / "vm" / "fw" / "session.json").exists()


def test_hostile_a_taken_port_after_a_free_one_is_still_refused(
    spawn: _Spawn, iso: Path, busy_port: int, capfd: pytest.CaptureFixture[str]
) -> None:
    """Every pair is checked, not only the first."""
    free = _free_port()
    result = _start(iso, f"{free}:22", f"{busy_port}:80")
    out = _flat(result, capfd)
    assert result.exit_code != 0, out
    assert str(busy_port) in out, out
    assert spawn.qemu == []
