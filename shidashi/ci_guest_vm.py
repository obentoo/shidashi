"""The CI guest VM's command line, its events and the toolbox it runs in (story 034).

Pure parts: :class:`GuestSpec`, :func:`qemu_argv`, :func:`cpus_for`, the event builders,
:func:`nested_kvm` (reads two sysfs files). I/O part: :class:`Toolbox`, which runs
``qemu-img``, ``xorriso`` and QEMU inside the story-024 toolbox container -- the slim
worker has none of them on its root -- through ``vmrunner.run_argv`` with the work disk
bound at the same path, so every path in a command is a host path.

:mod:`shidashi.ci_guest` re-exports what callers use; this split keeps both modules under
the 500-line limit.
"""

import contextlib
import json
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shidashi import vm, vmrunner

Runner = Callable[..., subprocess.CompletedProcess[str]]

#: The tools ``ci-guest exec`` may run inside the container: nothing else is reachable.
TOOLS = ("qemu-img", "qemu-system-x86_64", "xorriso")
_QEMU = "qemu-system-x86_64"
_CREDENTIALS = "opt/io.systemd.credentials/"
#: QMP ``quit`` first, then podman stops the container: SIGTERM, SIGKILL after this many s.
STOP_GRACE_S = 10
_STDERR_TAIL = 2000
_FIRMWARE_RECORD = "ovmf.json"


class CiGuestError(Exception):
    """A step of the CI guest's life failed: names the step and what it saw."""


@dataclass(frozen=True)
class GuestSpec:
    """The guest's size. No vsock CID: nothing in the guest listens on one."""

    cpus: int
    memory_gib: int


def cpus_for(nproc: int) -> int:
    """Half the worker's CPUs, at least 2 (R7.1): Shidashi builds keep the other half."""
    return max(2, nproc // 2)


def qemu_argv(
    spec: GuestSpec,
    *,
    disk: Path,
    cred_files: Mapping[str, Path],
    ovmf_code: tuple[str, str],
    ovmf_vars: Path,
    serial: Path,
    qmp: Path,
    seed: Path | None = None,
) -> list[str]:
    """QEMU's argv for a disk-image guest, job or prepare boot (R4.1, R4.8, R5.2, R8.1). Pure.

    Credentials travel as files through ``-fw_cfg`` -- never ``-smbios``, which would put
    their values in ``/proc/<pid>/cmdline``. No share of a worker directory: the disk, the
    firmware and, at the prepare boot only, the cidata seed as a read-only cdrom.
    """
    if spec.cpus < 1 or spec.memory_gib < 1:
        raise ValueError(f"impossible guest: {spec.cpus} vCPUs, {spec.memory_gib} GiB")
    code, fmt = ovmf_code
    argv = [
        _QEMU,
        "-name",
        "shidashi-ci",
        "-enable-kvm",
        "-machine",
        "q35",
        "-cpu",
        "host",
        "-smp",
        str(spec.cpus),
        "-m",
        f"{spec.memory_gib}G",
        "-nodefaults",
        "-drive",
        f"if=pflash,format={fmt},readonly=on,file={code}",
        "-drive",
        f"if=pflash,format={fmt},file={ovmf_vars}",
        "-drive",
        f"file={disk},if=virtio,format=qcow2,discard=unmap",
    ]
    if seed is not None:
        argv += ["-drive", f"file={seed},media=cdrom,readonly=on"]
    argv += ["-nic", "user,model=virtio-net-pci"]
    for name, path in cred_files.items():
        argv += ["-fw_cfg", f"name={_CREDENTIALS}{name},file={path}"]
    return [
        *argv,
        "-serial",
        f"file:{serial}",
        "-qmp",
        f"unix:{qmp},server=on,wait=off",
        "-no-reboot",
        "-display",
        "none",
    ]


def emit(event: str, **fields: Any) -> None:
    """One JSON object on one stderr line (R9.1). Callers never pass a JIT config."""
    sys.stderr.write(json.dumps({"event": event, **fields}) + "\n")
    sys.stderr.flush()


# --- the serial console ----------------------------------------------------------------------


def console_lines(serial: Path) -> list[str]:
    try:
        text = serial.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines()]


def console_tail(serial: Path, n: int = 20) -> str:
    lines = console_lines(serial)
    return "\n".join(lines[-n:]) if lines else "(no serial output)"


def console_has(serial: Path, marker: str) -> bool:
    """The marker as a line of its own: a line quoting it (cloud-init reporting a failed
    command) is not the guest printing it."""
    return marker in console_lines(serial)


# --- the toolbox container -------------------------------------------------------------------


def _tail(done: subprocess.CompletedProcess[str]) -> str:
    return (done.stderr or "").strip()[-_STDERR_TAIL:] or "(no error output)"


def _qmp_quit(qmp: Path, timeout_s: float = 5.0) -> None:
    """Ask QEMU to quit over its QMP socket; whatever fails, the caller kills it next."""
    with (
        contextlib.suppress(OSError, ValueError),
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock,
    ):
        sock.settimeout(timeout_s)
        sock.connect(str(qmp))
        stream = sock.makefile("rwb")
        stream.readline()  # the greeting
        for command in ("qmp_capabilities", "quit"):
            stream.write(json.dumps({"execute": command}).encode() + b"\n")
            stream.flush()
            stream.readline()


