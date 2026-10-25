"""Stand-ins for the builder VM, its guest and the actions runner (story 032, tasks 7.1, 7.3).

- :class:`FakeActionsRunner` stands in for actions/runner's ``actions-runner-linux-x64``
  tarball: its ``run.sh`` records each start (argv, environment, directory, the ``.env``
  beside it, ``/run/shidashi/jitconfig`` as it then was) and exits with the status the test
  chose; its ``config.sh`` records that it ran -- a JIT runner never runs it.
- :class:`FakeBuilderGuest` stands in for the guest: a command runs as root (uid 0 in a user
  namespace, ``bwrap``) in a root of its own where ``/run``, ``/mnt``, ``/work``, ``/root``,
  ``/opt`` and ``/srv`` are directories of the test, ``/mnt/out`` is the host's ``out/``
  share and ``/mnt/lab`` the cache share; the rest is the host's. ``systemd-run`` and
  ``systemctl`` are fakes that keep the units' state between commands (a unit runs its
  command at once and is then reported ``active`` for a few queries before its result);
  ``curl``/``wget`` serve the runner tarball; ``mount``, ``findmnt`` and friends succeed.
- :class:`FakeBuilderHost` stands in for the build host around ``lab/vm/builder.sh``: a copy
  of the checkout's ``lab/`` whose ``.venv/bin/python`` -- the interpreter every ``shidashi``
  of builder.sh runs -- is a recorder: ``vm start``/``vm stop`` flip the VM's state, ``vm
  run`` runs its command in the guest with the stdin it was given, ``ci jit-config`` prints
  the JIT config minted for the test, ``ci dispatch`` and ``release prune`` succeed.

Secrets (the token, the JIT config) are minted per test, never written in the source.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import shidashi
from tests._fake_github_api import mint_jit_config, mint_token

CHECKOUT = Path(shidashi.__file__).resolve().parents[1]
STANDARD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
#: The top-level directories that are the guest's own, not the host's.
GUEST_DIRS = ("run", "mnt", "work", "root", "opt", "srv")
JITCONFIG = "/run/shidashi/jitconfig"
SANDBOX_REASON = (
    "needs bwrap with unprivileged user namespaces to stand in for the builder guest "
    "(root, its own /run, /mnt and /work); a CI runner without them skips"
)


def guest_sandbox_works() -> bool:
    if shutil.which("bwrap") is None:
        return False
    try:
        done = subprocess.run(
            ["bwrap", "--unshare-user", "--uid", "0", "--bind", "/", "/", "true"],
            capture_output=True,
            timeout=30,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def copy_checkout(dest: Path) -> Path:
    """A checkout holding the real ``lab/`` (builder.sh finds the repo from its own path)."""
    shutil.copytree(CHECKOUT / "lab", dest / "lab", ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def pin_runner(checkout: Path, *, version: str, sha256: str) -> Path:
    """``lab/vm/runner.env`` pinned to ``version`` and ``sha256``, its other lines kept."""
    env = checkout / "lab" / "vm" / "runner.env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
    wanted = {"RUNNER_VERSION": version, "RUNNER_SHA256": sha256}
    kept = [
        line
        for line in lines
        if line.removeprefix("export ").split("=", 1)[0].strip() not in wanted
    ]
    env.write_text(
        "\n".join([*kept, *(f"{k}={v}" for k, v in wanted.items())]) + "\n", encoding="utf-8"
    )
    return env


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _python_script(path: Path, body: str) -> Path:
    return _script(path, f"#!{sys.executable}\n{body}")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- the actions runner -----------------------------------------------------------------------

_RECORDER = """
import json, os, sys
records, what, here, *argv = sys.argv[1:]
jit = {"exists": os.path.exists(%(jit)r)}
if jit["exists"]:
    jit["mode"] = os.stat(%(jit)r).st_mode & 0o7777
    with open(%(jit)r, encoding="utf-8", errors="replace") as handle:
        jit["content"] = handle.read()
