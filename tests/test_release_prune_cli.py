"""``shidashi release prune --keep N [--output-root DIR] [--dry-run] [--purge-pruned]``
on a real tree (story 032, task 5.2; R4.1-R4.6, Q13).

Contract (story.md R4, design.md "shidashi release prune", "Error Handling Strategy"):
every ``YYYY.MM.DD`` release directory but the N newest moves to
``<output-root>/.pruned/<name>`` by one ``os.replace`` (the same inode, nothing copied or
deleted); a directory whose ``.release.lock`` is held stays and is reported -- a lock FILE
left by a finished release is not a held lock; ``--dry-run`` prints the plan and moves
nothing; a name that is not a date is untouched; a move that fails exits 1 naming the
directory and leaves it in place; only ``--purge-pruned`` deletes, and only far older
``.pruned/`` entries.

Driven through the real CLI (typer ``CliRunner``). A held lock is held by another process
through story 030's ``release_sign.claim_release_dir``, the way a running release holds it.
"""

import datetime
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import shidashi
from shidashi import cli, release_sign

prune_runner = CliRunner()
PRUNE_CHECKOUT = Path(shidashi.__file__).resolve().parents[1]
PRUNE_CLI_NEWEST = datetime.date(2026, 11, 1)

#: Holds ``claim_release_dir(argv[1])`` until its stdin closes.
PRUNE_LOCK_HOLDER = """
import sys
from pathlib import Path
from shidashi import release_sign
with release_sign.claim_release_dir(Path(sys.argv[1])):
    print("held", flush=True)
    sys.stdin.read()
"""


def weekly_names(count: int, *, start: int = 0) -> list[str]:
    return [
        f"{PRUNE_CLI_NEWEST - datetime.timedelta(weeks=start + i):%Y.%m.%d}" for i in range(count)
    ]


def make_release(parent: Path, name: str) -> Path:
    directory = parent / name
    directory.mkdir(parents=True)
    (directory / "SHA256SUMS").write_text(f"{name}\n", encoding="utf-8")
    (directory / "bentoo.iso").write_bytes(name.encode())
    return directory


def run_prune(*args: str) -> Result:
    return prune_runner.invoke(cli.app, ["release", "prune", *args])


def flat(output: str) -> str:
    return "".join(output.split())


def inodes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_ino for p in sorted(root.rglob("*"))}


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    root = tmp_path / "releases"
    for name in weekly_names(7):
        make_release(root, name)
    (root / "notes.txt").write_text("not a release\n", encoding="utf-8")
    make_release(root, "latest")
    make_release(root, "2026.13.01")
    return root


