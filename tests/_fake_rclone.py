"""``FakeRclone``: stands in for rclone and its remote in the story 033 tests.

A script on PATH (``bin/rclone`` under the test's ``tmp_path``) whose bucket is a directory.
It logs every call in order: its argv, the positional arguments, the config it was told to
use, ``HOME``, the env keys it got, and what ``releases/index.json`` said at that moment, so
a test can tell an index write from a purge that came before it. A missing object is what the
real rclone 1.75 says for ``cat`` on a missing file: ``directory not found``, exit 3 (checked
on the local backend, 2026-10-24); a missing directory to ``purge`` exits 1. Credentials in
its config are generated at run time, never literals.

``FakeRunuser`` (``FAKE_RUNUSER_BODY``) does not switch users: it logs its argv and runs what
follows ``--``, so a test sees the ``runuser -u <user>`` wrapping under a faked ``sudo``.
"""

import getpass
import importlib
import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shidashi.invoker import InvokingUser

#: The fake's body. ``STATE`` is baked in when it is written: the runner hands rclone only
#: ``RCLONE_CONFIG``, ``PATH`` and ``HOME``, so the fake cannot find its bucket through env.
FAKE_RCLONE_BODY = r"""
import json, os, shutil, sys
from pathlib import Path

STATE = Path(__STATE__)
BUCKETS = STATE / "buckets"
VALUE_FLAGS = {"--config", "--retries", "--timeout", "--contimeout", "--low-level-retries",
               "--transfers", "--log-level", "--log-file", "--s3-upload-cutoff",
               "--s3-chunk-size", "--multi-thread-streams", "--stats", "--bwlimit"}


def local(token):
    remote, _, rest = token.partition(":")
    return BUCKETS / remote / rest


def index_dates(token):
    remote, _, rest = token.partition(":")
    bucket = rest.split("/", 1)[0]
    index = BUCKETS / remote / bucket / "releases" / "index.json"
    try:
        return [e.get("date") for e in json.loads(index.read_bytes())["releases"]]
    except Exception:
        return None


argv = sys.argv[1:]
cmd = argv[0] if argv else ""
args, config, i = [], os.environ.get("RCLONE_CONFIG"), 1
while i < len(argv):
    word = argv[i]
    if word in VALUE_FLAGS:
        if word == "--config":
            config = argv[i + 1]
        i += 2
        continue
    if word.startswith("--config="):
        config = word.split("=", 1)[1]
    elif not word.startswith("-"):
        args.append(word)
    i += 1
remotes = [a for a in args if ":" in a and not a.startswith("/")]
entry = {"argv": argv, "cmd": cmd, "args": args, "config": config,
         "home": os.environ.get("HOME"), "env_keys": sorted(os.environ),
         "index_dates": index_dates(remotes[0]) if remotes else None}
with open(STATE / "log.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps(entry) + "\n")

fail = {}
if (STATE / "fail.json").exists():
    fail = json.loads((STATE / "fail.json").read_text())
if fail.get("cmd") == cmd and any(fail.get("match", "") in a for a in args):
    sys.stderr.write(fail.get("stderr", "ERROR : injected failure\n"))
    sys.exit(int(fail.get("exit", 1)))


def not_found():
    sys.stderr.write("2026/10/24 20:36:46 ERROR : error listing: directory not found\n")
    sys.stderr.write("2026/10/24 20:36:46 NOTICE: Failed to cat with 2 errors: "
                     "last error was: directory not found\n")
    sys.exit(3)


if cmd == "copyto" and len(args) == 2:
    src = local(args[0]) if args[0] in remotes else Path(args[0])
    dst = local(args[1])
    if not src.is_file():
        not_found()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
elif cmd == "cat" and len(args) == 1:
    path = local(args[0])
    if not path.is_file():
        not_found()
    sys.stdout.buffer.write(path.read_bytes())
elif cmd == "moveto" and len(args) == 2:
    src, dst = local(args[0]), local(args[1])
    if not src.is_file():
        not_found()
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
elif cmd == "purge" and len(args) == 1:
    path = local(args[0])
    if not path.is_dir():
        sys.stderr.write("ERROR : Failed to purge: directory not found\n")
        sys.exit(1)
    shutil.rmtree(path)
else:
    sys.stderr.write(f"FakeRclone: unknown command {argv!r}\n")
    sys.exit(2)
"""

