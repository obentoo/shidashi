"""Unit tests of shidashi.schedule.plan -- which node makes which image.

Contract (design.md, story 014):
``plan(images, nodes, estimates) -> Plan(assignments, finish, makespan, method,
 skipped_nodes, unplaceable)``
where ``estimates`` is keyed by ``(image key, node name)``. A node's finish time is
the sum of its images' ``seconds`` plus each distinct trunk's ``trunk_seconds`` once;
the plan minimizes the slowest node's finish (the makespan), ties broken by fewer
trunks installed, then more work on the host, then deterministically. Exhaustive
while ``nodes ** images <= 4096``, greedy (longest first, earliest-finishing node) above.
"""

import itertools
from collections.abc import Iterable, Mapping, Sequence

import pytest

from shidashi import schedule


def _node(
    name: str,
    *,
    kind: str = "worker",
    arches: Iterable[str] = ("v3",),
    reachable: bool = True,
    note: str = "",
) -> schedule.Node:
    return schedule.Node(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        threads=16,
        load1=0.0,
        runnable_arches=frozenset(arches),
        trunks=frozenset(),
        sec_per_pkg=None,
        reachable=reachable,
        note=note,
    )


def _image(key: str, *, arch: str = "v3", trunk: str | None = None) -> schedule.ImageSpec:
    image = key.removeprefix(f"{arch}-").rsplit("-", 1)[0]
    trunk_key = f"{arch}-{trunk}-systemd" if trunk else None
    return schedule.ImageSpec(key=key, arch=arch, image=image, packages=1000, trunk_key=trunk_key)


def _estimates(
    images: Sequence[schedule.ImageSpec],
    nodes: Sequence[schedule.Node],
    seconds: Mapping[str, float],
    trunk: float = 0.0,
) -> dict[tuple[str, str], schedule.Estimate]:
    """The same cost of each image on every node (equal machines)."""
    return {
        (image.key, node.name): schedule.Estimate(
            seconds=seconds[image.key],
            trunk_seconds=trunk if image.trunk_key else 0.0,
            source="history",
        )
        for image in images
        for node in nodes
    }


def _check_shape(got: schedule.Plan, images: Sequence[schedule.ImageSpec]) -> None:
    # R1.1: every placeable image on exactly one node; the makespan is the slowest node
    assert set(got.assignments) == {i.key for i in images}
    assert got.makespan == pytest.approx(max(got.finish.values()))


# --- the filter (R1.2, R3.1) -------------------------------------------------------------


def test_plan_never_gives_a_worker_an_arch_its_cpu_cannot_run() -> None:
    images = [
        _image("znver5-kde-systemd", arch="znver5"),
        _image("znver5-minimal-systemd", arch="znver5"),
    ]
    host = _node("host", kind="host", arches=("v3", "znver5"))
    lab = _node("bentoo-lab", arches=("v3",))  # Zen 3: no AVX-512
    estimates = _estimates(images, [host, lab], {i.key: 1000.0 for i in images})
    # the worker would halve the makespan -- if it could run znver5
    got = schedule.plan(images, [host, lab], estimates)
    assert set(got.assignments.values()) == {"host"}


def test_plan_gives_a_worker_the_arch_it_can_run() -> None:
    images = [_image("v3-kde-systemd"), _image("v3-minimal-systemd")]
    host = _node("host", kind="host", arches=("v3", "znver5"))
    lab = _node("bentoo-lab", arches=("v3",))
    got = schedule.plan(
        images, [host, lab], _estimates(images, [host, lab], {i.key: 1000.0 for i in images})
    )
    _check_shape(got, images)
    assert sorted(got.assignments.values()) == ["bentoo-lab", "host"]
    assert got.makespan == pytest.approx(1000.0)


def test_plan_skips_an_unreachable_worker_and_says_why() -> None:
    images = [_image("v3-kde-systemd"), _image("v3-minimal-systemd")]
    host = _node("host", kind="host")
    lab = _node("bentoo-lab", reachable=False, note="status probe timed out after 10 s")
    got = schedule.plan(
        images, [host, lab], _estimates(images, [host, lab], {i.key: 100.0 for i in images})
    )
    _check_shape(got, images)
    assert set(got.assignments.values()) == {"host"}
    assert "bentoo-lab" in got.skipped_nodes
    assert "timed out" in got.skipped_nodes["bentoo-lab"]


def test_plan_leaves_out_an_image_no_node_can_run_and_places_the_others() -> None:
    images = [_image("arrowlake-kde-systemd", arch="arrowlake"), _image("v3-kde-systemd")]
    host = _node("host", kind="host", arches=("v3", "znver5"))
    lab = _node("bentoo-lab", arches=("v3",))
    got = schedule.plan(
        images, [host, lab], _estimates(images, [host, lab], {i.key: 100.0 for i in images})
    )
    assert "arrowlake-kde-systemd" not in got.assignments
    assert "v3-kde-systemd" in got.assignments
    # R1.7: the image left out is recorded, with the arch nobody runs
    assert got.unplaceable == {"arrowlake-kde-systemd": "no node runs arrowlake"}


