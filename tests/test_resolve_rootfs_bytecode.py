"""``apply_rootfs`` never copies Python bytecode into an image (story 021, design: Security).

git ignores ``__pycache__/`` (root .gitignore), but ``apply_rootfs`` copies a layer's
``rootfs/`` from the working tree: a ``.pyc`` left there by a local run of the worker's
scripts would ship beside the sources, stale the moment a source changes.
"""

from pathlib import Path

from shidashi.recipe import ResolvedRecipe
from shidashi.resolve import apply_rootfs


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="worker",
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(),
        portage_layers=("worker",),
    )


def _write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_bytecode_in_a_layer_tree_is_not_copied(tmp_path: Path) -> None:
    lib = tmp_path / "variants" / "worker" / "rootfs" / "usr" / "local" / "lib" / "shidashi"
    _write(lib / "kyomei_worker.py", b"print()\n")
    _write(lib / "__pycache__" / "kyomei_worker.cpython-314.pyc")
    _write(lib / "__pycache__" / "nested" / "deeper.txt")  # anything under __pycache__
    _write(lib / "stray.pyc")
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()

    copied = apply_rootfs(rootfs, _recipe(), variants_dir=tmp_path / "variants")

    assert copied == ("/usr/local/lib/shidashi/kyomei_worker.py",)
    shipped = rootfs / "usr" / "local" / "lib" / "shidashi"
    assert (shipped / "kyomei_worker.py").read_bytes() == b"print()\n"
    assert not (shipped / "__pycache__").exists()
    assert not (shipped / "stray.pyc").exists()


def test_hostile_near_twins_of_bytecode_are_still_copied(tmp_path: Path) -> None:
    """Wrongly collapse: only the ``__pycache__`` directory and ``*.pyc`` files are
    bytecode; a source, a ``.pyc``-like name or a ``pycache`` directory is not."""
    tree = tmp_path / "variants" / "worker" / "rootfs"
    for rel in ("etc/pycache/x.conf", "usr/lib/a.pyc.conf", "usr/lib/__pycache__.txt", "b.py"):
        _write(tree / rel)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()

    copied = apply_rootfs(rootfs, _recipe(), variants_dir=tmp_path / "variants")

    assert set(copied) == {
        "/etc/pycache/x.conf",
        "/usr/lib/a.pyc.conf",
        "/usr/lib/__pycache__.txt",
        "/b.py",
    }
