"""Story 028: a build sends an owned arch's images, and its factory, to the owner only."""

from pathlib import Path

from shidashi import schedule
from tests.test_cli_build_schedule import (  # noqa: F401  (lab is a fixture)
    HOST,
    LAB,
    SILENT,
    WORKER,
    Lab,
    _build,
    _plan_step,
    lab,
)
from tests.test_schedule_plan import _estimates, _image, _node

IMAGES = [_image("v3-minimal-systemd"), _image("v3-kde-systemd"), _image("v3-gnome-systemd")]
SECONDS = {"v3-minimal-systemd": 600.0, "v3-kde-systemd": 2000.0, "v3-gnome-systemd": 1800.0}


def _nodes(*, lab_reachable: bool = True) -> list[schedule.Node]:
    return [
        _node("dojo", kind="host", arches=("v3", "znver5")),
        _node("bentoo-lab", reachable=lab_reachable, note="" if lab_reachable else "offline"),
        _node("spare"),
    ]


def test_an_owned_arch_goes_to_its_owner_only() -> None:
    nodes = _nodes()
    got = schedule.plan(IMAGES, nodes, _estimates(IMAGES, nodes, SECONDS), owner="bentoo-lab")
    assert set(got.assignments.values()) == {"bentoo-lab"}
    assert got.unplaceable == {}


def test_an_unreachable_owner_leaves_the_images_unplaced_naming_it() -> None:
    nodes = _nodes(lab_reachable=False)
    got = schedule.plan(IMAGES, nodes, _estimates(IMAGES, nodes, SECONDS), owner="bentoo-lab")
    assert got.assignments == {}
    assert set(got.unplaceable) == {i.key for i in IMAGES}
    assert all("bentoo-lab" in why for why in got.unplaceable.values())


def test_an_owner_missing_from_the_nodes_is_named_too() -> None:
    nodes = [_node("dojo", kind="host")]
    got = schedule.plan(IMAGES, nodes, _estimates(IMAGES, nodes, SECONDS), owner="bentoo-lab")
    assert got.assignments == {}
    assert all("bentoo-lab" in why for why in got.unplaceable.values())


def test_without_an_owner_the_plan_is_unchanged() -> None:
    nodes = _nodes()
    estimates = _estimates(IMAGES, nodes, SECONDS)
    assert schedule.plan(IMAGES, nodes, estimates, owner=None) == schedule.plan(
        IMAGES, nodes, estimates
    )


def _options(cache_owner: str | None = None, owner_factory: bool = False) -> schedule.BuildOptions:
    return schedule.BuildOptions(
        arch="v3",
        init="systemd",
        output_dir=Path("/out"),
        images=("kde",),
        cache_owner=cache_owner,
        owner_factory=owner_factory,
    )


def _job_args(argv: list[str]) -> list[str]:
    return argv[argv.index("--") + 1 :]


def test_the_owners_job_runs_its_own_factory() -> None:
    options = _options(cache_owner="bentoo-lab", owner_factory=True)
    argv = schedule._worker_argv(
        "bentoo-lab", "kde-v3", Path("/r"), [IMAGES[1]], options, user=None
    )
    assert "--skip-factory" not in _job_args(argv)
    assert "--local" in _job_args(argv)


def test_a_skipped_factory_stays_skipped_on_the_owner() -> None:
    options = _options(cache_owner="bentoo-lab", owner_factory=False)
    argv = schedule._worker_argv(
        "bentoo-lab", "kde-v3", Path("/r"), [IMAGES[1]], options, user=None
    )
    assert "--skip-factory" in _job_args(argv)


def test_another_worker_never_runs_the_factory() -> None:
    options = _options()
    argv = schedule._worker_argv("spare", "kde-v3", Path("/r"), [IMAGES[1]], options, user=None)
    assert "--skip-factory" in _job_args(argv)


# --- the build CLI wiring -----------------------------------------------------------------


def _own_v3(name: str) -> None:
    from shidashi import config, workers
    from shidashi.workers import WorkerEntry

    path = config.workers_dir() / "workers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = WorkerEntry(
        name=name,
        host_key="ssh-ed25519 AAAA",
        host_key_fingerprint="SHA256:x",
        paired_at="2026-10-17T00:00:00Z",
        cache_archs=("v3",),
    )
    workers.save_registry(path, {name: entry})


def test_a_build_of_an_owned_arch_runs_no_host_factory_and_gives_the_owner_every_image(
    lab: Lab,  # noqa: F811 (the fixture imported above)
) -> None:
    _own_v3(LAB)
    lab.nodes = [HOST, WORKER]
    result = _build()
    assert result.exit_code == 0, result.output
    assert not [c for c in lab.calls if c.startswith("factory:")]  # the host has no binhost
    ((plan, _specs, kw),) = lab.executed
    assert set(plan.assignments.values()) == {LAB}
    assert kw["options"].cache_owner == LAB and kw["options"].owner_factory


def test_a_build_of_an_owned_arch_whose_owner_is_offline_places_nothing(
    lab: Lab,  # noqa: F811 (the fixture imported above)
) -> None:
    _own_v3(LAB)
    lab.nodes = [HOST, SILENT]
    result = _build("--plan")
    assert LAB in result.output
    assert "dojo" not in _plan_step(lab.root).get("assignments", {}).values()
