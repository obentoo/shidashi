"""Tests of shidashi.vm -- VM control over vsock SSH and the automated boot test."""

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, config, vm
from shidashi.system import load_system_config


def _cfg(target: str = "kde") -> Any:
    recipe = config.load_recipe("v3", target, "systemd")
    return load_system_config(recipe, variants_dir=config.variants_dir())


def test_qemu_argv_boots_headless_with_vsock_and_the_key_as_a_credential(tmp_path: Path) -> None:
    spec = vm.VmSpec(iso=Path("/b.iso"), cid=7)
    argv = vm.qemu_argv(
        spec,
        pubkey="ssh-ed25519 AAAA k",
        qmp=tmp_path / "q",
        serial=tmp_path / "s",
        pidfile=tmp_path / "p",
    )
    assert argv[:12] == [
        "qemu-system-x86_64",
        "-name",
        "bentoo-vm",
        "-enable-kvm",
        "-machine",
        "q35",
        "-cpu",
        "host",
        "-smp",
        "8",
        "-m",
        "8G",
    ]
    assert "vhost-vsock-pci,guest-cid=7" in argv
    assert argv[argv.index("-display") : argv.index("-display") + 2] == ["-display", "none"]
    creds = [argv[i + 1] for i, a in enumerate(argv) if a == "-smbios"]
    key = base64.b64encode(b"ssh-ed25519 AAAA k").decode()
    assert f"type=11,value=io.systemd.credential.binary:ssh.authorized_keys.root={key}" in creds
    assert any("systemd.unit-dropin.sshd@.service=" in c for c in creds)
    assert "pflash" not in " ".join(argv)


def test_qemu_argv_uefi_uses_ovmf_and_a_private_copy_of_its_variables(tmp_path: Path) -> None:
    spec = vm.VmSpec(iso=Path("/b.iso"), uefi=True)
    argv = vm.qemu_argv(
        spec,
        pubkey="k",
        qmp=tmp_path / "q",
        serial=tmp_path / "s",
        pidfile=tmp_path / "p",
        uefi_vars=tmp_path / "vars.qcow2",
        uefi_code=("/ovmf/CODE.qcow2", "qcow2"),
    )
    assert "if=pflash,format=qcow2,readonly=on,file=/ovmf/CODE.qcow2" in argv
    assert f"if=pflash,format=qcow2,file={tmp_path / 'vars.qcow2'}" in argv
    with pytest.raises(vm.VmError, match="OVMF"):
        vm.qemu_argv(spec, pubkey="k", qmp=tmp_path, serial=tmp_path, pidfile=tmp_path)


def test_ssh_argv_dials_vsock_through_systemds_proxy() -> None:
    argv = vm.ssh_argv(Path("/k"), 42, "uname -r")
    assert argv[-2:] == ["root@vsock/42", "uname -r"]
    assert "ProxyCommand=/usr/lib/systemd/systemd-ssh-proxy %h %p" in argv
    assert "BatchMode=yes" in argv and "UserKnownHostsFile=/dev/null" in argv


def test_boot_checks_come_from_the_images_own_configuration() -> None:
    names = [c.name for c in vm.boot_checks(_cfg(), init="systemd")]
    for expected in (
        "boot finished and healthy",
        "no failed units",
        "hostname",
        "locale",
        "timezone",
        "machine-id generated at boot",
        "os-release",
        "console keymap",
        "enabled: NetworkManager.service",
        "enabled: systemd-resolved.service",
        "disabled: sshd.service",
        "display manager running",
        "bentoo logged in automatically on seat0 (wayland)",
    ):
        assert expected in names
    minimal = [c.name for c in vm.boot_checks(_cfg("minimal"), init="systemd")]
    assert "display manager running" not in minimal
    assert "bentoo logged in automatically on seat0 (tty)" in minimal


@pytest.mark.parametrize(
    ("mode", "expect", "out", "passed"),
    [
        ("equal", "running", "running\n", True),
        ("equal", "running", "degraded", False),
        ("equal", "", "", True),
        ("machine-id", None, "09b99f4d8cfb4f5fa9683c4d423703f3\n", True),
        ("machine-id", None, "uninitialized", False),
        ("enabled-or-absent", None, "enabled", True),
        ("enabled-or-absent", None, "not-installed", True),
        ("enabled-or-absent", None, "disabled", False),
        ("disabled-or-absent", None, "disabled", True),
        # the first version printed both: `is-enabled` exits 1 on a disabled unit
        ("disabled-or-absent", None, "disabled\nnot-installed", False),
        ("contains", "wayland", "tty\nwayland\n", True),
        ("contains", "wayland", "tty", False),
    ],
)
def test_judge(mode: str, expect: str | None, out: str, passed: bool) -> None:
    assert vm.judge(vm.Check("c", "cmd", expect, mode), out, 0) is passed