@dataclass
class Toolbox:
    """The toolbox container: one ``podman run`` per step, ``root`` bound read-write."""

    root: Path
    runner: Runner

    def __post_init__(self) -> None:
        try:
            self.tag = vmrunner.ensure_image(vmrunner.runner_tarball(), runner=self.runner)
        except vmrunner.VmRunnerError as err:
            raise CiGuestError(f"the toolbox image: {err}") from err

    def argv(self, name: str, inner: Sequence[str], *, scratch: Path) -> list[str]:
        try:
            return vmrunner.run_argv(
                self.tag,
                name=name,
                inner_args=["ci-guest", *inner],
                venv=vmrunner._venv(),
                tree=vmrunner._tree(),
                mounts_ro=[],
                scratch=scratch,
                mounts_rw=[self.root],
                seccomp=vmrunner.seccomp_profile(scratch),
                cgroups="split",
            )
        except vmrunner.VmRunnerError as err:
            raise CiGuestError(f"the toolbox command line of {name}: {err}") from err

    def remove(self, name: str, *, grace_s: int = 0) -> None:
        container = f"shidashi-vm-{name}"
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            self.runner(
                ["podman", "rm", "-f", "-i", "--time", str(grace_s), container],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )

    def tool(
        self, step: str, args: Sequence[str], *, name: str, scratch: Path, timeout_s: int
    ) -> None:
        """Run ``args`` (``qemu-img ...``, ``xorriso ...``) in a container; a failure is a
        :class:`CiGuestError` naming ``step`` with the stderr tail."""
        # a container record whose conmon died (unit stopped, OOM) would hold the name
        self.remove(name)
        argv = self.argv(name, ["exec", *args], scratch=scratch)
        try:
            done = self.runner(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as err:
            self.remove(name, grace_s=0)
            raise CiGuestError(f"{step} took longer than {timeout_s} s") from err
        except OSError as err:
            raise CiGuestError(f"{step}: cannot run podman: {err}") from err
        if done.returncode != 0:
            raise CiGuestError(f"{step} failed (rc {done.returncode}): {_tail(done)}")

    def boot(
        self, argv: Sequence[str], *, name: str, scratch: Path, qmp: Path, timeout_s: int
    ) -> int | None:
        """QEMU in container ``shidashi-vm-<name>``; its exit code, or None past
        ``timeout_s`` -- then the VM is stopped: QMP ``quit``, then podman's kill."""
        self.remove(name, grace_s=0)
        command = self.argv(name, ["exec", *argv], scratch=scratch)
        try:
            done = self.runner(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            _qmp_quit(qmp)
            self.remove(name, grace_s=STOP_GRACE_S)
            return None
        except OSError as err:
            raise CiGuestError(f"the VM {name}: cannot run podman: {err}") from err
        # ``--rm`` removes it on a normal exit; an orphan of a killed client goes here
        self.remove(name, grace_s=0)
        return done.returncode

    def firmware(self, scratch: Path) -> tuple[Path, Path, str]:
        """OVMF code, its variables template and their format, copied once per toolbox
        image under ``<root>/firmware/<tag>/``: the slim worker has no OVMF on its root,
        only the toolbox has, and QEMU runs there with ``root`` bound."""
        dest = self.root / "firmware" / self.tag.rsplit(":", 1)[-1]
        record = dest / _FIRMWARE_RECORD
        if not record.is_file():
            try:
                code, vars_, fmt = vm.ovmf()
            except vm.VmError:
                # this node has none: the toolbox copies its own (``ci-guest firmware``)
                self._firmware_from_toolbox(dest, scratch)
            else:
                dest.mkdir(parents=True, exist_ok=True)
                write_firmware(dest, Path(code), Path(vars_), fmt)
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
            return dest / data["code"], dest / data["vars"], str(data["format"])
        except (OSError, ValueError, KeyError, TypeError) as err:
            raise CiGuestError(f"no usable OVMF firmware under {dest}: {err}") from err

    def _firmware_from_toolbox(self, dest: Path, scratch: Path) -> None:
        self.remove("ci-firmware")
        argv = self.argv("ci-firmware", ["firmware", str(dest)], scratch=scratch)
        try:
            done = self.runner(
                argv, capture_output=True, text=True, errors="replace", check=False, timeout=120
            )
        except (subprocess.TimeoutExpired, OSError) as err:
            raise CiGuestError(f"copy of the toolbox's OVMF firmware: {err}") from err
        if done.returncode != 0:
            raise CiGuestError(f"copy of the toolbox's OVMF firmware failed: {_tail(done)}")


def write_firmware(dest: Path, code: Path, vars_: Path, fmt: str) -> None:
    """Copy OVMF code and variables template into ``dest`` and record them; the record is
    written last, so a half copy is never taken for a whole one."""
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(code, dest / code.name)
    shutil.copyfile(vars_, dest / vars_.name)
    record = {"code": code.name, "vars": vars_.name, "format": fmt}
    part = dest / (_FIRMWARE_RECORD + ".part")
    part.write_text(json.dumps(record), encoding="utf-8")
    part.replace(dest / _FIRMWARE_RECORD)
