"""Boot a live ISO in a VM and drive it over SSH on AF_VSOCK -- no network, no screen.

systemd >= 256 ships systemd-ssh-generator: inside a VM with a vsock device it
binds sshd to AF_VSOCK on its own, and nothing listens on real hardware (there
is no vsock). The key comes in as a systemd credential through QEMU's SMBIOS
(``ssh.authorized_keys.root``), made per session and thrown away with it, so no
key ever reaches the ISO. The host dials ``root@vsock/<cid>`` through
``systemd-ssh-proxy``. QMP (QEMU's JSON API) powers off and takes a screenshot
when one is really wanted.

:func:`boot_test` is the automated boot test: it boots the ISO on BIOS and UEFI
and checks, from inside, everything the image's system.yaml declared -- running
with no failed unit, hostname, os-release, locale, keymap, timezone, a fresh
machine-id, the live user's autologin session, the display manager, the enabled
and disabled services. The expectations are read from the same configuration
that built the image (the ISO's own ``bentoo/build.json`` names the recipe), so
the test checks what was declared, not a second copy of it. Every command and
check is recorded in the run's audit trail.

No root needed: the ``kvm`` group opens /dev/kvm and /dev/vhost-vsock. Commands
run in the GUEST's shell over ssh; on the host, every process is started from an
argument list, never through a shell.
"""

import base64
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shidashi import audit, config
from shidashi.system import SystemConfig, display_manager, load_system_config

_SSH_PROXY = "/usr/lib/systemd/systemd-ssh-proxy"

#: OVMF firmware, (code, variables template, format), first found wins.
_OVMF = (
    ("/usr/share/edk2-ovmf/OVMF_CODE_4M.qcow2", "/usr/share/edk2-ovmf/OVMF_VARS_4M.qcow2", "qcow2"),
    ("/usr/share/edk2-ovmf/OVMF_CODE.fd", "/usr/share/edk2-ovmf/OVMF_VARS.fd", "raw"),
    ("/usr/share/qemu/edk2-x86_64-code.fd", "/usr/share/qemu/edk2-i386-vars.fd", "raw"),
)

#: Generates SSH host keys before a per-connection sshd, for ISOs made before
#: the image shipped sshd-keygen.service (2026-09-30). Harmless otherwise. Its
#: output goes to /dev/null: sshd@'s stdout is the connection itself.
_KEYGEN_DROPIN = '[Service]\nExecStartPre=+/bin/sh -c "/usr/bin/ssh-keygen -A >/dev/null 2>&1"\n'


class VmError(Exception):
    """The VM could not be started, reached or stopped."""


#: The host's virtiofs daemon (sys-fs/virtiofsd, or a distribution's path).
_VIRTIOFSD = ("/usr/libexec/virtiofsd", "/usr/lib/virtiofsd", "/usr/lib/qemu/virtiofsd")


@dataclass(frozen=True)
class Share:
    """A host directory the guest mounts with ``mount -t virtiofs <tag> <dir>``.

    Read-only shares run their daemon without a sandbox, so the guest sees the
    host's real owners (root, portage) and can only read. A writable share runs
    in virtiofsd's user namespace: what the guest's root writes belongs to the
    invoking user on the host, never to the host's root.
    """

    tag: str
    path: Path
    readonly: bool = True


@dataclass(frozen=True)
class VmSpec:
    """What to boot and how."""

    iso: Path
    uefi: bool = False
    cid: int = 42
    memory: str = "8G"
    cpus: int = 8
    display: str = "none"
    #: qcow2 disks attached as virtio (/dev/vda, /dev/vdb... in the guest).
    disks: tuple[Path, ...] = ()
    shares: tuple[Share, ...] = ()


def _credential(name: str, value: str) -> list[str]:
    encoded = base64.b64encode(value.encode()).decode()
    return ["-smbios", f"type=11,value=io.systemd.credential.binary:{name}={encoded}"]


