"""The runner's pure parts beyond the authored tests (story 024, task 2.1; R3.1, R4.3).

The cases the Reviewer found uncovered in ``tests/test_vmrunner.py``:

- the runner is rootful: a non-root caller on a node without QEMU is refused (R3.1);
- every missing piece is named with the command that provides it (the devices too);
- ``run_argv``'s ``variants`` is bound read-only and passed as ``SHIDASHI_VARIANTS_DIR``;
- ``run_argv``'s ``mounts_rw`` are writable, and nothing else besides scratch is (R4.3);
- EVERY failure to resolve the runner tarball -- the recipe, the seed pointer, the
  pins -- is a ``VmRunnerError``: the callers catch only ``vm.VmError``.

Hostile half first: what must be refused, then what must pass.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from shidashi import config, vmrunner
from tests.test_vmrunner import _TAG, _binds, _exists, _values, _which

_DEVICES = ("/dev/kvm", "/dev/vhost-vsock")
_INNER = ["vm", "inner-boot", "/isos/a.iso", "--cid", "57"]


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    return tmp_path / "cache"


def _uid(value: int) -> Callable[[], int]:
    return lambda: value


def _refusal(**probes: object) -> str:
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.preflight(**probes)  # type: ignore[arg-type]
    return str(raised.value)


# --- preflight: root ----------------------------------------------------------------------


def test_a_non_root_caller_is_refused_even_with_every_piece(cache: Path) -> None:
    """R3.1, hostile: the runner is rootful podman, never sent to rootless podman."""
    message = _refusal(
        which=_which("podman"),
        exists=_exists(*_DEVICES, vmrunner.runner_tarball()),
        euid=_uid(1000),
    )
    assert "root" in message
    assert "rootful podman" in message


def test_a_non_root_refusal_names_only_root(cache: Path) -> None:
    """Hostile, wrong collapse: the pieces that are there are not named missing."""
    tarball = vmrunner.runner_tarball()
    message = _refusal(which=_which("podman"), exists=_exists(*_DEVICES, tarball), euid=_uid(1000))
    for present in ("/dev/kvm", "/dev/vhost-vsock", str(tarball), "emerge app-containers/podman"):
        assert present not in message, present


def test_every_missing_piece_and_root_in_one_refusal(cache: Path) -> None:
    """Hostile, wrong split: root is named WITH the rest, not before it."""
    message = _refusal(which=_which(), exists=_exists(), euid=_uid(1000))
    for piece in ("root", "podman", "/dev/kvm", "/dev/vhost-vsock", str(vmrunner.runner_tarball())):
        assert piece in message, piece


def test_root_with_every_piece_passes(cache: Path) -> None:
    vmrunner.preflight(
        which=_which("podman"),
        exists=_exists(*_DEVICES, vmrunner.runner_tarball()),
        euid=_uid(0),
    )


def test_a_native_node_needs_no_root(cache: Path) -> None:
    """R1.3: with QEMU on PATH nothing of the runner is asked, root included."""
    vmrunner.preflight(which=_which("qemu-system-x86_64"), exists=_exists(), euid=_uid(1000))


# --- preflight: the fixes named -----------------------------------------------------------


def test_the_device_fixes_are_named(cache: Path) -> None:
    message = _refusal(
        which=_which("podman"), exists=_exists(vmrunner.runner_tarball()), euid=_uid(0)
    )
    for fix in ("modprobe kvm_amd", "kvm_intel", "SVM", "VT-x", "modprobe vhost_vsock"):
        assert fix in message, fix


def test_the_podman_fix_is_named(cache: Path) -> None:
    message = _refusal(
        which=_which(), exists=_exists(*_DEVICES, vmrunner.runner_tarball()), euid=_uid(0)
    )
    assert "emerge app-containers/podman" in message


def test_an_unresolvable_tarball_is_one_piece_of_the_refusal(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile, wrong split: a seeds tree that names no tarball is reported WITH podman,
    in the one refusal, not raised alone before it."""
    seeds = tmp_path / "no-seeds"
    seeds.mkdir()
    monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(seeds))
    message = _refusal(which=_which(), exists=_exists(*_DEVICES), euid=_uid(0))
    assert "podman" in message
    assert str(seeds / "stage3.toml") in message


# --- runner_tarball: every resolution failure is a VmRunnerError ---------------------------


def _copy(tmp_path: Path, source: Path, name: str) -> Path:
    dest = tmp_path / name
    shutil.copytree(source, dest)
    return dest


def _seeds(edit: Callable[[Path], None]) -> Callable[[Path, pytest.MonkeyPatch], None]:
    def apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seeds = _copy(tmp_path, config.seeds_dir(), "seeds")
        edit(seeds)
        monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(seeds))

    return apply


def _variants(edit: Callable[[Path], None]) -> Callable[[Path, pytest.MonkeyPatch], None]:
    def apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        variants = _copy(tmp_path, config.variants_dir(), "variants")
        edit(variants)
        monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))

    return apply


