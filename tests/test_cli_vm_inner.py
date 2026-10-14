"""The hidden inner entries the runner container runs (story 024, task 3.1;
R1.2, R4.4, R4.5).

Contract (design.md, Inner CLI entries):

- ``shidashi vm inner-boot ISO --cid N --firmware bios|uefi|both --report FILE
  [--screenshots DIR]`` calls ``vm.boot_test`` on CID ``N`` with NO CID lock of its own
  (the outer caller holds it, host-wide) and NO audit run of its own; writes the report as
  JSON to FILE atomically (a temporary file renamed onto FILE); on an exception writes
  ``{"error": "<text>"}`` the same way and exits 2; exits 0 otherwise -- the verdict is the
  report's ``passed``.
- ``shidashi vm inner-build-info ISO`` prints ``vm.read_build_info(ISO)`` as JSON.
- Both hidden: not in ``shidashi vm --help``.

``vm.boot_test`` and ``vm.read_build_info`` are fakes; nothing boots.
"""

import fcntl
import json
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import vm
from shidashi.cli import app

runner = CliRunner()


def _report(iso: Path, *, passed: bool = True) -> dict[str, Any]:
    firmware = {
        "ssh_after_s": 9.0,
        "checks": [{"check": "os-release", "passed": passed, "got": "x", "expected": "x"}],
        "metrics": {"kernel": "6.17"},
        "passed": passed,
    }
    return {
        "iso": str(iso),
        "build": {"arch": "v3", "flavor": "minimal", "init": "openrc"},
        "firmwares": {"bios": firmware, "uefi": firmware},
        "passed": passed,
    }


@pytest.fixture
def iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path / "runs"))
    path = tmp_path / "isos" / "bentoo-x.iso"
    path.parent.mkdir()
    path.write_bytes(b"ISO")
    return path


@pytest.fixture
def booted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, dict[str, Any]]]:
    calls: list[tuple[Path, dict[str, Any]]] = []

    def boot_test(iso: Path, **kw: Any) -> dict[str, Any]:
        calls.append((iso, kw))
        return _report(iso)

    monkeypatch.setattr(vm, "boot_test", boot_test)
    return calls


@pytest.fixture
def cid_held_outside() -> Generator[int]:
    """CID 57's host lock, held by another open file description -- as the outer caller
    holds it while the container runs. A second lock of 57 here would be refused."""
    path = vm.cid_lock_dir() / "cid-57.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield 57
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _inner_boot(iso: Path, report: Path, *extra: str, cid: int = 57) -> Any:
    return runner.invoke(
        app,
        [
            "vm",
            "inner-boot",
            str(iso),
            "--cid",
            str(cid),
            "--firmware",
            "both",
            "--report",
            str(report),
            *extra,
        ],
    )


# --- hostile half -------------------------------------------------------------------------


def test_inner_boot_takes_no_cid_lock_of_its_own(
    iso: Path,
    tmp_path: Path,
    booted: list[tuple[Path, dict[str, Any]]],
    cid_held_outside: int,
) -> None:
    """The outer caller holds CID 57 for the whole container run: the inner boots ON 57,
    neither refusing it as busy nor moving to another CID."""
    report = tmp_path / "scratch" / "vm" / "runner-57" / "report.json"
    report.parent.mkdir(parents=True)
    done = _inner_boot(iso, report, cid=cid_held_outside)
    assert done.exit_code == 0, done.output
    assert [kw["cid"] for _iso, kw in booted] == [57]


def test_an_exception_is_written_as_an_error_never_as_a_verdict(
    iso: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4.4: the outer must tell a broken boot test from failed checks."""

    def boot_test(_iso: Path, **_kw: Any) -> dict[str, Any]:
        raise vm.VmError("ssh never answered on vsock CID 57")

    monkeypatch.setattr(vm, "boot_test", boot_test)
    report = tmp_path / "report.json"
    done = _inner_boot(iso, report)
    assert done.exit_code == 2
    data = json.loads(report.read_text())
    assert "ssh never answered on vsock CID 57" in data["error"]
    assert "passed" not in data


@pytest.mark.parametrize("error", [KeyError("arch"), RuntimeError("qemu died")])
def test_any_exception_is_written_as_an_error(
    iso: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def boot_test(_iso: Path, **_kw: Any) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(vm, "boot_test", boot_test)
    report = tmp_path / "report.json"
    done = _inner_boot(iso, report)
    assert done.exit_code == 2
    assert set(json.loads(report.read_text())) == {"error"}


def test_the_report_replaces_a_stale_one_atomically(
    iso: Path,
    tmp_path: Path,
    booted: list[tuple[Path, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4.5: the report lands by a rename onto FILE -- never written in place, where a
    container killed mid-write would leave half a JSON the outer could read."""
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"stale": True}))
    renames: list[tuple[str, str]] = []
    real_replace = os.replace

    def replace(src: Any, dst: Any, *args: Any, **kw: Any) -> None:
        renames.append((os.fspath(src), os.fspath(dst)))
        real_replace(src, dst, *args, **kw)

    monkeypatch.setattr(os, "replace", replace)
    done = _inner_boot(iso, report)
    assert done.exit_code == 0, done.output
    assert json.loads(report.read_text()) == _report(iso)
    assert [dst for _src, dst in renames] == [str(report)]
    (src, _dst) = renames[0]
    assert src != str(report) and Path(src).parent == report.parent
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["report.json"]


