"""``release prune`` keeps ``--keep`` releases in the root AND ``--keep`` in ``.pruned``,
counting what this very run moved aside (story 032, task 11.1; R4.6, R6.1).

The audit of story 032 found that ``.pruned`` was read before the moves, so a weekly cycle
left ``--keep`` plus that week's moves there. Driven through the real CLI on a real tree.
"""

import datetime
from pathlib import Path

from typer.testing import CliRunner

from shidashi import cli

retention_runner = CliRunner()
NEWEST = datetime.date(2026, 11, 1)


def weeks(count: int, *, start: int = 0) -> list[str]:
    return [f"{NEWEST - datetime.timedelta(weeks=start + i):%Y.%m.%d}" for i in range(count)]


def make_release(parent: Path, name: str) -> None:
    (parent / name).mkdir(parents=True)
    (parent / name / "SHA256SUMS").write_text(name, encoding="utf-8")


def names_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and p.name[0] != ".")


def test_a_weekly_prune_leaves_keep_releases_in_the_root_and_keep_in_pruned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "releases"
    for name in weeks(6):  # this week's run left 6 in the root: 2 are to move
        make_release(root, name)
    for name in weeks(4, start=6):  # 4 already set aside by earlier weeks
        make_release(root / ".pruned", name)
    result = retention_runner.invoke(
        cli.app,
        ["release", "prune", "--keep", "4", "--purge-pruned", "--output-root", str(root)],
    )
    assert result.exit_code == 0, result.output
    assert names_in(root) == sorted(weeks(4))
    # the 4 newest of the 6 older ones: the 2 just moved and the 2 newest already there
    assert names_in(root / ".pruned") == sorted(weeks(4, start=4))


def test_dry_run_reports_the_purge_the_real_run_would_make(tmp_path: Path) -> None:
    root = tmp_path / "releases"
    for name in weeks(6):
        make_release(root, name)
    for name in weeks(4, start=6):
        make_release(root / ".pruned", name)
    result = retention_runner.invoke(
        cli.app,
        [
            "release",
            "prune",
            "--keep",
            "4",
            "--purge-pruned",
            "--dry-run",
            "--output-root",
            str(root),
        ],
    )
    assert result.exit_code == 0, result.output
    for name in weeks(2, start=8):  # the two oldest of the ten
        assert f"would delete .pruned/{name}" in result.output, result.output
    assert names_in(root / ".pruned") == sorted(weeks(4, start=6))  # nothing moved


def test_without_purge_pruned_the_moves_leave_everything_set_aside(tmp_path: Path) -> None:
    root = tmp_path / "releases"
    for name in weeks(6):
        make_release(root, name)
    for name in weeks(4, start=6):
        make_release(root / ".pruned", name)
    result = retention_runner.invoke(
        cli.app, ["release", "prune", "--keep", "4", "--output-root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert names_in(root / ".pruned") == sorted(weeks(6, start=4))  # 6 + 4: nothing deleted