def _write(relative: str, text: str) -> Callable[[Path], None]:
    def write(root: Path) -> None:
        (root / relative).write_text(text, encoding="utf-8")

    return write


def _remove(relative: str) -> Callable[[Path], None]:
    return lambda root: (root / relative).unlink()


def _no_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(tmp_path / "empty"))


_BROKEN = {
    "no variants tree": _no_variants,
    "recipe not YAML": _variants(_write("toolbox/recipe.yaml", "key: [unclosed\n")),
    "recipe not a mapping": _variants(_write("toolbox/recipe.yaml", "- a\n- b\n")),
    "recipe missing": _variants(_remove("toolbox/recipe.yaml")),
    "no stage3 pointer": _seeds(_remove("stage3.toml")),
    "stage3 pointer not TOML": _seeds(_write("stage3.toml", "snapshot = = 1\n")),
    "no systemd entry": _seeds(_write("stage3.toml", 'snapshot = "S"\nbase_url = "https://x"\n')),
    "no ::gentoo pin": _seeds(_remove("gentoo.toml")),
    "overlay pins not TOML": _seeds(_write("overlays.toml", "[[overlay\n")),
}


@pytest.mark.parametrize("broken", list(_BROKEN), ids=list(_BROKEN))
def test_every_resolution_failure_is_a_runner_error(
    broken: str, cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: no recipe, seed or pin failure escapes as anything a caller catching
    ``vm.VmError`` would miss; the message names the runner's toolbox and the cause."""
    _BROKEN[broken](tmp_path, monkeypatch)
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.runner_tarball()
    message = str(raised.value)
    assert "v3" in message and "toolbox" in message and "systemd" in message
    assert raised.value.__cause__ is not None
    assert str(raised.value.__cause__) in message


# --- run_argv: variants and mounts_rw -----------------------------------------------------


def _argv(tmp_path: Path, **extra: object) -> list[str]:
    return vmrunner.run_argv(
        _TAG,
        name="boot-57",
        inner_args=_INNER,
        venv=tmp_path / "venv",
        tree=tmp_path / "src",
        mounts_ro=[tmp_path / "isos"],
        scratch=tmp_path / "scratch",
        **extra,  # type: ignore[arg-type]
    )


def _env(argv: list[str]) -> dict[str, str]:
    return dict(e.split("=", 1) for e in _values(argv, "-e", "--env"))


def test_variants_outside_the_tree_are_bound_read_only_and_passed(tmp_path: Path) -> None:
    """An override (env or a worker's data dirs) reaches the inner at the same path."""
    variants = tmp_path / "data" / "variants"
    argv = _argv(tmp_path, variants=variants)
    binds = _binds(argv)
    assert binds[str(variants)] == (str(variants), True)
    assert _env(argv)["SHIDASHI_VARIANTS_DIR"] == str(variants)


def test_variants_inside_the_tree_need_no_bind_of_their_own(tmp_path: Path) -> None:
    """The tree is already bound read-only at the same path: the variants are there."""
    variants = tmp_path / "src" / "variants"
    argv = _argv(tmp_path, variants=variants)
    assert str(variants) not in _binds(argv)
    assert _env(argv)["SHIDASHI_VARIANTS_DIR"] == str(variants)


def test_mounts_rw_are_writable_and_nothing_else_is(tmp_path: Path) -> None:
    """R4.3, hostile: a screenshots dir is writable; the venv, tree, ISO dir and
    variants stay read-only."""
    shots = tmp_path / "shots"
    argv = _argv(tmp_path, variants=tmp_path / "data" / "variants", mounts_rw=[shots])
    binds = _binds(argv)
    assert all(src == dest for src, (dest, _ro) in binds.items()), binds
    writable = {src for src, (_dest, ro) in binds.items() if not ro}
    assert writable == {str(tmp_path / "scratch"), str(shots)}


@pytest.mark.parametrize(
    "where",
    ["venv", "tree", "mounts_ro", "scratch", "variants", "mounts_rw"],
)
@pytest.mark.parametrize("bad", ["relative/dir", "/abs/with:colon"])
def test_a_path_podman_cannot_bind_at_the_same_path_is_refused(
    tmp_path: Path, where: str, bad: str
) -> None:
    """Hostile: a relative path or one holding ':' would bind elsewhere or split the
    ``-v`` spec -- refused before podman sees it, naming the path."""
    args: dict[str, object] = {
        "venv": tmp_path / "venv",
        "tree": tmp_path / "src",
        "mounts_ro": [tmp_path / "isos"],
        "scratch": tmp_path / "scratch",
        "variants": tmp_path / "data" / "variants",
        "mounts_rw": [],
    }
    args[where] = [Path(bad)] if where in ("mounts_ro", "mounts_rw") else Path(bad)
    with pytest.raises(vmrunner.VmRunnerError) as raised:
        vmrunner.run_argv(_TAG, name="boot-57", inner_args=_INNER, **args)  # type: ignore[arg-type]
    assert bad in str(raised.value)