dotenv = os.path.join(here, ".env")
entry = {
    "argv": argv, "dir": here, "cwd": os.getcwd(), "env": dict(os.environ), "uid": os.geteuid(),
    "dotenv": open(dotenv, encoding="utf-8").read() if os.path.isfile(dotenv) else None,
    "jitconfig": jit,
}
with open(os.path.join(records, what + ".jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps(entry) + "\\n")
status = os.path.join(records, "exit-status")
sys.exit(int(open(status).read()) if os.path.isfile(status) else 0)
"""


class FakeActionsRunner:
    """Stands in for actions/runner's linux-x64 tarball (see the module docstring)."""

    def __init__(self, root: Path, *, version: str = "2.999.1") -> None:
        self.version = version
        self.records = root / "runner-records"
        self.records.mkdir(parents=True, exist_ok=True)
        recorder = _python_script(self.records / "recorder.py", _RECORDER % {"jit": JITCONFIG})
        self.tarball = root / f"actions-runner-linux-x64-{version}.tar.gz"
        files = {
            "run.sh": f'#!/bin/bash\nhere=$(cd "$(dirname "$0")" && pwd)\n'
            f'exec {sys.executable} {recorder} {self.records} run "$here" "$@"\n',
            "config.sh": f'#!/bin/bash\nhere=$(cd "$(dirname "$0")" && pwd)\n'
            f'exec {sys.executable} {recorder} {self.records} config "$here" "$@"\n',
            "env.sh": "#!/bin/bash\n",
            "bin/Runner.Listener": "#!/bin/bash\nexit 0\n",
        }
        with tarfile.open(self.tarball, "w:gz") as archive:
            for name, text in files.items():
                data = text.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.uid, info.gid = len(data), 0o755, 0, 0
                info.uname = info.gname = "root"
                archive.addfile(info, io.BytesIO(data))
        self.sha256 = hashlib.sha256(self.tarball.read_bytes()).hexdigest()

    def exit_with(self, status: int) -> None:
        (self.records / "exit-status").write_text(str(status), encoding="utf-8")

    def runs(self) -> list[dict[str, Any]]:
        return _jsonl(self.records / "run.jsonl")

    def config_calls(self) -> list[dict[str, Any]]:
        return _jsonl(self.records / "config.jsonl")


# --- the guest --------------------------------------------------------------------------------

_ENTER = """
import json, os, subprocess, sys
config = json.load(open(%(config)r))
argv = ["bwrap", "--unshare-user", "--uid", "0", "--gid", "0", "--die-with-parent",
        "--tmpfs", "/"]
for entry in sorted(os.listdir("/")):
    if entry in config["own"] or entry in ("proc", "dev"):
        continue
    path = "/" + entry
    if os.path.islink(path):
        argv += ["--symlink", os.readlink(path), path]
    elif os.path.isdir(path):
        argv += ["--bind", path, path]
argv += ["--proc", "/proc", "--dev", "/dev"]
for entry in config["own"]:
    argv += ["--bind", os.path.join(config["root"], entry), "/" + entry]
argv += ["--bind", config["out"], "/mnt/out", "--bind", config["cache"], "/mnt/lab"]
argv += ["--setenv", "PATH", config["path"], "--setenv", "HOME", "/root", "--chdir", "/"]
argv += ["bash", "-c", sys.argv[1]]
sys.exit(subprocess.run(argv, stdin=sys.stdin).returncode)
"""

_DOWNLOAD = """
import json, os, shutil, sys
config = json.load(open(%(config)r))
tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
url = next((a for a in args if a.startswith(("http://", "https://"))), "")
dest, directory, i = None, None, 0
while i < len(args):
    a = args[i]
    if tool == "curl" and a in ("-o", "--output"):
        dest = args[i + 1]; i += 1
    elif tool == "curl" and a.startswith("--output="):
        dest = a.split("=", 1)[1]
    elif tool == "curl" and a == "--output-dir":
        directory = args[i + 1]; i += 1
    elif tool == "curl" and a in ("-O", "--remote-name"):
        dest = os.path.basename(url)
    elif tool == "wget" and a.startswith("--output-document="):
        dest = a.split("=", 1)[1]
    elif tool == "wget" and a == "-P":
        directory = args[i + 1]; i += 1
    elif tool == "wget" and a.startswith("-") and not a.startswith("--") and a.endswith("O"):
        dest = args[i + 1]; i += 1
    i += 1
if tool == "wget" and dest is None:
    dest = os.path.basename(url)
if directory and dest not in (None, "-") and not os.path.isabs(dest):
    dest = os.path.join(directory, dest)
with open(os.path.join(config["state"], "downloads.jsonl"), "a") as log:
    log.write(json.dumps({"tool": tool, "args": args, "url": url, "dest": dest}) + "\\n")
with open(config["tarball"], "rb") as source:
    if dest in (None, "-"):
        shutil.copyfileobj(source, sys.stdout.buffer)
    else:
        with open(dest, "wb") as target:
            shutil.copyfileobj(source, target)
"""

_SYSTEMD_RUN = """
import json, os, subprocess, sys
config = json.load(open(%(config)r))
state = config["state"]
settings = json.load(open(os.path.join(state, "settings.json")))
args, i = sys.argv[1:], 0
unit, env, cwd, wait = None, {}, None, False
VALUED = {"-u", "--unit", "-E", "--setenv", "-p", "--property", "--description", "--slice",
          "--uid", "--gid", "--nice", "--working-directory", "--service-type", "-M",
          "--machine", "--on-active", "--on-calendar", "--timer-property", "--path-property"}
while i < len(args) and args[i].startswith("-"):
    a = args[i]
    name, eq, value = a.partition("=")
    if not eq and name in VALUED:
        value = args[i + 1]; i += 1
    if name in ("-u", "--unit"):
        unit = value
    elif name in ("-E", "--setenv"):
        key, _, val = value.partition("=")
        env[key] = val if _ else os.environ.get(key, "")
    elif name == "--working-directory":
        cwd = value
    elif name in ("--wait", "-W"):
        wait = True
    i += 1
command = args[i:]
unit = (unit or "run-u1").removesuffix(".service")
jit = {"exists": os.path.exists(%(jit)r)}
if jit["exists"]:
    jit["mode"] = os.stat(%(jit)r).st_mode & 0o7777
    jit["content"] = open(%(jit)r, encoding="utf-8", errors="replace").read()
with open(os.path.join(state, "systemd-run.jsonl"), "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "unit": unit, "command": command,
                          "setenv": env, "jitconfig": jit}) + "\\n")
record = os.path.join(state, "units", unit + ".json")
os.makedirs(os.path.dirname(record), exist_ok=True)
if settings.get("never_exits"):
    json.dump({"status": None, "polls": 0}, open(record, "w"))
    sys.exit(0)
done = subprocess.run(command, env={"PATH": config["path"], **env}, cwd=cwd or "/")
json.dump({"status": done.returncode, "polls": 0}, open(record, "w"))
sys.exit(done.returncode if wait else 0)
"""

_SYSTEMCTL = """
import json, os, sys
config = json.load(open(%(config)r))
state = config["state"]
settings = json.load(open(os.path.join(state, "settings.json")))
args = sys.argv[1:]
quiet = "--quiet" in args or "-q" in args
value_only = "--value" in args
props, words, i = [], [], 0
while i < len(args):
    a = args[i]
    if a in ("-p", "--property"):
        props += args[i + 1].split(","); i += 1
    elif a.startswith("--property="):
        props += a.split("=", 1)[1].split(",")
    elif not a.startswith("-"):
        words.append(a)
    i += 1
verb, units = (words[0] if words else ""), [w.removesuffix(".service") for w in words[1:]]
with open(os.path.join(state, "systemctl.jsonl"), "a") as log:
    log.write(json.dumps({"argv": args}) + "\\n")

def state_of(unit):
    record = os.path.join(state, "units", unit + ".json")
    if not os.path.isfile(record):
        return {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
                "Result": "success", "ExecMainStatus": "0", "ExecMainCode": "0"}
    data = json.load(open(record))
    data["polls"] += 1
    json.dump(data, open(record, "w"))
    if data["status"] is None or data["polls"] <= settings.get("active_polls", 2):
        return {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
                "Result": "success", "ExecMainStatus": "0", "ExecMainCode": "0"}
    ok = data["status"] == 0
    return {"LoadState": "loaded", "ActiveState": "inactive" if ok else "failed",
            "SubState": "dead" if ok else "failed", "Result": "success" if ok else "exit-code",
            "ExecMainStatus": str(data["status"]), "ExecMainCode": "1"}

if verb in ("is-active", "is-failed", "show", "status"):
    states = [state_of(u) for u in units] or [state_of("none")]
    first = states[0]
    if verb == "is-active":
        if not quiet:
            print("\\n".join(s["ActiveState"] for s in states))
        sys.exit(0 if any(s["ActiveState"] == "active" for s in states) else 3)
    if verb == "is-failed":
        if not quiet:
            print("\\n".join(s["ActiveState"] for s in states))
        sys.exit(0 if any(s["ActiveState"] == "failed" for s in states) else 1)
    if verb == "show":
        for p in props or list(first):
            print(first.get(p, "") if value_only else f"{p}={first.get(p, '')}")
        sys.exit(0)
    print(f"Active: {first['ActiveState']} ({first['SubState']})")
    sys.exit(0 if first["ActiveState"] == "active" else 3)
if verb == "list-units":
    for name in sorted(os.listdir(os.path.join(state, "units"))) if os.path.isdir(
        os.path.join(state, "units")) else []:
        print(name.removesuffix(".json") + ".service loaded active running")
sys.exit(0)
"""

_SUCCEED = "#!/bin/bash\nexit 0\n"
_SHORT_SLEEP = "#!/bin/bash\nexec /bin/sleep 0.05\n"


class FakeBuilderGuest:
    """Stands in for the builder VM's guest (see the module docstring)."""

    def __init__(self, root: Path, *, out: Path, cache: Path, runner: FakeActionsRunner) -> None:
        self.root = root
        self.runner = runner
        self.bin = root / "bin"
        self.state = root / "state"
        for name in (*GUEST_DIRS, "state/units"):
            (root / name).mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        path = f"{self.bin}:{STANDARD_PATH}"
        config = {
            "own": list(GUEST_DIRS),
            "root": str(root),
            "out": str(out),
            "cache": str(cache),
            "path": path,
            "state": str(self.state),
            "tarball": str(runner.tarball),
        }
        config_file = root / "config.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")
        self.settings(active_polls=2, never_exits=False)
        values = {"config": str(config_file), "jit": JITCONFIG}
        self.enter = _python_script(root / "enter", _ENTER % values)
        for tool in ("curl", "wget"):
            _python_script(self.bin / tool, _DOWNLOAD % values)
        _python_script(self.bin / "systemd-run", _SYSTEMD_RUN % values)
        _python_script(self.bin / "systemctl", _SYSTEMCTL % values)
        for tool in ("mount", "umount", "findmnt", "blkid", "mkfs.btrfs", "modprobe"):
            _script(self.bin / tool, _SUCCEED)
        _script(self.bin / "sleep", _SHORT_SLEEP)

    def settings(self, *, active_polls: int, never_exits: bool) -> None:
        (self.state / "settings.json").write_text(
            json.dumps({"active_polls": active_polls, "never_exits": never_exits}),
            encoding="utf-8",
        )

    def provision_jitconfig(self, content: str) -> Path:
        """``/run/shidashi/jitconfig`` as the host's weekly leaves it: 0600, in the guest."""
        path = self.root / JITCONFIG.removeprefix("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return path

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float = 120
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.enter), command],
            input=stdin.decode("utf-8"),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def downloads(self) -> list[dict[str, Any]]:
        return _jsonl(self.state / "downloads.jsonl")

    def units_started(self) -> list[dict[str, Any]]:
        return _jsonl(self.state / "systemd-run.jsonl")

    def unit_results(self) -> dict[str, int | None]:
        """Each unit started through ``systemd-run``, and the status its command ended with
        (``None``: still running)."""
        found: dict[str, int | None] = {}
        for record in sorted((self.state / "units").glob("*.json")):
            found[record.stem] = json.loads(record.read_text(encoding="utf-8"))["status"]
        return found

    def files_under(self, *dirs: str) -> list[Path]:
        return [p for d in dirs for p in (self.root / d).rglob("*") if p.is_file()]


