"""The runner's pure parts (story 024, task 2.1; R1.3, R1.4, R2.3, R3.1, R4.3).

Contract (design.md, Components & Interfaces and API Design):

- ``RUNNER_ARCH = "v3"``, ``RUNNER_INIT = "systemd"``: ONE runner for every init.
- ``VmRunnerError`` subclasses ``vm.VmError``.
- ``native(which=shutil.which)``: ``qemu-system-x86_64`` is on PATH.
- ``runner_tarball()``: the v3 × systemd toolbox fork point at the current snapshot and
  pins, under ``config.fork_points_dir()``.
- ``image_tag(tarball)``: ``localhost/shidashi-vm-runner:`` + the first 16 hex of the
  sha256 of the tarball's stamp -- NAME, size and mtime, as ``toolbox._stamp`` (task 2.4,
  author decision 2026-10-14) -- a new toolbox (snapshot, pins, build key) or one rebuilt
  under the same key is a new tag; the same file found elsewhere is the same tag.
- ``run_argv(tag, *, name, inner_args, venv, tree, mounts_ro, scratch)``: pure; every bind
  at the SAME path, read-only except scratch, the two devices only, no privilege, never
  a pull.
- ``preflight(*, which=..., exists=...)``: nothing when native; else ONE
  ``VmRunnerError`` naming every missing piece (podman, /dev/kvm, /dev/vhost-vsock, the
  runner tarball) with what provides it.

Identity rules are written hostile half first: what must stay apart, then what must
come together, then the plain case.
"""

import hashlib
import inspect
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from shidashi import config, toolbox, vm, vmrunner
from shidashi.recipe import TOOLBOX_STAGE
from shidashi.seed import load_pointer
from shidashi.tree import load_pin_id

_PREFIX = "localhost/shidashi-vm-runner:"

#: A podman/OCI reference: lower-case repository path, then a tag of [\w.-]{1,128}.
_REFERENCE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+:\w[\w.-]{0,127}$"
)


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    return tmp_path / "cache"


def _which(*present: str) -> Callable[[str], str | None]:
    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return which


# --- constants and the error -----------------------------------------------------------


def test_one_runner_for_every_init() -> None:
    assert vmrunner.RUNNER_ARCH == "v3"
    assert vmrunner.RUNNER_INIT == "systemd"


def test_a_runner_error_is_a_vm_error() -> None:
    """Every caller that catches vm.VmError today reports a runner failure too."""
    assert issubclass(vmrunner.VmRunnerError, vm.VmError)


# --- native ------------------------------------------------------------------------------


def test_another_qemu_is_not_the_x86_64_system_emulator() -> None:
    """Hostile: a node with qemu-img or another target's emulator is still not native."""
    assert vmrunner.native(which=_which("qemu-img", "qemu-system-aarch64", "podman")) is False


def test_native_without_qemu() -> None:
    assert vmrunner.native(which=_which()) is False


def test_native_with_qemu() -> None:
    assert vmrunner.native(which=_which("qemu-system-x86_64")) is True


# --- runner_tarball -----------------------------------------------------------------------


def _expected_tarball() -> Path:
    recipe = config.load_recipe("v3", TOOLBOX_STAGE, "systemd")
    return toolbox.tarball_path(
        recipe,
        snapshot=load_pointer("systemd", seeds_dir=config.seeds_dir()).snapshot,
        pins=load_pin_id(config.seeds_dir()),
        fork_points_dir=config.fork_points_dir(),
    )


def test_the_runner_tarball_is_the_v3_systemd_toolbox_under_the_fork_points(
    cache: Path,
) -> None:
    tarball = vmrunner.runner_tarball()
    assert tarball == _expected_tarball()
    assert tarball.parent == config.fork_points_dir() == cache / "fork-points"
    assert tarball.name.startswith("v3-systemd-") and tarball.name.endswith("-toolbox.tar")


