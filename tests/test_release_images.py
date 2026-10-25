"""``shidashi release --images`` (story 032, task 3.1; R2.1, R2.2).

Contract (story.md R2, design.md "shidashi release --unsigned and --images"): ``--images
LIST`` is a comma list of ``config.target_names()``; exactly those images reach every child
build -- one ``--images`` option, no longer the hard-coded ``all`` -- and ``all`` when the
option is not given. A name that is not a configured target exits 1 naming it, before any
build.

Driven through the real CLI with story 030's release lab (tests/test_release_cli.py). The
signed path (``--key``) is used so this task does not depend on ``--unsigned``.
"""

from pathlib import Path

import pytest

from shidashi import config
from tests.test_release_cli import ChildCall, Lab, all_pairs, release_lab
from tests.test_release_core import FPR

WEEKLY = "minimal,kde,gnome"


@pytest.fixture
def images_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


def _images_of(call: ChildCall) -> list[str]:
    """The images one child build was asked for: the value of its one ``--images``."""
    at = [i for i, arg in enumerate(call.argv) if arg == "--images"]
    assert len(at) == 1, f"expected exactly one --images in {call.argv}"
    return call.argv[at[0] + 1].split(",")


def test_the_targets_of_the_weekly_run_are_configured_targets() -> None:
    """The fixture's premise: the three weekly images are real targets."""
    assert set(WEEKLY.split(",")) <= set(config.target_names())


def test_images_given_reach_the_child_build_exactly(images_lab: Lab) -> None:
    result = images_lab.release("v3", "systemd", "--key", FPR, "--images", WEEKLY)
    assert images_lab.builds.calls, result.output
    for call in images_lab.builds.calls:
        assert _images_of(call) == WEEKLY.split(","), call.argv


def test_images_given_reach_every_child_build_of_all_variants(images_lab: Lab) -> None:
    result = images_lab.release("--all", "--key", FPR, "--images", WEEKLY)
    assert sorted((c.arch, c.init) for c in images_lab.builds.calls) == sorted(all_pairs()), (
        result.output
    )
    for call in images_lab.builds.calls:
        assert _images_of(call) == WEEKLY.split(","), call.argv


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="option-absent"),
        # Converse half: an explicit `all` is the same request as no option at all.
        pytest.param(["--images", "all"], id="explicit-all"),
    ],
)
def test_without_a_list_every_child_builds_all_images(images_lab: Lab, extra: list[str]) -> None:
    result = images_lab.release("v3", "systemd", "--key", FPR, *extra)
    assert images_lab.builds.calls, result.output
    for call in images_lab.builds.calls:
        assert _images_of(call) == ["all"], call.argv


@pytest.mark.parametrize(
    ("images", "unknown"),
    [
        pytest.param("minimal,plasma", "plasma", id="no-such-image"),
        # Hostile halves: stage names that are NOT targets (config.stage_names() lists them)
        # must not pass for images.
        pytest.param("minimal,desktop", "desktop", id="a-stage-not-a-target"),
        pytest.param("base,kde", "base", id="the-base-stage"),
    ],
)
def test_an_image_that_is_not_a_target_exits_1_naming_it_before_any_build(
    images_lab: Lab, images: str, unknown: str
) -> None:
    assert unknown not in config.target_names(), "the fixture's name became a target"
    result = images_lab.release("v3", "systemd", "--key", FPR, "--images", images)
    assert result.exit_code == 1, result.output
    assert unknown in result.output
    assert images_lab.builds.calls == []