# --- the host ---------------------------------------------------------------------------------

_HOST_PYTHON = """
import json, os, subprocess, sys
config = json.load(open(%(config)r))
args = sys.argv[1:]
if len(args) >= 2 and args[0] == "-c":
    args = args[2:]
data = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
vm_state = os.path.join(config["state"], "vm-running")
rc, out = 0, ""
if args[:2] == ["vm", "start"]:
    open(vm_state, "w").close()
elif args[:2] == ["vm", "stop"]:
    if os.path.exists(vm_state):
        os.unlink(vm_state)
elif args[:2] == ["vm", "run"]:
    rest, i = args[2:], 0
    while i < len(rest) and rest[i] in ("--name", "--work-dir", "--timeout"):
        i += 2
    command = " ".join(rest[i:])
    if not os.path.exists(vm_state):
        print("error: the builder VM is not running", file=sys.stderr)
        rc = 1
    else:
        rc = subprocess.run([config["enter"], command], input=data).returncode
elif args[:2] == ["ci", "jit-config"]:
    out = config["jit"] + "\\n"
with open(config["log"], "a", encoding="utf-8") as log:
    entry = {"args": args, "stdin": data.decode("utf-8", "replace"), "rc": rc}
    log.write(json.dumps(entry) + "\\n")
sys.stdout.write(out)
sys.exit(rc)
"""


