"""``GuestSandbox``: the CI guest's rendered scripts run on this host, for the story 034
tests of ``shidashi.ci_guest_seed`` (tests/test_ci_guest_user_data.py, tests/test_ci_contract.py).

The prepare check, the marker step of ``runcmd:`` and the growroot unit run for real
(bash, ``/bin/sh``) with:

- ``dpkg-query``, ``docker``, ``systemctl``, ``growpart``, ``resize2fs``, ``poweroff`` (and
  ``reboot``, ``shutdown``, ``halt``) as fakes answering from what a test declares -- never
  this host's own: every other command is this host's, through a PATH of symlinks that
  leaves the faked ones out;
- ``systemctl is-enabled UNIT...`` keeping systemd's rule: exit 0 when AT LEAST ONE unit
  is enabled;
- the guest's paths -- ``/opt/actions-runner``, ``/dev/console``, the check itself, an
  absolute ``/usr/bin/<faked>`` -- moved under the sandbox's directory.

A step that would run ``rm``, ``apt-get``, ``cloud-init``, ``curl``... is refused before it
runs: a check and a print have no use for them, and this host must not get them.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tests._fake_ci_toolbox import PREPARED_MARKER
from tests._fake_worker import systemd_expand

CHECK = "/usr/local/libexec/shidashi-prepare-check"
GROWROOT = "/etc/systemd/system/shidashi-growroot.service"
RUNNER_DIR = "/opt/actions-runner"
UNITS = ("shidashi-runner.service", "shidashi-growroot.service")


def written_file(doc: dict[str, Any], path: str) -> str:
    for entry in doc.get("write_files") or []:
        if isinstance(entry, dict) and entry.get("path") == path:
            return str(entry.get("content", ""))
    raise AssertionError(f"user-data writes no {path}")


def unit_settings(unit: str) -> dict[str, list[str]]:
    settings: dict[str, list[str]] = {}
    for line in unit.splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith(("#", ";")):
            settings.setdefault(key.strip(), []).append(value.strip())
    return settings


def runcmd_lines(doc: dict[str, Any]) -> list[str]:
    """``runcmd:`` as cloud-init writes it into its one ``/bin/sh`` script: a string as it
    is, a list as shell-quoted words."""
    lines = []
    for entry in doc.get("runcmd") or []:
        lines.append(shlex.join(map(str, entry)) if isinstance(entry, list) else str(entry))
    return lines


#: Commands the guest's scripts are checked against: always fakes, never this host's.
_FAKED = (
    "dpkg-query",
    "dpkg",
    "docker",
    "systemctl",
    "growpart",
    "resize2fs",
    "poweroff",
    "reboot",
    "shutdown",
    "halt",
)
_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
#: What a step that only checks and prints has no business running here.
_DESTRUCTIVE = re.compile(r"(?<![\w-])(rm|chown|dd|mkfs\S*|apt-get|apt|cloud-init|curl|wget)\b")


def host_bin_without_fakes(directory: Path) -> Path:
    """This host's commands as symlinks, minus :data:`_FAKED`: the guest's scripts keep
    bash, grep and test, and can never reach a real ``systemctl``, ``docker`` or
    ``poweroff``."""
    out = directory / "host-bin"
    out.mkdir(parents=True, exist_ok=True)
    for name in _BIN_DIRS:
        directory_ = Path(name)
        if not directory_.is_dir():
            continue
        for exe in directory_.iterdir():
            if exe.name in _FAKED or os.path.lexists(out / exe.name):
                continue
            if exe.is_file() and os.access(exe, os.X_OK):
                (out / exe.name).symlink_to(exe)
    return out


_RECORDING_FAKE = """#!{python} -I
import json, os, sys
name = os.path.basename(sys.argv[0])
with open({calls!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
with open({rcs!r}, encoding="utf-8") as f:
    sys.exit(int(json.load(f).get(name, 0)))
"""

#: ``dpkg-query -W -f FORMAT PKG...`` (and ``-s``, and ``dpkg -s``) answered from a status
#: table, each call recorded; a package it does not know is dpkg's "no packages found",
#: exit 1.
_DPKG_QUERY_FAKE = """#!{python} -I
import json, os, sys
with open({status!r}, encoding="utf-8") as f:
    table = json.load(f)
with open({calls!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
args, fmt, status_only, pkgs = sys.argv[1:], None, False, []
i = 0
while i < len(args):
    a = args[i]
    if a in ("-f", "--showformat"):
        fmt = args[i + 1] if i + 1 < len(args) else ""
        i += 2
        continue
    if a.startswith("--showformat="):
        fmt = a.split("=", 1)[1]
    elif a.startswith("-f"):
        fmt = a[2:].removeprefix("=")
    elif a in ("-s", "--status"):
        status_only = True
    elif not a.startswith("-"):
        pkgs.append(a)
    i += 1
rc = 0
for p in pkgs:
    st = table.get(p)
    if st is None:
        print(f"dpkg-query: no packages found matching {{p}}", file=sys.stderr)
        rc = 1
        continue
    if status_only and fmt is None:
        sys.stdout.write(f"Package: {{p}}\\nStatus: {{st}}\\n\\n")
        continue
    out = fmt if fmt is not None else "${{binary:Package}}\\t${{Version}}\\n"
    abbrev = "ii " if st == "install ok installed" else "rc "
    for field, value in (
        ("${{Status}}", st),
        ("${{db:Status-Status}}", st.split()[-1]),
        ("${{db:Status-Abbrev}}", abbrev),
        ("${{binary:Package}}", p),
        ("${{Package}}", p),
        ("${{Version}}", "1.0"),
    ):
        out = out.replace(field, value)
    sys.stdout.write(out.replace("\\\\n", "\\n").replace("\\\\t", "\\t"))
sys.exit(rc)
"""

#: ``systemctl is-enabled UNIT...`` with systemd's rule: exit 0 when AT LEAST ONE is
#: enabled. Every other verb (``poweroff`` too) is recorded and succeeds.
_SYSTEMCTL_FAKE = """#!{python} -I
import json, sys
with open({units!r}, encoding="utf-8") as f:
    units = json.load(f)
with open({calls!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps(["systemctl", *sys.argv[1:]]) + "\\n")
quiet = any(a in ("-q", "--quiet") for a in sys.argv[1:])
words = [a for a in sys.argv[1:] if not a.startswith("-")]
if words[:1] == ["is-enabled"]:
    any_on = False
    for name in words[1:]:
        state = units.get(name, units.get(name + ".service"))
        if state is None:
            print(f"Failed to get unit file state for {{name}}: No such file", file=sys.stderr)
            continue
        if not quiet:
            print(state)
        any_on = any_on or state == "enabled"
    sys.exit(0 if any_on else 1)
sys.exit(0)
"""


class GuestSandbox:
    """A prepared guest's view, on this host: package states, ``docker``, the runner's
    ``run.sh``, the units' enablement and the exit codes of ``growpart``/``resize2fs``,
    as a test declares them. :meth:`sandboxed` moves the guest's paths under ``base``."""

    def __init__(self, base: Path, host_bin: Path, packages: Iterable[str]) -> None:
        self.base = base
        self.fakes = base / "fakes"
        self.fakes.mkdir(parents=True)
        self.host_bin = host_bin
        self.status: dict[str, str] = {p: "install ok installed" for p in packages}
        self.units: dict[str, str] = dict.fromkeys(UNITS, "enabled")
        self.docker = True
        self.runner_mode: int | None = 0o755
        self.rcs: dict[str, int] = {}

    def sandboxed(self, script: str) -> str:
        text = script.replace(CHECK, str(self.base / "prepare-check"))
        text = text.replace(RUNNER_DIR, str(self.base / "actions-runner"))
        text = text.replace("/dev/console", str(self.base / "console"))
        names = "|".join(re.escape(n) for n in _FAKED)
        return re.sub(
            rf"(?<![\w./-])(?:/usr)?(?:/local)?/s?bin/({names})(?![\w.-])",
            lambda m: str(self.fakes / m.group(1)),
            text,
        )

    def _write(self, name: str, text: str) -> None:
        path = self.fakes / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def install(self) -> None:
        state = {
            "status": self.base / "dpkg-status.json",
            "units": self.base / "units.json",
            "rcs": self.base / "rcs.json",
            "calls": self.base / "calls.jsonl",
        }
        state["status"].write_text(json.dumps(self.status), encoding="utf-8")
        state["units"].write_text(json.dumps(self.units), encoding="utf-8")
        state["rcs"].write_text(json.dumps(self.rcs), encoding="utf-8")
        kw = {k: str(v) for k, v in state.items()}
        dpkg_query = _DPKG_QUERY_FAKE.format(python=sys.executable, **kw)
        self._write("dpkg-query", dpkg_query)
        self._write("dpkg", dpkg_query)  # ``dpkg -s PKG`` answers as ``dpkg-query -s PKG``
        self._write("systemctl", _SYSTEMCTL_FAKE.format(python=sys.executable, **kw))
        recording = _RECORDING_FAKE.format(python=sys.executable, **kw)
        for name in ("growpart", "resize2fs", "poweroff", "reboot", "shutdown", "halt"):
            self._write(name, recording)
        if self.docker:
            self._write("docker", recording)
        runner = self.base / "actions-runner" / "run.sh"
        runner.parent.mkdir(parents=True, exist_ok=True)
        if self.runner_mode is not None:
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runner.chmod(self.runner_mode)

    def env(self) -> dict[str, str]:
        return {"PATH": f"{self.fakes}:{self.host_bin}", "LANG": "C.UTF-8", "HOME": str(self.base)}

    def run(self, argv: list[str]) -> Any:
        return subprocess.run(
            argv,
            env=self.env(),
            cwd=self.base,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def put_check(self, doc: dict[str, Any]) -> Path:
        check = written_file(doc, CHECK)
        assert not _DESTRUCTIVE.search(check), f"the prepare check does more than check: {check}"
        path = self.base / "prepare-check"
        path.write_text(self.sandboxed(check), encoding="utf-8")
        path.chmod(0o755)
        return path

    def console(self) -> list[str]:
        path = self.base / "console"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def calls(self) -> list[list[str]]:
        path = self.base / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def complete_guest(base: Path, host_bin: Path, doc: dict[str, Any]) -> GuestSandbox:
    """A complete guest for ``doc``: every package it installs is installed."""
    packages = [str(p) for p in doc.get("packages") or [] if isinstance(p, str)]
    return GuestSandbox(base, host_bin, packages)


def spoil_guest(sandbox: GuestSandbox, spoil: dict[str, Any]) -> None:
    if "status" in spoil:
        package, state = spoil["status"]
        if state is None:
            sandbox.status.pop(package, None)
        else:
            sandbox.status[package] = state
    if "docker" in spoil:
        sandbox.docker = spoil["docker"]
    if "runner_mode" in spoil:
        sandbox.runner_mode = spoil["runner_mode"]
    if "unit" in spoil:
        unit, state = spoil["unit"]
        sandbox.units[unit] = state


def marker_step(doc: dict[str, Any]) -> str:
    """The ``runcmd:`` lines from the one that runs the prepare check to the one that prints
    the marker (the same line, or the check alone when the check prints it), as cloud-init
    runs them: one ``/bin/sh`` script, no ``set -e``."""
    lines = runcmd_lines(doc)
    at_check = [i for i, line in enumerate(lines) if CHECK in line]
    assert at_check, f"runcmd never runs {CHECK}: {lines}"
    first = at_check[0]
    at_marker = [i for i, line in enumerate(lines) if PREPARED_MARKER in line]
    assert all(i >= first for i in at_marker), "the marker is printed before the check runs"
    step = "\n".join(lines[first : max([first, *at_marker]) + 1])
    assert not _DESTRUCTIVE.search(step), f"the marker step does more than check and print: {step}"
    return step


def run_marker_step(sandbox: GuestSandbox, doc: dict[str, Any]) -> tuple[list[str], str]:
    """Run the marker step in ``sandbox``; the console's lines and stdout."""
    sandbox.install()
    sandbox.put_check(doc)
    done = sandbox.run(["/bin/sh", "-c", sandbox.sandboxed(marker_step(doc))])
    return sandbox.console(), done.stdout


def run_unit(sandbox: GuestSandbox, unit: str) -> bool:
    """A oneshot unit's ``ExecStartPre``/``ExecStart`` lines as systemd runs them -- words
    split and ``$$``/``${VAR}`` expanded by systemd, a ``-`` prefix or a
    ``SuccessExitStatus=`` code is no failure; whether the unit succeeded."""
    settings = unit_settings(unit)
    success = {
        int(c) for v in settings.get("SuccessExitStatus", []) for c in v.split() if c.isdigit()
    }
    sandbox.install()
    for key in ("ExecStartPre", "ExecStart"):
        for line in settings.get(key, []):
            text = sandbox.sandboxed(line)
            prefix = re.match(r"[-@:+!]*", text)
            flags = prefix.group(0) if prefix else ""
            words = systemd_expand(shlex.split(text[len(flags) :]), {})
            if "@" in flags:
                words = words[:1] + words[2:]
            done = sandbox.run(words)
            ok = done.returncode == 0 or "-" in flags
            ok = ok or (key == "ExecStart" and done.returncode in success)
            if not ok:
                return False
    return True
