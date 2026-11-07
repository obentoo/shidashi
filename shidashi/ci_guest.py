"""The worker side of the CI runner: fetch, prepare, run one job, sweep (story 034).

Runs on the worker as root, under ``/mnt/work/ci`` (the work disk survives reboots):

- ``images/<sha256>.img`` -- the pinned Ubuntu cloud image (:func:`fetch`);
- ``prepared/<key>.qcow2`` -- the image after its one prepare boot (:func:`prepare`);
- ``jobs/<job>/`` -- one job's overlay, credentials and console, deleted in every outcome
  (:func:`run_job`); ``results/<job>.json`` -- its exit code, read by the host's pool.

QEMU, ``qemu-img`` and ``xorriso`` run in the toolbox container (:mod:`ci_guest_vm`).
Every event is one JSON line on stderr; the JIT config is in none of them.
"""

import contextlib
import datetime
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shidashi import config, locks, vmrunner
from shidashi.ci_guest_seed import (
    JITCONFIG_CREDENTIAL,
    PREPARED_MARKER,
    REPO_CREDENTIAL,
    RESULT_PREFIX,
    GuestPins,
    load_pins,
    meta_data,
    prepare_user_data,
    prepared_key,
)
from shidashi.ci_guest_vm import (
    CiGuestError,
    GuestSpec,
    Runner,
    Toolbox,
    console_has,
    console_tail,
    console_value,
    cpus_for,
    emit,
    nested_kvm,
    qemu_argv,
    vm_end_event,
    vm_start_event,
)

__all__ = [
    "CiGuestError",
    "GuestSpec",
    "Limits",
    "cpus_for",
    "fetch",
    "nested_kvm",
    "prepare",
    "qemu_argv",
    "run_job",
    "sweep",
]

ROOT = Path("/mnt/work/ci")
RUN_DIR = Path("/run/shidashi/ci")
_CHUNK = 1024 * 1024
_SOCKET_TIMEOUT_S = 30
#: R3.4: a prepare boot that has not powered off by then is stopped.
PREPARE_TIMEOUT_S = 30 * 60
_PREPARE_SPEC = GuestSpec(cpus=4, memory_gib=4)
_PREPARED_SIZE = "16G"
_KEEP_PREPARED = 2
#: qemu-img on a multi-GiB image: generous, but bounded.
_TOOL_TIMEOUT_S = 30 * 60
#: GitHub job ids are numeric; a ``prepare-<key>`` id would collide with a prepare container.
_JOB = re.compile(r"[0-9]+")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Limits:
    """A job VM's bounds (design.md defaults). The vCPUs are not one: the worker derives
    them from its own CPU count (R7.1)."""

    memory_gib: int = 8
    disk_gib: int = 64
    timeout_min: int = 360
    min_free_gib: int = 20


def default_pins() -> GuestPins:
    """The pins of this tree: ``seeds/ci-guest.toml`` and ``lab/vm/runner.env``."""
    runner_env = Path(__file__).resolve().parent.parent / "lab" / "vm" / "runner.env"
    return load_pins(config.seeds_dir() / "ci-guest.toml", runner_env)


def _ms(began: float, clock: Callable[[], float] = time.monotonic) -> int:
    return max(0, round((clock() - began) * 1000))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# --- fetch ------------------------------------------------------------------------------------


def _download(url: str, part: Path, opener: Callable[..., Any]) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with opener(url, timeout=_SOCKET_TIMEOUT_S) as answer, part.open("wb") as out:
        while chunk := answer.read(_CHUNK):
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
        length = answer.headers.get("Content-Length") if answer.headers else None
        if length is not None and length.isdigit() and int(length) != size:
            raise OSError(f"connection closed after {size} of {length} bytes")
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest(), size


