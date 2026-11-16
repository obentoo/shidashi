"""Tests of shidashi.vm_dev_expiry and ``vm start --dev --max-life``.

A dev session whose caller died keeps a VM and a 40 GB work disk alive; ``--max-life``
gives it a deadline and a watchdog. Here: the deadline and its record, the option, the
JSON line, and who ends the watchdog. The clock is a ``FakeClock`` and the launcher a
``FakeWatchdogLauncher``, so nothing sleeps and nothing is spawned -- except where the
point is a real process: the watchdog a ``vm stop`` must find and end. What the watchdog
itself does at the deadline is tests/test_vm_dev_watchdog.py's.
"""

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import tree, vm, vm_dev, vm_dev_expiry
from shidashi.cli import app
from tests.test_vm_dev import FakeDevSession, FakeTrees, _info, _iso
from tests.test_vm_dev_cleanup import _dead_pid, _session

_REPO = Path(__file__).resolve().parents[1]
_NOON = datetime(2026, 11, 15, 12, 0, 0, 250_000, tzinfo=UTC)


class FakeClock:
    """A wall clock that only moves when told to."""

    def __init__(self, now: datetime = _NOON) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeWatchdogLauncher:
    """Stands in for the process ``launch_watchdog`` starts: records what it was asked
    and, like the real watchdog once it is up, puts a pid in the record."""

    def __init__(self, *, pid: int = 4242, fails: str = "") -> None:
        self.pid = pid
        self.fails = fails
        self.launched: list[tuple[Path, str, str]] = []

    def __call__(self, directory: Path, name: str, token: str) -> None:
        self.launched.append((directory, name, token))
        if self.fails:
            raise vm.VmError(self.fails)
        assert vm_dev_expiry.claim(directory, token, pid=self.pid)


# --- the deadline --------------------------------------------------------------------------


def test_the_deadline_is_utc_whole_seconds_and_reads_as_iso_8601() -> None:
    deadline = vm_dev_expiry.deadline_after(1.5, _NOON)
    assert deadline == datetime(2026, 11, 15, 13, 30, 0, tzinfo=UTC)
    assert vm_dev_expiry.utc_stamp(deadline) == "2026-11-15T13:30:00Z"
    assert datetime.fromisoformat("2026-11-15T13:30:00Z") == deadline


def test_a_deadline_asked_in_another_zone_is_still_printed_in_utc() -> None:
    local = datetime(2026, 7, 1, 13, 0, tzinfo=UTC).astimezone(timezone(timedelta(hours=-3)))
    assert local.hour == 10
    assert vm_dev_expiry.utc_stamp(vm_dev_expiry.deadline_after(0.02, local)) == (
        "2026-07-01T13:01:12Z"  # 0.02 h is 72 s
    )


@pytest.mark.parametrize("hours", [0.0, -1.0, float("nan"), float("inf"), 1e300])
def test_a_max_life_that_is_no_positive_span_is_told_why(hours: float) -> None:
    assert vm_dev_expiry.max_life_error(hours, now=_NOON) is not None


@pytest.mark.parametrize("hours", [0.02, 1.0, 72.0, 1e-9])
def test_a_positive_max_life_is_accepted(hours: float) -> None:
    assert vm_dev_expiry.max_life_error(hours, now=_NOON) is None


# --- the record ----------------------------------------------------------------------------


def test_arming_records_the_deadline_then_starts_the_watchdog(tmp_path: Path) -> None:
    launcher = FakeWatchdogLauncher()
    deadline = vm_dev_expiry.arm(tmp_path, name="dev", hours=2, clock=FakeClock(), launch=launcher)
    assert deadline == datetime(2026, 11, 15, 14, 0, 0, tzinfo=UTC)
    ((directory, name, token),) = launcher.launched
    assert (directory, name) == (tmp_path, "dev") and len(token) == 16
    # read back: the deadline printed is the deadline recorded
    assert vm_dev_expiry.read_expiry(tmp_path) == vm_dev_expiry.Expiry(deadline, token, 4242)
    assert json.loads((tmp_path / "expiry.json").read_text()) == {
        "deadline": "2026-11-15T14:00:00Z",
        "token": token,
        "pid": 4242,
    }


