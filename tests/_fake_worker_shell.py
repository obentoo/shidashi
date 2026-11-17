"""A stand-in for a build worker's shell, for the tests of the dev VM relay.

What the host's relay sends over ssh runs here, in a local shell: no network, no QEMU.
:meth:`FakeWorkerShell.install` puts two shims first on ``PATH``:

- ``ssh`` -- logs the destination and the command, answers 255 while the worker is
  :meth:`~FakeWorkerShell.unreachable`, then runs the command with ``/bin/sh`` in a
  session of its own (as sshd does) with ``/mnt/work`` rewritten into a temporary
  root. It ends like the real client: a signal sent to it never reaches the remote
  shell, which is what the relay's own stop has to do; and like sshd it reaps that
  shell when it ends, so a process group that ended is gone, not a zombie. The same
  shim is the "worker's" ssh to a guest (``root@vsock/<cid>``): that command runs as
  it is.
- ``rsync`` -- logs its arguments and copies the file, times kept, to where the
  destination names; :meth:`~FakeWorkerShell.rsync_fails` makes it exit 11.

On the worker side ``systemctl`` lists the units a test declared, ``findmnt`` knows
whether the work disk is "mounted" and ``nproc`` prints the worker's thread count.

The shipped Shidashi (``/mnt/work/runtime/venv/bin/python -c ... vm ...``) is one of:

- a ``FakeShippedShidashi`` (the default): it records how it was called (arguments,
  environment, directory) and plays ``vm start --dev`` (a session directory, a
  ``qemu.pid`` naming a live process called ``qemu-...``, the one JSON line),
  ``vm run`` (the command, run by a shell) and ``vm stop`` (process ended, directory
  removed). ``plan`` files change what one of them does;
- the real one (:meth:`~FakeWorkerShell.ship_real_shidashi`): this checkout, run by
  this interpreter from ``/mnt/work/src/<commit>`` -- for what only the worker's real
  ``vm run`` shows, a timeout and a stop that reaches the guest.

Run as a script it IS the fake shipped Shidashi: standard library only.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import pytest

    from shidashi.remote import Remote
    from shidashi.workers import WorkerEntry

_REPO = Path(__file__).resolve().parents[1]
#: The commit a test's "shipped" tree is under; ``worker.ship_head`` is stood in for.
SHIPPED = "5" * 40

_SSH = """#!/bin/sh
for last; do dest=$prev; prev=$last; done
printf '%s\\t%s\\0' "$dest" "$last" >> "$FAKE_WORKER_SHELL/ssh.log"
case "$dest" in
root@vsock/*) mapped=$last ;;
*)
  if [ -e "$FAKE_WORKER_SHELL/unreachable" ]; then
    echo "ssh: connect to host 192.0.2.10 port 22: No route to host" >&2
    exit 255
  fi
  # FakeSlotTool: the shipped tree here has no contrib/worker/slot, so a reservation
  # (`/bin/bash <tree>/contrib/worker/slot run --owner O --cpus C --memory M --wait W --`)
  # is logged and dropped, and its command runs as it would once granted. The real
  # script is tested on its own (tests/test_worker_slot_tool.py).
  w='[^ ]*'
  reserve="run --owner $w --cpus $w --memory $w --wait $w --"
  printf '%s\n' "$last" | grep -o "contrib/worker/slot $reserve" >> "$FAKE_WORKER_SHELL/slot.log"
  tree='/bin/bash /mnt/work/src/[0-9a-f]*/contrib/worker/slot'
  last=$(printf '%s' "$last" | sed "s#$tree $reserve ##g")
  mapped=$(printf '%s' "$last" | sed "s#/mnt/work#$FAKE_WORKER_SHELL/root/mnt/work#g")
  PATH="$FAKE_WORKER_SHELL/worker-bin:$PATH"
  export PATH
  HOME="$FAKE_WORKER_SHELL/root/root"
  XDG_STATE_HOME="$HOME/.local/state"
  XDG_DATA_HOME="$HOME/.local/share"
  export HOME XDG_STATE_HOME XDG_DATA_HOME
  # a worker's environment is its own: nothing of the host's Shidashi crosses ssh
  for name in $(env | sed -n 's/^\\(SHIDASHI_[A-Z_]*\\)=.*/\\1/p'); do unset "$name"; done
  ;;
esac
# the client waits for a shell that is a process of its own, as sshd's is: reaped when
# it ends, and left running when only the client is signalled. Its stdin goes through
# fd 3: POSIX gives a background job /dev/null as stdin BEFORE its own redirections, so
# `<&0` would hand it /dev/null -- dash, the CI's /bin/sh, does just that (2026-11-16)
exec 3<&0
setsid -w /bin/sh -c "$mapped" <&3 3<&- &
exec 3<&-
wait $!
"""

_RSYNC = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_WORKER_SHELL/rsync.log"
if [ -e "$FAKE_WORKER_SHELL/rsync-fails" ]; then
  echo "rsync: write failed: No space left on device (28)" >&2
  exit 11
fi
for last; do src=$prev; prev=$last; done
dest="$FAKE_WORKER_SHELL/root${last#*:}"
mkdir -p "$(dirname "$dest")" && cp -p "$src" "$dest" || exit 23
echo "sent $(stat -c %s "$src") bytes  received 35 bytes  1,000.00 bytes/sec"
"""