def fetch(pins: GuestPins, *, root: Path = ROOT, opener: Callable[..., Any] | None = None) -> Path:
    """The pinned base image on the work disk, verified (R2.1-R2.4). I/O."""
    opener = urllib.request.urlopen if opener is None else opener
    images = root / "images"
    target = images / f"{pins.image.sha256}.img"
    if target.is_file():
        if _sha256(target) == pins.image.sha256:
            return target
        target.unlink()
    images.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    began, url = time.monotonic(), pins.image.url
    try:
        actual, size = _download(url, part, opener)
    except (OSError, urllib.error.URLError, http.client.HTTPException) as err:
        part.unlink(missing_ok=True)
        emit("ci_fetch", url=url, bytes=0, duration_ms=_ms(began), result="failed")
        raise CiGuestError(f"download of {url} failed: {err}") from err
    if actual != pins.image.sha256:
        part.unlink(missing_ok=True)
        emit("ci_fetch", url=url, bytes=size, duration_ms=_ms(began), result="digest-mismatch")
        raise CiGuestError(
            f"{url}: sha256 is {actual}, seeds/ci-guest.toml pins {pins.image.sha256}"
        )
    os.replace(part, target)
    emit("ci_fetch", url=url, bytes=size, duration_ms=_ms(began), result="ok")
    return target


# --- sweep ------------------------------------------------------------------------------------