def test_the_record_is_there_before_the_watchdog_starts(tmp_path: Path) -> None:
    seen: list[vm_dev_expiry.Expiry | None] = []

    def launch(directory: Path, _name: str, _token: str) -> None:
        seen.append(vm_dev_expiry.read_expiry(directory))  # what the watchdog would read

    vm_dev_expiry.arm(tmp_path, name="dev", hours=1, clock=FakeClock(), launch=launch)
    (found,) = seen
    assert found is not None and found.pid is None
    assert found.deadline == datetime(2026, 11, 15, 13, 0, 0, tzinfo=UTC)


def test_two_sessions_never_share_a_token(tmp_path: Path) -> None:
    launcher = FakeWatchdogLauncher()
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        vm_dev_expiry.arm(tmp_path / name, name=name, hours=1, launch=launcher)
    assert launcher.launched[0][2] != launcher.launched[1][2]


def test_a_watchdog_that_does_not_start_leaves_no_record(tmp_path: Path) -> None:
    launcher = FakeWatchdogLauncher(fails="the expiry watchdog did not start: boom")
    with pytest.raises(vm.VmError, match="did not start"):
        vm_dev_expiry.arm(tmp_path, name="dev", hours=1, launch=launcher)
    assert not (tmp_path / "expiry.json").exists()
    assert vm_dev_expiry.read_expiry(tmp_path) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '"deadline"',
        '{"token": "t"}',
        '{"deadline": "tomorrow", "token": "t"}',
        '{"deadline": "2026-11-15T14:00:00+00:00", "token": "t"}',
        '{"deadline": "2026-11-15T14:00:00Z", "token": 7}',
        '{"deadline": "2026-11-15T14:00:00Z", "token": "t", "pid": "12"}',
        '{"deadline": "2026-11-15T14:00:00Z", "token": "t", "pid": true}',
    ],
)
def test_a_record_that_does_not_read_is_no_record(tmp_path: Path, text: str) -> None:
    (tmp_path / "expiry.json").write_text(text)
    assert vm_dev_expiry.read_expiry(tmp_path) is None


def test_a_session_without_a_record_has_no_deadline(tmp_path: Path) -> None:
    assert vm_dev_expiry.read_expiry(tmp_path) is None
    assert vm_dev_expiry.read_expiry(tmp_path / "removed") is None


def test_only_the_watchdog_of_the_token_claims_the_record(tmp_path: Path) -> None:
    vm_dev_expiry.arm(tmp_path, name="dev", hours=1, launch=lambda *_a: None)
    recorded = vm_dev_expiry.read_expiry(tmp_path)
    assert recorded is not None and recorded.pid is None
    assert vm_dev_expiry.claim(tmp_path, "another-token", pid=7) is False
    assert vm_dev_expiry.read_expiry(tmp_path) == recorded
    assert vm_dev_expiry.claim(tmp_path, recorded.token, pid=7) is True
    assert vm_dev_expiry.read_expiry(tmp_path) == vm_dev_expiry.Expiry(
        recorded.deadline, recorded.token, 7
    )
    assert vm_dev_expiry.claim(tmp_path / "removed", recorded.token, pid=7) is False


# --- the log -------------------------------------------------------------------------------


def test_the_expiry_log_is_beside_the_sessions_not_inside_one(tmp_path: Path) -> None:
    session = tmp_path / "vm" / "dev"
    session.mkdir(parents=True)
    assert vm_dev_expiry.expiry_log(session) == tmp_path / "vm" / "expiry.jsonl"
    deadline = datetime(2026, 11, 15, 14, 0, 0, tzinfo=UTC)
    for outcome in ("failed", "removed"):
        vm_dev_expiry.record_expiry(
            session, name="dev", deadline=deadline, outcome=outcome, now=_NOON, attempts=1
        )
    text = (tmp_path / "vm" / "expiry.jsonl").read_text()
    assert text.endswith("\n") and text.count("\n") == 2  # one event per line
    lines = [json.loads(line) for line in text.splitlines()]
    assert lines[1] == {
        "event": "vm.dev.expiry",
        "time": "2026-11-15T12:00:00Z",
        "session": "dev",
        "directory": str(session),
        "deadline": "2026-11-15T14:00:00Z",
        "outcome": "removed",
        "attempts": 1,
    }
    assert lines[0]["outcome"] == "failed"


