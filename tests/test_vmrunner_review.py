"""The tech review fixes of the runner (story 024, task 2.4; R1.1, R2.3, R4.2, R4.3, R4.5).

- seccomp: podman's default profile denies ``socket(AF_VSOCK)`` (family 40) and the boot
  test's ssh rides on vsock. The runner builds a minimal profile from the node's default
  -- that ONE rule removed, every other kept -- writes it under scratch and passes
  ``--security-opt seccomp=<file>``; a missing, unparseable or ruleless default profile is
  a ``VmRunnerError`` naming it, never ``unconfined``;
- SIGTERM never cuts the container's removal: one arriving while ``podman rm`` runs waits
  until the removal is done (then unwinds, if the run had not unwound already);
- a same-name container (the orphan of a SIGKILLed run: the caller holds the CID) is
  removed BEFORE the run, killed at once; a report is the verdict only with rc 0;
- binds are deduplicated -- podman refuses a duplicate destination (rc 125);
- the interpreter must be a venv's -- binding a system ``sys.prefix`` (``/usr``) would
  hide the image's own;
- ``podman import`` stages under ``TMPDIR=<scratch>``, not ``/var/tmp`` (a RAM root on
  the worker);
- ``podman rm -i``; podman's output decoded with ``errors="replace"``;
- ``native()`` looks ``shutil.which`` up when called.
"""

import copy
import json
import os
import shutil
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
    _option,
    _options,
    _Podman,
    _report,
    _tag,
    _Trail,
    _writes,
)

_HOST_PROFILE = Path("/usr/share/containers/seccomp.json")

# --- a default profile shaped like podman's (containers-common) -------------------------


def _arg(index: int, value: int, op: str) -> dict[str, Any]:
    return {"index": index, "value": value, "valueTwo": 0, "op": op}


#: The one rule the runner removes: socket(AF_VSOCK) -> EPERM.
_VSOCK_DENY: dict[str, Any] = {
    "names": ["socket"],
    "action": "SCMP_ACT_ERRNO",
    "args": [_arg(0, 40, "SCMP_CMP_EQ")],
    "comment": "",
    "includes": {},
    "excludes": {},
    "errnoRet": 1,
    "errno": "EPERM",
}

#: Every other rule must survive, including the ones that look like it.
_KEPT: list[dict[str, Any]] = [
    {"names": ["accept", "read", "write"], "action": "SCMP_ACT_ALLOW", "args": []},
    {
        "names": ["socket"],
        "action": "SCMP_ACT_ERRNO",
        "args": [_arg(0, 16, "SCMP_CMP_EQ"), _arg(2, 9, "SCMP_CMP_EQ")],
        "excludes": {"caps": ["CAP_AUDIT_WRITE"]},
        "errnoRet": 22,
        "errno": "EINVAL",
    },
    {
        "names": ["socket"],
        "action": "SCMP_ACT_ALLOW",
        "args": [_arg(0, 40, "SCMP_CMP_NE")],
        "includes": {"caps": ["CAP_AUDIT_WRITE"]},
    },
    # hostile look-alikes: the same argument on another syscall, another family
    {"names": ["bind"], "action": "SCMP_ACT_ERRNO", "args": [_arg(0, 40, "SCMP_CMP_EQ")]},
    {"names": ["socket"], "action": "SCMP_ACT_ERRNO", "args": [_arg(0, 41, "SCMP_CMP_EQ")]},
]


def _profile(*, deny: bool = True) -> dict[str, Any]:
    rules = [*_KEPT[:2], _VSOCK_DENY, *_KEPT[2:]] if deny else list(_KEPT)
    return {
        "defaultAction": "SCMP_ACT_ERRNO",
        "defaultErrno": "ENOSYS",
        "defaultErrnoRet": 38,
        "archMap": [{"architecture": "SCMP_ARCH_X86_64", "subArchitectures": ["SCMP_ARCH_X86"]}],
        "syscalls": copy.deepcopy(rules),
    }