class FakeBuilderHost:
    """Stands in for the build host around ``lab/vm/builder.sh`` (see the module docstring)."""

    def __init__(self, tmp_path: Path) -> None:
        self.checkout = copy_checkout(tmp_path / "checkout")
        self.vm_dir = tmp_path / "vm"
        self.state = self.vm_dir / "builder"
        self.out = self.state / "out"
        (self.out / "iso").mkdir(parents=True)
        (self.out / "iso" / "bentoo-2026.10.18-minimal-systemd-v3.iso").write_bytes(b"iso")
        self.cache = tmp_path / "cache"
        self.home = tmp_path / "home"
        self.token = mint_token()
        self.token_file = self.home / ".config" / "shidashi" / "github-token"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(self.token + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        self.jit = mint_jit_config()
        self.runner = FakeActionsRunner(tmp_path / "runner")
        pin_runner(self.checkout, version=self.runner.version, sha256=self.runner.sha256)
        self.guest = FakeBuilderGuest(
            tmp_path / "guest", out=self.out, cache=self.cache, runner=self.runner
        )
        self.host_bin = tmp_path / "host-bin"
        _script(self.host_bin / "sleep", _SHORT_SLEEP)
        _script(
            self.host_bin / "sudo",
            f'#!/bin/bash\necho "$*" >> {tmp_path / "sudo.log"}\nexit 1\n',
        )
        self.sudo_log = tmp_path / "sudo.log"
        self.log = tmp_path / "shidashi-calls.jsonl"
        config = {
            "state": str(tmp_path),
            "enter": str(self.guest.enter),
            "jit": self.jit,
            "log": str(self.log),
        }
        (tmp_path / "host-config.json").write_text(json.dumps(config), encoding="utf-8")
        _python_script(
            self.checkout / ".venv" / "bin" / "python",
            _HOST_PYTHON % {"config": str(tmp_path / "host-config.json")},
        )

    def builder(
        self, *args: str, env: dict[str, str] | None = None, timeout: float = 300
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.host_bin}:{os.environ.get('PATH', STANDARD_PATH)}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "SHIDASHI_VM_DIR": str(self.vm_dir),
            "SHIDASHI_CACHE": str(self.cache),
            **(env or {}),
        }
        return subprocess.run(
            ["bash", str(self.checkout / "lab" / "vm" / "builder.sh"), *args],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def calls(self) -> list[dict[str, Any]]:
        return _jsonl(self.log)
