"""Boot tests on a node without QEMU, in the toolbox container (story 024).

The slim worker (story 021) has podman and ``/dev/kvm`` but no QEMU, OVMF or
xorriso. When ``qemu-system-x86_64`` is missing, the WHOLE boot test runs inside
a podman container made from the v3-systemd toolbox -- the stage Shidashi already
builds -- with the node's own venv and source tree bound at the same paths. The
host, which has QEMU, keeps the native path.

ONE runner for every init: the init matters inside the VM, not on the side that
starts it. The runner is rootful podman: a non-root caller is refused, never sent
to rootless podman.

This module's pure parts: the decision (:func:`native`), the runner's tarball and
its image tag, the ``podman run`` argv and the preflight that names every missing
piece of the runner before any build work. Its one import: :func:`ensure_image`,
which makes the runner image from the toolbox tarball at most once. Its two entries:
:func:`boot_test_any` and :func:`build_info_any`, native where QEMU is, in the runner
container where it is not.
"""

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

import yaml

from shidashi import audit, config, locks, toolbox, vm
from shidashi.recipe import TOOLBOX_STAGE, RecipeChainError, RecipeSourceError
from shidashi.seed import SeedError, load_pointer
from shidashi.tree import load_pin_id

#: The runner's arch and init: one v3-systemd toolbox boot-tests ISOs of every init.
RUNNER_ARCH = "v3"
RUNNER_INIT = "systemd"

#: The binary whose presence makes a node native.
_QEMU = "qemu-system-x86_64"

#: The runner's image repository; the tag is a digest of the toolbox's file name.
_IMAGE_REPOSITORY = "localhost/shidashi-vm-runner"

#: The two devices a boot test needs: KVM and the vsock the guest's ssh rides on.
_KVM = "/dev/kvm"
_VHOST_VSOCK = "/dev/vhost-vsock"

#: The node's default seccomp profile, the one podman applies when given none.
_DEFAULT_SECCOMP = Path("/usr/share/containers/seccomp.json")

#: The runner's own profile, under ``<scratch>/vm``: the default minus the vsock rule.
_SECCOMP_PROFILE = "runner-seccomp.json"

#: ``AF_VSOCK``: the address family the default profile denies ``socket`` and the boot
#: test's ssh (``systemd-ssh-proxy``) needs.
_AF_VSOCK = 40

#: The lock serialising imports of the runner image, under the scratch directory.
_IMAGE_LOCK = "vm-runner.lock"

#: The command an imported image runs when none is given: an imported tarball has none.
_IMAGE_CMD = "CMD=/bin/bash"

#: Every container this module runs is named ``shidashi-vm-<name>``.
_CONTAINER_PREFIX = "shidashi-vm-"

#: How long ``podman rm -f`` lets a container stop before it kills it: this run's
#: container after the run; an orphan of an earlier run, before it, is killed at once.
_RM_TIME_S = 10
_ORPHAN_RM_TIME_S = 0

#: The two firmwares a boot test knows, and the inner flag that asks for both.
_FIRMWARES = frozenset({"bios", "uefi"})
_BOTH = "both"

#: How much of the container's stderr an error carries: its end, where the reason is.
_STDERR_TAIL = 2000

#: The keys of a complete boot-test report, as :func:`vm.boot_test` returns it.
_REPORT_KEYS = frozenset({"iso", "build", "firmwares", "passed"})

#: The inner entry: the CLI run by the venv's python inside the container.
_INNER_CLI = "from shidashi.cli import app; app()"

#: ``subprocess.run`` or a fake of it: the one way this module runs podman.
Runner = Callable[..., subprocess.CompletedProcess[str]]

#: The probes of the real node: preflight's defaults, captured once.
_REAL_WHICH: Callable[[str], str | None] = shutil.which
_REAL_EXISTS: Callable[[str], bool] = os.path.exists

#: Every way resolving the runner's recipe, seed pointer or pins can fail. ValueError
#: covers RecipeFileError, pydantic's ValidationError, TOMLDecodeError and a bad
#: encoding; TypeError a recipe that is not a mapping; OSError a missing file.
_RESOLUTION_ERRORS: tuple[type[BaseException], ...] = (
    SeedError,
    config.UnknownAxisError,
    RecipeChainError,
    RecipeSourceError,
    yaml.YAMLError,
    OSError,
    ValueError,
    TypeError,
)


