"""A fake worker for story 010's tests: what would run on the worker runs here.

Nothing reaches a network or a real worker. :meth:`FakeWorker.install` puts shims
first on ``PATH``:

- host side: ``ssh`` records every connection, then runs the remote command
  locally with ``bash -c`` under a temporary root that stands for the worker's
  ``/``; ``rsync`` records its argv (and which host owner locks exist at that
  moment), then runs the REAL rsync -- which reaches the "worker" through the
  fake ssh, so every transfer is a real local rsync; ``git`` sends anything aimed
  at the Shidashi checkout to a temporary repository, so a test chooses the
  commit and whether the tree is dirty without touching the real checkout.
- worker side: ``systemctl``, ``systemd-run`` (a detached "unit" that really runs
  its command, with only the ``--setenv`` environment, and with the shipped
  interpreter replaced by a fake ``shidashi``), ``findmnt``, ``mountpoint``,
  ``df``, ``nproc``, ``lscpu``, ``free``, ``lsblk``, ``emaint``, ``poweroff``,
  ``shutdown`` and -- only when the image "has" it -- ``smartctl``. Every other
  command is the host's own binary.

The worker paths that matter (``/mnt/work``, ``/proc/cpuinfo``, ``/proc/meminfo``,
``/etc/os-release``, ``/usr/lib/os-release``) are rewritten into the temporary
root on the way in, and the root prefix is stripped from text output on the way
out: the host sees worker paths, the worker's shell sees consistent ones.

Run as a script (``python -IBS _fake_worker.py NAME ARGS...``) it IS the shim NAME:
the shims use the standard library only.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import pytest

PYTHON = sys.executable
HELPER = Path(__file__).resolve()

#: ``/proc/cpuinfo`` flags of an AMD Ryzen 7 5700X (Zen 3) -- bentoo-lab's CPU.
#: Note the cpuinfo spellings: ``pni`` (SSE3), ``pclmulqdq``, ``fma``, ``sha_ni``;
#: no AVX-512 and no AVX-VNNI.
ZEN3_CPUINFO_FLAGS: tuple[str, ...] = tuple(
    [
        "fpu",
        "vme",
        "de",
        "pse",
        "tsc",
        "msr",
        "pae",
        "mce",
        "cx8",
        "apic",
        "sep",
        "mtrr",
        "pge",
        "mca",
        "cmov",
        "pat",
        "pse36",
        "clflush",
        "mmx",
        "fxsr",
        "sse",
        "sse2",
        "ht",
        "syscall",
        "nx",
        "mmxext",
        "fxsr_opt",
        "pdpe1gb",
        "rdtscp",
        "lm",
        "constant_tsc",
        "rep_good",
        "nopl",
        "nonstop_tsc",
        "cpuid",
        "extd_apicid",
        "aperfmperf",
        "rapl",
        "pni",
        "pclmulqdq",
        "monitor",
        "ssse3",
        "fma",
        "cx16",
        "sse4_1",
        "sse4_2",
        "movbe",
        "popcnt",
        "aes",
        "xsave",
        "avx",
        "f16c",
        "rdrand",
        "lahf_lm",
        "cmp_legacy",
        "svm",
        "extapic",
        "cr8_legacy",
        "abm",
        "sse4a",
        "misalignsse",
        "3dnowprefetch",
        "osvw",
        "ibs",
        "skinit",
        "wdt",
        "tce",
        "topoext",
        "perfctr_core",
        "perfctr_nb",
        "bpext",
        "perfctr_llc",
        "mwaitx",
        "cpb",
        "cat_l3",
        "cdp_l3",
        "hw_pstate",
        "ssbd",
        "mba",
        "ibrs",
        "ibpb",
        "stibp",
        "vmmcall",
        "fsgsbase",
        "bmi1",
        "avx2",
        "smep",
        "bmi2",
        "erms",
        "invpcid",
        "cqm",
        "rdt_a",
        "rdseed",
        "adx",
        "smap",
        "clflushopt",
        "clwb",
        "sha_ni",
        "xsaveopt",
        "xsavec",
        "xgetbv1",
        "xsaves",
        "cqm_llc",
        "cqm_occup_llc",
        "cqm_mbm_total",
        "cqm_mbm_local",
        "user_shstk",
        "clzero",
        "irperf",
        "xsaveerptr",
        "rdpru",
        "wbnoinvd",
        "arat",
        "npt",
        "lbrv",
        "svm_lock",
        "nrip_save",
        "tsc_scale",
        "vmcb_clean",
        "flushbyasid",
        "decodeassists",
        "pausefilter",
        "pfthreshold",
        "avic",
        "v_vmsave_vmload",
        "vgif",
        "v_spec_ctrl",
        "umip",
        "pku",
        "ospke",
        "vaes",
        "vpclmulqdq",
        "rdpid",
        "overflow_recov",
        "succor",
        "smca",
        "fsrm",
        "debug_swap",
    ]
)
ZEN3_MODEL = "AMD Ryzen 7 5700X 8-Core Processor"

GIB = 1024**3
MEM_TOTAL_KB = 33554432  # 32 GiB
MEM_AVAILABLE_KB = 30408704  # 29 GiB
WORK_AVAIL = 500_107_862_016  # bytes free on the work disk (~465.8 GiB)
WORK_SIZE = 1_000_204_886_016
ROOT_AVAIL = 8 * GIB  # the RAM root: what df reports for an UNMOUNTED /mnt/work
BUILD_ID = "20261005T101500Z-a1b2c3"

#: Commands the worker side must never reach on the host, or that a shim replaces.
_DENY = {
    "ssh",
    "scp",
    "sftp",
    "sudo",
    "doas",
    "su",
    "smartctl",
    "systemctl",
    "systemd-run",
    "findmnt",
    "mountpoint",
    "df",
    "nproc",
    "lscpu",
    "free",
    "lsblk",
    "emaint",
    "poweroff",
    "shutdown",
    "halt",
    "reboot",
    "pkill",
    "killall",
    "mount",
    "umount",
    "shidashi",
}
_WORKER_SHIMS = (
    "systemctl",
    "systemd-run",
    "findmnt",
    "mountpoint",
    "df",
    "nproc",
    "lscpu",
    "free",
    "lsblk",
    "emaint",
    "poweroff",
    "shutdown",
    "halt",
)
_MAPPED = (
    "/mnt/work",
    "/proc/cpuinfo",
    "/proc/meminfo",
    "/proc/loadavg",
    "/etc/os-release",
    "/usr/lib/os-release",
)
LOAD1 = 3.42


# ======================================================================================
# The test-side object
# ======================================================================================


class FakeWorker:
    """One fake worker under ``base``; see the module docstring."""

    def __init__(self, base: Path, *, name: str, address: str) -> None:
        self.base = base
        self.name = name
        self.address = address
        self.root = base / "worker-root"  # the worker's "/"
        self.work = self.root / "mnt" / "work"
        self.state = base / "fake-state"
        self.repo = base / "host-repo"  # stands for the Shidashi checkout
        self.venv = base / "host-venv"  # stands for the host's virtual environment
        self.host_cache = base / "cache"  # SHIDASHI_CACHE on the host
        self.keys = base / "keys"
        self.real_repo = _real_repo()
        self.head = ""
        self._watchdog: threading.Timer | None = None
        self.watchdog_fired = False

    # --- setup ------------------------------------------------------------------------

    @classmethod
    def install(
        cls,
        base: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        name: str = "bentoo-lab",
        address: str = "192.0.2.10",
        mounted: bool = True,
        smartctl: str | None = "PASSED",
        cpu_flags: tuple[str, ...] = ZEN3_CPUINFO_FLAGS,
        nproc: int = 16,
        watchdog_s: float = 120.0,
    ) -> FakeWorker:
        # the fake hands every transfer to the real rsync: without it, skip like the
        # other tests that need a host tool (CI images such as act's lack rsync)
        if shutil.which("rsync") is None:
            import pytest  # only TYPE_CHECKING imports it at module level

            pytest.skip("needs rsync: the fake worker runs the real one")
        from shidashi import config
        from shidashi.seed import load_pointer

        fw = cls(base, name=name, address=address)
        for d in (fw.state / "units", fw.root / "root", fw.work, fw.keys, fw.host_cache):
            d.mkdir(parents=True, exist_ok=True)
        (fw.keys / "id_ed25519").write_text("not a real key\n")
        (fw.keys / "known_hosts").write_text(f"{name} ssh-ed25519 AAAAC3NzaFAKE\n")
        generation = load_pointer("systemd", seeds_dir=config.seeds_dir()).snapshot
        fw._write_config(
            {
                "root": str(fw.root),
                "work": str(fw.work),
                "mounted": mounted,
                "smartctl": smartctl,
                "nproc": nproc,
                "cpu_model": ZEN3_MODEL,
                "cpu_flags": list(cpu_flags),
                "df_avail": WORK_AVAIL,
                "df_size": WORK_SIZE,
                "root_avail": ROOT_AVAIL,
                "rules": [],
                "generation": generation,
                "job": {"rc": 0, "lines": ["fake-shidashi: working"], "hold_s": 0.3},
                "host_locks_dir": str(fw.host_cache / "locks"),
                "real_repo": str(fw.real_repo),
                "fake_repo": str(fw.repo),
                "real_git": shutil.which("git") or "/usr/bin/git",
                "real_rsync": shutil.which("rsync") or "/usr/bin/rsync",
                "worker_path": "",
                "factory_fork_points": _factory_fork_points(
                    generation, str(config.variants_dir()), str(config.seeds_dir())
                ),
            }
        )
        fw._write_worker_files(cpu_flags)
        if mounted:
            (fw.work / ".shidashi").mkdir(exist_ok=True)
        fw._write_shims()
        fw._make_repo()
        fw._make_venv()
        monkeypatch.setenv("PATH", f"{fw.base / 'host-bin'}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_WORKER_STATE", str(fw.state))
        monkeypatch.setenv("SHIDASHI_CACHE", str(fw.host_cache))
        monkeypatch.setenv("SHIDASHI_SCRATCH", str(base / "scratch"))
        monkeypatch.setenv("XDG_DATA_HOME", str(base / "xdg"))
        # The host's virtual environment, as the push sees it: a small one.
        monkeypatch.setattr(sys, "prefix", str(fw.venv))
        monkeypatch.setattr(sys, "exec_prefix", str(fw.venv))
        fw._watchdog = threading.Timer(watchdog_s, fw._fire_watchdog)
        fw._watchdog.daemon = True
        fw._watchdog.start()
        return fw

    def _write_config(self, cfg: dict[str, Any]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.state / "config.json.tmp"
        tmp.write_text(json.dumps(cfg, indent=1))
        tmp.replace(self.state / "config.json")

    def config(self) -> dict[str, Any]:
        return json.loads((self.state / "config.json").read_text())  # type: ignore[no-any-return]

    def set(self, **changes: Any) -> None:
        """Change the fake's behaviour (``mounted``, ``smartctl``, ``nproc``, ...)."""
        cfg = self.config()
        cfg.update(changes)
        self._write_config(cfg)
        if "mounted" in changes and changes["mounted"]:
            (self.work / ".shidashi").mkdir(parents=True, exist_ok=True)
        elif "mounted" in changes:
            # unmounted, /mnt/work is the empty directory on the RAM root: the disk's
            # .shidashi marker is no longer visible there
            shutil.rmtree(self.work / ".shidashi", ignore_errors=True)
        if "cpu_flags" in changes:
            self._write_worker_files(tuple(changes["cpu_flags"]))

    def set_job(self, **changes: Any) -> None:
        """Change what the fake ``shidashi`` does when a job runs it."""
        cfg = self.config()
        cfg["job"].update(changes)
        self._write_config(cfg)

    def fail(
        self,
        pattern: str | None = None,
        *,
        host: str | None = None,
        code: int = 1,
        stderr: str = "",
        times: int | None = None,
        hang: float | None = None,
        stall: float | None = None,
    ) -> None:
        """Make the ssh connections whose remote command matches ``pattern`` (a
        regex) -- and/or that aim at ``host`` -- exit ``code`` with ``stderr``,
        at most ``times`` times; ``hang`` waits first (bounded by ConnectTimeout, like
        a host that does not answer); ``stall`` waits regardless (a host that accepts
        the connection and then says nothing)."""
        cfg = self.config()
        cfg["rules"].append(
            {
                "pattern": pattern,
                "host": host,
                "code": code,
                "stderr": stderr,
                "times": times,
                "hang": hang,
                "stall": stall,
                "id": len(cfg["rules"]),
            }
        )
        self._write_config(cfg)

    def unreachable(self, host: str | None = None) -> None:
        target = host or self.name
        self.fail(
            host=target,
            code=255,
            stderr=f"ssh: connect to host {target} port 22: Connection timed out\r\n",
        )

    def host_key_changed(self) -> None:
        self.fail(
            host=self.name,
            code=255,
            stderr=(
                "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
                "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
                "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
                "The fingerprint for the ED25519 key sent by the remote host is\n"
                "SHA256:ImpostorImpostorImpostorImpostorImpostor00.\n"
                "Host key verification failed.\n"
            ),
        )

    def activate_unit(self, unit: str, state: str = "active") -> None:
        """Mark a unit active on the worker (no process behind it); ``state`` is the
        ActiveState ``systemctl show`` reports (``activating``, ``deactivating``)."""
        marker = self.state / "units" / f"{_unit_name(unit)}.active"
        marker.write_text("0" if state == "active" else state)

    def deactivate_unit(self, unit: str) -> None:
        (self.state / "units" / f"{_unit_name(unit)}.active").unlink(missing_ok=True)

    def release_job(self) -> None:
        """Let a fake job that holds (``hold_until_release``) finish."""
        (self.state / "release").write_text("go")

    # --- the worker's disk --------------------------------------------------------------

    def put(self, rel: str, data: bytes | str = b"x") -> Path:
        """Create a file on the worker's /mnt/work (``rel`` is relative to it)."""
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    # --- what happened ------------------------------------------------------------------

    def calls(self, kind: str | None = None) -> list[dict[str, Any]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        out = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [c for c in out if kind is None or c["kind"] == kind]

    def ssh_commands(self) -> list[str]:
        return [c["command"] for c in self.calls("ssh")]

    def rsync_calls(self) -> list[dict[str, Any]]:
        return self.calls("rsync")

    def job_invocations(self) -> list[dict[str, Any]]:
        path = self.state / "job-invocations.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def powered_off(self) -> bool:
        return bool(self.calls("poweroff"))

    def assert_pinned(self) -> None:
        """Every connection used the pinned key of this worker and reached its address."""
        sshs = [c for c in self.calls("ssh") if c["alias"] in (self.name, None)]
        assert sshs, "no ssh connection was made"
        for c in sshs:
            opts = [o.replace(" ", "") for o in c["options"]]
            lowered = [o.lower() for o in opts]
            assert "stricthostkeychecking=no" not in lowered, c["argv"]
            assert f"hostkeyalias={self.name}".lower() in lowered, c["argv"]
            assert any(o.startswith("userknownhostsfile=") for o in lowered), c["argv"]

    def wait_for(self, predicate: Any, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    # --- the host's Shidashi checkout, as git sees it -----------------------------------

    def _git(self, *args: str) -> str:
        cfg = self.config()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        done = subprocess.run(
            [cfg["real_git"], "-C", str(self.repo), *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def _make_repo(self) -> None:
        files = {
            "pyproject.toml": '[project]\nname = "shidashi"\nversion = "0.0.0"\n',
            "shidashi/__init__.py": '"""fake checkout"""\n',
            "shidashi/cli.py": "# committed\n",
            "README.md": "# committed readme\n",
            ".gitignore": ".venv\n.env\n",
        }
        for rel, text in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "fake checkout")
        self.head = self._git("rev-parse", "HEAD")
        # untracked and ignored: must never be shipped nor make the tree "dirty"
        (self.repo / ".env").write_text("SHIDASHI_TOKEN=not-a-real-secret\n")
        (self.repo / ".venv").symlink_to(self.venv)

    def dirty(self) -> list[str]:
        """Edit two tracked files without committing; return their paths."""
        (self.repo / "shidashi" / "cli.py").write_text("# edited, not committed\n")
        (self.repo / "README.md").write_text("# edited readme\n")
        return ["shidashi/cli.py", "README.md"]

    def commit_recipes(self) -> str:
        """Commit this checkout's ``variants/`` and ``seeds/`` to the fake checkout; its HEAD.

        A sync resolves the fork-point keys at a commit (``worker.keys_at``): that
        commit must exist in the fake checkout and carry both trees.
        """
        from shidashi import config

        for name, src in (("variants", config.variants_dir()), ("seeds", config.seeds_dir())):
            shutil.copytree(src, self.repo / name)
        self._git("add", "-A")
        self._git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "the recipes")
        self.head = self._git("rev-parse", "HEAD")
        return self.head

    def _make_venv(self) -> None:
        site = self.venv / "lib" / "python3.14" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        (self.venv / "bin").mkdir(exist_ok=True)
        (self.venv / "bin" / "python").symlink_to(PYTHON)
        (self.venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.14\n")
        (site / "fakepkg").mkdir(exist_ok=True)
        (site / "fakepkg" / "__init__.py").write_text("VALUE = 1\n")
        (site / "_virtualenv.pth").write_text("import _virtualenv\n")
        # the editable install of Shidashi, as uv/hatchling write it, and the
        # setuptools spelling: both point at the HOST checkout
        (site / "_editable_impl_shidashi.pth").write_text(f"{self.real_repo}\n")
        (site / "__editable__.shidashi-0.1.1.pth").write_text(f"{self.real_repo}\n")

    # --- the "remote" side, through story 009's contract --------------------------------

    def remote(self) -> Any:
        from shidashi import remote

        return remote.Remote(
            name=self.name,
            address=self.address,
            key=self.keys / "id_ed25519",
            known_hosts=self.keys / "known_hosts",
            expected_fingerprint="SHA256:FakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFak",  # C3
        )

    def entry(
        self,
        *,
        name: str | None = None,
        address: str | None = None,
        cpu_flags: tuple[str, ...] | None = None,
    ) -> Any:
        """The worker's registry entry (story 009's ``WorkerEntry``)."""
        from shidashi.workers import WorkerEntry

        return WorkerEntry(
            name=name or self.name,
            address=address or self.address,
            host_key="ssh-ed25519 AAAAC3NzaFAKE",
            host_key_fingerprint="SHA256:FakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFak",
            paired_at="2026-10-05T10:00:00Z",
            cpu_flags=tuple(ZEN3_CPUINFO_FLAGS if cpu_flags is None else cpu_flags),
            image=BUILD_ID,
        )

    def register(self, *entries: Any) -> None:
        """Write the host's worker registry (story 009) with ``entries``."""
        from shidashi import config, workers

        wdir = config.workers_dir()
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "id_ed25519").write_text("not a real key\n")
        (wdir / "id_ed25519").chmod(0o600)
        (wdir / "known_hosts").write_text(f"{self.name} ssh-ed25519 AAAAC3NzaFAKE\n")
        chosen = entries or (self.entry(),)
        workers.save_registry(wdir / "workers.json", {e.name: e for e in chosen})

    # --- teardown -----------------------------------------------------------------------

    def _fire_watchdog(self) -> None:
        self.watchdog_fired = True
        self._kill_all()

    def _kill_all(self) -> None:
        pids = self.state / "pids"
        if not pids.exists():
            return
        for line in pids.read_text().split():
            with contextlib.suppress(ValueError, ProcessLookupError, PermissionError):
                os.killpg(int(line), signal.SIGKILL)
            with contextlib.suppress(ValueError, ProcessLookupError, PermissionError):
                os.kill(int(line), signal.SIGKILL)

    def close(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        self.release_job()
        self._kill_all()
        assert not self.watchdog_fired, "the fake worker's watchdog had to stop a hung call"

    # --- files of the worker's "/" and the shims ----------------------------------------

    def _write_worker_files(self, cpu_flags: tuple[str, ...]) -> None:
        proc = self.root / "proc"
        proc.mkdir(parents=True, exist_ok=True)
        nproc = self.config()["nproc"]
        blocks = []
        for i in range(nproc):
            blocks.append(
                f"processor\t: {i}\nvendor_id\t: AuthenticAMD\ncpu family\t: 25\n"
                f"model name\t: {ZEN3_MODEL}\nflags\t\t: {' '.join(cpu_flags)}\n"
            )
        (proc / "cpuinfo").write_text("\n".join(blocks) + "\n")
        (proc / "meminfo").write_text(
            f"MemTotal:       {MEM_TOTAL_KB} kB\nMemFree:        20000000 kB\n"
            f"MemAvailable:   {MEM_AVAILABLE_KB} kB\nBuffers:          100000 kB\n"
        )
        (proc / "loadavg").write_text(f"{LOAD1:.2f} 2.10 1.05 2/1234 5678\n")
        etc = self.root / "etc"
        etc.mkdir(exist_ok=True)
        (etc / "os-release").write_text(
            'NAME=Bentoo\nID=bentoo\nID_LIKE=gentoo\nPRETTY_NAME="Bentoo Linux"\n'
            f'BUILD_ID="{BUILD_ID}"\nIMAGE_ID=bentoo-worker-systemd-v3\n'
        )
        usrlib = self.root / "usr" / "lib"
        usrlib.mkdir(parents=True, exist_ok=True)
        (usrlib / "os-release").write_text('NAME=Gentoo\nID=gentoo\nPRETTY_NAME="Gentoo Linux"\n')

    def _write_shims(self) -> None:
        host_bin = self.base / "host-bin"
        worker_bin = self.base / "worker-bin"
        smart_bin = self.base / "worker-smart-bin"
        for d in (host_bin, worker_bin, smart_bin):
            d.mkdir(parents=True, exist_ok=True)
        for name in ("ssh", "rsync", "git"):
            _shim(host_bin / name, name)
        for name in _WORKER_SHIMS:
            _shim(worker_bin / name, name)
        _shim(smart_bin / "smartctl", "smartctl")
        sys_bin = _shared_sys_bin(os.environ.get("PATH", ""))
        cfg = self.config()
        cfg["worker_bin"] = str(worker_bin)
        cfg["smart_bin"] = str(smart_bin)
        cfg["sys_bin"] = str(sys_bin)
        self._write_config(cfg)


#: One farm of the host's binaries per PATH, shared by every fake worker of this
#: process: a farm is thousands of symlinks, and one per test exhausts a tmpfs's inodes.
_SYS_BINS: dict[str, Path] = {}


def _shared_sys_bin(path_env: str) -> Path:
    """The worker's ordinary commands: every binary on ``path_env`` except the denied
    ones and the project's venv, as symlinks in a directory removed at exit."""
    if path_env in _SYS_BINS:
        return _SYS_BINS[path_env]
    import atexit
    import tempfile

    sys_bin = Path(tempfile.mkdtemp(prefix="fake-worker-sysbin-"))
    atexit.register(shutil.rmtree, sys_bin, ignore_errors=True)
    seen: set[str] = set()
    for entry in path_env.split(os.pathsep):
        src = Path(entry)
        if not entry or "/.venv/" in f"{entry}/" or not src.is_dir():
            continue
        with contextlib.suppress(OSError):
            for item in src.iterdir():
                if item.name in _DENY or item.name in seen:
                    continue
                seen.add(item.name)
                with contextlib.suppress(OSError):
                    (sys_bin / item.name).symlink_to(item)
    _SYS_BINS[path_env] = sys_bin
    return sys_bin


def _shim(path: Path, name: str) -> None:
    path.write_text(f'#!/bin/sh\nexec "{PYTHON}" -IBS "{HELPER}" {name} "$@"\n')
    path.chmod(0o755)


def _real_repo() -> Path:
    import shidashi

    return Path(shidashi.__file__).resolve().parent.parent


def _unit_name(unit: str) -> str:
    return unit[: -len(".service")] if unit.endswith(".service") else unit


# --- small helpers for assertions ---------------------------------------------------------


def mentions_size(text: str, nbytes: int) -> bool:
    """Whether ``text`` shows ``nbytes`` in any usual rendering: bytes, kB, MiB, GiB
    (0-2 decimals), GB (1-2 decimals), with or without thousands separators."""
    candidates = {
        str(nbytes),
        f"{nbytes:,}",
        str(nbytes // 1024),
        str(nbytes // 1024**2),
    }
    for unit, base in (("G", 1024**3), ("G", 1000**3), ("T", 1024**4), ("T", 1000**4)):
        value = nbytes / base
        if value < 1:
            continue
        for decimals in (0, 1, 2):
            rendered = f"{value:.{decimals}f}"
            candidates.update({f"{rendered} {unit}", f"{rendered}{unit}"})
    return any(c in text for c in candidates)


def option_values(argv: list[str], *names: str) -> list[str]:
    """Values of ``--name=value`` / ``--name value`` / ``-n value`` options in argv."""
    values: list[str] = []
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name and i + 1 < len(argv):
                values.append(argv[i + 1])
            elif name.startswith("--") and arg.startswith(name + "="):
                values.append(arg[len(name) + 1 :])
    return values


#: A build key and a pins id no checkout resolves: the fork points of an orphan.
OTHER_BUILD_KEY = "b00000000"
OTHER_PINS = "p20260101.00000000"


def fork_point_names(
    gen: str,
    *,
    arch: str = "v3",
    init: str = "systemd",
    flavor: str = "gnome",
    stage: str = "desktop",
) -> dict[str, str]:
    """File names of the fork points this checkout restores at ``gen``: ``stage``'s fork
    point and the phase snapshot of ``flavor`` (both keyed by ``flavor``'s recipe) and the
    bootstrap checkpoint (keyed by the base), each ``-<pins>-<build key>-<x>.tar``.

    Composed by the real path functions under this checkout's ``variants/`` and
    ``seeds/`` (what :meth:`FakeWorker.commit_recipes` commits to the fake checkout).
    """
    from shidashi import config, factory, phases
    from shidashi.tree import load_pin_id

    pins = load_pin_id(config.seeds_dir())
    recipe = config.load_recipe(arch, flavor, init)
    base = config.load_recipe(arch, "base", init, any_stage=True)
    here = Path()
    stage_fp = phases.stage_fork_point_path(
        recipe, stage, snapshot=gen, pins=pins, fork_points_dir=here
    )
    snapshot = phases.phase_snapshot_path(
        recipe, snapshot=gen, pins=pins, phase=flavor, fork_points_dir=here
    )
    bootstrap = factory.bootstrap_fork_point_path(
        base, snapshot=gen, pins=pins, fork_points_dir=here
    )
    return {"stage": stage_fp.name, "phase_snapshot": snapshot.name, "bootstrap": bootstrap.name}


def orphan_fork_point_names(
    gen: str, *, arch: str = "v3", init: str = "systemd", flavor: str = "gnome"
) -> dict[str, str]:
    """The three orphan kinds of ``arch``'s desktop fork point at ``gen``, no longer
    restorable under this checkout: no build key (the naming before 2026-10-08),
    another build key, and other pins."""
    from shidashi import config, phases
    from shidashi.tree import load_pin_id

    pins = load_pin_id(config.seeds_dir())
    key = phases.build_key(config.load_recipe(arch, flavor, init))
    if pins == OTHER_PINS or key == OTHER_BUILD_KEY:
        raise RuntimeError(f"the orphan names collide with this checkout's: {pins}-{key}")
    stem = f"{arch}-{init}-{gen}"
    return {
        "orphan_no_key": f"{stem}-{pins}-desktop.tar",
        "orphan_other_key": f"{stem}-{pins}-{OTHER_BUILD_KEY}-desktop.tar",
        "orphan_other_pins": f"{stem}-{OTHER_PINS}-{key}-desktop.tar",
    }


@functools.cache
def _factory_fork_points(gen: str, variants: str, seeds: str) -> dict[str, str]:
    """``{"<arch>/<target>": name}``: the stage fork point a factory of that target
    leaves, under this checkout's keys (:func:`fork_point_names`, init systemd).

    Handed to the fake ``shidashi`` through its config (the shim imports nothing of
    shidashi), so a factory job leaves a fork point the pull can restore. ``variants``
    and ``seeds`` are only the cache key: the directories ``config`` reads.
    """
    del variants, seeds
    from shidashi import config

    return {
        f"{arch}/{target}": fork_point_names(gen, arch=arch, flavor=target, stage=target)["stage"]
        for arch in config.available_names("arch")
        for target in config.factory_names()
    }


def seed_host_cache(fw: FakeWorker, *, orphans: bool = False) -> dict[str, Path]:
    """The host cache a push reads: arch v3 at this generation, plus what must stay home
    (another generation of v3, znver5's binpkgs and fork points).

    The v3 fork points carry this checkout's keys (:func:`fork_point_names`): the
    stage fork point (``fork_point``), a phase snapshot and the bootstrap checkpoint.
    With ``orphans``, the three kinds of :func:`orphan_fork_point_names` wait beside them.
    """
    from shidashi import config
    from shidashi.seed import load_pointer

    gen = fw.config()["generation"]
    pointer = load_pointer("systemd", seeds_dir=config.seeds_dir())
    c = fw.host_cache
    forks = c / "fork-points"
    current = fork_point_names(gen)
    files = {
        "binpkg": c / "binpkgs" / "v3" / gen / "app-misc" / "a-1.gpkg.tar",
        "index": c / "binpkgs" / "v3" / gen / "Packages",
        "old_gen": c / "binpkgs" / "v3" / "20250101T000000Z" / "old-1.gpkg.tar",
        "other_arch": c / "binpkgs" / "znver5" / gen / "z-1.gpkg.tar",
        "distfile": c / "distfiles" / "big-1.0.tar.gz",
        "ccache": c / "ccache" / "0" / "entry",
        "sccache": c / "sccache" / "entry",
        "tree": c / "trees" / "gentoo-20260823.tar.xz",
        "repo": c / "repos" / "bentoo" / "profiles" / "repo_name",
        "stage3": c / pointer.filename,
        "fork_point": forks / current["stage"],
        "phase_snapshot": forks / current["phase_snapshot"],
        "bootstrap": forks / current["bootstrap"],
        "other_fork_point": forks / fork_point_names(gen, arch="znver5")["stage"],
    }
    if orphans:
        files.update({key: forks / name for key, name in orphan_fork_point_names(gen).items()})
    for key, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if key == "distfile":
            path.write_bytes(os.urandom(2 * 1024 * 1024))
        elif key == "fork_point":
            path.write_bytes(os.urandom(1024 * 1024))
        elif key == "index":
            path.write_text("# the host's OLD index\nCPV: host-only-entry\n")
        else:
            path.write_text(f"{key}\n")
    return files


# ======================================================================================
# The shims (run as ``python -IBS _fake_worker.py NAME ARGS...``)
# ======================================================================================


def _state() -> Path:
    return Path(os.environ["FAKE_WORKER_STATE"])


def _cfg() -> dict[str, Any]:
    return json.loads((_state() / "config.json").read_text())  # type: ignore[no-any-return]


def _log(kind: str, **fields: Any) -> None:
    record = {"kind": kind, "t": time.time(), "pid": os.getpid(), **fields}
    fd = os.open(_state() / "calls.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (json.dumps(record) + "\n").encode())
    finally:
        os.close(fd)


def _remember_pid(pid: int) -> None:
    fd = os.open(_state() / "pids", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, f"{pid}\n".encode())
    finally:
        os.close(fd)


def _map(command: str, root: str) -> str:
    alternatives = "|".join(re.escape(p) for p in _MAPPED)
    return re.sub(rf"(?<![\w/.-])({alternatives})", lambda m: root + m.group(1), command)


def _worker_path(cfg: dict[str, Any]) -> str:
    parts = [cfg["worker_bin"]]
    if cfg.get("smartctl"):
        parts.append(cfg["smart_bin"])
    parts.append(cfg["sys_bin"])
    return os.pathsep.join(parts)


def _worker_env(cfg: dict[str, Any]) -> dict[str, str]:
    return {
        "PATH": _worker_path(cfg),
        "HOME": str(Path(cfg["root"]) / "root"),
        "USER": "root",
        "LOGNAME": "root",
        "SHELL": "/bin/bash",
        "LANG": "C.UTF-8",
        "FAKE_WORKER_STATE": str(_state()),
    }


def _rule_hit(rule: dict[str, Any]) -> bool:
    """Count a hit for ``rule``; False once it has fired ``times`` times."""
    path = _state() / "rule-hits.json"
    with open(_state() / "rule-hits.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        hits = json.loads(path.read_text()) if path.exists() else {}
        n = hits.get(str(rule["id"]), 0)
        if rule.get("times") is not None and n >= rule["times"]:
            return False
        hits[str(rule["id"])] = n + 1
        path.write_text(json.dumps(hits))
        return True


_SSH_WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")


def _ssh(argv: list[str]) -> int:
    cfg = _cfg()
    opts: list[tuple[str, str | None]] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                letter = a[j]
                if letter in _SSH_WITH_ARG:
                    value = a[j + 1 :]
                    if not value:
                        i += 1
                        value = argv[i] if i < len(argv) else ""
                    opts.append((letter, value))
                    break
                opts.append((letter, None))
                j += 1
            i += 1
            continue
        break
    dest = argv[i] if i < len(argv) else None
    command = " ".join(argv[i + 1 :])
    options = [v for k, v in opts if k == "o" and v is not None]
    lowered = {o.split("=", 1)[0].strip().lower(): o.split("=", 1)[-1].strip() for o in options}
    host = dest.split("@")[-1] if dest else None
    alias = lowered.get("hostkeyalias")
    target = lowered.get("hostname") or host
    connect_timeout = lowered.get("connecttimeout")
    _log(
        "ssh",
        argv=argv,
        dest=dest,
        host=host,
        alias=alias,
        target=target,
        options=options,
        identity=[v for k, v in opts if k == "i"],
        login=[v for k, v in opts if k == "l"],
        connect_timeout=connect_timeout,
        command=command,
    )
    for rule in cfg["rules"]:
        if rule.get("host") and rule["host"] not in (host, alias, target):
            continue
        if rule.get("pattern") and not re.search(rule["pattern"], command, re.S):
            continue
        if not _rule_hit(rule):
            continue
        if rule.get("hang"):
            limit = float(connect_timeout) if connect_timeout else float(rule["hang"])
            time.sleep(min(float(rule["hang"]), limit))
        if rule.get("stall"):
            time.sleep(float(rule["stall"]))
        sys.stderr.write(rule.get("stderr") or "")
        sys.stderr.flush()
        return int(rule["code"])
    if not command.strip():
        return 0
    mapped = _map(command, cfg["root"])
    env = _worker_env(cfg)
    if "rsync --server" in mapped:  # binary protocol on stdout: no rewriting
        os.execve("/bin/bash", ["bash", "-c", mapped], env)
    root = cfg["root"].encode()
    child = subprocess.Popen(
        ["/bin/bash", "-c", mapped],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _remember_pid(os.getpid())
    _remember_pid(child.pid)

    def _stop(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _stop)

    def _copy(src: Any, dst: Any) -> None:
        for line in iter(src.readline, b""):
            with contextlib.suppress(BrokenPipeError):
                dst.write(line.replace(root, b""))
                dst.flush()

    threads = [
        threading.Thread(target=_copy, args=(child.stdout, sys.stdout.buffer), daemon=True),
        threading.Thread(target=_copy, args=(child.stderr, sys.stderr.buffer), daemon=True),
    ]
    for t in threads:
        t.start()
    code = child.wait()
    for t in threads:
        t.join(timeout=5)
    return code


def _rsync(argv: list[str]) -> int:
    cfg = _cfg()
    locks = Path(cfg["host_locks_dir"])
    held = sorted(p.name for p in locks.glob("*.owner.json")) if locks.is_dir() else []
    # the worker side is the one written ``host:path``: a push when it is the destination
    direction = "push" if argv and re.match(r"[^/]*:", argv[-1]) else "pull"
    _log("rsync", argv=argv, cwd=os.getcwd(), locks=held, direction=direction)
    os.execv(cfg["real_rsync"], ["rsync", *argv])
    return 127  # pragma: no cover


def _git_shim(argv: list[str]) -> int:
    cfg = _cfg()
    real: str = cfg["real_repo"]
    fake: str = cfg["fake_repo"]
    real_resolved = os.path.realpath(real)

    def redirect(arg: str) -> str:
        for prefix in {real, real_resolved}:
            if arg == prefix or arg.startswith(prefix + "/"):
                return fake + arg[len(prefix) :]
            for opt in ("--git-dir=", "--work-tree="):
                if arg.startswith(opt + prefix):
                    return opt + fake + arg[len(opt) + len(prefix) :]
        return arg

    args = [redirect(a) for a in argv]
    cwd = os.path.realpath(os.getcwd())
    if cwd == real_resolved or cwd.startswith(real_resolved + "/"):
        inside = Path(fake + cwd[len(real_resolved) :])
        os.chdir(inside if inside.is_dir() else fake)
    _log("git", argv=argv, redirected=args)
    os.execv(cfg["real_git"], ["git", *args])
    return 127  # pragma: no cover


def _systemctl(argv: list[str]) -> int:
    units = _state() / "units"
    with_value = {
        "-p",
        "--property",
        "-t",
        "--type",
        "--state",
        "-H",
        "--host",
        "-M",
        "--machine",
        "-n",
        "--lines",
        "-o",
        "--output",
    }
    pos: list[str] = []
    props: list[str] = []
    value_only = quiet = False
    it = iter(argv)
    for a in it:
        if a in with_value:
            v = next(it, "")
            if a in ("-p", "--property"):
                props.append(v)
            continue
        if a.startswith("--property="):
            props.append(a.split("=", 1)[1])
            continue
        if a == "--value":
            value_only = True
            continue
        if a in ("-q", "--quiet"):
            quiet = True
            continue
        if a.startswith("-"):
            continue
        pos.append(a)
    verb, names = (pos[0], pos[1:]) if pos else ("list-units", [])
    active = {p.name[: -len(".active")] for p in units.glob("*.active")}
    _log("systemctl", argv=argv, verb=verb, names=names)
    if verb == "list-units":
        for u in sorted(active):
            if not names or any(
                fnmatch.fnmatchcase(u, _unit_name(p)) or fnmatch.fnmatchcase(f"{u}.service", p)
                for p in names
            ):
                print(f"{u}.service loaded active running {u}")
        return 0
    if verb in ("is-active", "status"):
        rc = 0
        for n in names:
            on = _unit_name(n) in active
            if not quiet:
                print(
                    ("active" if on else "inactive")
                    if verb == "is-active"
                    else f"* {n}\n   Active: {'active (running)' if on else 'inactive (dead)'}"
                )
            rc = rc or (0 if on else 3)
        return rc if names else 3
    if verb == "show":
        for n in names:
            on = _unit_name(n) in active
            state = "inactive"
            if on:
                written = (units / f"{_unit_name(n)}.active").read_text().strip()
                state = written if written in ("activating", "deactivating") else "active"
            values = {
                "ActiveState": state,
                "SubState": "running" if on else "dead",
                "Result": "success",
            }
            for p in props or ["ActiveState"]:
                v = values.get(p, "")
                print(v if value_only else f"{p}={v}")
        return 0
    if verb in ("poweroff", "halt", "reboot", "kexec"):
        _log("poweroff", argv=argv, via="systemctl")
        return 0
    if verb in ("stop", "kill"):
        for n in names:
            marker = units / f"{_unit_name(n)}.active"
            with contextlib.suppress(ValueError, OSError):
                pid = int(marker.read_text() or "0")
                if pid > 0:
                    os.killpg(pid, signal.SIGKILL)
            marker.unlink(missing_ok=True)
        return 0
    return 0


_RUN_WITH_VALUE = {
    "--unit",
    "-u",
    "--setenv",
    "-E",
    "--working-directory",
    "-p",
    "--property",
    "--description",
    "--slice",
    "--uid",
    "--gid",
    "--nice",
    "--service-type",
    "-M",
    "--machine",
    "-H",
    "--host",
    "--on-active",
}


def _systemd_run(argv: list[str]) -> int:
    cfg = _cfg()
    unit: str | None = None
    env: dict[str, str] = {}
    wd: str | None = None
    flags: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-"):
            break
        if a.startswith("--") and "=" in a:
            key, value = a.split("=", 1)
            i += 1
        elif a in _RUN_WITH_VALUE:
            key, value = a, argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            flags.append(a)
            i += 1
            continue
        if key in ("--unit", "-u"):
            unit = value
        elif key in ("--setenv", "-E"):
            k, _, v = value.partition("=")
            env[k] = v
        elif key == "--working-directory":
            wd = value
    cmd = argv[i:]
    name = _unit_name(unit or f"run-fake{os.getpid()}")
    _log("systemd-run", argv=argv, unit=name, env=env, wd=wd, cmd=cmd, flags=flags)
    marker = _state() / "units" / f"{name}.active"
    if marker.exists():
        sys.stderr.write(
            f"Failed to start transient service unit: Unit {name}.service was already "
            "loaded or has a fragment file.\n"
        )
        return 1
    unit_env = {**_worker_env(cfg), **env}
    # systemd expands the command line as it executes it: ${VAR}, a whole-word $VAR
    # and $$ (-> $)
    cmd = cmd[:1] + systemd_expand(cmd[1:], unit_env)
    fake: list[str] = []
    for c in cmd:
        if re.search(r"/runtime/venv/bin/(python[0-9.]*|shidashi)$", c):
            fake += [PYTHON, "-IBS", str(HELPER), "fake-shidashi"]
            if c.endswith("/shidashi"):
                fake.append("--console-script")
        else:
            fake.append(c)
    spec = {"cmd": fake, "env": unit_env, "wd": wd}
    (_state() / "units" / f"{name}.spec.json").write_text(json.dumps(spec))
    (_state() / "units" / f"{name}.result").unlink(missing_ok=True)
    sync = any(f in flags for f in ("--wait", "-P", "--pipe", "--pty", "-t"))
    if sync:
        return _unit(name)
    p = subprocess.Popen(
        [PYTHON, "-IBS", str(HELPER), "__unit__", name],
        env={"FAKE_WORKER_STATE": str(_state()), "PATH": os.environ.get("PATH", "")},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _remember_pid(p.pid)
    result = _state() / "units" / f"{name}.result"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (marker.exists() or result.exists()):
        time.sleep(0.02)
    sys.stderr.write(f"Running as unit: {name}.service\n")
    return 0


def systemd_expand(words: list[str], env: dict[str, str]) -> list[str]:
    """What systemd makes of a unit's command-line words when it runs them: a word
    that is exactly ``$VAR`` becomes the variable's value split on whitespace (no
    word when unset), and inside any word ``${VAR}`` becomes its value (empty when
    unset) and ``$$`` a single ``$``."""
    out: list[str] = []
    for word in words:
        whole = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", word)
        if whole:
            out += env.get(whole.group(1), "").split()
            continue
        out.append(
            re.sub(
                r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                lambda m: "$" if m.group(0) == "$$" else env.get(m.group(1), ""),
                word,
            )
        )
    return out


def _unit(name: str) -> int:
    units = _state() / "units"
    spec = json.loads((units / f"{name}.spec.json").read_text())
    marker = units / f"{name}.active"
    marker.write_text(str(os.getpid()))
    try:
        wd = spec["wd"] or str(Path(_cfg()["root"]))
        if not Path(wd).is_dir():
            rc = 200  # systemd's EXIT_CHDIR
        else:
            with open(units / f"{name}.journal", "ab") as journal:
                rc = subprocess.call(
                    spec["cmd"],
                    env=spec["env"],
                    cwd=wd,
                    stdin=subprocess.DEVNULL,
                    stdout=journal,
                    stderr=subprocess.STDOUT,
                )
        (units / f"{name}.result").write_text(str(rc))
        return rc
    finally:
        marker.unlink(missing_ok=True)


def _parse_findmnt(argv: list[str]) -> tuple[str | None, str | None, bool, bool, bool]:
    cols: str | None = None
    target: str | None = None
    raw = noheadings = contains = False
    value_letters = set("oMTStOFNdwx")
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key, eq, value = a.partition("=")
            if key in ("--output", "--mountpoint", "--target", "--source", "--types", "--options"):
                if not eq:
                    i += 1
                    value = argv[i] if i < len(argv) else ""
                if key == "--output":
                    cols = value
                elif key in ("--mountpoint", "--target"):
                    target, contains = value, key == "--target"
            elif key == "--bytes":
                raw = True
            elif key == "--noheadings":
                noheadings = True
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in value_letters:
                    value = a[j + 1 :]
                    if not value:
                        i += 1
                        value = argv[i] if i < len(argv) else ""
                    if c == "o":
                        cols = value
                    elif c in "MT":
                        target, contains = value, c == "T"
                    break
                if c == "b":
                    raw = True
                elif c == "n":
                    noheadings = True
                j += 1
        else:
            target = a
        i += 1
    return cols, target, raw, noheadings, contains


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return str(n)  # pragma: no cover


def _findmnt(argv: list[str]) -> int:
    cfg = _cfg()
    work = cfg["work"]
    cols, target, raw, noheadings, contains = _parse_findmnt(argv)
    _log("findmnt", argv=argv)
    if not cfg["mounted"]:
        return 1
    if target is not None:
        norm = os.path.normpath(target)
        if not (norm == work or (contains and norm.startswith(work + "/"))):
            return 1
    names = [c.strip().upper() for c in (cols or "TARGET,SOURCE,FSTYPE,OPTIONS").split(",")]
    size, avail = int(cfg["df_size"]), int(cfg["df_avail"])
    values = {
        "TARGET": work,
        "SOURCE": "/dev/sda1",
        "FSTYPE": "btrfs",
        "OPTIONS": "rw,noatime",
        "LABEL": "SHIDASHI-WORK",
        "PARTLABEL": "SHIDASHI-WORK",
        "UUID": "0000-fake",
        "SIZE": str(size) if raw else _human(size),
        "AVAIL": str(avail) if raw else _human(avail),
        "USED": str(size - avail) if raw else _human(size - avail),
        "USE%": "50%",
    }
    if not noheadings:
        print(" ".join(names))
    print(" ".join(values.get(n, "-") for n in names))
    return 0


def _mountpoint(argv: list[str]) -> int:
    cfg = _cfg()
    paths = [a for a in argv if not a.startswith("-")]
    quiet = "-q" in argv or "--quiet" in argv
    _log("mountpoint", argv=argv)
    ok = bool(paths) and cfg["mounted"] and os.path.normpath(paths[-1]) == cfg["work"]
    if not quiet:
        print(f"{paths[-1] if paths else ''} is {'' if ok else 'not '}a mountpoint")
    return 0 if ok else 32


def _df(argv: list[str]) -> int:
    cfg = _cfg()
    work = cfg["work"]
    fields: list[str] | None = None
    bs: int | str = 1024
    paths: list[str] = []
    i = 0

    def _size(text: str) -> int:
        m = re.fullmatch(r"(\d*)([KMGT]?)(i?B?)", text.upper())
        if not m:
            return 1
        mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2)]
        return int(m.group(1) or "1") * mult

    while i < len(argv):
        a = argv[i]
        if a.startswith("--output"):
            fields = (
                a.split("=", 1)[1].split(",")
                if "=" in a
                else ["source", "fstype", "size", "used", "avail", "pcent", "target"]
            )
        elif a.startswith("--block-size="):
            bs = _size(a.split("=", 1)[1])
        elif a == "-B":
            i += 1
            bs = _size(argv[i])
        elif a.startswith("-B"):
            bs = _size(a[2:])
        elif a in ("-h", "--human-readable", "-H", "--si"):
            bs = "human"
        elif a == "-k":
            bs = 1024
        elif not a.startswith("-"):
            paths.append(a)
        i += 1
    _log("df", argv=argv)
    rows = []
    rc = 0
    for p in paths or [work]:
        norm = os.path.normpath(p)
        if not os.path.exists(norm):
            sys.stderr.write(f"df: {p}: No such file or directory\n")
            rc = 1
            continue
        if cfg["mounted"] and (norm == work or norm.startswith(work + "/")):
            src, fstype, size, avail, tgt = (
                "/dev/sda1",
                "btrfs",
                cfg["df_size"],
                cfg["df_avail"],
                work,
            )
        else:  # the live root, in RAM
            src, fstype, size, avail, tgt = "tmpfs", "tmpfs", 16 * GIB, cfg["root_avail"], "/"
        rows.append((src, fstype, int(size), int(avail), tgt))

    def fmt(n: int) -> str:
        return _human(n) if bs == "human" else str(-(-n // int(bs)))

    heads = {
        "source": "Filesystem",
        "fstype": "Type",
        "size": "Size",
        "used": "Used",
        "avail": "Avail",
        "pcent": "Use%",
        "target": "Mounted on",
        "file": "File",
    }
    cols = fields or ["source", "size", "used", "avail", "pcent", "target"]
    print(" ".join(heads.get(c, c) for c in cols))
    for src, fstype, size, avail, tgt in rows:
        vals = {
            "source": src,
            "fstype": fstype,
            "size": fmt(size),
            "used": fmt(size - avail),
            "avail": fmt(avail),
            "pcent": "50%",
            "target": tgt,
            "file": tgt,
        }
        print(" ".join(vals.get(c, "-") for c in cols))
    return rc


def _emaint(argv: list[str]) -> int:
    pkgdir = os.environ.get("PKGDIR")
    _log("emaint", argv=argv, pkgdir=pkgdir)
    if "binhost" not in argv:
        return 0
    if not pkgdir:
        sys.stderr.write("emaint: PKGDIR is not set (would index /var/cache/binpkgs)\n")
        return 1
    root = Path(pkgdir)
    if not root.is_dir():
        sys.stderr.write(f"emaint: {pkgdir}: no such PKGDIR\n")
        return 1
    found = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.name.endswith((".gpkg.tar", ".tbz2", ".xpak"))
    )
    body = f"# regenerated by the fake emaint\nPACKAGES: {len(found)}\n\n"
    body += "".join(f"CPV: {rel}\n\n" for rel in found)
    (root / "Packages").write_text(body)
    print(f"binhost: {len(found)} packages indexed")
    return 0


def _poweroff(name: str, argv: list[str]) -> int:
    _log("poweroff", argv=argv, via=name)
    return 0


def _smartctl(argv: list[str]) -> int:
    verdict = _cfg().get("smartctl") or "PASSED"
    _log("smartctl", argv=argv)
    print("smartctl 7.4 2023-08-01 r5530 [x86_64-linux] (fake)\n")
    if verdict == "UNDETECTED":  # a device-mapper disk: a usage error, exit bit 0
        print(f"{argv[-1]}: Unable to detect device type")
        print("Please specify device type with the -d option.\n")
        print("Use smartctl -h to get a usage summary\n")
        return 1
    print("=== START OF READ SMART DATA SECTION ===")
    print(f"SMART overall-health self-assessment test result: {verdict}")
    return 0 if verdict == "PASSED" else 8


def _simple(name: str, argv: list[str]) -> int:
    cfg = _cfg()
    _log(name, argv=argv)
    if name == "nproc":
        print(cfg["nproc"])
    elif name == "lscpu":
        print(f"Architecture: x86_64\nCPU(s): {cfg['nproc']}\nModel name: {cfg['cpu_model']}")
        print(f"Flags: {' '.join(cfg['cpu_flags'])}")
    elif name == "free":
        total, avail = MEM_TOTAL_KB * 1024, MEM_AVAILABLE_KB * 1024
        div = 1 if "-b" in argv else 1024**2 if "-m" in argv else 1024**3 if "-g" in argv else 1024
        print("               total        used        free      shared  buff/cache   available")
        print(f"Mem: {total // div} {(total - avail) // div} {avail // div} 0 0 {avail // div}")
    elif name == "lsblk":
        print("sda" if "PKNAME" in " ".join(argv).upper() else "sda1")
    return 0


def _inside(path: str | None, root: str) -> bool:
    return bool(path) and os.path.realpath(str(path)).startswith(os.path.realpath(root) + "/")


def _fake_shidashi(argv: list[str]) -> int:
    """What the worker's ``shidashi`` does in these tests: record, print, produce."""
    cfg = _cfg()
    job = cfg["job"]
    root = cfg["root"]
    args = list(argv)
    code = None
    if args[:1] == ["--console-script"]:
        args = args[1:]
    elif args[:1] in (["-c"], ["-m"]):
        code, args = args[1], args[2:]
    env = {
        k: os.environ.get(k)
        for k in ("SHIDASHI_CACHE", "SHIDASHI_SCRATCH", "SHIDASHI_RUNS", "PYTHONPATH")
    }
    locks = Path(cfg["host_locks_dir"])
    host_locks = {}
    if locks.is_dir():
        for p in locks.glob("*.owner.json"):
            with contextlib.suppress(OSError, ValueError):
                host_locks[p.name] = json.loads(p.read_text())
    pythonpath = env.get("PYTHONPATH") or ""
    shipped_cli = Path(pythonpath) / "shidashi" / "cli.py"
    record: dict[str, Any] = {
        "args": args,
        "code": code,
        "cwd": os.getcwd(),
        "env": env,
        "pid": os.getpid(),
        "host_locks": host_locks,
        "shipped_cli": shipped_cli.read_text() if shipped_cli.is_file() else None,
        "during": [],
    }
    for spec in job.get("during", []):
        done = subprocess.run(
            spec["argv"],
            env=spec["env"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=spec.get("cwd"),
        )
        record["during"].append(
            {"argv": spec["argv"], "rc": done.returncode, "output": done.stdout + done.stderr}
        )
    with open(_state() / "job-invocations.jsonl", "a") as out:
        out.write(json.dumps(record) + "\n")
    print(f"fake-shidashi: {' '.join(args)}", flush=True)
    for line in job.get("lines", []):
        print(line, flush=True)
    if job.get("hold_until_release"):
        deadline = time.monotonic() + 90
        while not (_state() / "release").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    time.sleep(float(job.get("hold_s", 0)))
    writes = [env.get("SHIDASHI_CACHE"), env.get("SHIDASHI_RUNS")]
    out_dir = None
    for i, a in enumerate(args):
        if a in ("--output-dir", "-o") and i + 1 < len(args):
            out_dir = args[i + 1]
        elif a.startswith("--output-dir="):
            out_dir = a.split("=", 1)[1]
    command = args[0] if args else ""
    # positionals as the real CLI reads them: the --output-dir a job adds right after
    # the command is an option, not the arch
    rest = args[1:]
    if rest[:1] in (["--output-dir"], ["-o"]):
        rest = rest[2:]
    elif rest[:1] and rest[0].startswith("--output-dir="):
        rest = rest[1:]
    if command in ("factory", "assemble", "build") and rest:
        if not all(_inside(w, root) for w in writes) or (out_dir and not _inside(out_dir, root)):
            print(
                f"fake-shidashi: refusing to write outside the worker: {writes} {out_dir}",
                file=sys.stderr,
                flush=True,
            )
            return 97
        arch, gen = rest[0], cfg["generation"]
        cache = Path(str(env["SHIDASHI_CACHE"]))
        target = rest[1] if len(rest) > 1 and not rest[1].startswith("-") else "minimal"
        writes_pkgdir = command == "factory" or (
            command == "build" and "--skip-factory" not in args
        )
        if writes_pkgdir:  # the factory: binpkgs, a fork point, sources, compiler cache
            pk = cache / "binpkgs" / arch / gen / "app-misc"
            pk.mkdir(parents=True, exist_ok=True)
            (pk / "built-by-job-1.gpkg.tar").write_bytes(b"binpkg built by the job\n")
            fp = cache / "fork-points"
            fp.mkdir(parents=True, exist_ok=True)
            # the current key's name (install computed it), else the pre-key one
            known: dict[str, str] = cfg.get("factory_fork_points", {})
            name = known.get(f"{arch}/{target}", f"{arch}-{target}-systemd-{gen}.tar")
            (fp / name).write_bytes(b"fork point by the job\n")
            (cache / "distfiles").mkdir(parents=True, exist_ok=True)
            (cache / "distfiles" / "fetched-by-job.tar.gz").write_bytes(b"distfile\n")
            (cache / "ccache").mkdir(parents=True, exist_ok=True)
            (cache / "ccache" / "entry-by-job").write_bytes(b"ccache\n")
        run_dir = Path(str(env["SHIDASHI_RUNS"])) / "20261005T120000Z-f00d01"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events.jsonl").write_text(
            json.dumps({"kind": "run.start", "fake": True}) + "\n"
        )
        if command in ("assemble", "build"):  # assembles only READ the binhost
            iso_dir = Path(out_dir) if out_dir else Path(os.getcwd())
            iso_dir.mkdir(parents=True, exist_ok=True)
            (iso_dir / f"bentoo-fake-{target}-{arch}.iso").write_bytes(b"ISO\n")
            (iso_dir / "SHA256SUMS").write_text(f"0000  bentoo-fake-{target}-{arch}.iso\n")
    return int(job.get("rc", 0))


def main(name: str, argv: list[str]) -> int:
    if name == "ssh":
        return _ssh(argv)
    if name == "rsync":
        return _rsync(argv)
    if name == "git":
        return _git_shim(argv)
    if name == "systemctl":
        return _systemctl(argv)
    if name == "systemd-run":
        return _systemd_run(argv)
    if name == "__unit__":
        return _unit(argv[0])
    if name == "findmnt":
        return _findmnt(argv)
    if name == "mountpoint":
        return _mountpoint(argv)
    if name == "df":
        return _df(argv)
    if name == "emaint":
        return _emaint(argv)
    if name in ("poweroff", "shutdown", "halt"):
        return _poweroff(name, argv)
    if name == "smartctl":
        return _smartctl(argv)
    if name == "fake-shidashi":
        return _fake_shidashi(argv)
    return _simple(name, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
