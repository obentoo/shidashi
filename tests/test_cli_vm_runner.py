"""The three callers boot-test through the runner (story 024, task 3.2;
R1.4, R1.5, R1.6, R3.1, R4.1, Q9).

Contract (design.md, The three callers):

- ``assemble --boot-test``, the serial ``build --boot-test`` and ``vm test`` call
  ``vmrunner.boot_test_any`` (``vm test`` reads build info through
  ``vmrunner.build_info_any``); their CID handling and audit steps are unchanged -- the
  CID lock is held by the caller around the whole runner call, and the ``boot-test`` step
  records ``cid`` and ``passed``.
- ``assemble --boot-test`` and ``build --boot-test`` call ``vmrunner.preflight()`` BEFORE
  any build work: a missing runner fails in seconds with the error printed, exit 1.
- The README tells how a node without QEMU boot-tests (Q9).

The runner is faked at ``vmrunner``'s boundary; ``vm.boot_test`` and
``vm.read_build_info`` must not be reached directly any more.
"""

import errno
import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import shidashi
from shidashi import cli, schedule, vm, vmrunner
from shidashi.assembler import AssembleResult
from shidashi.cli import app

runner = CliRunner()

#: Every build is planned (story 014): plan on this host alone, never from the real
#: recipes' worlds nor the user's paired workers (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("host_only_planner")


def _report(iso: Path, *, passed: bool = True) -> dict[str, Any]:
    firmware = {
        "ssh_after_s": 11.0,
        "checks": [{"check": "os-release", "passed": passed, "got": "Bentoo", "expected": "x"}],
        "metrics": {"kernel": "6.17"},
        "passed": passed,
    }
    return {
        "iso": str(iso),
        "build": {"arch": "v3", "flavor": "minimal", "init": "systemd"},
        "firmwares": {"bios": firmware, "uefi": firmware},
        "passed": passed,
        "runner": "localhost/shidashi-vm-runner:0123456789abcdef",
    }


def _cid_held(cid: int) -> bool:
    """Whether another open file description holds CID ``cid``'s host lock."""
    path = vm.cid_lock_dir() / f"cid-{cid}.lock"
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return True
        raise
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class _Runner:
    """``vmrunner``'s boundary as a recorder; ``vm`` itself must not be reached."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.booted: list[dict[str, Any]] = []
        self.preflights = 0
        self.passed = True
        self.error: Exception | None = None
        self.preflight_error: Exception | None = None
        monkeypatch.setattr(vmrunner, "boot_test_any", self.boot_test_any)
        monkeypatch.setattr(vmrunner, "build_info_any", self.build_info_any)
        monkeypatch.setattr(vmrunner, "preflight", self.preflight)

        def direct(*_a: object, **_k: object) -> Any:
            raise AssertionError("vm reached directly, not through vmrunner")

        monkeypatch.setattr(vm, "boot_test", direct)
        monkeypatch.setattr(vm, "read_build_info", direct)

    def boot_test_any(self, iso: Path, **kw: Any) -> dict[str, Any]:
        cid = kw["cid"]
        self.booted.append({"iso": iso, **kw, "cid_held": _cid_held(cid)})
        if self.error is not None:
            raise self.error
        return _report(iso, passed=self.passed)

    def build_info_any(self, iso: Path, **_kw: Any) -> dict[str, Any]:
        return {"arch": "v3", "flavor": "minimal", "init": "systemd"}

    def preflight(self, *_a: object, **_k: object) -> None:
        self.preflights += 1
        if self.preflight_error is not None:
            raise self.preflight_error


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Factory and Assembler as recorders; cache, scratch and runs under tmp."""
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    calls: list[str] = []

    class FakeFactory:
        def __init__(self, recipe: Any, pkgdir: Path) -> None:
            self.recipe = recipe

        def build(self, **_k: object) -> None:
            calls.append(f"factory:{self.recipe.flavor}")

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            self.recipe = recipe
            calls.append(f"assembler:{recipe.flavor}:{recipe.init}")

        def assemble(self, output_dir: Path, **_kw: object) -> AssembleResult:
            calls.append(f"assemble:{self.recipe.flavor}")
            iso = output_dir / f"bentoo-x-{self.recipe.flavor}-{self.recipe.init}.iso"
            iso.parent.mkdir(parents=True, exist_ok=True)
            iso.write_bytes(b"ISO")
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    monkeypatch.setattr(cli, "Factory", FakeFactory)
    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    return calls