@pytest.fixture
def held_lock(output_root: Path) -> Iterator[Path]:
    """The oldest release directory, held by a running release in another process."""
    directory = output_root / weekly_names(7)[-1]
    env = {**os.environ, "PYTHONPATH": str(PRUNE_CHECKOUT)}
    holder = subprocess.Popen(
        [sys.executable, "-c", PRUNE_LOCK_HOLDER, str(directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        yield directory
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=30)


def test_prune_moves_the_older_releases_to_pruned_by_rename_and_keeps_the_newest(
    output_root: Path,
) -> None:
    releases = weekly_names(7)
    before = inodes(output_root)
    result = run_prune("--keep", "4", "--output-root", str(output_root))
    assert result.exit_code == 0, result.output
    for name in releases[:4]:
        assert (output_root / name / "SHA256SUMS").is_file(), name
    for name in releases[4:]:
        assert not (output_root / name).exists(), name
        moved = output_root / ".pruned" / name
        assert (moved / "SHA256SUMS").read_text(encoding="utf-8") == f"{name}\n"
        # one os.replace: the directory and its files keep their inodes (Q13)
        assert moved.stat().st_ino == before[name]
        assert (moved / "bentoo.iso").stat().st_ino == before[f"{name}/bentoo.iso"]
    # names that are not dates are untouched (R4.4)
    assert (output_root / "notes.txt").is_file()
    assert (output_root / "latest" / "SHA256SUMS").is_file()
    assert (output_root / "2026.13.01" / "SHA256SUMS").is_file()


def test_prune_dry_run_prints_the_plan_and_moves_nothing(output_root: Path) -> None:
    before = inodes(output_root)
    result = run_prune("--keep", "4", "--output-root", str(output_root), "--dry-run")
    assert result.exit_code == 0, result.output
    for name in weekly_names(7)[4:]:
        assert name in result.output
    assert inodes(output_root) == before
    assert not (output_root / ".pruned").exists()


def test_prune_leaves_a_release_whose_lock_is_held_in_place_and_reports_it(
    output_root: Path, held_lock: Path
) -> None:
    result = run_prune("--keep", "4", "--output-root", str(output_root))
    assert (held_lock / "SHA256SUMS").is_file(), result.output
    assert not (output_root / ".pruned" / held_lock.name).exists()
    assert held_lock.name in result.output
    for name in weekly_names(7)[4:-1]:
        assert (output_root / ".pruned" / name).is_dir(), (name, result.output)


def test_prune_moves_a_release_whose_lock_file_was_left_by_a_finished_release(
    output_root: Path,
) -> None:
    """Hostile half: a ``.release.lock`` FILE is not a held lock -- every finished release
    leaves one behind, and none of them may stay forever."""
    oldest = output_root / weekly_names(7)[-1]
    with release_sign.claim_release_dir(oldest):
        pass
    assert (oldest / release_sign.LOCK_FILE).exists(), "the lock file was not left behind"
    result = run_prune("--keep", "4", "--output-root", str(output_root))
    assert result.exit_code == 0, result.output
    assert (output_root / ".pruned" / oldest.name / "SHA256SUMS").is_file()


def test_prune_a_move_that_fails_exits_1_naming_the_directory_and_leaves_it(
    output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = weekly_names(7)[-1]
    real_replace = os.replace

    def failing_replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        if Path(str(src)).name == stuck:
            raise OSError(5, "Input/output error", str(src))
        real_replace(src, dst, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", failing_replace)
    result = run_prune("--keep", "4", "--output-root", str(output_root))
    assert result.exit_code == 1, result.output
    assert stuck in flat(result.output)
    assert (output_root / stuck / "SHA256SUMS").read_text(encoding="utf-8") == f"{stuck}\n"


@pytest.mark.parametrize("keep", ["0", "-1"])
def test_prune_refuses_a_keep_below_one_and_moves_nothing(output_root: Path, keep: str) -> None:
    before = inodes(output_root)
    result = run_prune("--keep", keep, "--output-root", str(output_root))
    assert result.exit_code != 0, result.output
    assert inodes(output_root) == before


@pytest.fixture
def with_pruned(tmp_path: Path) -> Path:
    """Four kept releases in the root, eight older ones and a non-date entry in .pruned/."""
    root = tmp_path / "releases"
    for name in weekly_names(4):
        make_release(root, name)
    for name in weekly_names(8, start=4):
        make_release(root / ".pruned", name)
    make_release(root / ".pruned", "notes")
    return root


def test_prune_without_purge_pruned_deletes_nothing(with_pruned: Path) -> None:
    fifth = weekly_names(1, start=4)[0]  # one more release than --keep, back in the root
    shutil.move(with_pruned / ".pruned" / fifth, with_pruned / fifth)
    before = {name.removeprefix(".pruned/") for name in inodes(with_pruned)}
    result = run_prune("--keep", "4", "--output-root", str(with_pruned))
    assert result.exit_code == 0, result.output
    assert (with_pruned / ".pruned" / fifth / "SHA256SUMS").is_file(), result.output
    after = {name.removeprefix(".pruned/") for name in inodes(with_pruned)}
    assert before <= after, f"gone: {sorted(before - after)}"


def test_purge_pruned_deletes_only_far_older_pruned_entries(with_pruned: Path) -> None:
    pruned = weekly_names(8, start=4)
    result = run_prune("--keep", "4", "--output-root", str(with_pruned), "--purge-pruned")
    assert result.exit_code == 0, result.output
    for name in weekly_names(4):
        assert (with_pruned / name / "SHA256SUMS").is_file(), name
    assert (with_pruned / ".pruned" / pruned[0] / "SHA256SUMS").is_file()
    for name in pruned[4:]:  # 8+ releases older than the newest kept one
        assert not (with_pruned / ".pruned" / name).exists(), name
    assert (with_pruned / ".pruned" / "notes" / "SHA256SUMS").is_file()
