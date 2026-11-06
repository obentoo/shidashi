"""The guest VM's QEMU command line, job and prepare boots (story 034, task 2.2; R4.1, R4.8,
R5.2, R7.1, R8.1).

Contract (design.md, "ci_guest.qemu_argv(...)"): pure
``qemu_argv(spec, *, disk, cred_files, ovmf_code, ovmf_vars, serial, qmp, seed=None)
-> list[str]`` with ``spec = GuestSpec(cpus, memory_gib)`` -- no vsock CID: the guest gets
no vsock device --, ``cred_files`` a mapping credential name -> file, ``ovmf_code`` the
firmware ``(path, format)`` as ``vm.ovmf()`` gives them and ``ovmf_vars`` the boot's own
copy of the variables:

- KVM, ``q35``, ``-cpu host`` (nested KVM when the host allows it, R8.1), ``-smp`` and
  ``-m`` from the spec (R7.1);
- ``disk`` as the virtio qcow2 disk (R4.1); with ``seed`` (the prepare boot's cidata ISO)
  exactly one more drive, a read-only cdrom, and nothing else; user-mode networking; no
  vsock device (nothing in the guest listens on it);
- one ``-fw_cfg name=opt/io.systemd.credentials/<name>,file=<path>`` per credential, none
  for an empty mapping: the JIT config travels in a file, never in argv -- no ``-smbios``
  (R5.2);
- no share of a worker directory: no ``-virtfs``, ``-fsdev``, ``vhost-user-fs`` (R4.8);
- ``-serial file:``, ``-qmp unix:``, ``-no-reboot``, ``-display none``;
- ``ValueError`` for ``cpus < 1`` or ``memory_gib < 1``.

``cpus_for(nproc) = max(2, nproc // 2)``: half the worker's CPUs, at least 2 (R7.1).
"""

from pathlib import Path
from typing import Any

import pytest

from tests._fake_ci_toolbox import option_values
from tests._pending import try_import

qemu_argv: Any = try_import("shidashi.ci_guest", "qemu_argv")
GuestSpec: Any = try_import("shidashi.ci_guest", "GuestSpec")
cpus_for: Any = try_import("shidashi.ci_guest", "cpus_for")

JOB = Path("/mnt/work/ci/jobs/9001")
PREP = Path("/mnt/work/ci/prepare/0123456789abcdef")
CREDS = {"shidashi.jitconfig": JOB / "jitconfig.cred", "shidashi.repo": JOB / "repo.cred"}
OVERLAY = JOB / "overlay.qcow2"
SEED = PREP / "seed.iso"


def argv_for(
    spec: Any,
    creds: dict[str, Path] | None = None,
    *,
    disk: Path = OVERLAY,
    seed: Path | None = None,
) -> list[str]:
    extra: dict[str, Any] = {} if seed is None else {"seed": seed}
    argv = qemu_argv(
        spec,
        disk=disk,
        cred_files=CREDS if creds is None else creds,
        ovmf_code=("/usr/share/edk2-ovmf/OVMF_CODE.fd", "raw"),
        ovmf_vars=disk.parent / "OVMF_VARS.fd",
        serial=disk.parent / "serial.log",
        qmp=disk.parent / "qmp.sock",
        **extra,
    )
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), argv
    assert argv, "qemu_argv returned no command line"
    return argv


def non_firmware_drives(argv: list[str]) -> list[list[str]]:
    return [d.split(",") for d in option_values(argv, "-drive") if "if=pflash" not in d.split(",")]


def without_option(argv: list[str], option: str, value: str) -> list[str]:
    """``argv`` with the one ``option value`` pair removed."""
    for i, word in enumerate(argv):
        if word == option and i + 1 < len(argv) and argv[i + 1] == value:
            return argv[:i] + argv[i + 2 :]
    raise AssertionError(f"no {option} {value} in {argv}")


def test_argv_boots_the_disk_under_kvm_with_the_hosts_cpu() -> None:
    """R4.1, R8.1."""
    argv = argv_for(GuestSpec(cpus=8, memory_gib=8))
    assert argv[0].endswith("qemu-system-x86_64")
    assert "-enable-kvm" in argv
    assert option_values(argv, "-machine")[:1] == ["q35"]
    assert option_values(argv, "-cpu") == ["host"]
    disk = [d for d in non_firmware_drives(argv) if f"file={OVERLAY}" in d]
    assert len(disk) == 1, option_values(argv, "-drive")
    assert "if=virtio" in disk[0] and "format=qcow2" in disk[0], disk


def test_argv_takes_cpus_and_memory_from_the_spec() -> None:
    """R7.1: the spec's vCPUs and GiB, nothing more."""
    argv = argv_for(GuestSpec(cpus=6, memory_gib=12))
    assert option_values(argv, "-smp")[0].split(",")[0] == "6"
    assert option_values(argv, "-m")[0] in ("12G", "12g", "12288", "12288M")