@pytest.fixture
def default_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The node's default seccomp profile, a fake of podman's."""
    path = tmp_path / "containers" / "seccomp.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_profile()))
    monkeypatch.setattr(vmrunner, "_DEFAULT_SECCOMP", path)
    return path


@pytest.fixture
def iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_profile: Path) -> Path:
    """A node without QEMU with podman, the devices and the runner tarball; the ISO."""
    del default_profile
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


def _never(argv: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"podman ran: {list(argv)}")


def _boot(iso: Path, runner: Any, **kw: Any) -> dict[str, Any]:
    kw.setdefault("cid", 57)
    trail = kw.pop("trail", None) or _Trail()
    with audit.recording(trail):
        result: dict[str, Any] = vmrunner.boot_test_any(iso, runner=runner, **kw)
    return result


_FIXED_TAG = "localhost/shidashi-vm-runner:0123456789abcdef"


def _run_argv(tmp_path: Path, **kw: Any) -> list[str]:
    args: dict[str, Any] = {
        "name": "boot-57",
        "inner_args": ["vm", "inner-boot"],
        "venv": tmp_path / "venv",
        "tree": tmp_path / "src",
        "mounts_ro": [tmp_path / "isos"],
        "scratch": tmp_path / "scratch",
    }
    args.update(kw)
    return vmrunner.run_argv(_FIXED_TAG, **args)


def _head(argv: list[str], tag: str = _FIXED_TAG) -> list[str]:
    """The options before the image tag."""
    return argv[: argv.index(tag)]


# --- seccomp: the minimal profile ----------------------------------------------------------


def test_run_argv_carries_the_seccomp_profile_when_given(tmp_path: Path) -> None:
    profile = tmp_path / "scratch" / "vm" / "runner-seccomp.json"
    head = _head(_run_argv(tmp_path, seccomp=profile))
    assert _option(head, "--security-opt") == f"seccomp={profile}"


def test_run_argv_adds_no_security_option_without_a_profile(tmp_path: Path) -> None:
    assert "--security-opt" not in _run_argv(tmp_path)


def test_the_minimal_profile_drops_only_the_vsock_rule(
    tmp_path: Path, default_profile: Path
) -> None:
    """Hostile, wrong collapse: the look-alike rules (another family, another syscall,
    the CAP_AUDIT_WRITE allow) stay; every top-level setting stays."""
    path = vmrunner.seccomp_profile(tmp_path / "scratch")
    assert path == tmp_path / "scratch" / "vm" / "runner-seccomp.json"
    assert json.loads(path.read_text()) == _profile(deny=False)
    assert json.loads(default_profile.read_text()) == _profile()  # the default untouched


def test_the_minimal_profile_replaces_a_stale_one(tmp_path: Path, default_profile: Path) -> None:
    del default_profile
    stale = tmp_path / "scratch" / "vm" / "runner-seccomp.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    vmrunner.seccomp_profile(tmp_path / "scratch")
    assert json.loads(stale.read_text()) == _profile(deny=False)
    assert [p.name for p in stale.parent.iterdir()] == ["runner-seccomp.json"]  # no temp left


@pytest.mark.skipif(not _HOST_PROFILE.is_file(), reason=f"no {_HOST_PROFILE} on this machine")
def test_the_hosts_own_default_profile_loses_exactly_one_rule(tmp_path: Path) -> None:
    default = json.loads(_HOST_PROFILE.read_text())
    scratch = tmp_path / "scratch"
    minimal = json.loads(vmrunner.seccomp_profile(scratch, default=_HOST_PROFILE).read_text())
    removed = [r for r in default["syscalls"] if r not in minimal["syscalls"]]
    assert len(minimal["syscalls"]) == len(default["syscalls"]) - 1
    (rule,) = removed
    assert rule["names"] == ["socket"] and rule["action"] == "SCMP_ACT_ERRNO"
    assert [(a["index"], a["value"], a["op"]) for a in rule["args"]] == [(0, 40, "SCMP_CMP_EQ")]
    assert {k: v for k, v in minimal.items() if k != "syscalls"} == {
        k: v for k, v in default.items() if k != "syscalls"
    }


@pytest.mark.parametrize(
    "content",
    [None, '{"syscalls": [', "[]", json.dumps(_profile(deny=False)), '{"syscalls": 7}'],
    ids=["missing", "bad-json", "not-an-object", "no-vsock-rule", "syscalls-not-a-list"],
)
def test_a_default_profile_without_the_rule_is_refused_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    """Never a silent ``unconfined``: the error names the file."""
    path = tmp_path / "seccomp.json"
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(vmrunner, "_DEFAULT_SECCOMP", path)
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.seccomp_profile(tmp_path / "scratch")
    assert str(path) in str(raised.value)
    assert not (tmp_path / "scratch" / "vm" / "runner-seccomp.json").exists()


def test_an_unwritable_profile_is_a_runner_error_naming_it(
    tmp_path: Path, default_profile: Path
) -> None:
    del default_profile
    scratch = tmp_path / "scratch"
    scratch.write_text("not a directory")
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.seccomp_profile(scratch)
    assert str(scratch) in str(raised.value)


@pytest.mark.parametrize("call", ["boot", "info"])
def test_both_entries_run_under_the_minimal_profile(iso: Path, call: str) -> None:
    podman = _Podman(_tag(), _writes(_report(iso)))
    if call == "boot":
        _boot(iso, podman)
    else:
        podman.inner = lambda argv, _p: subprocess.CompletedProcess(argv, 0, "{}", "")
        vmrunner.build_info_any(iso, runner=podman)
    (run,) = podman.runs()
    profile = config.scratch_dir() / "vm" / "runner-seccomp.json"
    assert _options(_head(run, _tag()), "--security-opt") == [f"seccomp={profile}"]
    assert json.loads(profile.read_text()) == _profile(deny=False)


def test_a_missing_default_profile_refuses_before_podman_runs(
    iso: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    podman = _Podman(_tag(), _writes(_report(iso)))
    monkeypatch.setattr(vmrunner, "_DEFAULT_SECCOMP", tmp_path / "nowhere.json")
    with pytest.raises(vmrunner.VmRunnerError, match="nowhere.json"):
        _boot(iso, podman)
    assert podman.runs() == []


# --- SIGTERM never cuts the removal ---------------------------------------------------------


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


class _TermDuringRm(_Podman):
    """``podman rm`` receives SIGTERM mid-way; records whether it ran to its end."""

    def __init__(self, tag: str, inner: Any, *, at: int) -> None:
        super().__init__(tag, inner)
        self.at = at  # which removal is signalled: 0 the one before the run, -1 after
        self.finished: list[bool] = []

    def __call__(self, argv: Sequence[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if argv[1] != "rm":
            return super().__call__(argv, **kw)
        index = len(self.finished)
        self.finished.append(False)
        signalled = index == self.at or (self.at == -1 and self.runs())
        if signalled:
            os.kill(os.getpid(), signal.SIGTERM)
        done = super().__call__(argv, **kw)
        self.finished[index] = True
        return done


@pytest.mark.parametrize("at", [0, -1], ids=["before-the-run", "after-the-run"])
def test_sigterm_during_the_removal_waits_for_it_then_unwinds(
    iso: Path, previous_sigterm: Any, at: int
) -> None:
    """The removal is not cut (its podman client would be SIGKILLed); the stop request
    is not lost either -- it unwinds once the removal is done."""
    podman = _TermDuringRm(_tag(), _writes(_report(iso)), at=at)
    with pytest.raises(SystemExit) as raised:
        _boot(iso, podman)
    assert raised.value.code == 128 + signal.SIGTERM
    assert podman.finished and all(podman.finished)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN  # fired: one-shot


def test_a_second_sigterm_during_the_removal_is_ignored(iso: Path, previous_sigterm: Any) -> None:
    """The run unwound on the first; the second neither cuts the removal nor raises again."""

    def terminated(argv: list[str], _path: Path | None) -> subprocess.CompletedProcess[str]:
        os.kill(os.getpid(), signal.SIGTERM)
        return subprocess.CompletedProcess(argv, 0, "", "")

    podman = _TermDuringRm(_tag(), terminated, at=-1)
    with pytest.raises(SystemExit) as raised:
        _boot(iso, podman)
    assert raised.value.code == 128 + signal.SIGTERM
    assert raised.value.__context__ is None  # one SystemExit, not one on top of another
    assert podman.finished == [True, True]
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN  # fired: one-shot


@pytest.mark.parametrize("call", ["boot", "info"])
def test_a_run_without_sigterm_restores_the_previous_handler(
    iso: Path, previous_sigterm: Any, call: str
) -> None:
    podman = _Podman(_tag(), _writes(_report(iso)))
    if call == "boot":
        _boot(iso, podman)
    else:
        podman.inner = lambda argv, _p: subprocess.CompletedProcess(argv, 0, "{}", "")
        vmrunner.build_info_any(iso, runner=podman)
    assert signal.getsignal(signal.SIGTERM) is previous_sigterm


def test_a_fired_sigterm_is_not_re_armed_for_the_callers_cleanup(
    iso: Path, previous_sigterm: Any
) -> None:
    """The previous handler (a one-shot like cli._terminated, or SIG_DFL) must not come
    back: a second SIGTERM during the caller's own cleanup is ignored."""

    def terminated(argv: list[str], _path: Path | None) -> subprocess.CompletedProcess[str]:
        os.kill(os.getpid(), signal.SIGTERM)
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(SystemExit):
        _boot(iso, _Podman(_tag(), terminated))
    os.kill(os.getpid(), signal.SIGTERM)  # the caller's cleanup: neither _Unhandled nor death
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN


# --- the orphan of a SIGKILLed run, and whose report it is ----------------------------------


def test_a_same_name_orphan_is_removed_before_the_run(iso: Path) -> None:
    podman = _Podman(_tag(), _writes(_report(iso)))
    _boot(iso, podman)
    verbs = [c[1] for c in podman.calls if c[1] in ("rm", "run")]
    assert verbs == ["rm", "run", "rm"]
    for rm in podman.removals():
        assert rm[-1] == "shidashi-vm-boot-57"
        assert {"-f", "-i"} <= set(rm)
    orphan, own = podman.removals()
    assert _option(orphan, "--time") == "0"  # nothing waits on an orphan
    assert _option(own, "--time") == "10"  # this run's QEMU gets its grace


def test_build_info_removes_a_same_name_orphan_before_the_run(iso: Path) -> None:
    podman = _Podman(_tag(), lambda argv, _p: subprocess.CompletedProcess(argv, 0, "{}", ""))
    vmrunner.build_info_any(iso, runner=podman)
    assert [c[1] for c in podman.calls if c[1] in ("rm", "run")] == ["rm", "run", "rm"]


def test_a_complete_report_with_a_failing_exit_is_not_the_verdict(iso: Path) -> None:
    """Hostile: an orphan (or a crash after the write) leaves a complete report while
    this run's container failed."""
    podman = _Podman(_tag(), _writes(_report(iso), rc=137, stderr="Killed"))
    trail = _Trail()
    with pytest.raises(vmrunner.VmRunnerError, match="137"):
        _boot(iso, podman, trail=trail)
    assert trail.attached == []