_WORKER_BIN = {
    "systemctl": '#!/bin/sh\ncat "$FAKE_WORKER_SHELL/units" 2>/dev/null\nexit 0\n',
    "findmnt": '#!/bin/sh\n[ -e "$FAKE_WORKER_SHELL/mounted" ] && echo /mnt/work\n',
    "nproc": '#!/bin/sh\ncat "$FAKE_WORKER_SHELL/nproc" 2>/dev/null\n',
}


class FakeWorkerShell:
    """One stand-in worker under ``base``; see the module docstring."""

    name = "bentoo-lab"
    address = "192.0.2.10"

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "root"
        self.work = self.root / "mnt" / "work"
        self.sessions = self.work / "scratch" / "vm"

    @classmethod
    def install(cls, base: Path, monkeypatch: pytest.MonkeyPatch) -> FakeWorkerShell:
        """A mounted, idle, 16-thread worker with a ``FakeShippedShidashi``; the shims
        first on ``PATH`` and the host's registry, state and audit trails under ``base``."""
        shell = cls(base / "worker-shell")
        for directory in (shell.base / "bin", shell.base / "worker-bin", shell.work):
            directory.mkdir(parents=True)
        for name, text in {"ssh": _SSH, "rsync": _RSYNC}.items():
            _script(shell.base / "bin" / name, text)
        for name, text in _WORKER_BIN.items():
            _script(shell.base / "worker-bin" / name, text)
        # the "QEMU" a fake start leaves running: any process whose name starts so
        os.symlink(shutil.which("sleep") or "/bin/sleep", shell.base / "qemu-system-x86_64")
        (shell.base / "mounted").touch()
        (shell.base / "nproc").write_text("16\n")
        python = shell.work / "runtime" / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        _script(python, f'#!/bin/sh\nexec {sys.executable} -IBS {Path(__file__).resolve()} "$@"\n')
        monkeypatch.setenv("FAKE_WORKER_SHELL", str(shell.base))
        monkeypatch.setenv("PATH", f"{shell.base / 'bin'}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("XDG_DATA_HOME", str(base / "xdg-data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(base / "xdg-state"))
        monkeypatch.setenv("SHIDASHI_RUNS", str(base / "runs"))
        monkeypatch.setenv("SHIDASHI_CACHE", str(base / "cache"))
        monkeypatch.setenv("SHIDASHI_SCRATCH", str(base / "scratch"))
        monkeypatch.setenv("SHIDASHI_DEV_ISO_DIR", str(base / "no-stash"))
        monkeypatch.delenv("SHIDASHI_DEV_WORKER", raising=False)
        shell.register()
        return shell

    # --- the host's side ----------------------------------------------------------------

    def entry(self) -> WorkerEntry:
        from shidashi.workers import WorkerEntry

        return WorkerEntry(
            name=self.name,
            address=self.address,
            host_key="ssh-ed25519 AAAAC3NzaFAKE",
            host_key_fingerprint="SHA256:FakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFak",
            paired_at="2026-11-15T10:00:00Z",
        )

    def register(self) -> None:
        """Pair the worker: the registry ``shidashi worker`` reads."""
        from shidashi import config, workers

        directory = config.workers_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "id_ed25519").write_text("not a real key\n")
        workers.save_registry(directory / "workers.json", {self.name: self.entry()})

    def remote(self) -> Remote:
        from shidashi.remote import Remote

        return Remote.for_worker(self.entry())

    # --- what the worker is like --------------------------------------------------------

    def unreachable(self, gone: bool = True) -> None:
        _flag(self.base / "unreachable", gone)

    def unmounted(self) -> None:
        _flag(self.base / "mounted", False)

    def rsync_fails(self) -> None:
        _flag(self.base / "rsync-fails", True)

    def threads(self, count: str) -> None:
        (self.base / "nproc").write_text(count)

    def running_job(self, *jobs: str) -> None:
        """Units ``systemctl list-units`` shows as running Shidashi jobs."""
        lines = [f"shidashi-job-{job}.service loaded active running a job\n" for job in jobs]
        (self.base / "units").write_text("".join(lines))

    def plan(self, command: str, **plan: Any) -> None:
        """What the fake shipped ``vm <command>`` does instead of succeeding:
        ``exit``, ``stderr``, ``stdout`` (in place of its own line), ``hold_s``."""
        (self.base / f"plan-{command}.json").write_text(json.dumps(plan))

    def ship_real_shidashi(self, commit: str = SHIPPED) -> None:
        """Make the shipped Shidashi this checkout, run by this interpreter."""
        python = self.work / "runtime" / "venv" / "bin" / "python"
        _script(python, f'#!/bin/sh\nexec {sys.executable} "$@"\n')
        tree = self.work / "src" / commit
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "shidashi").symlink_to(_REPO / "shidashi")

    def shipped_tree(self, commit: str = SHIPPED) -> Path:
        """The directory a shipped Shidashi is run from; made, as a ship would."""
        tree = self.work / "src" / commit
        tree.mkdir(parents=True, exist_ok=True)
        return tree

    def guest_session(self, name: str, *, cid: int = 61) -> Path:
        """A session the real ``vm run`` can load, as ``vm start`` leaves one."""
        directory = self.sessions / name
        directory.mkdir(parents=True, exist_ok=True)
        spec = {"iso": "/b.iso", "uefi": False, "cid": cid, "memory": "1G", "cpus": 1}
        (directory / "session.json").write_text(json.dumps({**spec, "dev": True}))
        return directory

    # --- what happened ------------------------------------------------------------------

    def ssh_calls(self) -> list[tuple[str, str]]:
        """``(destination, command)`` of every ssh, in order."""
        log = self.base / "ssh.log"
        if not log.exists():
            return []
        calls = [call.split("\t", 1) for call in log.read_text().split("\0") if call]
        return [(dest, command) for dest, command in calls]

    def worker_commands(self) -> list[str]:
        """The commands the host sent the worker (never the worker's own to a guest)."""
        return [command for dest, command in self.ssh_calls() if "vsock/" not in dest]

    def rsync_calls(self) -> list[str]:
        log = self.base / "rsync.log"
        return log.read_text().splitlines() if log.exists() else []

    def shidashi_calls(self) -> list[dict[str, Any]]:
        """How the fake shipped Shidashi was called: ``args``, ``env``, ``cwd``."""
        log = self.base / "shidashi.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def close(self) -> None:
        """End every "QEMU" a fake start left."""
        for pidfile in self.sessions.glob("*/qemu.pid"):
            _end(pidfile)