def test_the_runner_tarball_takes_no_init() -> None:
    """R1.4: an openrc ISO is boot-tested by the same runner as a systemd one."""
    required = [
        p
        for p in inspect.signature(vmrunner.runner_tarball).parameters.values()
        if p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert required == []


def _seeds_copy(tmp_path: Path, name: str, edit: Callable[[Path], None]) -> Path:
    seeds = tmp_path / name
    shutil.copytree(config.seeds_dir(), seeds)
    edit(seeds)
    return seeds


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path} no longer holds {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_a_new_snapshot_or_pin_is_a_new_tarball_and_a_new_tag(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.3, hostile: the toolbox of another snapshot or other pins must never be served
    the image of the old one -- the tarball and the tag both change."""
    current = vmrunner.runner_tarball()
    snapshot = load_pointer("systemd", seeds_dir=config.seeds_dir()).snapshot
    gentoo_date = re.search(
        r'^date = "(\d+)"', (config.seeds_dir() / "gentoo.toml").read_text(), re.MULTILINE
    )
    assert gentoo_date is not None
    other_snapshot = _seeds_copy(
        tmp_path,
        "snap",
        lambda s: _replace(s / "stage3.toml", snapshot, "20990101T000000Z"),
    )
    other_pins = _seeds_copy(
        tmp_path,
        "pins",
        lambda s: _replace(
            s / "gentoo.toml", f'date = "{gentoo_date.group(1)}"', 'date = "20990101"'
        ),
    )
    found = {current}
    for seeds in (other_snapshot, other_pins):
        monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(seeds))
        found.add(vmrunner.runner_tarball())
    assert len(found) == 3, found
    for tarball in found:  # the tag stats the tarball (task 2.4)
        _toolbox(tarball)
    assert len({vmrunner.image_tag(t) for t in found}) == 3


# --- image_tag ----------------------------------------------------------------------------


def test_toolboxes_that_differ_in_one_key_part_never_share_a_tag(tmp_path: Path) -> None:
    """Hostile, wrong collapse: names one field apart (snapshot, pins, build key, init,
    arch) are different toolboxes -- every one of them, not just a pair, gets its own tag."""
    names = [
        "v3-systemd-20260823T153057Z-p20260928.60cfe9d6-b392fdb91-toolbox.tar",
        "v3-systemd-20260824T153057Z-p20260928.60cfe9d6-b392fdb91-toolbox.tar",
        "v3-systemd-20260823T153057Z-p20260929.60cfe9d6-b392fdb91-toolbox.tar",
        "v3-systemd-20260823T153057Z-p20260928.60cfe9d6-b392fdb92-toolbox.tar",
        "v3-openrc-20260823T153057Z-p20260928.60cfe9d6-b392fdb91-toolbox.tar",
        "znver5-systemd-20260823T153057Z-p20260928.60cfe9d6-b392fdb91-toolbox.tar",
    ]
    tags = [vmrunner.image_tag(_toolbox(tmp_path / "fork-points" / n)) for n in names]
    assert len(set(tags)) == len(names), tags


#: One mtime for every toolbox a test makes: only the name or the size tells them apart.
_MTIME_NS = 1_787_500_000_000_000_000


def _toolbox(path: Path, content: bytes = b"toolbox") -> Path:
    """A toolbox tarball at ``path``: the tag stats it (task 2.4)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, ns=(_MTIME_NS, _MTIME_NS))
    return path


def test_the_same_toolbox_elsewhere_is_the_same_tag(tmp_path: Path) -> None:
    """Hostile, wrong split: the host's cache and the worker's work disk hold the same
    toolbox (same name, size and mtime: a sync keeps them) -- the same tag; a file
    rebuilt under the same key (another content and size) is another one."""
    name = "v3-systemd-20260823T153057Z-p20260928.60cfe9d6-b392fdb91-toolbox.tar"
    host = _toolbox(tmp_path / "host" / name, b"one")
    worker = _toolbox(tmp_path / "mnt" / "work" / "cache" / "fork-points" / name, b"one")
    rebuilt = _toolbox(tmp_path / "rebuilt" / name, b"another build")
    assert vmrunner.image_tag(host) == vmrunner.image_tag(worker)
    assert vmrunner.image_tag(rebuilt) != vmrunner.image_tag(host)


def test_the_tag_is_the_prefix_and_16_hex_of_the_name(tmp_path: Path) -> None:
    name = "v3-systemd-SNAP-P-bKEY-toolbox.tar"
    path = _toolbox(tmp_path / name)
    stamp = f"{name} {len(b'toolbox')} {_MTIME_NS}\n"
    digest = hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:16]
    tag = vmrunner.image_tag(path)
    assert tag == _PREFIX + digest
    assert _REFERENCE.match(tag), tag


# --- run_argv -----------------------------------------------------------------------------

_TAG = _PREFIX + "0123456789abcdef"


_INNER = ["vm", "inner-boot", "/isos/a.iso", "--cid", "57"]


def _argv(tmp_path: Path, *, name: str = "boot-57") -> list[str]:
    return vmrunner.run_argv(
        _TAG,
        name=name,
        inner_args=_INNER,
        venv=tmp_path / "venv",
        tree=tmp_path / "src",
        mounts_ro=[tmp_path / "isos"],
        scratch=tmp_path / "scratch",
    )


def _options(argv: list[str]) -> list[tuple[str, str | None]]:
    """``(option, value)`` for every option before the image, ``--opt v`` or ``--opt=v``."""
    with_value = {
        "--name",
        "--network",
        "--device",
        "-v",
        "--volume",
        "-e",
        "--env",
        "-w",
        "--workdir",
        "--pull",
    }
    head = argv[2 : argv.index(_TAG)]
    found: list[tuple[str, str | None]] = []
    i = 0
    while i < len(head):
        token = head[i]
        if "=" in token and token.startswith("--"):
            option, value = token.split("=", 1)
            found.append((option, value))
        elif token in with_value:
            found.append((token, head[i + 1]))
            i += 1
        else:
            found.append((token, None))
        i += 1
    return found


def _values(argv: list[str], *names: str) -> list[str]:
    return [v for o, v in _options(argv) if o in names and v is not None]


