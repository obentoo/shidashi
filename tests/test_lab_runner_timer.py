"""``builder.sh timer-install`` and the weekly user timer (story 032, task 7.4; R6.6).

Contract (story.md R6.6; design.md "Units"): ``lab/systemd/shidashi-weekly.timer`` fires
``OnCalendar=`` every Sunday at 01:00 UTC with ``Persistent=true`` (a missed run is caught
up), and ``shidashi-weekly.service`` runs ``builder.sh weekly``. ``lab/vm/builder.sh
timer-install`` installs both as the user's units and enables the timer -- ``systemctl
--user``, never root, never ``sudo``.

The units are read as files (and normalised by ``systemd-analyze calendar`` when it is
there); ``timer-install`` runs for real with ``systemctl`` and ``sudo`` recorded by fakes
on PATH and ``HOME``/``XDG_CONFIG_HOME`` under ``tmp_path``.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._fake_builder_guest import CHECKOUT, STANDARD_PATH

UNITS = CHECKOUT / "lab" / "systemd"
WEEKLY_TIMER = UNITS / "shidashi-weekly.timer"
WEEKLY_SERVICE = UNITS / "shidashi-weekly.service"
BUILDER = CHECKOUT / "lab" / "vm" / "builder.sh"
TRUE_WORDS = {"true", "yes", "1", "on"}


def unit_settings(path: Path) -> dict[str, dict[str, list[str]]]:
    """``[Section] Key=value`` of a unit file; a key may repeat."""
    assert path.is_file(), f"{path} is missing"
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        elif "=" in line and current is not None:
            key, _, value = line.partition("=")
            current.setdefault(key.strip(), []).append(value.strip())
    return sections


def normalised_calendar(spec: str) -> str:
    if shutil.which("systemd-analyze") is None:
        return spec
    done = subprocess.run(
        ["systemd-analyze", "calendar", spec], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    found = re.search(r"Normalized form:\s*(.+)", done.stdout)
    assert found, done.stdout
    return found.group(1).strip()


def test_timer_fires_every_sunday_at_0100_utc_and_catches_up_a_missed_run() -> None:
    timer = unit_settings(WEEKLY_TIMER).get("Timer", {})
    calendars = [c for c in timer.get("OnCalendar", []) if c]
    assert len(calendars) == 1, timer
    normalised = normalised_calendar(calendars[0])
    if shutil.which("systemd-analyze") is not None:
        # Hostile half: "Sun 01:00" without UTC is the host's local time.
        assert normalised == "Sun *-*-* 01:00:00 UTC", normalised
    else:
        assert re.search(r"\bSun\b", normalised) and "01:00" in normalised, normalised
        assert normalised.rstrip().endswith("UTC"), normalised
    assert [v.lower() for v in timer.get("Persistent", [])] in [[w] for w in TRUE_WORDS], timer
    assert "timers.target" in " ".join(
        unit_settings(WEEKLY_TIMER).get("Install", {}).get("WantedBy", [])
    )
    target = timer.get("Unit", ["shidashi-weekly.service"])
    assert target == ["shidashi-weekly.service"], timer


def test_timer_service_runs_builder_weekly() -> None:
    service = unit_settings(WEEKLY_SERVICE).get("Service", {})
    starts = service.get("ExecStart", [])
    assert len(starts) == 1, service
    assert "builder.sh" in starts[0] and re.search(r"\bweekly\b", starts[0]), starts
    assert service.get("User", []) in ([], [""]), "a user unit runs as its user"


def write_fake(path: Path, log: Path, code: int = 0) -> None:
    path.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> {log}\nexit {code}\n', encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def timer_home(tmp_path: Path) -> Path:
    fakes = tmp_path / "bin"
    fakes.mkdir()
    write_fake(fakes / "systemctl", tmp_path / "systemctl.log")
    write_fake(fakes / "sudo", tmp_path / "sudo.log", code=1)
    (tmp_path / "home").mkdir()
    return tmp_path


def run_timer_install(root: Path) -> subprocess.CompletedProcess[str]:
    assert BUILDER.is_file()
    home = root / "home"
    return subprocess.run(
        ["bash", str(BUILDER), "timer-install"],
        env={
            "PATH": f"{root / 'bin'}:{os.environ.get('PATH', STANDARD_PATH)}",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def logged_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def test_timer_install_installs_and_enables_the_user_timer_without_root(timer_home: Path) -> None:
    done = run_timer_install(timer_home)
    assert done.returncode == 0, done.stdout + done.stderr
    calls = logged_lines(timer_home / "systemctl.log")
    assert calls, "systemctl was never called"
    assert all("--user" in call.split() for call in calls), calls
    enabled = [c for c in calls if "enable" in c.split() and "shidashi-weekly.timer" in c]
    assert enabled, calls
    started = any("--now" in c.split() for c in enabled) or any(
        ("start" in c.split() or "restart" in c.split()) and "shidashi-weekly.timer" in c
        for c in calls
    )
    assert started, f"the timer is enabled but never started: {calls}"
    user_units = timer_home / "home" / ".config" / "systemd" / "user"
    installed = all((user_units / n.name).exists() for n in (WEEKLY_TIMER, WEEKLY_SERVICE))
    linked = any(
        "link" in c.split() and str(WEEKLY_TIMER) in c and str(WEEKLY_SERVICE) in c for c in calls
    ) or all(
        any("link" in c.split() and str(n) in c for c in calls)
        for n in (WEEKLY_TIMER, WEEKLY_SERVICE)
    )
    assert installed or linked, f"the units are neither in {user_units} nor linked: {calls}"
    assert logged_lines(timer_home / "sudo.log") == [], "timer-install ran sudo"
