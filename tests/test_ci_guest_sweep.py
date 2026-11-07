"""Sweep orphan job directories (story 034, task 3.2; R4.6, R5.3).

Contract (design.md, "shidashi/ci_guest.py" -> ``sweep``):
``sweep(root, *, alive, run_dir=/run/shidashi/ci, prepare=False) -> list[str]`` deletes,
unless ``alive`` says its QEMU container still runs:

- every ``<root>/jobs/<name>`` (the container ``shidashi-vm-ci-<name>``);
- every ``<run_dir>/<name>`` -- a JIT config left by a dispatch whose unit never started;
- only with ``prepare=True`` -- passed by a caller holding ``prepare.lock`` -- every
  ``<root>/prepare/<key>`` (the container ``shidashi-vm-ci-prepare-<key>``);

and returns the names it removed. ``images/``, ``prepared/`` and ``results/`` are never
touched; a symlink is unlinked, never followed. ``alive`` is asked about each directory by
its name or its container's (``shidashi-vm-ci-<name>``, ``shidashi-vm-ci-prepare-<key>``).

Identity rules are written hostile half first.
"""

import os
from pathlib import Path
from typing import Any

from tests._pending import try_import

sweep: Any = try_import("shidashi.ci_guest", "sweep")

#: The ways ``alive`` may be asked about job ``a``'s VM: the directory's name, or its
#: container's. ``a-1`` and ``a1`` are neither.
A_RUNNING = {"a", "ci-a", "shidashi-vm-ci-a"}
KEY_A = "0123456789abcdef"
KEY_B = "fedcba9876543210"


def layout(tmp_path: Path) -> tuple[Path, Path]:
    root, run_dir = tmp_path / "ci", tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    return root, run_dir


def test_sweep_deletes_the_dirs_of_dead_vms_and_keeps_the_living(tmp_path: Path) -> None:
    """R4.6. Hostile collapse first: ``a`` running must not keep ``a-1`` or ``a1``."""
    root, run_dir = layout(tmp_path)
    for name in ("a", "a-1", "a1", "b"):
        (root / "jobs" / name).mkdir(parents=True)
        (root / "jobs" / name / "overlay.qcow2").write_bytes(b"x")
    for kept in ("images", "prepared", "results"):
        (root / kept).mkdir(parents=True)
        (root / kept / "keep.me").write_bytes(b"x")
    asked: list[str] = []

    def alive(name: str) -> bool:
        asked.append(name)
        return name in A_RUNNING

    removed = sweep(root, alive=alive, run_dir=run_dir)
    assert sorted(removed) == ["a-1", "a1", "b"]
    assert sorted(p.name for p in (root / "jobs").iterdir()) == ["a"]
    for kept in ("images", "prepared", "results"):
        assert (root / kept / "keep.me").exists(), kept
    assert set(asked) & A_RUNNING, asked


def test_sweep_never_follows_a_symlink_out_of_the_jobs_directory(tmp_path: Path) -> None:
    """Hostile: a job "directory" that is a symlink to a worker directory is unlinked,
    never emptied -- the worker's files stay."""
    root, run_dir = layout(tmp_path)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "data").write_bytes(b"keep")
    (root / "jobs").mkdir(parents=True)
    (root / "jobs" / "evil").symlink_to(precious, target_is_directory=True)
    removed = sweep(root, alive=lambda n: False, run_dir=run_dir)
    assert removed == ["evil"]
    assert (precious / "data").read_bytes() == b"keep"
    assert not os.path.lexists(root / "jobs" / "evil")


def test_sweep_removes_a_jitconfig_whose_unit_never_started_and_keeps_a_running_jobs(
    tmp_path: Path,
) -> None:
    """R5.3: a dispatch whose ``systemd-run`` died after the JIT config landed on tmpfs
    leaves ``<run_dir>/<job>/jitconfig``; a job whose VM runs keeps its entry."""
    root, run_dir = layout(tmp_path)
    for name in ("7000", "7001"):
        (run_dir / name).mkdir()
        (run_dir / name / "jitconfig").write_text("x", encoding="utf-8")
    removed = sweep(root, alive=lambda n: "7001" in n, run_dir=run_dir)
    assert removed == ["7000"]
    assert not (run_dir / "7000").exists()
    assert (run_dir / "7001" / "jitconfig").exists()


def test_sweep_leaves_prepare_alone_unless_the_caller_holds_the_lock(tmp_path: Path) -> None:
    """Hostile: without ``prepare=True`` -- a job's sweep -- no work dir of a preparation
    is touched, dead or not."""
    root, run_dir = layout(tmp_path)
    (root / "jobs" / "7002").mkdir(parents=True)
    for key in (KEY_A, KEY_B):
        (root / "prepare" / key).mkdir(parents=True)
        (root / "prepare" / key / "disk.qcow2").write_bytes(b"x")
    removed = sweep(root, alive=lambda n: False, run_dir=run_dir)
    assert removed == ["7002"], "the job orphan was not swept"
    assert sorted(p.name for p in (root / "prepare").iterdir()) == [KEY_A, KEY_B]


def test_sweep_with_the_lock_removes_a_dead_preparation_and_keeps_a_running_one(
    tmp_path: Path,
) -> None:
    root, run_dir = layout(tmp_path)
    for key in (KEY_A, KEY_B):
        (root / "prepare" / key).mkdir(parents=True)
        (root / "prepare" / key / "disk.qcow2").write_bytes(b"x")
    removed = sweep(root, alive=lambda n: KEY_A in n, run_dir=run_dir, prepare=True)
    assert removed == [KEY_B]
    assert (root / "prepare" / KEY_A / "disk.qcow2").exists()
    assert not (root / "prepare" / KEY_B).exists()