class _FakeSession(vm.Session):
    """Answers each check command as a healthy kde live would."""

    answers: dict[str, str] = {}
    stopped: list[str] = []

    def start(self) -> None:
        pass

    def run_command(self, command: str, *, timeout: int = 600) -> vm.GuestResult:
        out = next((v for k, v in self.answers.items() if k in command), "")
        return vm.GuestResult(command, 0, out, "", 0.1)

    def wait_ssh(self, *, timeout: float = 300, interval: float = 5) -> float:
        return 21.0

    def stop(self) -> None:
        self.stopped.append(self.directory.name)


def test_boot_test_runs_every_check_per_firmware_and_attaches_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(
        vm, "read_build_info", lambda iso: {"arch": "v3", "flavor": "kde", "init": "systemd"}
    )
    _FakeSession.stopped = []
    _FakeSession.answers = {
        "is-system-running": "running",
        "--failed": "",
        "hostnamectl": "bentoo",
        "locale.conf": "en_US.UTF-8",
        "Timezone": "UTC",
        "machine-id": "09b99f4d8cfb4f5fa9683c4d423703f3",
        "os-release": "Bentoo",
        "vconsole": "us",
        "is-enabled sshd": "disabled",
        "is-enabled systemd-networkd": "disabled",
        "is-enabled systemd-homed": "disabled",
        "is-enabled": "enabled",
        "is-active display-manager": "active",
        "loginctl": "wayland",
    }
    monkeypatch.setattr(audit, "repo_state", lambda: {})
    with audit.run(tmp_path / "runs", command="vm-test", argv=[]) as trail:
        report = vm.boot_test(Path("/b.iso"), session_factory=_FakeSession)
    assert report["passed"], [
        c for f in report["firmwares"].values() for c in f["checks"] if not c["passed"]
    ]
    assert set(report["firmwares"]) == {"bios", "uefi"}
    assert _FakeSession.stopped == ["test-bios", "test-uefi"]  # always powered off
    manifest = audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))
    steps = [s["step"] for s in manifest["steps"]]
    assert "boot:bios/check:hostname" in steps and steps[-1] == "boot:uefi"
    assert "boot-test.json" in manifest["attachments"]


def test_boot_test_reports_a_failed_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(
        vm, "read_build_info", lambda iso: {"arch": "v3", "flavor": "kde", "init": "systemd"}
    )
    _FakeSession.answers = {"os-release": "Gentoo"}
    report = vm.boot_test(Path("/b.iso"), firmwares=("bios",), session_factory=_FakeSession)
    assert not report["passed"]
    failed = [c["check"] for c in report["firmwares"]["bios"]["checks"] if not c["passed"]]
    assert "os-release" in failed


@pytest.mark.skipif(shutil.which("xorriso") is None, reason="needs xorriso (dev-libs/libisoburn)")
def test_read_build_info_refuses_an_iso_without_one(tmp_path: Path) -> None:
    with pytest.raises(vm.VmError, match="build.json"):
        vm.read_build_info(tmp_path / "missing.iso")