@pytest.mark.parametrize("scratch", [True, False], ids=["scratch", "user-state"])
def test_the_log_follows_the_session_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch: bool
) -> None:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = vm.session_dir("dev") if scratch else vm.user_session_dir("dev")
    root = tmp_path / "scratch" / "vm" if scratch else tmp_path / "state" / "shidashi" / "vm"
    assert vm_dev_expiry.expiry_log(directory) == root / "expiry.jsonl"


# --- start_dev -----------------------------------------------------------------------------


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTrees:
    """A host whose ISO, trees, disk and QEMU are stood in for (as tests/test_vm_dev.py)."""
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_CID_LOCKS", str(tmp_path / "cids"))
    (tmp_path / "cids").mkdir(mode=0o700)
    monkeypatch.setattr(vm, "read_build_info", lambda _iso: _info())
    monkeypatch.setattr(vm, "ensure_disk", lambda path, size: path)
    trees = FakeTrees(tmp_path / "cache" / "repos")
    monkeypatch.setattr(tree, "ensure_tree", trees.gentoo)
    monkeypatch.setattr(tree, "ensure_overlay", trees.overlay)
    FakeDevSession.instances.clear()
    return trees


def _start(tmp_path: Path, **kw: Any) -> vm_dev.DevVm:
    defaults: dict[str, Any] = {
        "name": "dev",
        "iso": _iso(tmp_path / "cache" / "releases", "2026.11.12"),
        "init": "systemd",
        "arch": "v3",
        "directory": tmp_path / "sessions" / "dev",
        "session_factory": FakeDevSession,
        "repo": tmp_path,
        "clock": FakeClock(),
        "launch_watchdog": FakeWatchdogLauncher(),
    }
    return vm_dev.start_dev(**{**defaults, **kw})


def test_a_start_with_a_max_life_reports_when_the_session_expires(
    tmp_path: Path, host: FakeTrees
) -> None:
    launcher = FakeWatchdogLauncher()
    dev = _start(tmp_path, max_life=0.02, launch_watchdog=launcher)
    assert dev.expires == datetime(2026, 11, 15, 12, 1, 12, tzinfo=UTC)
    assert dev.as_json()["expires"] == "2026-11-15T12:01:12Z"
    assert [(d, n) for d, n, _token in launcher.launched] == [(dev.directory, "dev")]
    recorded = vm_dev_expiry.read_expiry(dev.directory)
    assert recorded is not None and recorded.deadline == dev.expires


def test_the_hours_count_from_a_ready_vm_not_from_the_boot(tmp_path: Path, host: FakeTrees) -> None:
    # a boot and a guest setup take a minute: hours of a VM that cannot be used yet
    clock = FakeClock()

    class SlowToBoot(FakeDevSession):
        def run_command(self, command: str, *, timeout: float | None = 600) -> vm.GuestResult:
            clock.now += timedelta(minutes=5)
            return super().run_command(command, timeout=timeout)

    launched_at: list[datetime] = []
    launcher = FakeWatchdogLauncher()

    def launch(directory: Path, name: str, token: str) -> None:
        launched_at.append(clock.now)
        launcher(directory, name, token)

    dev = _start(
        tmp_path, max_life=1, clock=clock, session_factory=SlowToBoot, launch_watchdog=launch
    )
    assert launched_at == [_NOON + timedelta(minutes=5)]  # once the setup was done
    assert dev.expires == datetime(2026, 11, 15, 13, 5, 0, tzinfo=UTC)


def test_a_start_without_a_max_life_starts_no_watchdog(tmp_path: Path, host: FakeTrees) -> None:
    launcher = FakeWatchdogLauncher()
    dev = _start(tmp_path, launch_watchdog=launcher)
    assert dev.expires is None and dev.as_json()["expires"] is None
    assert launcher.launched == []
    assert vm_dev_expiry.read_expiry(dev.directory) is None


def test_a_failed_setup_starts_no_watchdog(tmp_path: Path, host: FakeTrees) -> None:
    def failing(spec: vm.VmSpec, directory: Path) -> FakeDevSession:
        return FakeDevSession(spec, directory, setup_rc=1)

    launcher = FakeWatchdogLauncher()
    with pytest.raises(vm.VmError, match="boom"):
        _start(tmp_path, max_life=1, session_factory=failing, launch_watchdog=launcher)
    assert launcher.launched == []