def test_argv_hands_each_credential_by_fw_cfg_file_and_never_by_smbios() -> None:
    """R5.2: a credential's VALUE never reaches argv; ``-smbios type=11`` would put it in
    ``/proc/<pid>/cmdline``. The prepare boot has none: no ``-fw_cfg`` at all."""
    argv = argv_for(GuestSpec(cpus=2, memory_gib=4))
    fw_cfg = sorted(option_values(argv, "-fw_cfg"))
    assert fw_cfg == sorted(
        f"name=opt/io.systemd.credentials/{name},file={path}" for name, path in CREDS.items()
    )
    assert "-smbios" not in argv
    assert option_values(argv_for(GuestSpec(cpus=4, memory_gib=4), {}), "-fw_cfg") == []


def test_argv_shares_no_worker_directory() -> None:
    """R4.8: without a seed the guest sees its disk and nothing of the worker's filesystem."""
    argv = argv_for(GuestSpec(cpus=2, memory_gib=4))
    joined = " ".join(argv)
    assert "-drive" in argv, "no disk at all"
    for forbidden in ("-virtfs", "-fsdev", "vhost-user-fs", "virtio-9p", "-hda", "-cdrom"):
        assert forbidden not in joined, forbidden
    assert [d for d in non_firmware_drives(argv) if f"file={OVERLAY}" not in d] == []


def test_argv_runs_headless_with_serial_qmp_user_networking_and_no_vsock() -> None:
    """No vsock device and no CID: nothing in the guest listens on one."""
    argv = argv_for(GuestSpec(cpus=2, memory_gib=4))
    assert option_values(argv, "-serial") == [f"file:{JOB / 'serial.log'}"]
    assert option_values(argv, "-qmp")[0].startswith(f"unix:{JOB / 'qmp.sock'}")
    assert option_values(argv, "-display") == ["none"]
    assert "-no-reboot" in argv
    assert any(n.startswith("user") for n in option_values(argv, "-nic"))
    assert not [w for w in argv if "vsock" in w], argv
    with pytest.raises(TypeError):
        GuestSpec(cpus=2, memory_gib=4, cid=40)


def test_argv_a_seed_adds_exactly_one_read_only_cdrom_and_nothing_else() -> None:
    """The prepare boot: its disk, plus the cidata ISO as a read-only cdrom -- and the rest
    of the command line is the job boot's."""
    spec = GuestSpec(cpus=4, memory_gib=4)
    plain = argv_for(spec, {}, disk=PREP / "disk.qcow2")
    seeded = argv_for(spec, {}, disk=PREP / "disk.qcow2", seed=SEED)
    seeds = [d for d in non_firmware_drives(seeded) if f"file={SEED}" in d]
    assert len(seeds) == 1, option_values(seeded, "-drive")
    assert "media=cdrom" in seeds[0] and "readonly=on" in seeds[0], seeds
    assert without_option(seeded, "-drive", ",".join(seeds[0])) == plain
    assert not [w for w in plain if str(SEED) in w]


def test_argv_boots_uefi_with_read_only_code_and_the_boots_own_variables() -> None:
    argv = argv_for(GuestSpec(cpus=2, memory_gib=4))
    pflash = [d.split(",") for d in option_values(argv, "-drive") if "if=pflash" in d.split(",")]
    assert len(pflash) == 2, pflash
    code = [p for p in pflash if "file=/usr/share/edk2-ovmf/OVMF_CODE.fd" in p]
    vars_ = [p for p in pflash if f"file={JOB / 'OVMF_VARS.fd'}" in p]
    assert len(code) == 1 and "readonly=on" in code[0]
    assert len(vars_) == 1 and "readonly=on" not in vars_[0]


@pytest.mark.parametrize(
    ("cpus", "memory_gib"),
    [
        pytest.param(0, 4, id="no-cpu"),
        pytest.param(-1, 4, id="negative-cpu"),
        pytest.param(2, 0, id="no-memory"),
        pytest.param(2, -4, id="negative-memory"),
    ],
)
def test_argv_refuses_an_impossible_spec(cpus: int, memory_gib: int) -> None:
    assert argv_for(GuestSpec(cpus=1, memory_gib=1)), "the smallest spec has no command line"
    with pytest.raises(ValueError):
        argv_for(GuestSpec(cpus=cpus, memory_gib=memory_gib))


def test_argv_cpus_for_is_half_the_workers_cpus_at_least_two() -> None:
    """R7.1: a 16-thread worker gives 8; a small one still gives 2."""
    table = {32: 16, 16: 8, 5: 2, 4: 2, 3: 2, 2: 2, 1: 2}
    assert {n: cpus_for(n) for n in table} == table
