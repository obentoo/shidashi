"""``JobHost``: one worker's CI directories and toolbox for the story 034 tests of
``ci_guest.run_job`` (tests/test_ci_guest_run.py).

- ``root`` stands for ``/mnt/work/ci`` and ``run_dir`` for ``/run/shidashi/ci``; the
  prepared image of the repository's own pins (seeds/ci-guest.toml, lab/vm/runner.env) is
  in place, so a job boots without preparing, and the JIT config a pool would dispatch is
  in ``run_dir/<job>/jitconfig`` (0600);
- the toolbox is ``FakeCiToolbox`` (tests/_fake_ci_toolbox.py), whose "guest" records what
  it saw when it booted -- its credential files, whether the tmpfs copy was still there,
  the job directories -- and prints the result line a test decides;
- ``nproc`` and ``/sys/module`` are the test's: half of ``nproc`` vCPUs, nesting as the
  test writes it.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from shidashi import config
from tests._fake_ci_toolbox import (
    RESULT_PREFIX,
    Boot,
    FakeCiToolbox,
    no_download,
    option_values,
    stand_in_toolbox_host,
)
from tests._fake_github_api import mint_jit_config
from tests._pending import try_import

run_job: Any = try_import("shidashi.ci_guest", "run_job")
Limits: Any = try_import("shidashi.ci_guest", "Limits")
CiGuestError: Any = try_import("shidashi.ci_guest", "CiGuestError")
load_pins: Any = try_import("shidashi.ci_guest_seed", "load_pins")
prepare_user_data: Any = try_import("shidashi.ci_guest_seed", "prepare_user_data")
prepared_key: Any = try_import("shidashi.ci_guest_seed", "prepared_key")

REPO_ROOT = Path(__file__).resolve().parents[1]
JOB = "9001"
REPO = "obentoo/private-app"


class FakeClock:
    """``time.monotonic`` that moves one second per reading."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@dataclass
class DuringBoot:
    """What the VM saw while it ran: its credential files and the tmpfs copy."""

    creds: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    tmpfs_jitconfig: bool | None = None
    jobs_present: list[str] = field(default_factory=list)


def write_sys_module(base: Path, *, amd: str | None = None, intel: str | None = None) -> Path:
    """A ``/sys/module`` with ``kvm_amd``/``kvm_intel`` ``parameters/nested`` as given."""
    for module, value in (("kvm_amd", amd), ("kvm_intel", intel)):
        if value is not None:
            (base / module / "parameters").mkdir(parents=True)
            (base / module / "parameters" / "nested").write_text(value, encoding="utf-8")
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class JobHost:
    root: Path
    run_dir: Path
    jitconfig: str
    toolbox: FakeCiToolbox
    clock: FakeClock
    prepared: Path
    sys_module: Path
    seen: DuringBoot = field(default_factory=DuringBoot)
    #: called when the VM boots, before it "runs" (a test reads the stream so far)
    at_boot: Callable[[], None] | None = None

    def place_jitconfig(self, job: str = JOB, *, mode: int = 0o600) -> Path:
        directory = self.run_dir / job
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "jitconfig"
        path.write_text(self.jitconfig, encoding="utf-8")
        path.chmod(mode)
        return path

    def guest(self, result: str | None, *, rc: int = 0, hang: bool = False) -> None:
        """The job VM: records what it sees, then prints ``result`` as its result line."""

        def boot(argv: list[str]) -> Boot:
            if self.at_boot is not None:
                self.at_boot()
            for value in option_values(argv, "-fw_cfg"):
                parts = dict(p.split("=", 1) for p in value.split(",") if "=" in p)
                path = Path(parts.get("file", ""))
                name = parts.get("name", "").removeprefix("opt/io.systemd.credentials/")
                if path.is_file():
                    self.seen.creds[name] = (path.stat().st_mode & 0o777, path.read_bytes())
            self.seen.tmpfs_jitconfig = (self.run_dir / JOB / "jitconfig").exists()
            jobs = self.root / "jobs"
            self.seen.jobs_present = sorted(p.name for p in jobs.iterdir()) if jobs.is_dir() else []
            line = "" if result is None else f"{RESULT_PREFIX}{result}\n"
            return Boot(
                rc=rc, serial=f"[  OK  ] Started shidashi-runner.service\n{line}", hang=hang
            )

        self.toolbox.on_boot = boot

    def job_dir(self, job: str = JOB) -> Path:
        return self.root / "jobs" / job

    def result_file(self, job: str = JOB) -> dict[str, Any] | None:
        """``results/<job>.json`` as the pool will read it, or ``None``."""
        path = self.root / "results" / f"{job}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), data
        return data

    def files_holding_the_jitconfig(self) -> list[Path]:
        secret = self.jitconfig.encode()
        return [p for p in self.root.rglob("*") if p.is_file() and secret in p.read_bytes()]


def limits(**changes: Any) -> Any:
    values = dict(memory_gib=4, disk_gib=40, timeout_min=360, min_free_gib=0)
    values.update(changes)
    return Limits(**values)


def job_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobHost:
    stand_in_toolbox_host(monkeypatch, tmp_path / "host")
    no_download(monkeypatch)
    pins = load_pins(config.seeds_dir() / "ci-guest.toml", REPO_ROOT / "lab" / "vm" / "runner.env")
    key = prepared_key(pins, prepare_user_data(pins))
    root = tmp_path / "ci"
    prepared = root / "prepared" / f"{key}.qcow2"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"QFI\xfb prepared stand-in")
    h = JobHost(
        root,
        tmp_path / "run",
        mint_jit_config(),
        FakeCiToolbox(),
        FakeClock(),
        prepared,
        write_sys_module(tmp_path / "sys-module", amd="0\n"),
    )
    h.place_jitconfig()
    return h


def run(
    host: JobHost,
    *,
    job: str = JOB,
    repo: str = REPO,
    nproc: int | None = 4,
    sys_module: Path | None = None,
    **changes: Any,
) -> tuple[int, str]:
    """``run_job``'s exit code, and the error's text when it raised ``CiGuestError``."""
    try:
        rc = run_job(
            job,
            repo,
            root=host.root,
            run_dir=host.run_dir,
            limits=limits(**changes),
            runner=host.toolbox,
            clock=host.clock,
            nproc=lambda: nproc,
            sys_module=host.sys_module if sys_module is None else sys_module,
        )
    except CiGuestError as err:
        return 1, str(err)
    return rc, ""


def events(text: str) -> list[dict[str, Any]]:
    found = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "event" in obj:
            found.append(obj)
    return found


def stopped_after_boot(toolbox: FakeCiToolbox) -> bool:
    last = max(i for i, c in enumerate(toolbox.calls) if any("qemu-system" in w for w in c))
    return any(
        c[:1] == ["podman"] and any(v in c for v in ("kill", "stop", "rm"))
        for c in toolbox.calls[last + 1 :]
    )