class VmRunnerError(vm.VmError):
    """The runner is missing or failed; the message names what and the fix.

    A :class:`vm.VmError`: every caller that reports a VM failure reports this too.
    """


def native(which: Callable[[str], str | None] | None = None) -> bool:
    """Whether ``qemu-system-x86_64`` is on PATH: the boot test runs here, natively.

    ``which`` ``None`` is :func:`shutil.which`, looked up when called.
    """
    return (shutil.which if which is None else which)(_QEMU) is not None


def _toolbox_command() -> str:
    return f"shidashi factory {RUNNER_ARCH} {TOOLBOX_STAGE} {RUNNER_INIT}"


def _tarball_fix() -> str:
    """The two ways a node gets the runner tarball. Pure."""
    return (
        f"{_toolbox_command()} on this node, or "
        f"shidashi worker sync push <worker> --arch {RUNNER_ARCH} from the host"
    )


def runner_tarball() -> Path:
    """The v3 × systemd toolbox fork point at the current snapshot and pins. I/O.

    Resolved as assemble resolves its toolbox (:func:`toolbox.tarball_path` under
    :func:`config.fork_points_dir`); takes no init (R1.4). EVERY failure to resolve
    it -- the recipe, the seed pointer, the pins -- is a :class:`VmRunnerError`.
    """
    try:
        seeds = config.seeds_dir()
        recipe = config.load_recipe(RUNNER_ARCH, TOOLBOX_STAGE, RUNNER_INIT)
        pointer = load_pointer(RUNNER_INIT, seeds_dir=seeds)
        pins = load_pin_id(seeds)
        return toolbox.tarball_path(
            recipe,
            snapshot=pointer.snapshot,
            pins=pins,
            fork_points_dir=config.fork_points_dir(),
        )
    except _RESOLUTION_ERRORS as err:
        raise VmRunnerError(
            f"cannot resolve the runner's toolbox ({RUNNER_ARCH} {TOOLBOX_STAGE} "
            f"{RUNNER_INIT}): {err}"
        ) from err


def image_tag(tarball: Path) -> str:
    """``localhost/shidashi-vm-runner:`` + 16 hex of sha256 of the tarball's stamp: its
    name, size and mtime, as :mod:`shidashi.toolbox` keys its rootfs. I/O (a stat).

    The name carries snapshot, pins and build key: a new toolbox is a new tag. The size
    and mtime tell a toolbox rebuilt under the same key from the one imported before.
    The same file synced elsewhere with its times (the host's cache, a worker's work
    disk) is the same tag. A tarball that cannot be stat'ed is a :class:`VmRunnerError`.
    """
    try:
        stat = tarball.stat()
    except OSError as err:
        raise VmRunnerError(f"cannot stat the runner tarball {tarball}: {err}") from err
    stamp = f"{tarball.name} {stat.st_size} {stat.st_mtime_ns}\n"
    digest = hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:16]
    return f"{_IMAGE_REPOSITORY}:{digest}"


def _container(name: str) -> str:
    """The container's name: ``shidashi-vm-<name>``. Pure."""
    return f"{_CONTAINER_PREFIX}{name}"


def _bindable(path: Path) -> Path:
    """``path``, refused when podman cannot bind it at the same path inside. Pure.

    ``pyvenv.cfg``, ``PYTHONPATH``, ``SHIDASHI_SCRATCH`` and the report's ``iso``
    all assume the same path; a relative one has none, and a ``:`` splits the spec.
    """
    if not path.is_absolute() or ":" in str(path):
        raise VmRunnerError(
            f"cannot bind {path} into the runner at the same path: "
            "it must be absolute and hold no ':'"
        )
    return path


def _bind(path: Path, *, read_only: bool) -> list[str]:
    """``-v P:P[:ro]``: the path at the SAME path inside. Pure."""
    spec = f"{_bindable(path)}:{path}"
    return ["-v", f"{spec}:ro" if read_only else spec]