def parse_share(text: str) -> Share:
    """``TAG=DIR`` (read-only) or ``TAG=DIR:rw``. Pure, but checks the directory."""
    tag, sep, rest = text.partition("=")
    if not sep or not tag or not rest:
        raise VmError(f"--share {text!r}: expected TAG=DIR or TAG=DIR:rw")
    if not tag.replace("-", "").replace("_", "").isalnum():
        raise VmError(f"--share {text!r}: the tag is letters, digits, - and _")
    readonly = True
    if rest.endswith(":rw"):
        rest, readonly = rest[: -len(":rw")], False
    path = Path(rest).resolve()
    if not path.is_dir():
        raise VmError(f"--share {text!r}: {path} is not a directory")
    return Share(tag, path, readonly)


def ensure_disk(path: Path, size: str | None) -> Path:
    """``path`` as a qcow2 disk, created sparse with ``size`` when missing. I/O."""
    if path.is_file():
        return path
    if size is None:
        raise VmError(f"--disk {path} does not exist: give --disk-size to create it")
    path.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["qemu-img", "create", "-q", "-f", "qcow2", str(path), size],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise VmError(f"qemu-img could not create {path}: {done.stderr.strip()}")
    return path


def virtiofsd() -> str:
    """The host's virtiofsd. I/O."""
    for path in _VIRTIOFSD:
        if Path(path).is_file():
            return path
    found = shutil.which("virtiofsd")
    if found is None:
        raise VmError("virtiofsd is missing on the host (sys-fs/virtiofsd): needed by --share")
    return found


def virtiofsd_argv(daemon: str, share: Share, socket_path: Path) -> list[str]:
    """One share's virtiofsd command line. Pure."""
    argv = [daemon, f"--socket-path={socket_path}", f"--shared-dir={share.path}"]
    if share.readonly:
        argv += ["--readonly", "--sandbox=none"]
    return [*argv, "--cache=auto", "--log-level=error"]


def ovmf() -> tuple[str, str, str]:
    """The host's OVMF (code, vars template, format). I/O."""
    for code, vars_, fmt in _OVMF:
        if Path(code).is_file() and Path(vars_).is_file():
            return code, vars_, fmt
    raise VmError("no OVMF firmware on the host (sys-firmware/edk2-bin or edk2-ovmf)")


def qemu_argv(
    spec: VmSpec,
    *,
    pubkey: str,
    qmp: Path,
    serial: Path,
    pidfile: Path,
    uefi_vars: Path | None = None,
    uefi_code: tuple[str, str] | None = None,
    share_sockets: Sequence[Path] = (),
) -> list[str]:
    """The QEMU command line. Pure.

    ``-cpu host``: the image is built for x86-64-v3 and would not run on QEMU's
    generic CPU. The ISO is attached read-only; the live root is an overlay in RAM.
    ``share_sockets`` (one per ``spec.shares``) are the virtiofsd sockets; a
    vhost-user device needs the guest's RAM shared, hence the memfd backend.
    """
    if len(share_sockets) != len(spec.shares):
        raise VmError("one virtiofsd socket per share")
    argv = [
        "qemu-system-x86_64",
        "-name",
        "bentoo-vm",
        "-enable-kvm",
        "-machine",
        "q35",
        "-cpu",
        "host",
        "-smp",
        str(spec.cpus),
        "-m",
        spec.memory,
    ]
    if spec.shares:
        argv += [
            "-object",
            f"memory-backend-memfd,id=mem,size={spec.memory},share=on",
            "-numa",
            "node,memdev=mem",
        ]
    if spec.uefi:
        if uefi_code is None or uefi_vars is None:
            raise VmError("UEFI needs the OVMF code and a copy of its variables")
        code, fmt = uefi_code
        argv += [
            "-drive",
            f"if=pflash,format={fmt},readonly=on,file={code}",
            "-drive",
            f"if=pflash,format={fmt},file={uefi_vars}",
        ]
    argv += [
        "-drive",
        f"file={spec.iso},media=cdrom,readonly=on",
        "-boot",
        "d",
        "-vga",
        "virtio",
        "-display",
        spec.display,
        "-device",
        f"vhost-vsock-pci,guest-cid={spec.cid}",
        *_credential("ssh.authorized_keys.root", pubkey),
        *_credential("systemd.unit-dropin.sshd@.service", _KEYGEN_DROPIN),
        "-qmp",
        f"unix:{qmp},server,nowait",
        "-serial",
        f"file:{serial}",
        "-nic",
        "user,model=virtio-net-pci",
        *(
            arg
            for disk in spec.disks
            for arg in ("-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap")
        ),
        *(
            arg
            for i, (share, sock) in enumerate(zip(spec.shares, share_sockets, strict=True))
            for arg in (
                "-chardev",
                f"socket,id=fs{i},path={sock}",
                "-device",
                f"vhost-user-fs-pci,queue-size=1024,chardev=fs{i},tag={share.tag}",
            )
        ),
        "-daemonize",
        "-pidfile",
        str(pidfile),
    ]
    return argv


