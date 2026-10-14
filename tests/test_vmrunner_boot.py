"""The boot test and the build info through the runner (story 024, task 2.3;
R1.1, R1.2, R1.3, R1.4, R1.7, R4.2, R4.3, R4.4, R4.5).

Contract (design.md):

- ``boot_test_any(iso, *, firmwares, cid, screenshots=None, runner=subprocess.run)``:
  native (QEMU on PATH) is ``vm.boot_test`` unchanged, no container. Otherwise the runner
  image is ensured and ``vm inner-boot ISO --cid N --firmware bios|uefi|both --report F``
  runs in it (``F`` under scratch: ``<scratch>/vm/runner-<cid>/report.json``); the report
  is read back with the native shape, gains ``runner: <tag>`` and is attached to the run
  in force as ``boot-test``.
- A report ``{"error": ...}`` is an error, never a verdict; no complete report is an
  error naming the exit status and the end of stderr -- a stale or half report is never
  read as a verdict.
- ``podman rm -f`` of the container ALWAYS runs afterwards, its own failure ignored --
  passed, failed, errored or interrupted.
- ``build_info_any(iso, *, runner=subprocess.run)``: native ``vm.read_build_info``;
  otherwise ``vm inner-build-info ISO`` in the runner, JSON on stdout.

podman is a fake ``runner`` that plays the container: it writes the report the inner
entry would write. ``native`` and ``preflight`` are stubbed to describe a node without
QEMU that has every runner piece; the image check goes through the real
``ensure_image`` against the fake.
"""

import contextlib
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, config, vm, vmrunner

_CLI = "from shidashi.cli import app; app()"


def _firmware(passed: bool = True) -> dict[str, Any]:
    return {
        "ssh_after_s": 12.5,
        "checks": [
            {"check": "os-release", "passed": passed, "got": "Bentoo", "expected": "Bentoo"}
        ],
        "metrics": {"kernel": "6.17.1-bentoo"},
        "passed": passed,
    }


def _report(iso: Path, *, passed: bool = True) -> dict[str, Any]:
    return {
        "iso": str(iso),
        "build": {"arch": "v3", "flavor": "worker", "init": "systemd"},
        "firmwares": {"bios": _firmware(passed), "uefi": _firmware(passed)},
        "passed": passed,
    }


class _Trail(audit.Recorder):
    def __init__(self) -> None:
        self.attached: list[tuple[str, Any]] = []

    def attach(self, name: str, data: Any) -> Path | None:
        self.attached.append((name, json.loads(json.dumps(data))))
        return None


Inner = Callable[[list[str], Path | None], subprocess.CompletedProcess[str]]


class _Podman:
    """The podman CLI with the runner image present; ``podman run`` calls ``inner``."""

    def __init__(self, tag: str, inner: Inner) -> None:
        self.tag = tag
        self.inner = inner
        self.calls: list[list[str]] = []
        self.rm_rc = 0
        self.run_error: BaseException | None = None

    def __call__(self, argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        assert argv[0] == "podman", argv
        if argv[1:3] == ["image", "exists"]:
            return subprocess.CompletedProcess(argv, 0 if argv[3] == self.tag else 1, "", "")
        if argv[1] == "run":
            if self.run_error is not None:
                raise self.run_error
            inner = argv[argv.index(_CLI) + 1 :]
            report = _option(inner, "--report")
            return self.inner(inner, None if report is None else Path(report))
        if argv[1] == "rm":
            return subprocess.CompletedProcess(argv, self.rm_rc, "", "Error: no such container")
        raise AssertionError(f"unexpected podman call: {argv}")

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "run"]

    def removals(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "rm"]


def _option(argv: Sequence[str], name: str) -> str | None:
    for i, token in enumerate(argv):
        if token == name and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return None


def _options(argv: list[str], *names: str) -> list[str]:
    """Every value of the options ``names`` before the image tag."""
    found: list[str] = []
    for i, token in enumerate(argv):
        if token in names and i + 1 < len(argv):
            found.append(argv[i + 1])
        for name in names:
            if name.startswith("--") and token.startswith(name + "="):
                found.append(token.split("=", 1)[1])
    return found


