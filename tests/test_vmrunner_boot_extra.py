"""The boot test and the build info through the runner, beyond the authored tests
(story 024, task 2.3; R1.1, R1.4, R4.2, R4.3, R4.4).

The cases ``tests/test_vmrunner_boot.py`` leaves out:

- firmwares map to ``--firmware``: both in any order is ``both``; an empty or unknown
  firmware is a ``VmRunnerError`` before podman runs at all;
- ``build_info_any`` runs the preflight and imports an absent image first (it is the
  first call of ``vm test`` on a fresh node), and refuses without podman;
- an ``OSError`` of the report directory (a root-owned or broken scratch) and a missing
  ``podman`` binary are ``VmRunnerError`` -- the callers catch only ``vm.VmError``;
- SIGTERM while the container runs unwinds through the removal and leaves SIG_IGN, the
  previous handler NOT re-armed (task 2.4, author decision 2026-10-14) -- a stopped
  worker job or serial build leaves no container;
- the outer ``variants`` directory reaches the inner; the venv and the tree are bound
  read-only at the same path; the screenshots directory is created before the bind;
- a report that lacks a verdict is never read as one.
"""

import json
import os
import signal
import subprocess
import sys
from collections.abc import Generator, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from shidashi import audit, config, vmrunner
from tests.test_vmrunner_boot import (
    _CLI,
    _option,
    _options,
    _Podman,
    _removed,
    _report,
    _tag,
    _Trail,
    _writes,
)