# --- the plain cases ----------------------------------------------------------------------


def test_inner_boot_writes_the_report_and_opens_no_audit_run(
    iso: Path, tmp_path: Path, booted: list[tuple[Path, dict[str, Any]]]
) -> None:
    report = tmp_path / "report.json"
    done = _inner_boot(iso, report)
    assert done.exit_code == 0, done.output
    assert json.loads(report.read_text()) == _report(iso)
    ((path, kw),) = booted
    assert path == iso
    assert tuple(kw["firmwares"]) == ("bios", "uefi") and kw["cid"] == 57
    assert kw.get("screenshots") is None
    runs = tmp_path / "runs"
    assert not runs.exists() or list(runs.iterdir()) == []  # the outer run records it


def test_failed_checks_still_exit_0(
    iso: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict is the report's ``passed``; the exit status says the report is whole."""
    monkeypatch.setattr(vm, "boot_test", lambda i, **_k: _report(i, passed=False))
    report = tmp_path / "report.json"
    done = _inner_boot(iso, report)
    assert done.exit_code == 0, done.output
    assert json.loads(report.read_text())["passed"] is False


@pytest.mark.parametrize(("flag", "expected"), [("bios", ("bios",)), ("uefi", ("uefi",))])
def test_one_firmware(
    iso: Path,
    tmp_path: Path,
    booted: list[tuple[Path, dict[str, Any]]],
    flag: str,
    expected: tuple[str, ...],
) -> None:
    done = runner.invoke(
        app,
        [
            "vm",
            "inner-boot",
            str(iso),
            "--cid",
            "60",
            "--firmware",
            flag,
            "--report",
            str(tmp_path / "r.json"),
        ],
    )
    assert done.exit_code == 0, done.output
    assert tuple(booted[0][1]["firmwares"]) == expected and booted[0][1]["cid"] == 60


def test_screenshots_reach_the_boot_test(
    iso: Path, tmp_path: Path, booted: list[tuple[Path, dict[str, Any]]]
) -> None:
    shots = tmp_path / "shots"
    done = _inner_boot(iso, tmp_path / "r.json", "--screenshots", str(shots))
    assert done.exit_code == 0, done.output
    assert booted[0][1]["screenshots"] == shots


def test_inner_build_info_prints_the_isos_build_json(
    iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = {"arch": "v3", "flavor": "worker", "init": "systemd", "date": "20261013"}
    seen: list[Path] = []

    def read_build_info(path: Path) -> dict[str, Any]:
        seen.append(path)
        return info

    monkeypatch.setattr(vm, "read_build_info", read_build_info)
    done = runner.invoke(app, ["vm", "inner-build-info", str(iso)])
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout) == info
    assert seen == [iso]


def test_inner_build_info_fails_without_printing_json(
    iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read_build_info(path: Path) -> dict[str, Any]:
        raise vm.VmError(f"{path} has no bentoo/build.json")

    monkeypatch.setattr(vm, "read_build_info", read_build_info)
    done = runner.invoke(app, ["vm", "inner-build-info", str(iso)])
    assert done.exit_code != 0
    assert "has no bentoo/build.json" in done.output
    assert done.stdout.strip() == "" or not done.stdout.lstrip().startswith("{")


def test_the_inner_entries_are_hidden_but_exist() -> None:
    shown = runner.invoke(app, ["vm", "--help"])
    assert shown.exit_code == 0
    assert "inner-boot" not in shown.output and "inner-build-info" not in shown.output
    for command in ("inner-boot", "inner-build-info"):
        assert runner.invoke(app, ["vm", command, "--help"]).exit_code == 0, command