def test_a_report_of_another_iso_is_not_the_verdict(iso: Path) -> None:
    other = iso.with_name("bentoo-20261012-minimal-openrc-v3.iso")
    podman = _Podman(_tag(), _writes(_report(other)))
    trail = _Trail()
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        _boot(iso, podman, trail=trail)
    assert str(other) in str(raised.value) and str(iso) in str(raised.value)
    assert trail.attached == []


def test_an_inner_error_still_wins_over_its_exit_status(iso: Path) -> None:
    podman = _Podman(_tag(), _writes({"error": "ssh never answered"}, rc=2))
    with pytest.raises(vmrunner.VmRunnerError, match="ssh never answered"):
        _boot(iso, podman)


# --- the image tag follows the tarball's stamp ----------------------------------------------

_NAME = "v3-systemd-20260823T153057Z-p20260928.60cfe9d6-b392fdb91-toolbox.tar"


def _tarball(where: Path, content: bytes = b"toolbox", mtime_ns: int = 10**18) -> Path:
    path = where / _NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_a_rebuilt_toolbox_under_the_same_name_is_a_new_tag(tmp_path: Path) -> None:
    """Hostile, wrong collapse: the same key rebuilt (another size, or the same size at
    another time) must not be served the old image -- as toolbox._stamp sees it."""
    first = vmrunner.image_tag(_tarball(tmp_path / "a"))
    resized = vmrunner.image_tag(_tarball(tmp_path / "b", content=b"toolbox, rebuilt"))
    retimed = vmrunner.image_tag(_tarball(tmp_path / "c", mtime_ns=10**18 + 1))
    assert len({first, resized, retimed}) == 3


def test_the_same_stamp_elsewhere_is_the_same_tag(tmp_path: Path) -> None:
    """The host's cache and a worker's synced copy (rsync -t keeps size and mtime)."""
    host = _tarball(tmp_path / "host")
    worker = _tarball(tmp_path / "mnt" / "work" / "cache" / "fork-points", content=b"tOOLBOX")
    assert vmrunner.image_tag(host) == vmrunner.image_tag(worker)


def test_the_tag_is_16_hex_of_the_stamp(tmp_path: Path) -> None:
    import hashlib

    path = _tarball(tmp_path)
    stamp = f"{_NAME} {len(b'toolbox')} {10**18}\n"
    digest = hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:16]
    assert vmrunner.image_tag(path) == f"localhost/shidashi-vm-runner:{digest}"


def test_the_tag_of_a_missing_tarball_is_a_runner_error(tmp_path: Path) -> None:
    with pytest.raises(vmrunner.VmRunnerError, match=_NAME):
        vmrunner.image_tag(tmp_path / _NAME)


# --- duplicate binds ------------------------------------------------------------------------