def container_alive(runner: Runner) -> Callable[[str], bool]:
    """Whether a container runs, by podman's own answer; podman that cannot be asked is
    an error -- a directory is never deleted on a guess."""

    def alive(container: str) -> bool:
        try:
            done = runner(
                ["podman", "container", "inspect", "--format", "{{.State.Running}}", container],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as err:
            raise CiGuestError(f"cannot ask podman whether {container} runs: {err}") from err
        return done.returncode == 0 and (done.stdout or "").strip() == "true"

    return alive


def _remove_entry(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)  # a symlink is unlinked, never followed
    else:
        shutil.rmtree(path)


def sweep(
    root: Path,
    *,
    alive: Callable[[str], bool],
    run_dir: Path = RUN_DIR,
    prepare: bool = False,
) -> list[str]:
    """Delete the job, JIT-config and -- with ``prepare``, under ``prepare.lock`` --
    preparation directories whose QEMU container no longer runs (R4.6, R5.3)."""
    groups = [(root / "jobs", "ci-"), (run_dir, "ci-")]
    if prepare:
        groups.append((root / "prepare", "ci-prepare-"))
    removed: list[str] = []
    for parent, prefix in groups:
        if not parent.is_dir():
            continue
        for entry in sorted(parent.iterdir(), key=lambda p: p.name):
            if alive(f"shidashi-vm-{prefix}{entry.name}"):
                continue
            _remove_entry(entry)
            removed.append(entry.name)
    if removed:
        emit("ci_sweep", removed=removed)
    return removed


# --- prepare ----------------------------------------------------------------------------------


def _preflight() -> None:
    try:
        vmrunner.preflight()
    except vmrunner.VmRunnerError as err:
        raise CiGuestError(str(err)) from err


def _keep_newest(prepared: Path) -> None:
    images = sorted(prepared.glob("*.qcow2"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in images[_KEEP_PREPARED:]:
        old.unlink(missing_ok=True)


def _prepare_boot(pins: GuestPins, key: str, work: Path, root: Path, toolbox: Toolbox) -> Path:
    """Seed, copy, boot, check the marker, compress; the final image's ``.part``."""
    base = fetch(pins, root=root)
    user_data, meta = work / "user-data", work / "meta-data"
    user_data.write_text(prepare_user_data(pins), encoding="utf-8")
    meta.write_text(meta_data(f"prepare-{key}"), encoding="utf-8")
    seed, disk = work / "seed.iso", work / "disk.qcow2"
    serial, qmp, vars_ = work / "serial.log", work / "qmp.sock", work / "OVMF_VARS"
    name = f"ci-prepare-{key}"
    steps = [
        ("xorriso", ["xorriso", "-as", "mkisofs", "-o", str(seed), "-V", "cidata", "-J", "-R"]),
        ("qemu-img convert", ["qemu-img", "convert", "-f", "qcow2", "-O", "qcow2"]),
        ("qemu-img resize", ["qemu-img", "resize", str(disk), _PREPARED_SIZE]),
    ]
    steps[0][1].extend([str(user_data), str(meta)])
    steps[1][1].extend([str(base), str(disk)])
    for step, args in steps:
        toolbox.tool(step, args, name=f"{name}-tool", scratch=work, timeout_s=_TOOL_TIMEOUT_S)
    code, vars_template, fmt = toolbox.firmware(work)
    shutil.copyfile(vars_template, vars_)
    argv = qemu_argv(
        _PREPARE_SPEC,
        disk=disk,
        cred_files={},
        ovmf_code=(str(code), fmt),
        ovmf_vars=vars_,
        serial=serial,
        qmp=qmp,
        seed=seed,
    )
    rc = toolbox.boot(argv, name=name, scratch=work, qmp=qmp, timeout_s=PREPARE_TIMEOUT_S)
    if rc is None:
        raise CiGuestError(
            f"the prepare boot did not power off within {PREPARE_TIMEOUT_S // 60} min; "
            f"last serial lines:\n{console_tail(serial)}"
        )
    if rc != 0 or not console_has(serial, PREPARED_MARKER):
        raise CiGuestError(
            f"the prepare boot ended (rc {rc}) without {PREPARED_MARKER}; "
            f"last serial lines:\n{console_tail(serial)}"
        )
    part = root / "prepared" / f"{key}.qcow2.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    toolbox.tool(
        "qemu-img convert -c",
        ["qemu-img", "convert", "-c", "-f", "qcow2", "-O", "qcow2", str(disk), str(part)],
        name=f"{name}-tool",
        scratch=work,
        timeout_s=_TOOL_TIMEOUT_S,
    )
    with part.open("rb") as handle:
        os.fsync(handle.fileno())
    return part


def prepare(
    pins: GuestPins,
    *,
    root: Path = ROOT,
    runner: Runner = subprocess.run,
    run_dir: Path = RUN_DIR,
) -> Path:
    """The prepared image of ``pins``, made once (R3.1, R3.4-R3.6). I/O."""
    key = prepared_key(pins, prepare_user_data(pins))
    final = root / "prepared" / f"{key}.qcow2"
    if final.is_file():
        return final
    _preflight()
    root.mkdir(parents=True, exist_ok=True)
    with locks.hold(root / "prepare.lock", what="the CI guest preparation"):
        if final.is_file():  # made by the preparation this one waited for
            return final
        sweep(root, alive=container_alive(runner), run_dir=run_dir, prepare=True)
        began = time.monotonic()
        work = root / "prepare" / key
        part = final.with_name(final.name + ".part")
        try:
            toolbox = Toolbox(root, runner)
            # under prepare.lock a live prepare container of this key is an orphan of a
            # killed run: stop it and take its work dir, or no prepare could ever start
            toolbox.remove(f"ci-prepare-{key}")
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            part = _prepare_boot(pins, key, work, root, toolbox)
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            emit("ci_prepare", key=key, duration_ms=_ms(began), result="failed")
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _keep_newest(final.parent)
        emit("ci_prepare", key=key, duration_ms=_ms(began), result="ok")
        return final


# --- run one job ------------------------------------------------------------------------------


def _take_jitconfig(run_dir: Path, job: str) -> str:
    """Read and remove ``<run_dir>/<job>/jitconfig`` (R5.3): a regular 0600 file of the
    caller's, never a symlink."""
    path = run_dir / job / "jitconfig"
    try:
        info = path.lstat()
    except OSError as err:
        raise CiGuestError(f"no JIT config at {path}: {err}") from err
    if not stat.S_ISREG(info.st_mode):
        raise CiGuestError(f"{path} is not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
        raise CiGuestError(f"{path} must be mode 0600 and owned by uid {os.geteuid()}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as handle:
            value = handle.read()
    except OSError as err:
        raise CiGuestError(f"cannot read {path}: {err}") from err
    path.unlink()
    with contextlib.suppress(OSError):
        path.parent.rmdir()
    if not value.strip():
        raise CiGuestError(f"{path} was empty")
    return value


def _secret_file(path: Path, value: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)


def _write_result(root: Path, job: str, repo: str, rc: int) -> None:
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    ended = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    part = results / f".{job}.json.part"
    part.write_text(
        json.dumps({"job": job, "repo": repo, "rc": rc, "ended": ended}),
        encoding="utf-8",
    )
    os.replace(part, results / f"{job}.json")


def _validate(job: str, repo: str) -> None:
    if not _JOB.fullmatch(job):
        raise CiGuestError(f"job id {job!r} is not a GitHub job id (digits only)")
    if not _REPO.fullmatch(repo):
        raise CiGuestError(f"repository {repo!r} is not OWNER/NAME")


def run_job(
    job: str,
    repo: str,
    *,
    root: Path = ROOT,
    run_dir: Path = RUN_DIR,
    limits: Limits | None = None,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    nproc: Callable[[], int | None] = os.cpu_count,
    sys_module: Path = Path("/sys/module"),
    pins: GuestPins | None = None,
) -> int:
    """One GitHub Actions job in one ephemeral VM: 0 pass, 1 fail, 3 low disk, 4 timeout
    (R4). Refusals before the job is accepted raise :class:`CiGuestError`. I/O."""
    limits = Limits() if limits is None else limits
    _validate(job, repo)
    _preflight()
    jitconfig = _take_jitconfig(run_dir, job)  # before the sweep, which would take it
    root.mkdir(parents=True, exist_ok=True)
    sweep(root, alive=container_alive(runner), run_dir=run_dir)
    if shutil.disk_usage(root).free < limits.min_free_gib * 1024**3:
        _write_result(root, job, repo, 3)
        return 3
    work = root / "jobs" / job
    # ``systemctl stop`` of the unit: SIGTERM unwinds through the ``finally`` instead of
    # killing the process around it, which left the JIT config on the work disk (review 3.x).
    with vmrunner.unwinding_sigterm():
        rc, result, began, started = 1, "error", clock(), False
        try:
            prepared = prepare(
                default_pins() if pins is None else pins, root=root, runner=runner, run_dir=run_dir
            )
            work.mkdir(parents=True)
            toolbox = Toolbox(root, runner)
            overlay = work / "overlay.qcow2"
            toolbox.tool(
                "qemu-img create",
                ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(prepared.absolute())]
                + [str(overlay), f"{limits.disk_gib}G"],
                name=f"ci-{job}-tool",
                scratch=work,
                timeout_s=_TOOL_TIMEOUT_S,
            )
            creds = {
                JITCONFIG_CREDENTIAL: work / "jitconfig.cred",
                REPO_CREDENTIAL: work / "repo.cred",
            }
            _secret_file(creds[JITCONFIG_CREDENTIAL], jitconfig)
            _secret_file(creds[REPO_CREDENTIAL], repo)
            del jitconfig
            code, vars_template, fmt = toolbox.firmware(work)
            shutil.copyfile(vars_template, work / "OVMF_VARS")
            spec = GuestSpec(cpus=cpus_for(nproc() or 2), memory_gib=limits.memory_gib)
            serial, qmp = work / "serial.log", work / "qmp.sock"
            argv = qemu_argv(
                spec,
                disk=overlay,
                cred_files=creds,
                ovmf_code=(str(code), fmt),
                ovmf_vars=work / "OVMF_VARS",
                serial=serial,
                qmp=qmp,
            )
            nested = nested_kvm(sys_module)
            vm_start_event(job, repo, cpus=spec.cpus, memory_gib=spec.memory_gib, nested=nested)
            began, started = clock(), True
            exit_code = toolbox.boot(
                argv, name=f"ci-{job}", scratch=work, qmp=qmp, timeout_s=limits.timeout_min * 60
            )
            value = console_value(serial, RESULT_PREFIX)
            if exit_code is None:
                rc, result = 4, "timeout"
            elif value == "0":
                rc, result = 0, "pass"
            elif value is not None:
                rc, result = 1, "fail"
            else:
                emit("ci_vm_console", job=job, repo=repo, tail=console_tail(serial))
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if started:
                vm_end_event(job, repo, duration_ms=_ms(began, clock), result=result)
            _write_result(root, job, repo, rc)
    return rc