def test_plan_records_no_unplaceable_image_when_every_image_has_a_node() -> None:
    images = [_image("v3-kde-systemd"), _image("v3-minimal-systemd")]
    host = _node("host", kind="host")
    got = schedule.plan(images, [host], _estimates(images, [host], {i.key: 100.0 for i in images}))
    assert got.unplaceable == {}


# --- the makespan, with a trunk counted once per node (R1.3) ------------------------------


def test_plan_finds_the_optimum_where_longest_first_would_miss_it() -> None:
    # greedy longest-first gives 700 here; the optimum is 300+300 | 200+200+200 = 600
    seconds = {"a": 300.0, "b": 300.0, "c": 200.0, "d": 200.0, "e": 200.0}
    images = [_image(k) for k in seconds]
    nodes = [_node("host", kind="host"), _node("bentoo-lab")]
    got = schedule.plan(images, nodes, _estimates(images, nodes, seconds))
    _check_shape(got, images)
    assert got.method == "exhaustive"
    assert got.makespan == pytest.approx(600.0)


def test_plan_counts_two_different_trunks_on_one_node_twice() -> None:
    # hostile collapse: "server" and "desktop" are two installs, not one
    images = [_image("v3-kde-systemd", trunk="desktop"), _image("v3-web-systemd", trunk="server")]
    host = _node("host", kind="host")
    lab = _node("bentoo-lab", arches=("znver5",))  # cannot help: everything on the host
    estimates = _estimates(images, [host, lab], {i.key: 100.0 for i in images}, trunk=1000.0)
    got = schedule.plan(images, [host, lab], estimates)
    assert got.finish["host"] == pytest.approx(100 + 100 + 1000 + 1000)


def test_plan_counts_a_shared_trunk_once_and_keeps_its_images_together() -> None:
    # hostile split: kde and gnome share "desktop" -- one install serves both.
    # trunk once: {kde, gnome} = 1000+100+100 = 1200 | {minimal} = 900 -> 1200;
    # splitting the pair would cost a second trunk: {kde, minimal} = 2000.
    seconds = {"v3-kde-systemd": 100.0, "v3-gnome-systemd": 100.0, "v3-minimal-systemd": 900.0}
    images = [
        _image("v3-kde-systemd", trunk="desktop"),
        _image("v3-gnome-systemd", trunk="desktop"),
        _image("v3-minimal-systemd"),
    ]
    nodes = [_node("host", kind="host"), _node("bentoo-lab")]
    got = schedule.plan(images, nodes, _estimates(images, nodes, seconds, trunk=1000.0))
    _check_shape(got, images)
    assert got.assignments["v3-kde-systemd"] == got.assignments["v3-gnome-systemd"]
    assert got.assignments["v3-minimal-systemd"] != got.assignments["v3-kde-systemd"]
    assert got.makespan == pytest.approx(1200.0)


def test_plan_splits_a_trunk_when_installing_it_twice_still_wins() -> None:
    # the trunk is cheap, the flavors are long: {kde} 1100 | {gnome} 1100 beats 2100 together
    seconds = {"v3-kde-systemd": 1000.0, "v3-gnome-systemd": 1000.0}
    images = [_image(k, trunk="desktop") for k in seconds]
    nodes = [_node("host", kind="host"), _node("bentoo-lab")]
    got = schedule.plan(images, nodes, _estimates(images, nodes, seconds, trunk=100.0))
    assert got.assignments["v3-kde-systemd"] != got.assignments["v3-gnome-systemd"]
    assert sorted(got.finish.values()) == pytest.approx([1100.0, 1100.0])  # a trunk on each


# --- the tie-breaks (R1.4) ---------------------------------------------------------------


def test_plan_on_equal_makespans_prefers_fewer_trunks_installed() -> None:
    # {kde, gnome} | {minimal} and {kde, minimal} | {gnome} both finish at 300;
    # the first installs the trunk once, the second twice
    seconds = {"v3-kde-systemd": 100.0, "v3-gnome-systemd": 100.0, "v3-minimal-systemd": 100.0}
    images = [
        _image("v3-kde-systemd", trunk="desktop"),
        _image("v3-gnome-systemd", trunk="desktop"),
        _image("v3-minimal-systemd"),
    ]
    nodes = [_node("host", kind="host"), _node("bentoo-lab")]
    got = schedule.plan(images, nodes, _estimates(images, nodes, seconds, trunk=100.0))
    assert got.makespan == pytest.approx(300.0)
    assert got.assignments["v3-kde-systemd"] == got.assignments["v3-gnome-systemd"]


