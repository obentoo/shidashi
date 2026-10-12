"""``assemble`` as a child of ``build`` (story 006, task 6.2).

Contract (design.md): ``assemble --result-file PATH`` (hidden) writes
``{image, exit_code, run_id, isos, duration_s}`` -- also when the assemble fails --
so the parent never parses a log; ``assemble --boot-test --cid N`` boot-tests its
own ISO (R4.1), a failed check failing the child (R4.2). ``--boot-test`` and ``--cid``
are user-facing; only ``--result-file`` is hidden. The CID is ALLOCATED: the child holds
``<scratch>/vm-cids/cid-<n>.lock`` (``vm.allocate_cid(base)``, wait=False) from ``N``
upward, so no two boot tests on the machine -- a build's children or a hand-run
``assemble --boot-test`` -- share a CID. The session directory follows the CID:
``test-<firmware>`` at the default CID 42 (as today), ``test-<cid>-<firmware>`` otherwise.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, vm
from shidashi.assembler import AssemblerError, AssembleResult
from shidashi.cli import app

runner = CliRunner()


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    return tmp_path


def _fake(monkeypatch: pytest.MonkeyPatch, outcome: Exception | None = None) -> None:
    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            if outcome is not None:
                raise outcome
            iso = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
            iso.parent.mkdir(parents=True, exist_ok=True)
            iso.write_bytes(b"ISO")
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    monkeypatch.setattr(cli, "Assembler", FakeAssembler)


def _run_id(lab: Path) -> str:
    (run_dir,) = (lab / "runs").iterdir()
    return run_dir.name


def test_result_file_records_a_successful_child(lab: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    result_file = lab / "scratch" / "logs" / "build-R-minimal.json"
    out = lab / "out"
    done = runner.invoke(
        app,
        ["assemble", "v3", "minimal", "systemd", "-o", str(out), "--result-file", str(result_file)],
    )
    assert done.exit_code == 0, done.output
    data = json.loads(result_file.read_text())
    assert data["image"] == "minimal"
    assert data["exit_code"] == 0
    assert data["run_id"] == _run_id(lab)
    assert data["isos"] == [str(out / "bentoo-x-minimal.iso")]
    assert isinstance(data["duration_s"], int | float) and data["duration_s"] >= 0


@pytest.mark.parametrize(
    "error",
    [
        AssemblerError("emerge failed"),
        subprocess.CalledProcessError(1, ["emerge"], stderr="!!! no binpkg"),
    ],
)
def test_result_file_records_a_failed_child(
    lab: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _fake(monkeypatch, error)
    result_file = lab / "result.json"
    done = runner.invoke(
        app, ["assemble", "v3", "minimal", "systemd", "--result-file", str(result_file)]
    )
    assert done.exit_code == 1
    data = json.loads(result_file.read_text())
    assert data["exit_code"] == 1  # never 0 for a failed assemble
    assert data["isos"] == []
    assert data["run_id"] == _run_id(lab)


def test_result_file_is_a_hidden_option() -> None:
    shown = runner.invoke(app, ["assemble", "--help"])
    assert shown.exit_code == 0
    assert "--result-file" not in shown.output


def _report(passed: bool) -> dict[str, object]:
    return {
        "passed": passed,
        "firmwares": {"uefi": {"checks": [{"check": "os-release", "passed": passed}]}},
    }


def test_boot_test_boots_the_childs_iso_on_its_cid(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(monkeypatch)
    booted: list[tuple[str, object]] = []

    def boot_test(iso: Path, **kw: object) -> dict[str, object]:
        booted.append((iso.name, kw.get("cid")))
        return _report(True)

    monkeypatch.setattr(vm, "boot_test", boot_test)
    result_file = lab / "result.json"
    done = runner.invoke(
        app,
        [
            "assemble",
            "v3",
            "minimal",
            "systemd",
            "-o",
            str(lab / "out"),
            "--boot-test",
            "--cid",
            "44",
            "--result-file",
            str(result_file),
        ],
    )
    assert done.exit_code == 0, done.output
    assert booted == [("bentoo-x-minimal.iso", 44)]
    assert json.loads(result_file.read_text())["exit_code"] == 0


def test_a_failed_boot_test_fails_the_child_naming_the_check(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(monkeypatch)
    monkeypatch.setattr(vm, "boot_test", lambda iso, **_k: _report(False))
    result_file = lab / "result.json"
    done = runner.invoke(
        app,
        [
            "assemble",
            "v3",
            "minimal",
            "systemd",
            "-o",
            str(lab / "out"),
            "--boot-test",
            "--cid",
            "43",
            "--result-file",
            str(result_file),
        ],
    )
    assert done.exit_code == 1
    assert "os-release" in done.output
    assert json.loads(result_file.read_text())["exit_code"] == 1


def test_without_boot_test_nothing_is_booted(lab: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)

    def boot_test(iso: Path, **_k: object) -> dict[str, object]:
        raise AssertionError("booted without --boot-test")

    monkeypatch.setattr(vm, "boot_test", boot_test)
    done = runner.invoke(app, ["assemble", "v3", "minimal", "systemd", "-o", str(lab / "out")])
    assert done.exit_code == 0, done.output


class _Session(vm.Session):
    directories: list[Path] = []

    def start(self) -> None:
        _Session.directories.append(self.directory)

    def run_command(self, command: str, *, timeout: int = 600) -> vm.GuestResult:
        return vm.GuestResult(command, 0, "", "", 0.1)

    def wait_ssh(self, *, timeout: float = 300, interval: float = 5) -> float:
        return 1.0

    def stop(self) -> None:
        pass


def test_boot_tests_on_two_cids_never_share_a_session_directory(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4.1: kde's and gnome's boot tests run at once, on CIDs 42 and 43; their VMs'
    work directories (qmp socket, pidfile, serial log) must be distinct."""
    monkeypatch.setattr(
        vm, "read_build_info", lambda iso: {"arch": "v3", "flavor": "minimal", "init": "systemd"}
    )
    _Session.directories = []
    vm.boot_test(Path("/out/bentoo-kde.iso"), cid=42, session_factory=_Session)
    kde = set(_Session.directories)
    _Session.directories = []
    vm.boot_test(Path("/out/bentoo-gnome.iso"), cid=43, session_factory=_Session)
    gnome = set(_Session.directories)
    assert len(kde) == 2 and len(gnome) == 2  # one per firmware
    assert kde.isdisjoint(gnome), (kde, gnome)