def _writes(report: dict[str, Any] | str | None, rc: int = 0, stderr: str = "") -> Inner:
    def inner(argv: list[str], path: Path | None) -> subprocess.CompletedProcess[str]:
        if report is not None:
            assert path is not None, argv
            path.write_text(report if isinstance(report, str) else json.dumps(report))
        return subprocess.CompletedProcess(argv, rc, "", stderr)

    return inner


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A node without QEMU with podman, the devices and the runner tarball; the ISO."""
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(vmrunner, "native", lambda *_a, **_k: False)
    monkeypatch.setattr(vmrunner, "preflight", lambda *_a, **_k: None)
    tarball = vmrunner.runner_tarball()
    tarball.parent.mkdir(parents=True)
    tarball.write_bytes(b"toolbox")
    iso = tmp_path / "isos" / "bentoo-20261013-worker-systemd-v3.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"ISO")
    return iso


def _tag() -> str:
    return vmrunner.image_tag(vmrunner.runner_tarball())


def _boot(
    iso: Path,
    podman: _Podman,
    *,
    cid: int = 57,
    firmwares: Sequence[str] = ("bios", "uefi"),
    screenshots: Path | None = None,
) -> tuple[dict[str, Any], _Trail]:
    trail = _Trail()
    with audit.recording(trail):
        report = vmrunner.boot_test_any(
            iso, firmwares=firmwares, cid=cid, screenshots=screenshots, runner=podman
        )
    return report, trail


# --- hostile half: what must never be read as a verdict -----------------------------------


def test_a_stale_report_of_an_earlier_run_is_never_the_verdict(node: Path) -> None:
    """R4.5, wrong collapse: an earlier run's passing report sits where this run writes
    its own; this container ends without writing -- the old report is not its verdict."""
    stale = config.scratch_dir() / "vm" / "runner-57" / "report.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(_report(node)))
    podman = _Podman(_tag(), _writes(None, rc=125, stderr="Error: OCI runtime error\n"))
    trail = _Trail()
    with pytest.raises(vmrunner.VmRunnerError) as raised, audit.recording(trail):
        vmrunner.boot_test_any(node, firmwares=("bios", "uefi"), cid=57, runner=podman)
    assert "125" in str(raised.value)
    assert trail.attached == []


def test_an_inner_error_is_an_error_not_a_failed_check(node: Path) -> None:
    """R4.4, wrong collapse: a broken boot test never becomes a "failed" verdict."""
    text = "VmError: ssh never answered on vsock CID 57 after 300 s"
    podman = _Podman(_tag(), _writes({"error": text}, rc=2))
    trail = _Trail()
    with (
        pytest.raises(vmrunner.VmRunnerError, match="ssh never answered") as raised,
        audit.recording(trail),
    ):
        vmrunner.boot_test_any(node, firmwares=("bios", "uefi"), cid=57, runner=podman)
    assert text in str(raised.value)
    assert trail.attached == []


def test_a_half_written_report_is_never_the_verdict(node: Path) -> None:
    """R4.5: a container killed mid-write leaves no verdict, only an error."""
    podman = _Podman(_tag(), _writes('{"iso": "/x", "firmwares": {"bios": {"pass', rc=137))
    with pytest.raises(vmrunner.VmRunnerError, match="137"):
        _boot(node, podman)


@pytest.mark.parametrize(
    "stderr",
    [
        "HEAD-OF-STDERR\n" + "x" * 3000 + "\nError: crun: executable file not found\n",
        "Error: crun: executable file `/opt/venv/bin/python` not found: No such file\n",
    ],
)
def test_no_report_names_the_exit_status_and_the_end_of_stderr(node: Path, stderr: str) -> None:
    """R4.5 (and the venv-interpreter GOTCHA): the reason surfaces, never a bare rc."""
    podman = _Podman(_tag(), _writes(None, rc=127, stderr=stderr))
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        _boot(node, podman)
    message = str(raised.value)
    assert "127" in message
    assert stderr.rstrip()[-30:] in message
    assert "HEAD-OF-STDERR" not in message  # the end of it, not 3 KB of it


def test_a_clean_exit_without_a_report_is_an_error(node: Path) -> None:
    podman = _Podman(_tag(), _writes(None, rc=0))
    with pytest.raises(vmrunner.VmRunnerError):
        _boot(node, podman)


# --- R1.3: the native path ----------------------------------------------------------------


def test_a_node_with_qemu_runs_natively_without_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vmrunner, "native", lambda *_a, **_k: True)
    seen: list[tuple[Path, dict[str, object]]] = []
    native = _report(tmp_path / "a.iso")

    def boot_test(iso: Path, **kw: object) -> dict[str, Any]:
        seen.append((iso, kw))
        return native

    def no_podman(argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"a container on a native node: {argv}")

    monkeypatch.setattr(vm, "boot_test", boot_test)
    shots = tmp_path / "shots"
    report = vmrunner.boot_test_any(
        tmp_path / "a.iso", firmwares=("uefi",), cid=44, screenshots=shots, runner=no_podman
    )
    assert report == native and "runner" not in report
    assert seen == [(tmp_path / "a.iso", {"firmwares": ("uefi",), "cid": 44, "screenshots": shots})]


def test_build_info_natively_reads_the_iso_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vmrunner, "native", lambda *_a, **_k: True)
    monkeypatch.setattr(vm, "read_build_info", lambda iso: {"arch": "v3", "iso": str(iso)})

    def no_podman(argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"a container on a native node: {argv}")

    info = vmrunner.build_info_any(tmp_path / "a.iso", runner=no_podman)
    assert info == {"arch": "v3", "iso": str(tmp_path / "a.iso")}


# --- R1.1, R1.2, R1.7: the container path -------------------------------------------------


def test_the_report_has_the_native_shape_and_names_the_runner(node: Path) -> None:
    podman = _Podman(_tag(), _writes(_report(node)))
    report, trail = _boot(node, podman)
    assert report == {**_report(node), "runner": _tag()}
    for firmware in report["firmwares"].values():
        assert {"ssh_after_s", "checks", "metrics", "passed"} <= firmware.keys()
    assert trail.attached == [("boot-test", report)]


def test_a_failed_check_is_a_verdict_as_natively(node: Path) -> None:
    podman = _Podman(_tag(), _writes(_report(node, passed=False)))
    report, trail = _boot(node, podman)
    assert report["passed"] is False
    assert trail.attached == [("boot-test", report)]


@pytest.mark.parametrize(
    ("firmwares", "flag"),
    [(("bios", "uefi"), "both"), (("bios",), "bios"), (("uefi",), "uefi")],
)
def test_the_inner_entry_boots_the_iso_on_the_requested_firmwares_and_cid(
    node: Path, firmwares: tuple[str, ...], flag: str
) -> None:
    podman = _Podman(_tag(), _writes(_report(node)))
    _boot(node, podman, cid=61, firmwares=firmwares)
    (run,) = podman.runs()
    inner = run[run.index(_CLI) + 1 :]
    assert inner[:3] == ["vm", "inner-boot", str(node)]
    assert _option(inner, "--cid") == "61"
    assert _option(inner, "--firmware") == flag
    report_path = _option(inner, "--report")
    assert report_path is not None
    assert Path(report_path).is_relative_to(config.scratch_dir())


def test_one_runner_image_for_every_init(node: Path) -> None:
    """R1.4: an openrc ISO boots in the same image as a systemd one."""
    openrc = node.with_name("bentoo-20261013-minimal-openrc-v3.iso")
    openrc.write_bytes(b"ISO")

    def own_report(argv: list[str], path: Path | None) -> subprocess.CompletedProcess[str]:
        # each boot reports its own ISO: a report of another is refused (task 2.4)
        return _writes(_report(Path(argv[2])))(argv, path)

    podman = _Podman(_tag(), own_report)
    _boot(node, podman, cid=57)
    _boot(openrc, podman, cid=58)
    assert [r.count(_tag()) for r in podman.runs()] == [1, 1]


def test_two_cids_are_two_containers(node: Path) -> None:
    """R4.1: concurrent boot tests never share a container name."""
    podman = _Podman(_tag(), _writes(_report(node)))
    _boot(node, podman, cid=57)
    _boot(node, podman, cid=58)
    names = [_options(r, "--name") for r in podman.runs()]
    assert names == [["shidashi-vm-boot-57"], ["shidashi-vm-boot-58"]]


# --- R4.3: what the container may write -------------------------------------------------


def _writable(run: list[str]) -> set[str]:
    binds = [v.split(":") for v in _options(run, "-v", "--volume")]
    return {b[0] for b in binds if len(b) < 3 or "ro" not in b[2].split(",")}


def test_the_iso_is_read_only_and_only_scratch_is_writable(node: Path) -> None:
    podman = _Podman(_tag(), _writes(_report(node)))
    _boot(node, podman)
    (run,) = podman.runs()
    assert _writable(run) == {str(config.scratch_dir())}
    assert f"{node.parent}:{node.parent}:ro" in _options(run, "-v", "--volume")
    assert "--privileged" not in run


def test_a_screenshots_directory_is_the_one_other_writable_bind(node: Path, tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    podman = _Podman(_tag(), _writes(_report(node)))
    _boot(node, podman, screenshots=shots)
    (run,) = podman.runs()
    assert _writable(run) == {str(config.scratch_dir()), str(shots)}
    inner = run[run.index(_CLI) + 1 :]
    assert _option(inner, "--screenshots") == str(shots)


# --- R4.2: no container outlives the boot test ------------------------------------------


def _removed(podman: _Podman, name: str) -> bool:
    return any(name in c and ("-f" in c or "--force" in c) for c in podman.removals())


@pytest.mark.parametrize(
    "inner",
    [
        _writes({"iso": "", "build": {}, "firmwares": {}, "passed": True}),
        _writes({"error": "boom"}, rc=2),
        _writes(None, rc=125, stderr="Error: OCI runtime error"),
    ],
    ids=["passed", "errored", "no-report"],
)
def test_the_container_is_removed_however_the_boot_test_ends(node: Path, inner: Inner) -> None:
    podman = _Podman(_tag(), inner)
    with contextlib.suppress(vmrunner.VmRunnerError):
        _boot(node, podman, cid=57)
    assert podman.calls.index(podman.runs()[0]) < podman.calls.index(podman.removals()[-1])
    assert _removed(podman, "shidashi-vm-boot-57")


def test_the_container_is_removed_when_the_boot_test_is_interrupted(node: Path) -> None:
    podman = _Podman(_tag(), _writes(_report(node)))
    podman.run_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        _boot(node, podman, cid=57)
    assert _removed(podman, "shidashi-vm-boot-57")


def test_a_failed_removal_does_not_hide_the_verdict(node: Path) -> None:
    podman = _Podman(_tag(), _writes(_report(node)))
    podman.rm_rc = 1
    report, _ = _boot(node, podman)
    assert report["passed"] is True


# --- build_info_any in the container --------------------------------------------------------


def _prints(stdout: str, rc: int = 0, stderr: str = "") -> Inner:
    def inner(argv: list[str], _path: Path | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    return inner


def test_build_info_is_read_in_the_runner(node: Path) -> None:
    info = {"arch": "v3", "flavor": "worker", "init": "systemd"}
    podman = _Podman(_tag(), _prints(json.dumps(info) + "\n"))
    assert vmrunner.build_info_any(node, runner=podman) == info
    (run,) = podman.runs()
    assert run[run.index(_CLI) + 1 :] == ["vm", "inner-build-info", str(node)]
    (name,) = _options(run, "--name")
    assert name.startswith("shidashi-vm-info-")
    assert f"{node.parent}:{node.parent}:ro" in _options(run, "-v", "--volume")
    assert _removed(podman, name)


@pytest.mark.parametrize(
    ("stdout", "rc", "stderr"),
    [
        ("", 1, "error: /isos/x.iso has no bentoo/build.json"),
        ('{"arch": "v3", "fla', 0, "partial"),
    ],
    ids=["failed", "unparseable"],
)
def test_build_info_failures_are_runner_errors(
    node: Path, stdout: str, rc: int, stderr: str
) -> None:
    podman = _Podman(_tag(), _prints(stdout, rc, stderr))
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.build_info_any(node, runner=podman)
    if rc != 0:
        assert "has no bentoo/build.json" in str(raised.value)
    assert podman.removals()