def _binds(argv: list[str]) -> dict[str, tuple[str, bool]]:
    """source -> (destination, read-only) for every ``-v``."""
    binds: dict[str, tuple[str, bool]] = {}
    for spec in _values(argv, "-v", "--volume"):
        source, dest, *opts = spec.split(":")
        flags = opts[0].split(",") if opts else []
        binds[source] = (dest, "ro" in flags)
    return binds


def test_only_scratch_is_writable(tmp_path: Path) -> None:
    """R4.3, hostile: the venv, the tree and the ISO dir must not become writable."""
    argv = _argv(tmp_path)
    writable = {src for src, (_dest, ro) in _binds(argv).items() if not ro}
    assert writable == {str(tmp_path / "scratch")}


def test_every_bind_is_at_the_same_path(tmp_path: Path) -> None:
    """pyvenv.cfg, PYTHONPATH, SHIDASHI_SCRATCH and the report's iso all assume it."""
    argv = _argv(tmp_path)
    binds = _binds(argv)
    assert set(binds) == {
        str(tmp_path / "venv"),
        str(tmp_path / "src"),
        str(tmp_path / "isos"),
        str(tmp_path / "scratch"),
    }
    assert all(src == dest for src, (dest, _ro) in binds.items()), binds
    assert {src for src, (_d, ro) in binds.items() if ro} == {
        str(tmp_path / "venv"),
        str(tmp_path / "src"),
        str(tmp_path / "isos"),
    }


def test_two_devices_and_no_privilege(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert sorted(_values(argv, "--device")) == ["/dev/kvm", "/dev/vhost-vsock"]
    options = {o for o, _v in _options(argv)}
    assert "--privileged" not in options
    assert "--cap-add" not in options
    assert not any(t.startswith("--privileged") for t in argv)


def test_never_pulls_and_removes_itself(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert argv[:2] == ["podman", "run"]
    assert "--rm" in {o for o, _v in _options(argv)}
    assert _values(argv, "--pull") == ["never"]
    assert _values(argv, "--network") == ["host"]


def test_the_name_follows_the_caller(tmp_path: Path) -> None:
    """R4.1: two concurrent boot tests (distinct CIDs) never collide on the name."""
    first = _argv(tmp_path, name="boot-57")
    second = _argv(tmp_path, name="boot-58")
    assert _values(first, "--name") == ["shidashi-vm-boot-57"]
    assert _values(second, "--name") == ["shidashi-vm-boot-58"]


def test_environment_and_workdir(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    env = dict(e.split("=", 1) for e in _values(argv, "-e", "--env"))
    assert env["SHIDASHI_SCRATCH"] == str(tmp_path / "scratch")
    assert env["PYTHONPATH"] == str(tmp_path / "src")
    assert env["SHIDASHI_VARIANTS_DIR"] == str(tmp_path / "src" / "variants")
    assert _values(argv, "-w", "--workdir") == [str(tmp_path / "src")]


def test_the_command_is_the_venvs_python_running_the_cli(tmp_path: Path) -> None:
    argv = _argv(tmp_path)
    assert argv.count(_TAG) == 1
    assert argv[argv.index(_TAG) + 1 :] == [
        str(tmp_path / "venv" / "bin" / "python"),
        "-c",
        "from shidashi.cli import app; app()",
        *_INNER,
    ]


# --- preflight ----------------------------------------------------------------------------

_DEVICES = ("/dev/kvm", "/dev/vhost-vsock")


def _exists(*present: object) -> Callable[[object], bool]:
    names = {str(p) for p in present}

    def exists(path: object) -> bool:
        return str(path) in names

    return exists


def test_preflight_names_only_what_is_missing(cache: Path) -> None:
    """Hostile, wrong collapse: one missing device must not be reported as everything."""
    tarball = vmrunner.runner_tarball()
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.preflight(which=_which("podman"), exists=_exists("/dev/kvm", tarball))
    message = str(raised.value)
    assert "/dev/vhost-vsock" in message
    assert "/dev/kvm" not in message
    assert str(tarball) not in message


def test_preflight_names_every_missing_piece_at_once(cache: Path) -> None:
    """Hostile, wrong split: one refusal names all four, not only the first it met."""
    tarball = vmrunner.runner_tarball()
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.preflight(which=_which(), exists=_exists())
    message = str(raised.value)
    for piece in ("podman", "/dev/kvm", "/dev/vhost-vsock", str(tarball)):
        assert piece in message, piece


def test_preflight_names_the_commands_that_provide_the_tarball(cache: Path) -> None:
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.preflight(which=_which("podman"), exists=_exists(*_DEVICES))
    message = str(raised.value)
    assert str(vmrunner.runner_tarball()) in message
    assert "shidashi factory v3 toolbox systemd" in message
    assert "shidashi worker sync push" in message


def test_preflight_passes_with_every_piece(cache: Path) -> None:
    vmrunner.preflight(which=_which("podman"), exists=_exists(*_DEVICES, vmrunner.runner_tarball()))


def test_preflight_needs_nothing_on_a_native_node(cache: Path) -> None:
    """R1.3: a node with QEMU never needs podman, the devices check or the tarball."""
    vmrunner.preflight(which=_which("qemu-system-x86_64"), exists=_exists())
