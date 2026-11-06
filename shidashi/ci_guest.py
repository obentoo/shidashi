"""The worker side of the CI runner: fetch, prepare, run one job, sweep (story 034).

Runs on the worker as root, under ``/mnt/work/ci`` (the work disk survives reboots):

- ``images/<sha256>.img`` -- the pinned Ubuntu cloud image (:func:`fetch`);
- ``prepared/<key>.qcow2`` -- the image after its one prepare boot (:func:`prepare`);
- ``jobs/<job>/`` -- one job's overlay, credentials and console, deleted in every outcome
  (:func:`run_job`); ``results/<job>.json`` -- its exit code, read by the host's pool.

QEMU, ``qemu-img`` and ``xorriso`` run in the toolbox container (:mod:`ci_guest_vm`).
Every event is one JSON line on stderr; the JIT config is in none of them.
"""

import hashlib
import http.client
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shidashi import config, locks, vmrunner
from shidashi.ci_guest_seed import (
    PREPARED_MARKER,
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
    cpus_for,
    emit,
    qemu_argv,
)

__all__ = [
    "CiGuestError",
    "GuestSpec",
    "cpus_for",
    "fetch",
    "prepare",
    "qemu_argv",
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