def test_plan_on_equal_makespans_and_trunks_gives_the_host_more_work() -> None:
    seconds = {"x": 100.0, "y": 60.0}
    images = [_image(k) for k in seconds]
    nodes = [_node("host", kind="host"), _node("bentoo-lab")]
    got = schedule.plan(images, nodes, _estimates(images, nodes, seconds))
    assert got.assignments == {"x": "host", "y": "bentoo-lab"}


def test_plan_the_host_is_the_node_of_kind_host_not_the_node_named_host() -> None:
    # hostile: a worker whose NAME is "host"; the machine running shidashi is "dojo"
    seconds = {"x": 100.0, "y": 60.0}
    images = [_image(k) for k in seconds]
    nodes = [_node("dojo", kind="host"), _node("host", kind="worker")]
    got = schedule.plan(images, nodes, _estimates(images, nodes, seconds))
    assert got.assignments == {"x": "dojo", "y": "host"}


def test_plan_is_the_same_whatever_order_the_nodes_come_in() -> None:
    # two identical workers, two identical images: only the deterministic tie-break decides
    seconds = {"a": 100.0, "b": 100.0}
    images = [_image(k) for k in seconds]
    host = _node("host", kind="host", arches=("znver5",))  # out of the race
    w1, w2 = _node("lab-1"), _node("lab-2")
    first = schedule.plan(images, [host, w1, w2], _estimates(images, [host, w1, w2], seconds))
    again = schedule.plan(images, [host, w1, w2], _estimates(images, [host, w1, w2], seconds))
    swapped = schedule.plan(images, [w2, w1, host], _estimates(images, [w2, w1, host], seconds))
    assert first.assignments == again.assignments == swapped.assignments
    assert sorted(first.assignments.values()) == ["lab-1", "lab-2"]


# --- the search size (Constraints) -------------------------------------------------------


def _optimum(seconds: Sequence[float], nodes: int) -> float:
    best = float("inf")
    for combo in itertools.product(range(nodes), repeat=len(seconds)):
        finish = [0.0] * nodes
        for cost, where in zip(seconds, combo, strict=True):
            finish[where] += cost
        best = min(best, max(finish))
    return best


def _longest_first(seconds: Sequence[float], nodes: int) -> list[float]:
    finish = [0.0] * nodes
    for cost in sorted(seconds, reverse=True):
        finish[finish.index(min(finish))] += cost
    return sorted(finish)


def test_plan_is_exhaustive_at_exactly_4096_combinations() -> None:
    # 2 nodes ** 12 images = 4096; longest-first gives 3300 here, the optimum is 3200
    costs = [800.0, 100.0, 700.0, 700.0, 300.0, 300.0, 800.0, 300.0, 700.0, 500.0, 700.0, 500.0]
    seconds = {f"i{n:02d}": c for n, c in enumerate(costs)}
    images = [_image(k) for k in seconds]
    nodes = [_node("host", kind="host"), _node("bentoo-lab")]
    got = schedule.plan(images, nodes, _estimates(images, nodes, seconds))
    _check_shape(got, images)
    assert got.method == "exhaustive"
    assert got.makespan == pytest.approx(_optimum(costs, 2)) == pytest.approx(3200.0)


def test_plan_is_greedy_longest_first_above_4096_combinations() -> None:
    # 2 ** 13 = 8192 > 4096
    costs = [
        800.0,
        100.0,
        700.0,
        700.0,
        300.0,
        300.0,
        800.0,
        300.0,
        700.0,
        500.0,
        700.0,
        500.0,
        50.0,
    ]
    seconds = {f"i{n:02d}": c for n, c in enumerate(costs)}
    images = [_image(k) for k in seconds]
    nodes = [_node("host", kind="host"), _node("bentoo-lab")]
    got = schedule.plan(images, nodes, _estimates(images, nodes, seconds))
    _check_shape(got, images)
    assert got.method == "greedy"
    assert sorted(got.finish.values()) == pytest.approx(_longest_first(costs, 2))


def test_plan_greedy_still_honours_the_cpu_filter() -> None:
    seconds = {f"i{n:02d}": 100.0 + n for n in range(13)}
    images = [_image(k) for k in seconds]
    images[0] = _image("i00", arch="znver5")
    host = _node("host", kind="host", arches=("v3", "znver5"))
    lab = _node("bentoo-lab", arches=("v3",))
    got = schedule.plan(images, [host, lab], _estimates(images, [host, lab], seconds))
    assert got.method == "greedy"
    assert got.assignments["i00"] == "host"
    assert set(got.assignments) == {i.key for i in images}