@pytest.fixture
def iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A node without QEMU with podman, the devices and the runner tarball; the ISO."""
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(vmrunner, "native", lambda *_a, **_k: False)
    monkeypatch.setattr(vmrunner, "preflight", lambda *_a, **_k: None)
    tarball = vmrunner.runner_tarball()
    tarball.parent.mkdir(parents=True)
    tarball.write_bytes(b"toolbox")
    path = tmp_path / "isos" / "bentoo-20261013-worker-systemd-v3.iso"
    path.parent.mkdir()
    path.write_bytes(b"ISO")
    return path


def _no_podman(argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(2, "No such file or directory", "podman")


def _never(argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"podman ran: {list(argv)}")


class _Absent(_Podman):
    """The podman CLI where the runner image is absent until it is imported."""

    def __init__(self, tag: str, stdout: str) -> None:
        super().__init__(tag, lambda argv, _p: subprocess.CompletedProcess(argv, 0, stdout, ""))
        self.imported = False

    def __call__(self, argv: Sequence[str], **kw: object) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        if argv[1:3] == ["image", "exists"]:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0 if self.imported else 1, "", "")
        if argv[1] == "import":
            self.calls.append(argv)
            self.imported = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        return super().__call__(argv, **kw)


def _boot(iso: Path, runner: Any, **kw: Any) -> dict[str, Any]:
    kw.setdefault("cid", 57)
    result: dict[str, Any] = vmrunner.boot_test_any(iso, runner=runner, **kw)
    return result


# --- hostile half -----------------------------------------------------------------------


@pytest.mark.parametrize("firmwares", [(), ("efi",), ("bios", "seabios")], ids=str)
def test_an_empty_or_unknown_firmware_is_refused_before_podman_runs(
    iso: Path, firmwares: tuple[str, ...]
) -> None:
    with pytest.raises(vmrunner.VmRunnerError, match="firmware"):
        _boot(iso, _never, firmwares=firmwares)


def test_a_report_without_a_verdict_is_never_read_as_one(iso: Path) -> None:
    """R4.5: a parseable report that lacks ``passed`` is not a complete report."""
    incomplete = {k: v for k, v in _report(iso).items() if k != "passed"}
    podman = _Podman(_tag(), _writes(incomplete))
    trail = _Trail()
    with pytest.raises(vmrunner.VmRunnerError, match="rc 0"), audit.recording(trail):
        _boot(iso, podman)
    assert trail.attached == []
    assert _removed(podman, "shidashi-vm-boot-57")


def test_an_unwritable_report_directory_is_a_runner_error(iso: Path) -> None:
    """A scratch that is not a directory (or is root-owned): named, before podman runs."""
    config.scratch_dir().write_text("not a directory")
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        _boot(iso, _Podman(_tag(), _writes(_report(iso))))
    assert str(config.scratch_dir()) in str(raised.value)


def test_a_missing_podman_binary_is_a_runner_error_for_the_boot_test(iso: Path) -> None:
    with pytest.raises(vmrunner.VmRunnerError, match="podman"):
        _boot(iso, _no_podman)


def test_a_missing_podman_binary_is_a_runner_error_for_the_build_info(iso: Path) -> None:
    with pytest.raises(vmrunner.VmRunnerError, match="podman"):
        vmrunner.build_info_any(iso, runner=_no_podman)


@pytest.mark.parametrize("call", ["boot", "info"])
def test_a_failing_preflight_refuses_before_podman_runs(
    iso: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    def preflight(*_a: object, **_k: object) -> None:
        raise vmrunner.VmRunnerError("podman: emerge app-containers/podman")

    monkeypatch.setattr(vmrunner, "preflight", preflight)
    with pytest.raises(vmrunner.VmRunnerError, match="emerge app-containers/podman"):
        if call == "boot":
            _boot(iso, _never)
        else:
            vmrunner.build_info_any(iso, runner=_never)


class _Unhandled(Exception):
    """What the previous SIGTERM handler raises: proof it was not the one in force."""


@pytest.fixture
def previous_sigterm() -> Generator[Any]:
    def handler(signum: int, frame: FrameType | None) -> None:
        raise _Unhandled(signum)

    before = signal.signal(signal.SIGTERM, handler)
    try:
        yield handler
    finally:
        signal.signal(signal.SIGTERM, before)


def _terminated(stdout: str = "") -> Any:
    def inner(argv: list[str], _path: Path | None) -> subprocess.CompletedProcess[str]:
        os.kill(os.getpid(), signal.SIGTERM)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return inner


def test_sigterm_while_the_boot_test_runs_removes_the_container(
    iso: Path, previous_sigterm: Any
) -> None:
    podman = _Podman(_tag(), _terminated())
    with pytest.raises(SystemExit) as raised:
        _boot(iso, podman, cid=57)
    assert raised.value.code == 128 + signal.SIGTERM
    assert _removed(podman, "shidashi-vm-boot-57")
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN  # fired: not re-armed


def test_sigterm_while_the_build_info_runs_removes_the_container(
    iso: Path, previous_sigterm: Any
) -> None:
    podman = _Podman(_tag(), _terminated("{}"))
    with pytest.raises(SystemExit):
        vmrunner.build_info_any(iso, runner=podman)
    (run,) = podman.runs()
    (name,) = _options(run, "--name")
    assert _removed(podman, name)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN  # fired: not re-armed


# --- what must still pass ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("firmwares", "flag"),
    [(("uefi", "bios"), "both"), (["bios", "uefi"], "both"), (("uefi", "uefi"), "uefi")],
    ids=str,
)
def test_firmwares_map_to_the_inner_flag_in_any_order(
    iso: Path, firmwares: Sequence[str], flag: str
) -> None:
    podman = _Podman(_tag(), _writes(_report(iso)))
    _boot(iso, podman, firmwares=firmwares)
    (run,) = podman.runs()
    assert _option(run[run.index(_CLI) + 1 :], "--firmware") == flag


def test_build_info_imports_an_absent_runner_image_first(iso: Path) -> None:
    """``vm test`` reads the build info FIRST: on a fresh node it is the import (R2.1)."""
    info = {"arch": "v3", "flavor": "worker", "init": "systemd"}
    podman = _Absent(_tag(), json.dumps(info))
    assert vmrunner.build_info_any(iso, runner=podman) == info
    verbs = [c[1] for c in podman.calls]
    assert verbs.index("import") < verbs.index("run")
    (imported,) = [c for c in podman.calls if c[1] == "import"]
    assert imported[-2:] == [str(vmrunner.runner_tarball()), _tag()]
    assert _tag() in podman.runs()[0]


def test_build_info_that_is_not_an_object_is_a_runner_error(iso: Path) -> None:
    podman = _Podman(_tag(), lambda argv, _p: subprocess.CompletedProcess(argv, 0, "[]", ""))
    with pytest.raises(vmrunner.VmRunnerError):
        vmrunner.build_info_any(iso, runner=podman)


def test_the_outer_variants_reach_the_inner_read_only(
    iso: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants = tmp_path / "variants"
    variants.symlink_to(config.variants_dir(), target_is_directory=True)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))
    podman = _Podman(_tag(), _writes(_report(iso)))
    _boot(iso, podman)
    (run,) = podman.runs()
    assert f"{variants}:{variants}:ro" in _options(run, "-v", "--volume")
    assert f"SHIDASHI_VARIANTS_DIR={variants}" in _options(run, "-e", "--env")


def test_the_venv_and_the_tree_are_bound_read_only_at_the_same_path(iso: Path) -> None:
    podman = _Podman(_tag(), _writes(_report(iso)))
    _boot(iso, podman)
    (run,) = podman.runs()
    venv = Path(sys.prefix)
    tree = Path(vmrunner.__file__).resolve().parent.parent
    binds = _options(run, "-v", "--volume")
    assert f"{venv}:{venv}:ro" in binds
    assert f"{tree}:{tree}:ro" in binds
    assert run[run.index(_CLI) - 2] == str(venv / "bin" / "python")


def test_a_missing_screenshots_directory_is_created_before_the_bind(
    iso: Path, tmp_path: Path
) -> None:
    shots = tmp_path / "later" / "shots"
    podman = _Podman(_tag(), _writes(_report(iso)))
    _boot(iso, podman, screenshots=shots)
    assert shots.is_dir()
    (run,) = podman.runs()
    assert f"{shots}:{shots}" in _options(run, "-v", "--volume")