def _binds(binds: Iterable[tuple[Path, bool]]) -> list[str]:
    """``-v`` of every ``(path, read_only)``, each path ONCE: podman refuses a duplicate
    destination (rc 125). A path bound both ways is writable. Pure."""
    read_only: dict[Path, bool] = {}
    for path, ro in binds:
        read_only[path] = read_only.get(path, True) and ro
    argv: list[str] = []
    for path, ro in read_only.items():
        argv += _bind(path, read_only=ro)
    return argv


def run_argv(
    tag: str,
    *,
    name: str,
    inner_args: Sequence[str],
    venv: Path,
    tree: Path,
    variants: Path | None = None,
    mounts_ro: Iterable[Path],
    scratch: Path,
    mounts_rw: Iterable[Path] = (),
    seccomp: Path | None = None,
) -> list[str]:
    """``podman run`` of ``inner_args`` through the venv's python in image ``tag``. Pure.

    Every bind is at the SAME path, and every path bound once; the venv, the tree,
    ``mounts_ro`` and ``variants`` read-only, only ``scratch`` and ``mounts_rw``
    writable (R4.3) -- a path given both ways is writable. The two devices only, no
    privilege, never a pull. ``variants`` defaults to the tree's ``variants/``; it is
    bound only when the tree does not already hold it. ``seccomp`` is the profile the
    container runs under (:func:`seccomp_profile`); ``None`` leaves podman's default.
    """
    variants_dir = tree / "variants" if variants is None else _bindable(variants)
    read_only = [venv, tree, *mounts_ro]
    if not variants_dir.is_relative_to(tree):
        read_only.append(variants_dir)
    binds = _binds(
        [*((p, True) for p in read_only), (scratch, False), *((p, False) for p in mounts_rw)]
    )
    security = [] if seccomp is None else ["--security-opt", f"seccomp={seccomp}"]
    return [
        "podman",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        _container(name),
        "--network=host",
        "--device",
        _KVM,
        "--device",
        _VHOST_VSOCK,
        *security,
        *binds,
        "-e",
        f"SHIDASHI_SCRATCH={scratch}",
        "-e",
        f"PYTHONPATH={tree}",
        "-e",
        f"SHIDASHI_VARIANTS_DIR={variants_dir}",
        "-w",
        str(tree),
        tag,
        str(venv / "bin" / "python"),
        "-c",
        _INNER_CLI,
        *inner_args,
    ]


def _root() -> int:
    """The euid of a described node that names none: root."""
    return 0


def preflight(
    *,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
    euid: Callable[[], int] | None = None,
) -> None:
    """Nothing on a native node; else ONE :class:`VmRunnerError` naming every missing
    piece of the runner with the command that provides it (R3.1). I/O.

    The probes describe the node. ``euid`` ``None`` reads the real one
    (:func:`os.geteuid`) when the node probed is the real one -- ``which`` and
    ``exists`` both left at their defaults; a caller describing another node
    (``which``/``exists`` injected) describes its user too, root unless it says so.
    """
    if native(which):
        return
    if euid is None:
        real_node = which is _REAL_WHICH and exists is _REAL_EXISTS
        euid = os.geteuid if real_node else _root
    missing: list[str] = []
    if euid() != 0:
        missing.append("root: run as root -- the runner is rootful podman")
    if which("podman") is None:
        missing.append("podman: emerge app-containers/podman (the worker image ships it)")
    if not exists(_KVM):
        missing.append(
            f"{_KVM}: modprobe kvm_amd or kvm_intel, with SVM/VT-x enabled in the firmware"
        )
    if not exists(_VHOST_VSOCK):
        missing.append(f"{_VHOST_VSOCK}: modprobe vhost_vsock")
    provide = _tarball_fix()
    try:
        tarball = runner_tarball()
    except VmRunnerError as err:
        missing.append(f"the runner tarball: {err}; then {provide}")
    else:
        if not exists(str(tarball)):
            missing.append(f"the runner tarball {tarball}: {provide}")
    if missing:
        raise VmRunnerError(
            f"no {_QEMU} on this node and the boot-test runner is incomplete: " + "; ".join(missing)
        )