def test_read_build_info_names_a_missing_xorriso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without xorriso the user got a bare FileNotFoundError (found by `act`)."""
    monkeypatch.setattr(shutil, "which", lambda _tool: None)
    with pytest.raises(vm.VmError, match="xorriso is needed"):
        vm.read_build_info(tmp_path / "any.iso")


def test_load_session_needs_a_started_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path))
    with pytest.raises(vm.VmError, match="vm start"):
        vm.load_session("nope")
    d = vm.session_dir("x")
    d.mkdir(parents=True)
    (d / "session.json").write_text(
        json.dumps({"iso": "/b.iso", "uefi": True, "cid": 9, "memory": "4G", "cpus": 2})
    )
    assert vm.load_session("x").spec == vm.VmSpec(Path("/b.iso"), True, 9, "4G", 2)


def test_run_command_records_the_guest_command_in_the_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def runner(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "7.2.6\n", "")

    monkeypatch.setattr(audit, "repo_state", lambda: {})
    session = vm.Session(vm.VmSpec(Path("/b.iso")), tmp_path, runner=runner)
    with audit.run(tmp_path / "runs", command="vm", argv=[]) as trail:
        assert session.run_command("uname -r").stdout == "7.2.6\n"
    events = [
        e for e in audit.read_events(trail.path / "events.jsonl") if e["kind"] == "vm.command"
    ]
    assert events[0]["command"] == "uname -r" and events[0]["exit_code"] == 0


# --- disks and virtiofs shares (the builder guest) --------------------------------------


def _argv(tmp_path: Path, spec: vm.VmSpec, sockets: tuple[Path, ...] = ()) -> list[str]:
    return vm.qemu_argv(
        spec,
        pubkey="ssh-ed25519 AAAA test",
        qmp=tmp_path / "qmp.sock",
        serial=tmp_path / "serial.log",
        pidfile=tmp_path / "qemu.pid",
        share_sockets=sockets,
    )


def test_a_disk_is_a_virtio_qcow2_drive(tmp_path: Path) -> None:
    spec = vm.VmSpec(iso=tmp_path / "x.iso", disks=(tmp_path / "work.qcow2",))
    argv = _argv(tmp_path, spec)
    at = argv.index(f"file={tmp_path / 'work.qcow2'},if=virtio,format=qcow2,discard=unmap")
    assert argv[at - 1] == "-drive"


def test_shares_need_shared_memory_and_one_vhost_user_fs_each(tmp_path: Path) -> None:
    shares = (vm.Share("lab", tmp_path), vm.Share("out", tmp_path, readonly=False))
    spec = vm.VmSpec(iso=tmp_path / "x.iso", memory="32G", shares=shares)
    sockets = (tmp_path / "a.sock", tmp_path / "b.sock")
    argv = _argv(tmp_path, spec, sockets)
    # vhost-user devices read the guest's RAM: it must be a shared memfd of -m's size
    assert "memory-backend-memfd,id=mem,size=32G,share=on" in argv
    assert "node,memdev=mem" in argv
    assert f"socket,id=fs0,path={sockets[0]}" in argv
    assert "vhost-user-fs-pci,queue-size=1024,chardev=fs1,tag=out" in argv


def test_without_shares_memory_stays_private(tmp_path: Path) -> None:
    argv = _argv(tmp_path, vm.VmSpec(iso=tmp_path / "x.iso"))
    assert not any("memory-backend" in a for a in argv)


def test_one_socket_per_share(tmp_path: Path) -> None:
    spec = vm.VmSpec(iso=tmp_path / "x.iso", shares=(vm.Share("lab", tmp_path),))
    with pytest.raises(vm.VmError, match="one virtiofsd socket per share"):
        _argv(tmp_path, spec)


def test_a_read_only_share_shows_real_owners_and_refuses_writes(tmp_path: Path) -> None:
    argv = vm.virtiofsd_argv("/usr/libexec/virtiofsd", vm.Share("lab", tmp_path), tmp_path / "s")
    assert "--readonly" in argv and "--sandbox=none" in argv
    assert f"--shared-dir={tmp_path}" in argv and f"--socket-path={tmp_path / 's'}" in argv


def test_a_writable_share_keeps_the_user_namespace(tmp_path: Path) -> None:
    """What the guest's root writes must belong to the invoking user, not root."""
    share = vm.Share("out", tmp_path, readonly=False)
    argv = vm.virtiofsd_argv("/usr/libexec/virtiofsd", share, tmp_path / "s")
    assert "--readonly" not in argv
    assert not any(a.startswith("--sandbox") for a in argv)  # the default: namespace


def test_parse_share(tmp_path: Path) -> None:
    assert vm.parse_share(f"lab={tmp_path}") == vm.Share("lab", tmp_path.resolve())
    assert vm.parse_share(f"out={tmp_path}:rw") == vm.Share("out", tmp_path.resolve(), False)
    for bad in ("lab", f"={tmp_path}", f"l a b={tmp_path}", f"lab={tmp_path / 'nope'}"):
        with pytest.raises(vm.VmError):
            vm.parse_share(bad)


def test_ensure_disk_needs_a_size_to_create(tmp_path: Path) -> None:
    with pytest.raises(vm.VmError, match="--disk-size"):
        vm.ensure_disk(tmp_path / "work.qcow2", None)
    existing = tmp_path / "old.qcow2"
    existing.write_bytes(b"QFI")
    assert vm.ensure_disk(existing, None) == existing


@pytest.mark.skipif(shutil.which("qemu-img") is None, reason="needs qemu-img")
def test_ensure_disk_creates_a_sparse_qcow2(tmp_path: Path) -> None:
    disk = vm.ensure_disk(tmp_path / "sub" / "work.qcow2", "1G")
    assert disk.read_bytes()[:4] == b"QFI\xfb"
    assert disk.stat().st_size < 1024 * 1024  # sparse: metadata only


def test_a_session_remembers_its_disks_and_shares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path))
    directory = vm.session_dir("builder")
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(
        json.dumps(
            {
                "iso": "/x.iso",
                "uefi": False,
                "cid": 62,
                "memory": "32G",
                "cpus": 24,
                "disks": ["/w.qcow2"],
                "shares": [{"tag": "out", "path": "/o", "readonly": False}],
            }
        )
    )
    spec = vm.load_session("builder").spec
    assert spec.disks == (Path("/w.qcow2"),)
    assert spec.shares == (vm.Share("out", Path("/o"), readonly=False),)


def test_an_old_session_file_has_no_disks_or_shares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path))
    directory = vm.session_dir("old")
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(
        json.dumps({"iso": "/x.iso", "uefi": True, "cid": 42, "memory": "8G", "cpus": 8})
    )
    spec = vm.load_session("old").spec
    assert spec.disks == () and spec.shares == ()