def _script(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _flag(path: Path, wanted: bool) -> None:
    if wanted:
        path.touch()
    else:
        path.unlink(missing_ok=True)


def _end(pidfile: Path) -> None:
    with contextlib.suppress(OSError, ValueError):
        os.kill(int(pidfile.read_text()), signal.SIGKILL)


# ======================================================================================
# The fake shipped Shidashi (this file run as a script)
# ======================================================================================


def _option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def _fake_shipped_shidashi(argv: list[str]) -> int:
    base = Path(os.environ["FAKE_WORKER_SHELL"])
    args = argv[2:] if argv[:1] == ["-c"] else argv
    env = {key: value for key, value in os.environ.items() if key.startswith("SHIDASHI_")}
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
    with (base / "shidashi.jsonl").open("a") as log:
        log.write(json.dumps({"args": args, "env": env, "cwd": os.getcwd()}) + "\n")
    command = " ".join(args[:2])
    plan_file = base / f"plan-{args[1] if len(args) > 1 else ''}.json"
    plan = json.loads(plan_file.read_text()) if plan_file.exists() else {}
    time.sleep(float(plan.get("hold_s", 0)))
    sys.stderr.write(plan.get("stderr", ""))
    if "stdout" in plan:
        sys.stdout.write(plan["stdout"])
    if plan.get("exit"):
        return int(plan["exit"])
    name = _option(args, "--name") or "bentoo"
    session = Path(os.environ["SHIDASHI_SCRATCH"]) / "vm" / name
    if command == "vm start":
        session.mkdir(parents=True, exist_ok=True)
        (session / "session.json").write_text(json.dumps({"dev": True, "cid": 43}))
        qemu = subprocess.Popen(
            [str(base / "qemu-system-x86_64"), "600"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        (session / "qemu.pid").write_text(f"{qemu.pid}\n")
        if "stdout" not in plan:
            line = {
                "name": name,
                "cid": 43,
                "iso": _option(args, "--iso"),
                "pin": "p20261101.28ad071a",
                "binhost": [],
                "staged": {"repo": "bentoo-staged", "path": "/var/db/repos/staged"},
                "ready": True,
                "expires": "2026-11-16T12:00:00Z",
            }
            print(json.dumps(line), flush=True)
        return 0
    if command == "vm run":
        return subprocess.run(["/bin/sh", "-c", args[args.index("--") + 1]], check=False).returncode
    if command == "vm stop":
        if not (session / "session.json").exists():
            sys.stderr.write(
                f"error: no VM session named {name!r}: run `shidashi vm start` first\n"
            )
            return 1
        _end(session / "qemu.pid")
        shutil.rmtree(session)
        return 0
    sys.stderr.write(f"fake shipped shidashi: no such command: {args}\n")
    return 64


if __name__ == "__main__":
    raise SystemExit(_fake_shipped_shidashi(sys.argv[1:]))