def _boot_steps(runs: Path, command: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for manifest in runs.glob("*/manifest.json"):
        data = json.loads(manifest.read_text())
        if data.get("command") == command:
            steps += [s for s in data["steps"] if str(s.get("step", "")).startswith("boot-test")]
    return steps


# --- hostile half: the preflight refuses only what needs the runner -----------------------


def _no_runner() -> Exception:
    return vmrunner.VmRunnerError(
        "cannot boot-test without QEMU: podman (missing); /dev/vhost-vsock (missing)"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["assemble", "v3", "minimal", "systemd", "-o", "out"],
        ["build", "v3", "systemd", "--images", "minimal", "--serial", "-o", "out"],
    ],
    ids=["assemble", "build"],
)
def test_without_boot_test_a_missing_runner_stops_nothing(
    lab: list[str], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """Wrong collapse: a build that boots nothing must not need the runner."""
    fake = _Runner(monkeypatch)
    fake.preflight_error = _no_runner()
    # build: the host's serial share (story 014); assemble ignores it
    done = runner.invoke(app, argv, env={schedule.SERIAL_SHARE_ENV: "1"})
    assert done.exit_code == 0, done.output
    assert "assemble:minimal" in lab
    assert fake.booted == []


@pytest.mark.parametrize(
    "argv",
    [
        ["assemble", "v3", "minimal", "systemd", "-o", "out", "--boot-test"],
        ["build", "v3", "systemd", "--images", "minimal", "--serial", "-o", "out", "--boot-test"],
    ],
    ids=["assemble", "build"],
)
def test_boot_test_without_a_runner_fails_before_any_build_work(
    lab: list[str], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """R3.1: seconds, not an hour of factory and assemble, then a refusal."""
    fake = _Runner(monkeypatch)
    fake.preflight_error = _no_runner()
    done = runner.invoke(app, argv)
    assert done.exit_code == 1
    assert "podman (missing)" in done.output and "/dev/vhost-vsock (missing)" in done.output
    assert lab == []
    assert fake.booted == []


# --- R1.6, R1.4: assemble and build boot-test through the runner ----------------------------


@pytest.mark.parametrize("init", ["systemd", "openrc"])
def test_assemble_boot_tests_through_the_runner_holding_its_cid(
    lab: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, init: str
) -> None:
    fake = _Runner(monkeypatch)
    done = runner.invoke(
        app, ["assemble", "v3", "minimal", init, "-o", "out", "--boot-test", "--cid", "44"]
    )
    assert done.exit_code == 0, done.output
    assert fake.preflights >= 1
    (boot,) = fake.booted
    assert boot["iso"].name == f"bentoo-x-minimal-{init}.iso"
    assert boot["cid"] == 44
    assert boot["cid_held"] is True  # the caller's lock spans the container run
    (step,) = _boot_steps(tmp_path / "runs", "assemble")
    assert step["cid"] == 44 and step["passed"] is True


def test_assemble_fails_naming_the_check_a_runner_boot_failed(
    lab: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _Runner(monkeypatch)
    fake.passed = False
    done = runner.invoke(app, ["assemble", "v3", "minimal", "systemd", "-o", "out", "--boot-test"])
    assert done.exit_code == 1
    assert "os-release" in done.output


def test_assemble_reports_a_runner_error(lab: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Runner(monkeypatch)
    fake.error = vmrunner.VmRunnerError("the runner container exited 125: Error: OCI runtime")
    done = runner.invoke(app, ["assemble", "v3", "minimal", "systemd", "-o", "out", "--boot-test"])
    assert done.exit_code == 1
    assert "exited 125" in done.output


def test_serial_build_boot_tests_every_iso_through_the_runner(
    lab: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _Runner(monkeypatch)
    done = runner.invoke(
        app,
        [
            "build",
            "v3",
            "systemd",
            "--images",
            "minimal,worker",
            "--skip-factory",
            "--serial",
            "-o",
            "out",
            "--boot-test",
        ],
        env={schedule.SERIAL_SHARE_ENV: "1"},  # the host's serial share (story 014)
    )
    assert done.exit_code == 0, done.output
    assert fake.preflights >= 1
    assert [b["iso"].name for b in fake.booted] == [
        "bentoo-x-minimal-systemd.iso",
        "bentoo-x-worker-systemd.iso",
    ]
    assert all(b["cid_held"] for b in fake.booted)
    steps = _boot_steps(tmp_path / "runs", "build")
    assert [s["step"] for s in steps] == ["boot-test:minimal", "boot-test:worker"]
    assert all("cid" in s and s["passed"] is True for s in steps)


# --- R1.5: vm test ------------------------------------------------------------------------


@pytest.fixture
def iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("COLUMNS", "500")
    path = tmp_path / "bentoo-x-minimal-systemd.iso"
    path.write_bytes(b"ISO")
    return path


def test_vm_test_prints_both_firmwares_from_the_runner(
    iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _Runner(monkeypatch)
    done = runner.invoke(app, ["vm", "test", str(iso), "--cid", "61"])
    assert done.exit_code == 0, done.output
    assert "boot test bios: PASSED" in done.output
    assert "boot test uefi: PASSED" in done.output
    (boot,) = fake.booted
    assert boot["iso"] == iso.resolve() and boot["cid"] == 61 and boot["cid_held"] is True
    assert tuple(boot["firmwares"]) == ("bios", "uefi")


def test_vm_test_exits_1_exactly_when_a_check_fails(
    iso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _Runner(monkeypatch)
    fake.passed = False
    done = runner.invoke(app, ["vm", "test", str(iso), "--cid", "61"])
    assert done.exit_code == 1
    assert "FAILED" in done.output and "os-release" in done.output


def test_vm_test_reports_a_runner_error(iso: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Runner(monkeypatch)
    fake.error = vmrunner.VmRunnerError("the runner container exited 127: exec failed")
    done = runner.invoke(app, ["vm", "test", str(iso), "--cid", "61"])
    assert done.exit_code == 1
    assert "exited 127" in done.output


# --- Q9: the README ------------------------------------------------------------------------


def test_the_readme_says_how_a_node_without_qemu_boot_tests() -> None:
    readme = Path(shidashi.__file__).resolve().parents[1] / "README.md"
    paragraphs = re.split(r"\n\s*\n", readme.read_text(encoding="utf-8"))
    about = [
        p
        for p in paragraphs
        if "qemu" in p.lower() and "toolbox" in p.lower() and "podman" in p.lower()
    ]
    assert about, "README: no paragraph on the boot test in the toolbox container"
    assert any("/dev/vhost-vsock" in p and "/dev/kvm" in p for p in about), about