#: A ``runuser`` that does not switch users: it logs its argv and runs what follows ``--``.
FAKE_RUNUSER_BODY = r"""
import json, os, sys
from pathlib import Path

STATE = Path(__STATE__)
argv = sys.argv[1:]
with open(STATE / "runuser.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\n")
rest = argv[argv.index("--") + 1 :]
os.execvp(rest[0], rest)
"""


def write_script(path: Path, body: str, state: Path) -> Path:
    path.write_text(
        f"#!{sys.executable}\n" + body.replace("__STATE__", repr(str(state))), encoding="utf-8"
    )
    path.chmod(0o755)
    return path


@dataclass
class FakeRclone:
    """Stands in for rclone and its remote: ``bin/rclone`` on PATH, a directory as the
    bucket, and an rclone config holding a generated access key."""

    bin_dir: Path
    state: Path
    config: Path
    access_key_id: str
    secret_access_key: str
    remote: str = "r2:bentoo"

    @property
    def bucket(self) -> Path:
        name, _, bucket = self.remote.partition(":")
        return self.state / "buckets" / name / bucket

    def obj(self, path: str) -> Path:
        return self.bucket / path

    def calls(self) -> list[dict[str, Any]]:
        log = self.state / "log.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def commands(self) -> list[str]:
        return [c["cmd"] for c in self.calls()]

    def runuser_calls(self) -> list[list[str]]:
        log = self.state / "runuser.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def fail(self, cmd: str, match: str, stderr: str | None = None, code: int = 1) -> None:
        """The next ``cmd`` whose arguments contain ``match`` fails with ``stderr``."""
        spec: dict[str, Any] = {"cmd": cmd, "match": match, "exit": code}
        if stderr is not None:
            spec["stderr"] = stderr
        (self.state / "fail.json").write_text(json.dumps(spec), encoding="utf-8")

    def leaky_stderr(self) -> str:
        """What rclone may print on a failure: the access key in every shape it takes."""
        kid, secret = self.access_key_id, self.secret_access_key
        return (
            f"2026/10/24 21:00:00 DEBUG : access_key_id = {kid}\n"
            f"2026/10/24 21:00:00 DEBUG : secret_access_key = {secret}\n"
            "2026/10/24 21:00:00 DEBUG : Authorization: AWS4-HMAC-SHA256 "
            f"Credential={kid}/20261024/auto/s3/aws4_request, "
            f"SignedHeaders=host;x-amz-date, Signature={secrets.token_hex(32)}\n"
            f"2026/10/24 21:00:01 ERROR : https://x.r2.cloudflarestorage.com/bentoo?"
            f"X-Amz-Credential={kid}%2F20261024%2Fauto%2Fs3%2Faws4_request: "
            f"403 Forbidden (secret_access_key={secret})\n"
        )

    def snapshot(self) -> dict[str, bytes]:
        root = self.state / "buckets"
        if not root.exists():
            return {}
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def install_fake_rclone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRclone:
    """``FakeRclone`` first on PATH, its config at ``$RCLONE_CONFIG`` (0600), no sudo."""
    base = tmp_path / "fake-rclone"
    bin_dir, state = base / "bin", base / "state"
    bin_dir.mkdir(parents=True)
    state.mkdir()
    write_script(bin_dir / "rclone", FAKE_RCLONE_BODY, state)
    kid, secret = secrets.token_hex(16), secrets.token_hex(32)
    config = base / "rclone.conf"
    config.write_text(
        "[r2]\ntype = s3\nprovider = Cloudflare\n"
        f"access_key_id = {kid}\nsecret_access_key = {secret}\n"
        "endpoint = https://x.r2.cloudflarestorage.com\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("RCLONE_CONFIG", str(config))
    monkeypatch.delenv("SUDO_USER", raising=False)
    return FakeRclone(bin_dir, state, config, kid, secret)


def as_sudo(monkeypatch: pytest.MonkeyPatch, home: Path) -> InvokingUser:
    """The process looks like ``sudo`` run by this user, with ``home`` as their home."""
    user = InvokingUser(
        name=getpass.getuser(),
        uid=os.getuid(),
        gid=os.getgid(),
        home=home,
        data_home=home / ".local" / "share",
    )
    for module_name in ("shidashi.invoker", "shidashi.gpgsign", "shidashi.release_upload"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, "invoking_user"):
            monkeypatch.setattr(module, "invoking_user", lambda: user)
    monkeypatch.setenv("SUDO_USER", user.name)
    return user