def _podman(
    argv: Sequence[str], runner: Runner, *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` captured and unchecked, its output decoded whatever its bytes; a
    podman that cannot start is a :class:`VmRunnerError` naming the command. I/O."""
    options: dict[str, Any] = {}
    if env is not None:
        options["env"] = dict(env)
    try:
        return runner(
            list(argv), capture_output=True, text=True, errors="replace", check=False, **options
        )
    except OSError as err:
        raise VmRunnerError(f"cannot run {shlex.join(argv)}: {err}") from err


def _image_exists(tag: str, runner: Runner) -> bool:
    """``podman image exists``: rc 0 present, 1 absent, any other rc an error. I/O."""
    done = _podman(["podman", "image", "exists", tag], runner)
    if done.returncode in (0, 1):
        return done.returncode == 0
    raise VmRunnerError(
        f"podman image exists {tag} failed (rc {done.returncode}): {done.stderr.strip()}"
    )


def ensure_image(tarball: Path, *, runner: Runner = subprocess.run) -> str:
    """The runner image of ``tarball``, imported unless it exists; returns its tag. I/O.

    The first look takes no lock. When the image is absent, ``<scratch>/vm-runner.lock``
    is held EXCLUSIVE and the image looked for again -- another boot test may have
    imported it while this one waited (R2.2) -- and only then ``podman import``ed with
    ``CMD=/bin/bash`` (an imported tarball carries no CMD and no env: the run gives
    every variable through ``-e``). Only ``image exists`` and ``import``: no registry
    (R2.1). Every failure -- the tarball missing, podman missing or failing, the lock
    -- is a :class:`VmRunnerError` naming what failed (R2.4); nothing is retried. The
    import stages under ``TMPDIR=<scratch>``: podman's default, ``/var/tmp``, is the
    worker's RAM root, and the tarball is ~5 GiB.
    """
    if not tarball.is_file():
        raise VmRunnerError(f"no runner tarball at {tarball}: {_tarball_fix()}")
    tag = image_tag(tarball)
    if _image_exists(tag, runner):
        return tag
    scratch = config.scratch_dir().absolute()
    lock = scratch / _IMAGE_LOCK
    with contextlib.ExitStack() as stack:
        try:
            # hold() creates the scratch directory when it is missing
            stack.enter_context(locks.hold(lock, what="the vm runner image import"))
        except (locks.LockError, OSError) as err:
            raise VmRunnerError(f"cannot take the runner image lock {lock}: {err}") from err
        if _image_exists(tag, runner):
            return tag
        done = _podman(
            ["podman", "import", "--change", _IMAGE_CMD, str(tarball), tag],
            runner,
            env={**os.environ, "TMPDIR": str(scratch)},
        )
        if done.returncode != 0:
            raise VmRunnerError(
                f"cannot import the runner image from {tarball} "
                f"(podman rc {done.returncode}): {done.stderr.strip()}"
            )
    return tag


# --- the seccomp profile: the default minus the vsock rule ------------------------------


def _denies_vsock(rule: object) -> bool:
    """Whether ``rule`` is the default profile's ``socket(AF_VSOCK)`` -> errno. Pure."""
    if not isinstance(rule, dict):
        return False
    args = rule.get("args")
    return (
        rule.get("names") == ["socket"]
        and rule.get("action") == "SCMP_ACT_ERRNO"
        and isinstance(args, list)
        and len(args) == 1
        and isinstance(args[0], dict)
        and (args[0].get("index"), args[0].get("value"), args[0].get("op"))
        == (0, _AF_VSOCK, "SCMP_CMP_EQ")
    )


def _minimal_profile(default: Path) -> dict[str, Any]:
    """``default`` without its ``socket(AF_VSOCK)`` deny rule, every other rule and
    setting kept. I/O (reads ``default``)."""
    try:
        profile = json.loads(default.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:  # ValueError: bad JSON or a bad encoding
        raise VmRunnerError(f"cannot read the default seccomp profile {default}: {err}") from err
    rules = profile.get("syscalls") if isinstance(profile, dict) else None
    if not isinstance(rules, list):
        raise VmRunnerError(
            f"the default seccomp profile {default} is not a seccomp profile "
            "(no list of syscall rules)"
        )
    kept = [rule for rule in rules if not _denies_vsock(rule)]
    if len(kept) == len(rules):
        raise VmRunnerError(
            f"the default seccomp profile {default} has no rule denying socket(AF_VSOCK): "
            "the runner will not guess which rule keeps the guest's vsock ssh out"
        )
    return {**profile, "syscalls": kept}


def seccomp_profile(scratch: Path, *, default: Path | None = None) -> Path:
    """``<scratch>/vm/runner-seccomp.json``: the node's default profile minus ONLY the
    rule denying ``socket(AF_VSOCK)``, which the boot test's ssh rides on. I/O.

    ``default`` ``None`` is podman's own (``/usr/share/containers/seccomp.json``), so
    the profile follows the installed podman. A missing or unparseable default, one
    without that rule, or a profile that cannot be written is a :class:`VmRunnerError`
    naming the file -- never a fallback to ``unconfined``. Written whole (a temp file,
    then a rename): concurrent boot tests share the file.
    """
    profile = _minimal_profile(_DEFAULT_SECCOMP if default is None else default)
    path = scratch / "vm" / _SECCOMP_PROFILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(profile, out, indent=2)
            os.replace(temp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
    except OSError as err:
        raise VmRunnerError(f"cannot write the runner's seccomp profile {path}: {err}") from err
    return path


# --- the boot test and the build info through the runner --------------------------------


def _firmware_flag(firmwares: Sequence[str]) -> str:
    """The inner entry's ``--firmware``: ``both``, ``bios`` or ``uefi``. Pure.

    Both in any order is ``both``; one is itself. An empty or unknown firmware is a
    :class:`VmRunnerError`, raised before podman runs.
    """
    wanted = set(firmwares)
    unknown = sorted(wanted - _FIRMWARES)
    if not wanted or unknown:
        raise VmRunnerError(
            f"cannot boot-test on firmware {', '.join(unknown) or '(none)'}: "
            f"choose among {', '.join(sorted(_FIRMWARES))}"
        )
    return _BOTH if wanted == _FIRMWARES else wanted.pop()


def _tree() -> Path:
    """The checkout holding the ``shidashi`` package: bound read-only, the inner's cwd."""
    return Path(__file__).resolve().parent.parent


def _venv() -> Path:
    """This interpreter's venv: bound read-only, its python the inner's interpreter.

    A system interpreter is refused: its prefix (``/usr``) bound over the image's own
    would hide the image's tools and libraries.
    """
    if sys.prefix == sys.base_prefix:
        raise VmRunnerError(
            f"the runner needs shidashi run from a venv: {sys.prefix} is a system "
            "interpreter's prefix, and binding it would hide the image's own; "
            "run shidashi from its venv (uv sync, then .venv/bin/shidashi)"
        )
    return Path(sys.prefix)


def _runner_image(runner: Runner) -> str:
    """The venv check and the preflight, then the runner image of the current toolbox;
    its tag. I/O."""
    _venv()
    preflight()
    return ensure_image(runner_tarball(), runner=runner)


def _directory(path: Path, *, what: str) -> Path:
    """``path`` created when missing; its ``OSError`` a :class:`VmRunnerError`. I/O.

    podman refuses a bind whose source does not exist.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise VmRunnerError(f"cannot create {what} {path}: {err}") from err
    return path


def _argv(
    tag: str,
    *,
    name: str,
    inner_args: Sequence[str],
    iso: Path,
    scratch: Path,
    mounts_rw: Iterable[Path] = (),
) -> list[str]:
    """:func:`run_argv` with this node's venv, tree and variants, the ISO's directory
    read-only, under the minimal seccomp profile. I/O (reads the configuration, writes
    the profile)."""
    return run_argv(
        tag,
        name=name,
        inner_args=inner_args,
        venv=_venv(),
        tree=_tree(),
        variants=config.variants_dir().absolute(),
        mounts_ro=[iso.parent],
        scratch=scratch,
        mounts_rw=mounts_rw,
        seccomp=seccomp_profile(scratch),
    )


def _sigterm_unwinds(signum: int, frame: FrameType | None) -> None:
    """SIGTERM as an exit that unwinds, so the container's removal still runs.

    Fires once: a second SIGTERM must not cut the removal the first one started.
    """
    del frame
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def _sigterm_waits() -> Generator[None]:
    """For the block, a SIGTERM waits for its end, then unwinds as :func:`_sigterm_unwinds`.

    The removal runs in such a block: a SIGTERM raising inside it would kill its podman
    client mid-way. Changes nothing off the main thread (no handler can be installed
    there) or unless :func:`_sigterm_unwinds` is in force -- once it fired, SIGTERM is
    already ignored.
    """
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getsignal(signal.SIGTERM) is not _sigterm_unwinds
    ):
        yield
        return
    pending: list[int] = []

    def wait(signum: int, frame: FrameType | None) -> None:
        del frame
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # fires once, as the one it stands for
        pending.append(signum)

    signal.signal(signal.SIGTERM, wait)
    try:
        yield
    finally:
        if not pending:
            signal.signal(signal.SIGTERM, _sigterm_unwinds)
    if pending:
        raise SystemExit(128 + pending[0])


@contextlib.contextmanager
def _unwinding_sigterm() -> Generator[None]:
    """For the block, SIGTERM raises on the main thread; the previous handler after.

    A stopped worker job unit or serial build then removes the container instead of
    dying around it. Off the main thread nothing can be installed; a SIGTERM ignored
    on purpose stays ignored. The previous handler comes back only when SIGTERM did
    not fire: once it did, SIG_IGN stays -- the previous one (a one-shot such as
    ``cli._terminated``, or the default) re-armed would cut the caller's own cleanup.
    The removals run under :func:`_sigterm_waits`.
    """
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
    ):
        yield
        return
    previous = signal.signal(signal.SIGTERM, _sigterm_unwinds)
    try:
        yield
    finally:
        # still ours: it never fired (a fired one left SIG_IGN, which stays)
        if signal.getsignal(signal.SIGTERM) is _sigterm_unwinds:
            # None: a handler not installed from Python; the default is the closest one
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


def _remove(name: str, runner: Runner, *, time_s: int = _RM_TIME_S) -> None:
    """``podman rm -f -i --time <time_s>`` of the container, its own failure ignored; a
    SIGTERM waits for it to end. I/O.

    ``podman run --rm`` does not stop the container when the caller dies: without
    this, an interrupted build leaves QEMU running on the CID it just released.
    """
    with _sigterm_waits(), contextlib.suppress(subprocess.SubprocessError, OSError):
        runner(
            ["podman", "rm", "-f", "-i", "--time", str(time_s), _container(name)],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )


def _run(argv: Sequence[str], *, name: str, runner: Runner) -> subprocess.CompletedProcess[str]:
    """``podman run`` of ``argv``, the container ``name`` removed before and ALWAYS after.

    Before: a container already holding the name is the orphan of a SIGKILLed run (the
    caller holds the CID); it is killed at once, nothing waits on it. I/O.
    """
    with _unwinding_sigterm():
        _remove(name, runner, time_s=_ORPHAN_RM_TIME_S)
        try:
            return _podman(argv, runner)
        finally:
            _remove(name, runner)


def _ended(name: str, done: subprocess.CompletedProcess[str], what: str) -> VmRunnerError:
    """The error of a container that ended without ``what``: its rc and stderr's end."""
    tail = (done.stderr or "").strip()[-_STDERR_TAIL:]
    return VmRunnerError(
        f"the runner container {_container(name)} ended (rc {done.returncode}) "
        f"without {what}: {tail or '(no error output)'}"
    )


def _json_object(text: str) -> dict[str, Any] | None:
    """``text`` as a JSON object, or ``None`` when it is not one. Pure."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _fresh_report(scratch: Path, cid: int) -> Path:
    """``<scratch>/vm/runner-<cid>/report.json``, its directory made and a stale report
    of an earlier run removed: only this run's report can be read as its verdict. I/O."""
    path = _directory(scratch / "vm" / f"runner-{cid}", what="the runner report directory")
    report = path / "report.json"
    try:
        report.unlink(missing_ok=True)
    except OSError as err:
        raise VmRunnerError(f"cannot remove the stale runner report {report}: {err}") from err
    return report


def _read_report(path: Path) -> dict[str, Any] | None:
    """The report the inner entry wrote, or ``None`` when absent or not a JSON object."""
    try:
        text = path.read_text()
    except OSError, UnicodeDecodeError:
        return None
    return _json_object(text)


def boot_test_any(
    iso: Path,
    *,
    firmwares: Sequence[str] = ("bios", "uefi"),
    cid: int,
    screenshots: Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Boot-test ``iso``: natively where QEMU is, else in the runner container. I/O.

    Native: :func:`vm.boot_test` unchanged (R1.3). Container: the preflight, the runner
    image, then ``vm inner-boot`` writing ``<scratch>/vm/runner-<cid>/report.json``
    (a stale one removed first). The report comes back with the native shape plus
    ``runner: <tag>`` and is attached to the run in force as ``boot-test`` (R1.2,
    R1.7). The inner's ``{"error": ...}`` is a :class:`VmRunnerError` with its text
    (R4.4); no complete report, or one with a non-zero rc, is one naming the rc and the
    end of stderr, and a report of another ISO is refused (R4.5). The container is
    removed before the run (an orphan holding the name) and however the run ends
    (R4.2). The caller holds the CID.
    """
    if native():
        return vm.boot_test(iso, firmwares=firmwares, cid=cid, screenshots=screenshots)
    flag = _firmware_flag(firmwares)
    tag = _runner_image(runner)
    iso = iso.resolve()
    scratch = config.scratch_dir().absolute()
    report_path = _fresh_report(scratch, cid)
    inner = [
        "vm",
        "inner-boot",
        str(iso),
        "--cid",
        str(cid),
        "--firmware",
        flag,
        "--report",
        str(report_path),
    ]
    mounts_rw: list[Path] = []
    if screenshots is not None:
        shots = _directory(screenshots.resolve(), what="the screenshots directory")
        mounts_rw.append(shots)
        inner += ["--screenshots", str(shots)]
    name = f"boot-{cid}"
    argv = _argv(tag, name=name, inner_args=inner, iso=iso, scratch=scratch, mounts_rw=mounts_rw)
    done = _run(argv, name=name, runner=runner)
    report = _read_report(report_path)
    if report is not None and "error" in report:
        raise VmRunnerError(f"the boot test in the runner failed: {report['error']}")
    if report is None or not _REPORT_KEYS.issubset(report):
        raise _ended(name, done, f"a complete report at {report_path}")
    if done.returncode != 0:
        raise _ended(name, done, f"a clean exit, so its report at {report_path} is no verdict")
    if report["iso"] != str(iso):
        raise VmRunnerError(
            f"the runner report at {report_path} is of {report['iso']}, not of {iso}: "
            "not this run's verdict"
        )
    report["runner"] = tag
    audit.current().attach("boot-test", report)
    return report


def build_info_any(iso: Path, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    """The ISO's ``bentoo/build.json``: natively where QEMU is, else in the runner. I/O.

    Container: the preflight and the runner image first -- ``vm test`` calls this
    before the boot test, so on a fresh node this is the import -- then ``vm
    inner-build-info`` printing JSON. A non-zero rc or output that is not a JSON
    object is a :class:`VmRunnerError` with the end of stderr. The container is
    removed however the run ends.
    """
    if native():
        return vm.read_build_info(iso)
    tag = _runner_image(runner)
    iso = iso.resolve()
    scratch = _directory(config.scratch_dir().absolute(), what="the scratch directory")
    name = f"info-{os.getpid()}"
    inner = ["vm", "inner-build-info", str(iso)]
    argv = _argv(tag, name=name, inner_args=inner, iso=iso, scratch=scratch)
    done = _run(argv, name=name, runner=runner)
    if done.returncode != 0:
        raise _ended(name, done, f"the build info of {iso}")
    info = _json_object(done.stdout)
    if info is None:
        raise _ended(name, done, f"the build info of {iso} as a JSON object")
    return info