def _binds(argv: list[str], tag: str = _FIXED_TAG) -> list[tuple[str, bool]]:
    found: list[tuple[str, bool]] = []
    for spec in _options(_head(argv, tag), "-v", "--volume"):
        source, dest, *opts = spec.split(":")
        assert source == dest, spec
        found.append((source, bool(opts) and "ro" in opts[0].split(",")))
    return found


def test_a_read_only_bind_already_bound_is_skipped(tmp_path: Path) -> None:
    isos = tmp_path / "isos"
    binds = _binds(_run_argv(tmp_path, mounts_ro=[isos, isos, tmp_path / "venv"]))
    assert len(binds) == len({src for src, _ro in binds})
    assert (str(isos), True) in binds


@pytest.mark.parametrize("where", ["mounts_ro", "scratch-in-mounts_ro"])
def test_a_writable_bind_wins_over_a_read_only_one(tmp_path: Path, where: str) -> None:
    """The ISO directory is the screenshots directory, or the scratch holds the ISOs."""
    if where == "mounts_ro":
        shared = tmp_path / "isos"
        argv = _run_argv(tmp_path, mounts_ro=[shared], mounts_rw=[shared])
    else:
        shared = tmp_path / "scratch"
        argv = _run_argv(tmp_path, mounts_ro=[shared])
    binds = _binds(argv)
    assert len(binds) == len({src for src, _ro in binds}), binds
    assert (str(shared), False) in binds


def test_a_boot_test_with_screenshots_beside_the_iso_binds_it_once(iso: Path) -> None:
    podman = _Podman(_tag(), _writes(_report(iso)))
    _boot(iso, podman, screenshots=iso.parent)
    (run,) = podman.runs()
    binds = _binds(run, _tag())
    assert len(binds) == len({src for src, _ro in binds}), binds
    assert (str(iso.parent), False) in binds


# --- the interpreter must be a venv's -------------------------------------------------------


@pytest.mark.parametrize("call", ["boot", "info"])
def test_a_system_interpreter_is_refused_before_podman_runs(
    iso: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    with pytest.raises(vmrunner.VmRunnerError, match="venv") as raised:
        if call == "boot":
            _boot(iso, _never)
        else:
            vmrunner.build_info_any(iso, runner=_never)
    assert sys.prefix in str(raised.value)


# --- the import's staging, rm -i, undecodable output ----------------------------------------


class _Recording:
    """podman with the image absent until imported; records every call's keywords."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.imported = False

    def __call__(self, argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        self.calls.append((argv, kw))
        if argv[1:3] == ["image", "exists"]:
            return subprocess.CompletedProcess(argv, 0 if self.imported else 1, "", "")
        if argv[1] == "import":
            self.imported = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected podman call: {argv}")


def test_the_import_stages_under_scratch(iso: Path) -> None:
    """Not /var/tmp: on the worker that is the RAM root, and the tarball is ~5 GiB."""
    del iso
    podman = _Recording()
    vmrunner.ensure_image(vmrunner.runner_tarball(), runner=podman)
    ((argv, kw),) = [c for c in podman.calls if c[0][1] == "import"]
    env = kw["env"]
    assert env["TMPDIR"] == str(config.scratch_dir().absolute())
    assert env["PATH"] == os.environ["PATH"]  # the rest of the environment kept
    assert Path(env["TMPDIR"]).is_dir()


def _undecodable(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    """A real process whose stderr is not UTF-8, run with the caller's keywords."""
    script = "import sys; sys.stderr.buffer.write(b'Error: bad \\xff\\xfe byte'); sys.exit(125)"
    done: subprocess.CompletedProcess[str] = subprocess.run([sys.executable, "-c", script], **kw)
    return subprocess.CompletedProcess(list(argv), done.returncode, done.stdout, done.stderr)


def test_undecodable_podman_output_does_not_raise(iso: Path) -> None:
    del iso
    with pytest.raises(vmrunner.VmRunnerError, match="rc 125") as raised:
        vmrunner.ensure_image(vmrunner.runner_tarball(), runner=_undecodable)
    assert "Error: bad" in str(raised.value)


def test_undecodable_output_of_the_run_still_names_the_exit_status(iso: Path) -> None:
    def podman(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["image", "exists"]:
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        return _undecodable(argv, **kw)

    with pytest.raises(vmrunner.VmRunnerError, match="rc 125"):
        _boot(iso, podman)


# --- native() follows shutil.which ----------------------------------------------------------


def test_native_looks_shutil_which_up_when_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert vmrunner.native() is True
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert vmrunner.native() is False