def test_the_default_cid_keeps_todays_session_names_another_cid_gets_its_own(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vm, "read_build_info", lambda iso: {"arch": "v3", "flavor": "minimal", "init": "systemd"}
    )
    _Session.directories = []
    vm.boot_test(Path("/out/bentoo-kde.iso"), cid=42, session_factory=_Session)
    assert sorted(d.name for d in _Session.directories) == ["test-bios", "test-uefi"]
    _Session.directories = []
    vm.boot_test(Path("/out/bentoo-kde.iso"), cid=143, session_factory=_Session)
    assert sorted(d.name for d in _Session.directories) == ["test-143-bios", "test-143-uefi"]


_HOLD_CID = """
import sys, time
from pathlib import Path
from shidashi import locks
with locks.hold(Path(sys.argv[1]), what="cid 44"):
    Path(sys.argv[2]).touch()
    time.sleep(60)
"""


def test_a_busy_cid_is_skipped_for_the_next_free_one(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4.1: another boot test holds CID 44 -> this child boots on 45, never on 44."""
    import sys
    import time

    _fake(monkeypatch)
    booted: list[object] = []

    def boot_test(iso: Path, **kw: object) -> dict[str, object]:
        booted.append(kw.get("cid"))
        return _report(True)

    monkeypatch.setattr(vm, "boot_test", boot_test)
    lock = lab / "scratch" / "vm-cids" / "cid-44.lock"
    lock.parent.mkdir(parents=True)
    ready = lab / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_CID, str(lock), str(ready)])
    try:
        for _ in range(500):
            if ready.exists():
                break
            time.sleep(0.02)
        assert ready.exists(), "the holder never took the CID lock"
        done = runner.invoke(
            app,
            [
                "assemble",
                "v3",
                "minimal",
                "systemd",
                "-o",
                str(lab / "out"),
                "--boot-test",
                "--cid",
                "44",
            ],
        )
    finally:
        holder.kill()
        holder.wait()
    assert done.exit_code == 0, done.output
    assert booted == [45]


def test_the_boot_test_is_a_step_of_the_childs_run_carrying_its_cid(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cluster reads which CID each boot test used from the assemble's own trail."""
    _fake(monkeypatch)
    monkeypatch.setattr(vm, "boot_test", lambda iso, **_k: _report(True))
    done = runner.invoke(
        app,
        [
            "assemble",
            "v3",
            "minimal",
            "systemd",
            "-o",
            str(lab / "out"),
            "--boot-test",
            "--cid",
            "150",
        ],
    )
    assert done.exit_code == 0, done.output
    (run_dir,) = (lab / "runs").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    steps = [s for s in manifest["steps"] if s["step"] == "boot-test"]
    assert len(steps) == 1 and steps[0]["cid"] == 150 and steps[0]["passed"] is True
