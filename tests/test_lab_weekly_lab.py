"""``lab/weekly-lab.sh`` -- the weekly unsigned release built on the worker, no GitHub runner.

The script only orchestrates: it checks the worker answers and owns the arch's cache, runs
``shidashi release --unsigned``, prints the report, and prunes only after a green run (a
failed week must not push a good release out of the window). ``shidashi`` is a recorder in
a copy of the checkout's ``lab/``, the way ``tests/_fake_builder_guest.py`` stands in for it.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._fake_builder_guest import CHECKOUT, STANDARD_PATH

RECORDER = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[3:]  # python -c CODE ARGS...
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"args": args, "scratch": os.environ.get("SHIDASHI_SCRATCH")}}) + "\\n")
if args[:2] == ["worker", "status"] or args == ["worker", "status"]:
    print(os.environ.get("FAKE_STATUS", "bentoo-lab  reachable  max v3  16 threads"))
elif args[:2] == ["worker", "cache-owner"]:
    print(os.environ.get("FAKE_OWNS", "bentoo-lab owns the cache of: v3"))
elif args[:1] == ["release"] and args[1:2] not in (["report"], ["prune"]):
    print("v3/systemd: ok")
    print("release: " + os.environ["FAKE_RELEASE_DIR"])
    sys.exit(int(os.environ.get("FAKE_RELEASE_RC", "0")))
"""


class WeeklyLab:
    def __init__(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.root = tmp_path
        self.checkout = tmp_path / "checkout"
        shutil.copytree(
            CHECKOUT / "lab", self.checkout / "lab", ignore=shutil.ignore_patterns("__pycache__")
        )
        python = self.checkout / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(RECORDER, encoding="utf-8")
        python.chmod(0o755)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for tool in ("sudo", "systemctl"):
            fake = self.bin / tool
            fake.write_text(f'#!/bin/bash\necho "$*" >> {tmp_path / (tool + ".log")}\n')
            fake.chmod(0o755)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log = tmp_path / "calls.jsonl"
        self.release_dir = tmp_path / "releases" / "2026.11.10"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', STANDARD_PATH)}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "FAKE_LOG": str(self.log),
            "FAKE_RELEASE_DIR": str(self.release_dir),
            "SHIDASHI_SCRATCH": str(self.root / "scratch"),
            "WEEKLY_OUT": str(self.root / "releases"),
            **env,
        }
        return subprocess.run(
            ["bash", str(self.checkout / "lab" / "weekly-lab.sh"), *args],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.is_file():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line)["args"] for line in lines]

    def index(self, *prefix: str) -> int:
        found = [i for i, c in enumerate(self.calls()) if c[: len(prefix)] == list(prefix)]
        assert found, f"no `shidashi {' '.join(prefix)}`: {self.calls()}"
        return found[0]

    def no_call(self, *prefix: str) -> bool:
        return all(c[: len(prefix)] != list(prefix) for c in self.calls())


@pytest.fixture
def lab(tmp_path: Path) -> WeeklyLab:
    return WeeklyLab(tmp_path)


def option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def test_a_green_cycle_checks_the_worker_builds_unsigned_reports_then_prunes(
    lab: WeeklyLab,
) -> None:
    done = lab.run("weekly")
    assert done.returncode == 0, done.stdout + done.stderr
    status = lab.index("worker", "status")
    owner = lab.index("worker", "cache-owner")
    built = lab.index("release", "v3")
    report = lab.index("release", "report")
    prune = lab.index("release", "prune")
    assert status < built and owner < built < report < prune
    release = lab.calls()[built]
    assert release[:3] == ["release", "v3", "systemd"]
    assert "--unsigned" in release and "--key" not in release
    assert option(release, "--images") == "minimal,kde,gnome"
    assert lab.calls()[report][2] == str(lab.release_dir)
    pruning = lab.calls()[prune]
    assert option(pruning, "--keep") == "4" and "--purge-pruned" in pruning
    assert lab.no_call("worker", "poweroff")
    assert not (lab.root / "sudo.log").exists(), "the cycle ran sudo"


def test_the_scratch_is_the_users_not_the_root_owned_default(lab: WeeklyLab) -> None:
    lab.run("weekly", SHIDASHI_SCRATCH="")
    scratch = [json.loads(x)["scratch"] for x in lab.log.read_text().splitlines()]
    assert scratch and all(s == f"/var/tmp/shidashi-{os.getlogin()}" for s in scratch), scratch


def test_a_failed_release_still_reports_but_prunes_nothing_and_fails(lab: WeeklyLab) -> None:
    """Hostile half: a failed week's directory must not count toward the kept releases."""
    done = lab.run("weekly", FAKE_RELEASE_RC="1")
    assert done.returncode == 1, done.stdout + done.stderr
    assert not lab.no_call("release", "report")
    assert lab.no_call("release", "prune")
    assert lab.no_call("worker", "poweroff")


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"FAKE_STATUS": "bentoo-lab  unreachable  no answer"}, id="worker-down"),
        pytest.param({"FAKE_OWNS": "bentoo-lab owns the cache of: no arch"}, id="arch-not-owned"),
        # a worker that owns another arch must not pass for owning v3
        pytest.param({"FAKE_OWNS": "bentoo-lab owns the cache of: znver5"}, id="other-arch"),
    ],
)
def test_it_stops_before_any_build_when_the_worker_cannot_build_the_arch(
    lab: WeeklyLab, env: dict[str, str]
) -> None:
    done = lab.run("weekly", **env)
    assert done.returncode == 1, done.stdout + done.stderr
    assert lab.no_call("release")


def test_poweroff_is_opt_in_and_only_after_a_green_run(tmp_path: Path) -> None:
    green = WeeklyLab(tmp_path / "green")
    assert green.run("weekly", WEEKLY_POWEROFF="1").returncode == 0
    assert green.index("release", "prune") < green.index("worker", "poweroff")
    failed = WeeklyLab(tmp_path / "failed")
    failed.run("weekly", WEEKLY_POWEROFF="1", FAKE_RELEASE_RC="1")
    assert failed.no_call("worker", "poweroff")


def test_timer_install_installs_the_user_units_with_systemctl_user_only(lab: WeeklyLab) -> None:
    done = lab.run("timer-install")
    assert done.returncode == 0, done.stdout + done.stderr
    units = lab.home / ".config" / "systemd" / "user"
    service = (units / "shidashi-weekly-lab.service").read_text(encoding="utf-8")
    assert f"{lab.checkout}/lab/weekly-lab.sh weekly" in service and "@REPO@" not in service
    timer = (units / "shidashi-weekly-lab.timer").read_text(encoding="utf-8")
    assert "OnCalendar=Sun *-*-* 01:00:00 UTC" in timer and "Persistent=true" in timer
    calls: Any = (lab.root / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert calls and all(c.startswith("--user") for c in calls), calls
    assert any("enable --now shidashi-weekly-lab.timer" in c for c in calls)
    assert not (lab.root / "sudo.log").exists()
