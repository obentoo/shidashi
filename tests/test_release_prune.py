"""``release_prune.plan`` -- which release directories stay, move aside or go (story 032,
task 5.1; R4.1, R4.4, R4.6).

Contract (tasks.md 5.1 Objective, design.md "shidashi release prune"): pure
``plan(names, keep, purge) -> Plan(move, purge, skipped)``. ``names`` are the entries of the
output root, ``purge`` the entries of ``<output-root>/.pruned/`` that ``--purge-pruned``
may delete. Only ``YYYY.MM.DD`` names that are real dates are releases; the ``keep`` newest
stay, the newest always; every older release is to move (R4.1); every other name is left
untouched and reported as skipped (R4.4); a ``.pruned/`` entry is purged only when its date
is far older than the kept releases, never a non-date one (R4.6).

R4.6's threshold is worded two ways (story.md: "more than N releases older than the newest
kept one"; design.md: ".pruned/ purged after four more weeks"). These tests assert only what
both readings agree on: the newest ``.pruned/`` entry stays, the ones 2N or more releases
older than the newest kept one go.
"""

import datetime
import random
from collections.abc import Iterable
from typing import Any

import pytest

from tests._pending import try_import

plan: Any = try_import("shidashi.release_prune", "plan")

PLAN_NEWEST = datetime.date(2026, 11, 1)


def weekly_releases(count: int, *, start: int = 0) -> list[str]:
    """``count`` weekly release names, the newest first, ``start`` weeks before NEWEST."""
    return [f"{PLAN_NEWEST - datetime.timedelta(weeks=start + i):%Y.%m.%d}" for i in range(count)]


def named(items: Iterable[object]) -> set[str]:
    """The names in one of the plan's lists, whether it holds names or (name, why) pairs."""
    found: set[str] = set()
    for item in items:
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, tuple) and item and isinstance(item[0], str):
            found.add(item[0])
        else:
            found.add(str(getattr(item, "name", item)))
    return found


@pytest.mark.parametrize(
    ("count", "keep", "moved"),
    [
        pytest.param(6, 4, 2, id="six-keep-four"),
        pytest.param(5, 1, 4, id="keep-only-the-newest"),
        pytest.param(4, 4, 0, id="exactly-keep"),
        pytest.param(3, 10, 0, id="fewer-than-keep"),
    ],
)
def test_plan_moves_every_release_but_the_keep_newest(count: int, keep: int, moved: int) -> None:
    releases = weekly_releases(count)
    done = plan(releases, keep, [])
    assert named(done.move) == set(releases[keep:])
    assert len(named(done.move)) == moved
    assert releases[0] not in named(done.move)


def test_plan_does_not_depend_on_the_order_of_the_names() -> None:
    releases = weekly_releases(7)
    shuffled = releases[:]
    random.Random(32).shuffle(shuffled)
    assert named(plan(shuffled, 4, []).move) == set(releases[4:])


def test_plan_never_moves_the_newest_release_whatever_keep_is() -> None:
    releases = weekly_releases(5)
    try:
        done = plan(releases, 0, [])
    except ValueError:
        return  # refusing keep < 1 keeps the newest too
    assert releases[0] not in named(done.move)
    assert named(done.move) == set(releases[1:])


NOT_DATES = [
    # Hostile halves of "a release is a YYYY.MM.DD date": names that look like one.
    "latest",
    "2026.10.1",
    "2026-10-25",
    "2026.13.01",
    "2026.02.30",
    "2026.10.25.bak",
    "x2026.10.25",
    "1999.00.00",
    "20261025",
]


def test_plan_leaves_every_name_that_is_not_a_date_untouched_and_reports_it() -> None:
    releases = weekly_releases(6)
    done = plan([*NOT_DATES, *releases], 4, [])
    assert named(done.move) == set(releases[4:])
    assert not named(done.move) & set(NOT_DATES)
    assert not named(done.purge) & set(NOT_DATES)
    assert set(NOT_DATES) <= named(done.skipped)


def test_plan_never_moves_the_pruned_directory_itself() -> None:
    done = plan([".pruned", *weekly_releases(6)], 4, [])
    assert ".pruned" not in named(done.move)


def test_plan_treats_a_real_leap_day_as_a_release() -> None:
    """Converse half: a valid but unusual date is a release, not a skipped name."""
    releases = [*weekly_releases(4), "2024.02.29"]
    done = plan([*releases, "2026.02.29"], 4, [])
    assert "2024.02.29" in named(done.move)
    assert "2026.02.29" not in named(done.move)
    assert "2026.02.29" in named(done.skipped)


def test_plan_purges_nothing_when_no_pruned_entry_is_offered() -> None:
    done = plan(weekly_releases(12), 4, [])
    assert named(done.purge) == set()


def test_plan_purges_only_pruned_entries_far_older_than_the_kept_releases() -> None:
    kept = weekly_releases(4)  # weeks 0-3 stay in the output root
    pruned = weekly_releases(8, start=4)  # weeks 4-11 sit in .pruned/
    done = plan(kept, 4, [*pruned, "notes"])
    purged = named(done.purge)
    assert purged <= set(pruned), purged
    assert pruned[0] not in purged, "the newest pruned release went at once"
    assert set(pruned[4:]) <= purged, "releases 8+ weeks older than the newest kept stayed"
    assert "notes" not in purged
    assert not purged & set(kept)
    assert named(done.move) == set()