def test_a_watchdog_that_does_not_start_powers_the_vm_off(tmp_path: Path, host: FakeTrees) -> None:
    # asked for a limit: a VM running without one is not what was asked for
    launcher = FakeWatchdogLauncher(fails="the expiry watchdog did not start (exit 1): boom")
    with pytest.raises(vm.VmError, match="watchdog did not start"):
        _start(tmp_path, max_life=1, launch_watchdog=launcher)
    assert FakeDevSession.instances[0].events[-1] == "stop"
    assert vm_dev_expiry.read_expiry(tmp_path / "sessions" / "dev") is None


def test_a_start_forgets_the_deadline_of_the_dead_session_it_replaces(
    tmp_path: Path, host: FakeTrees
) -> None:
    # same name, QEMU died without a stop: its deadline must not end the new session
    directory = _session(tmp_path / "sessions", "dev", dev=True, pid=_dead_pid())
    vm_dev_expiry.arm(directory, name="dev", hours=1, launch=FakeWatchdogLauncher())
    dev = _start(tmp_path)
    assert dev.expires is None
    assert vm_dev_expiry.read_expiry(directory) is None


def test_a_refused_start_leaves_the_running_session_its_deadline(
    tmp_path: Path, host: FakeTrees
) -> None:
    """Hostile: a second start under the name of a session whose QEMU runs. It is
    refused -- and must not have disarmed the session it did not replace."""
    directory = _session(tmp_path / "sessions", "dev", dev=True, pid=os.getpid())
    vm_dev_expiry.arm(directory, name="dev", hours=1, launch=FakeWatchdogLauncher())
    armed = vm_dev_expiry.read_expiry(directory)
    with pytest.raises(vm.VmError, match="already running"):
        _start(tmp_path, max_life=5)
    assert armed is not None and vm_dev_expiry.read_expiry(directory) == armed


# --- the CLI -------------------------------------------------------------------------------


@pytest.fixture
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``vm start --dev`` with ``start_dev`` stood in for; what it was called with."""
    captured: dict[str, Any] = {}

    def fake_start(**kw: Any) -> vm_dev.DevVm:
        captured.update(kw)
        expires = None
        if kw["max_life"] is not None:
            expires = vm_dev_expiry.deadline_after(kw["max_life"], _NOON)
        return vm_dev.DevVm("dev", 43, Path("/i.iso"), "p1", [], Path("/s"), expires)

    monkeypatch.setattr(vm_dev, "start_dev", fake_start)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SHIDASHI_SCRATCH", raising=False)
    return captured


def test_cli_max_life_reaches_the_start_and_the_json_line(started: dict[str, Any]) -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--dev", "--max-life", "0.5"])
    assert result.exit_code == 0, result.output
    assert started["max_life"] == 0.5
    (line,) = result.stdout.splitlines()  # still exactly one line: bentoolkit parses it
    assert json.loads(line)["expires"] == "2026-11-15T12:30:00Z"
    assert json.loads(line)["ready"] is True


def test_cli_without_max_life_prints_a_null_expiry(started: dict[str, Any]) -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--dev"])
    assert result.exit_code == 0, result.output
    assert started["max_life"] is None
    line = json.loads(result.stdout)
    assert "expires" in line and line["expires"] is None


@pytest.mark.parametrize("hours", ["0", "-1", "nan", "inf", "1e300", "soon"])
def test_cli_refuses_a_max_life_that_is_no_positive_number_before_anything_boots(
    started: dict[str, Any], hours: str
) -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--dev", f"--max-life={hours}"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--max-life" in result.stderr
    assert started == {}


def test_cli_refuses_max_life_without_dev(tmp_path: Path, started: dict[str, Any]) -> None:
    # a plain session is never removed: a deadline it would not honour is a usage error
    iso = tmp_path / "b.iso"
    iso.write_bytes(b"iso")
    result = CliRunner().invoke(app, ["vm", "start", str(iso), "--max-life", "1"])
    assert result.exit_code == 2
    assert "--max-life needs --dev" in result.stderr
    assert started == {}


def test_vm_start_help_names_max_life_and_hides_pins_json() -> None:
    result = CliRunner().invoke(app, ["vm", "start", "--help"])
    assert "--max-life" in result.output and "HOURS" in result.output
    assert "--pins-json" not in result.output