def ssh_argv(key: Path, cid: int, command: str, *, timeout: int = 5) -> list[str]:
    """``ssh root@vsock/<cid> <command>`` through systemd-ssh-proxy. Pure.

    Host keys change with every boot of a live medium: never recorded, never
    checked (the transport is a local vsock, not a network).
    """
    return [
        "ssh",
        "-i",
        str(key),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ProxyCommand={_SSH_PROXY} %h %p",
        f"root@vsock/{cid}",
        command,
    ]


@dataclass
class GuestResult:
    """A command run in the guest."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


@dataclass
class Session:
    """A running VM: its directory holds the key, sockets, pid and serial log."""

    spec: VmSpec
    directory: Path
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    started: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> Path:
        return self.directory / "id_ed25519"

    @property
    def qmp_socket(self) -> Path:
        return self.directory / "qmp.sock"

    @property
    def pidfile(self) -> Path:
        return self.directory / "qemu.pid"

    @property
    def serial(self) -> Path:
        return self.directory / "serial.log"

    # --- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        if shutil.which("qemu-system-x86_64") is None:
            raise VmError("qemu-system-x86_64 is missing on the host")
        if not Path(_SSH_PROXY).exists():
            raise VmError(f"{_SSH_PROXY} is missing: vsock SSH needs systemd >= 256 on the host")
        # An existing root-owned directory passes mkdir(exist_ok=True): only the write
        # probe catches it before QEMU does, with a traceback (2026-10-08).
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            for stale in (self.key, self.key.with_suffix(".pub"), self.qmp_socket, self.pidfile):
                stale.unlink(missing_ok=True)
            probe = self.directory / ".write-probe"
            probe.touch()
            probe.unlink()
        except OSError as err:
            raise VmError(
                f"cannot write the VM session directory {self.directory}: {err.strerror}; "
                "choose another with --work-dir or SHIDASHI_SCRATCH"
            ) from err
        self.runner(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"shidashi-vm-{self.directory.name}",
                "-f",
                str(self.key),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        sockets = self._start_shares()
        uefi_vars = uefi_code = None
        if self.spec.uefi:
            code, template, fmt = ovmf()
            uefi_vars = self.directory / f"vars.{fmt}"
            shutil.copyfile(template, uefi_vars)
            uefi_code = (code, fmt)
        argv = qemu_argv(
            self.spec,
            pubkey=self.key.with_suffix(".pub").read_text().strip(),
            qmp=self.qmp_socket,
            serial=self.serial,
            pidfile=self.pidfile,
            uefi_vars=uefi_vars,
            uefi_code=uefi_code,
            share_sockets=sockets,
        )
        done = self.runner(argv, capture_output=True, text=True)
        if done.returncode != 0:
            self._stop_shares()
            raise VmError(f"qemu failed to start: {done.stderr.strip()}")
        self.started = time.monotonic()
        (self.directory / "session.json").write_text(
            json.dumps(
                {
                    "iso": str(self.spec.iso),
                    "uefi": self.spec.uefi,
                    "cid": self.spec.cid,
                    "memory": self.spec.memory,
                    "cpus": self.spec.cpus,
                    "disks": [str(d) for d in self.spec.disks],
                    "shares": [
                        {"tag": s.tag, "path": str(s.path), "readonly": s.readonly}
                        for s in self.spec.shares
                    ],
                }
            )
        )
        audit.current().event(
            "vm.start", iso=str(self.spec.iso), uefi=self.spec.uefi, cid=self.spec.cid
        )

    def _start_shares(self) -> list[Path]:
        """One virtiofsd per share, each on its socket; their pids go to the session."""
        if not self.spec.shares:
            return []
        daemon = virtiofsd()
        sockets: list[Path] = []
        pids: list[int] = []
        for share in self.spec.shares:
            if not share.path.is_dir():
                raise VmError(f"share {share.tag!r}: {share.path} is not a directory")
            sock = self.directory / f"virtiofs-{share.tag}.sock"
            sock.unlink(missing_ok=True)
            log = (self.directory / f"virtiofs-{share.tag}.log").open("w")
            proc = subprocess.Popen(
                virtiofsd_argv(daemon, share, sock),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            pids.append(proc.pid)
            deadline = time.monotonic() + 10
            while not sock.exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    self._kill(pids)
                    raise VmError(
                        f"virtiofsd for {share.tag!r} did not start; "
                        f"see {self.directory / f'virtiofs-{share.tag}.log'}"
                    )
                time.sleep(0.1)
            sockets.append(sock)
        (self.directory / "virtiofsd.pids").write_text(" ".join(map(str, pids)))
        return sockets

    @staticmethod
    def _kill(pids: Sequence[int]) -> None:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)

    def _stop_shares(self) -> None:
        pids_file = self.directory / "virtiofsd.pids"
        if pids_file.is_file():
            self._kill([int(p) for p in pids_file.read_text().split()])
            pids_file.unlink()

    def run_command(self, command: str, *, timeout: int = 600) -> GuestResult:
        """Run ``command`` as root in the guest's shell; recorded in the audit trail."""
        start = time.monotonic()
        try:
            done = self.runner(
                ssh_argv(self.key, self.spec.cid, command),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            result = GuestResult(
                command,
                done.returncode,
                done.stdout,
                done.stderr,
                round(time.monotonic() - start, 3),
            )
        except subprocess.TimeoutExpired:
            result = GuestResult(command, -1, "", "timeout", round(time.monotonic() - start, 3))
        audit.current().event(
            "vm.command",
            command=command,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            stdout=result.stdout[-4000:],
            stderr=result.stderr[-2000:],
        )
        return result

    def wait_ssh(self, *, timeout: float = 300, interval: float = 5) -> float:
        """Seconds from start until SSH answered; :class:`VmError` after ``timeout``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.run_command("true", timeout=30).exit_code == 0:
                return round(time.monotonic() - self.started, 1)
            time.sleep(interval)
        raise VmError(f"no SSH over vsock/{self.spec.cid} within {timeout:.0f} s")

    def qmp(self, command: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """One QMP command; returns its ``return`` (or raises on ``error``). I/O."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(str(self.qmp_socket))
            reader = sock.makefile("r")
            json.loads(reader.readline())  # greeting
            reply: dict[str, Any] = {}
            requests: list[dict[str, Any]] = [{"execute": "qmp_capabilities"}]
            requests.append({"execute": command, **({"arguments": arguments} if arguments else {})})
            for request in requests:
                sock.sendall(json.dumps(request).encode() + b"\n")
                while True:
                    reply = json.loads(reader.readline())
                    if "event" not in reply:
                        break
                if "error" in reply:
                    raise VmError(f"QMP {request['execute']}: {reply['error']}")
            result: dict[str, Any] = reply.get("return", {})
            return result

    def screenshot(self, dest: Path) -> Path:
        """The screen as PNG (PPM when ImageMagick is missing), through QMP."""
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "screen.ppm"
            self.qmp("screendump", {"filename": str(ppm)})
            if shutil.which("magick"):
                self.runner(["magick", str(ppm), str(dest)], check=True, capture_output=True)
            else:
                dest = dest.with_suffix(".ppm")
                shutil.copyfile(ppm, dest)
        audit.current().artifact(dest, role="screenshot", digest=False)
        return dest

    def stop(self) -> None:
        """Power off through QMP; kill the process if QEMU does not answer."""
        try:
            self.qmp("quit")
        except OSError, VmError, json.JSONDecodeError:
            if self.pidfile.is_file():
                with contextlib.suppress(ProcessLookupError, ValueError):
                    os.kill(int(self.pidfile.read_text().strip()), signal.SIGTERM)
        self._stop_shares()
        audit.current().event("vm.stop", uptime_s=round(time.monotonic() - self.started, 1))


def session_dir(name: str) -> Path:
    return config.scratch_dir() / "vm" / name


def load_session(name: str) -> Session:
    """The session ``vm start`` left (for ``vm run``/``stop``/``screenshot``)."""
    directory = session_dir(name)
    try:
        data = json.loads((directory / "session.json").read_text())
    except (OSError, json.JSONDecodeError) as err:
        raise VmError(f"no VM session named {name!r}: run `shidashi vm start` first") from err
    spec = VmSpec(
        iso=Path(data["iso"]),
        uefi=data["uefi"],
        cid=data["cid"],
        memory=data["memory"],
        cpus=data["cpus"],
        disks=tuple(Path(d) for d in data.get("disks", ())),
        shares=tuple(
            Share(s["tag"], Path(s["path"]), s["readonly"]) for s in data.get("shares", ())
        ),
    )
    return Session(spec, directory)


# --- the boot test ----------------------------------------------------------------------


def read_build_info(iso: Path) -> dict[str, Any]:
    """The ISO's own ``bentoo/build.json`` (xorriso, no mount, no root). I/O."""
    if shutil.which("xorriso") is None:
        raise VmError("xorriso is needed to read the ISO's bentoo/build.json (dev-libs/libisoburn)")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "build.json"
        done = subprocess.run(
            [
                "xorriso",
                "-osirrox",
                "on",
                "-indev",
                str(iso),
                "-extract",
                "/bentoo/build.json",
                str(dest),
            ],
            capture_output=True,
            text=True,
        )
        if done.returncode != 0 or not dest.is_file():
            raise VmError(f"{iso} has no bentoo/build.json (built before 2026-09-30?)")
        info: dict[str, Any] = json.loads(dest.read_text())
        return info


@dataclass(frozen=True)
class Check:
    """One thing to confirm inside the booted system."""

    name: str
    command: str
    expect: str | None = None
    #: How the output is compared (see :func:`judge`).
    mode: str = "equal"


def _unit_state(unit: str) -> str:
    """``enabled``/``disabled``/... or ``not-installed``. An explicit ``if``:
    ``is-enabled`` exits 1 on a disabled unit, so ``a && b || c`` printed both."""
    return (
        f"if systemctl list-unit-files {unit} --no-legend | grep -q .; "
        f"then systemctl is-enabled {unit}; else echo not-installed; fi"
    )


def boot_checks(cfg: SystemConfig, *, init: str) -> list[Check]:
    """What the booted live system must show, from the image's own configuration. Pure."""
    live = cfg.live
    checks = [
        Check("boot finished and healthy", "systemctl is-system-running --wait", "running"),
        Check("no failed units", "systemctl --failed --no-legend --plain", ""),
        Check("hostname", "hostnamectl hostname", cfg.hostname),
        Check("locale", "sed -n 's/^LANG=//p' /etc/locale.conf | tr -d '\"'", cfg.locale),
        Check("timezone", "timedatectl show -p Timezone --value", cfg.timezone),
        Check("machine-id generated at boot", "cat /etc/machine-id", None, "machine-id"),
    ]
    if cfg.os_release.get("NAME"):
        checks.append(
            Check("os-release", '. /etc/os-release && echo "$NAME"', cfg.os_release["NAME"])
        )
    if init == "systemd":
        checks.append(
            Check("console keymap", "sed -n 's/^KEYMAP=//p' /etc/vconsole.conf", cfg.keymap)
        )
        checks += [
            Check(f"enabled: {u}", _unit_state(u), None, "enabled-or-absent")
            for u in cfg.services.systemd.enable
        ]
        checks += [
            Check(f"disabled: {u}", _unit_state(u), None, "disabled-or-absent")
            for u in cfg.services.systemd.disable
        ]
    manager = display_manager(cfg, init=init)
    if manager is not None:
        checks.append(
            Check("display manager running", "systemctl is-active display-manager", "active")
        )
    if live.autologin:
        session_type = "wayland" if (live.session and manager) else "tty"
        checks.append(
            Check(
                f"{live.user} logged in automatically on seat0 ({session_type})",
                "for s in $(loginctl list-sessions --no-legend | "
                f'awk \'$3=="{live.user}" && $4=="seat0" {{print $1}}\'); '
                'do loginctl show-session "$s" -p Type --value; done',
                session_type,
                "contains",
            )
        )
    return checks


def judge(check: Check, stdout: str, exit_code: int) -> bool:
    """Whether ``check`` passed on this output. Pure."""
    del exit_code  # the output says it all; `is-enabled` exits 1 on "disabled"
    out = stdout.strip()
    if check.mode == "machine-id":
        return len(out) == 32 and all(c in "0123456789abcdef" for c in out)
    if check.mode == "enabled-or-absent":
        return out in ("enabled", "enabled-runtime", "alias", "static", "indirect", "not-installed")
    if check.mode == "disabled-or-absent":
        return out in ("disabled", "masked", "not-installed")
    if check.mode == "contains":
        return check.expect is not None and check.expect in out.split()
    return out == (check.expect or "")


#: Measured, never judged: how long the boot took, what it logged, what it uses.
METRICS = {
    "systemd-analyze": "systemd-analyze time --no-pager",
    "journal errors": "journalctl -b -p err -q --no-pager | wc -l",
    "memory": "free -m | sed -n 2p",
    "root filesystem": "df -h / | sed -n 2p",
    "kernel": "uname -r",
}


def boot_test(
    iso: Path,
    *,
    firmwares: Sequence[str] = ("bios", "uefi"),
    cid: int = 42,
    screenshots: Path | None = None,
    session_factory: Callable[[VmSpec, Path], Session] = Session,
) -> dict[str, Any]:
    """Boot ``iso`` on each firmware and run :func:`boot_checks`. Audited.

    Returns ``{"passed": bool, "firmwares": {name: {...}}}``; the caller decides
    the exit code. A screenshot per firmware when ``screenshots`` is a directory.
    """
    info = read_build_info(iso)
    recipe = config.load_recipe(info["arch"], info["flavor"], info["init"])
    cfg = load_system_config(recipe, variants_dir=config.variants_dir())
    checks = boot_checks(cfg, init=info["init"])
    run = audit.current()
    report: dict[str, Any] = {"iso": str(iso), "build": info, "firmwares": {}}
    for firmware in firmwares:
        spec = VmSpec(iso=iso, uefi=firmware == "uefi", cid=cid)
        session = session_factory(spec, session_dir(f"test-{firmware}"))
        results: list[dict[str, Any]] = []
        metrics: dict[str, str] = {}
        ssh_after: float | None = None
        with run.step(f"boot:{firmware}") as step:
            session.start()
            try:
                ssh_after = session.wait_ssh()
                step.add(ssh_after_s=ssh_after)
                for check in checks:
                    with run.step(f"check:{check.name}") as check_step:
                        done = session.run_command(check.command)
                        passed = judge(check, done.stdout, done.exit_code)
                        got = done.stdout.strip()[:500]
                        check_step.add(passed=passed, got=got, expected=check.expect)
                    results.append(
                        {
                            "check": check.name,
                            "passed": passed,
                            "got": got,
                            "expected": check.expect,
                        }
                    )
                for name, command in METRICS.items():
                    metrics[name] = session.run_command(command).stdout.strip()
                if screenshots is not None:
                    screenshots.mkdir(parents=True, exist_ok=True)
                    session.screenshot(screenshots / f"{iso.stem}-{firmware}.png")
            finally:
                session.stop()
            failed = [r["check"] for r in results if not r["passed"]]
            step.add(checks=len(results), failed=failed)
        report["firmwares"][firmware] = {
            "ssh_after_s": ssh_after,
            "checks": results,
            "metrics": metrics,
            "passed": not failed,
        }
    report["passed"] = all(f["passed"] for f in report["firmwares"].values())
    run.attach("boot-test", report)
    return report
