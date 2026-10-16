"""Unit tests of shidashi.schedule -- the node model, free threads and the estimator.

Contract (design.md v3, story 014): ``Node``, ``ImageSpec``, ``Estimate``,
``free_threads(node) = max(1.0, threads - load1)``, ``with_history(node, history)``
(the one writer of ``Node.sec_per_pkg``) and
``estimate(image, node, history) -> Estimate(seconds, trunk_seconds, source)``.
The history is a duck-typed stand-in for ``History`` (``duration(key)``,
``trunk_duration(trunk_key)``, ``sec_per_pkg()``, ``run_ids()``); image keys are
``<arch>-<image>-<init>``.

v3 (R2.5): a trunk is identified by its trunk IMAGE KEY (``v3-desktop-systemd``), never
by a checkpoint id: ``ImageSpec.trunk_key`` is compared with ``Node.trunks``, the set of
image keys that claim the node's install checkpoints. Install fingerprints embed cache
paths that differ between the host and a worker, so a checkpoint id never matches across
nodes. ``ImageSpec`` has no ``trunk`` field any more; it carries the target name
``image`` the children take.
"""

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass, field

import pytest

from shidashi import schedule

#: The desktop trunk's image key, as image_specs makes it and as checkpoints are claimed.
DESKTOP_KEY = "v3-desktop-systemd"


@dataclass
class FakeHistory:
    """What ``load_history`` returns for one node: durations by image and by trunk key."""

    images: dict[str, float] = field(default_factory=dict)
    trunks: dict[str, float] = field(default_factory=dict)
    per_pkg: float | None = None

    def duration(self, image_key: str) -> float | None:
        return self.images.get(image_key)

    def trunk_duration(self, trunk_key: str) -> float | None:
        return self.trunks.get(trunk_key)

    def sec_per_pkg(self) -> float | None:
        return self.per_pkg

    def run_ids(self) -> list[str]:
        return []


def _node(
    *,
    name: str = "host",
    kind: str = "host",
    threads: int = 16,
    load1: float = 0.0,
    arches: Iterable[str] = ("v3",),
    trunks: Iterable[str] = (),
    sec_per_pkg: float | None = None,
) -> schedule.Node:
    return schedule.Node(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        threads=threads,
        load1=load1,
        runnable_arches=frozenset(arches),
        trunks=frozenset(trunks),
        sec_per_pkg=sec_per_pkg,
        reachable=True,
    )


def _image(
    name: str = "kde",
    *,
    trunk: bool = False,
    packages: int = 1000,
    trunk_packages: int = 700,
) -> schedule.ImageSpec:
    """``trunk=True`` gives the image the desktop trunk (its key ``v3-desktop-systemd``)."""
    return schedule.ImageSpec(
        key=f"v3-{name}-systemd",
        arch="v3",
        image=name,
        packages=packages,
        trunk_key=DESKTOP_KEY if trunk else None,
        trunk_packages=trunk_packages if trunk else 0,
    )


# --- the v3 model --------------------------------------------------------------------------


def test_image_spec_names_its_trunk_by_key_and_carries_its_target_name() -> None:
    names = {f.name for f in dataclasses.fields(schedule.ImageSpec)}
    assert {"key", "arch", "image", "packages", "trunk_key", "trunk_packages"} <= names
    assert "trunk" not in names  # no checkpoint id: it never matched across nodes
    spec = _image("kde", trunk=True)
    assert spec.image == "kde" and spec.trunk_key == DESKTOP_KEY


def test_outcome_says_whether_its_iso_has_an_sbom_and_defaults_to_yes() -> None:
    names = {f.name for f in dataclasses.fields(schedule.Outcome)}
    assert "sbom" in names
    outcome = schedule.Outcome(
        image="v3-kde-systemd", node="host", ok=True, duration_s=1.0, estimate_s=1.0, log=None
    )
    assert outcome.sbom is True


# --- free_threads (R2.4) -----------------------------------------------------------------


def test_free_threads_is_threads_minus_the_one_minute_load() -> None:
    assert schedule.free_threads(_node(threads=16, load1=4.5)) == pytest.approx(11.5)
    assert schedule.free_threads(_node(threads=16, load1=0.0)) == pytest.approx(16.0)


def test_free_threads_never_goes_below_one_thread() -> None:
    # a running factory makes the load exceed the threads: the node is busy, not dead
    assert schedule.free_threads(_node(threads=16, load1=15.5)) == pytest.approx(1.0)
    assert schedule.free_threads(_node(threads=16, load1=40.0)) == pytest.approx(1.0)


# --- estimate: where the cost comes from (R2.1, R2.2, R2.3) ------------------------------


def test_estimate_prefers_the_history_over_the_package_rate() -> None:
    # both sources are present and disagree: the measured run wins
    history = FakeHistory(images={"v3-kde-systemd": 300.0}, per_pkg=2.0)
    got = schedule.estimate(_image(packages=1000), _node(sec_per_pkg=2.0), history)
    assert got.source == "history"
    assert got.seconds == pytest.approx(300.0)


def test_estimate_without_history_uses_the_nodes_own_seconds_per_package() -> None:
    # 2.0 s/pkg is the node's measured rate, not the 1.7 s default
    history = FakeHistory(per_pkg=2.0)
    got = schedule.estimate(_image(packages=1000), _node(sec_per_pkg=2.0), history)
    assert got.source == "packages"
    assert got.seconds == pytest.approx(2000.0)


def test_estimate_without_any_measure_uses_1_7_seconds_per_package() -> None:
    got = schedule.estimate(_image(packages=1000), _node(sec_per_pkg=None), FakeHistory())
    assert got.source == "default"
    assert got.seconds == pytest.approx(1700.0)


def test_estimate_history_of_another_image_is_not_this_images_history() -> None:
    # openrc's run of kde is a different image: kde/systemd has no history
    history = FakeHistory(images={"v3-kde-openrc": 300.0, "znver5-kde-systemd": 250.0})
    got = schedule.estimate(_image(packages=1000), _node(), history)
    assert got.source != "history"
    assert got.seconds == pytest.approx(1700.0)


# --- estimate: free-thread scaling (R2.4) ------------------------------------------------


def test_estimate_divides_the_cost_by_the_share_of_free_threads() -> None:
    history = FakeHistory(images={"v3-kde-systemd": 100.0})
    idle = schedule.estimate(_image(), _node(threads=16, load1=0.0), history)
    half = schedule.estimate(_image(), _node(threads=16, load1=8.0), history)
    assert idle.seconds == pytest.approx(100.0)
    assert half.seconds == pytest.approx(200.0)  # half the threads free: twice as long


def test_estimate_on_a_saturated_node_is_capped_by_the_one_thread_floor() -> None:
    history = FakeHistory(images={"v3-kde-systemd": 100.0})
    busy = schedule.estimate(_image(), _node(threads=16, load1=40.0), history)
    assert busy.seconds == pytest.approx(1600.0)  # 16 / max(1, 16 - 40) = 16x


def test_estimate_scales_the_trunk_cost_by_the_same_free_share() -> None:
    history = FakeHistory(images={"v3-kde-systemd": 100.0}, trunks={DESKTOP_KEY: 1000.0})
    image = _image(trunk=True)
    idle = schedule.estimate(image, _node(threads=16, load1=0.0), history)
    half = schedule.estimate(image, _node(threads=16, load1=8.0), history)
    assert idle.trunk_seconds == pytest.approx(1000.0)
    assert half.trunk_seconds == pytest.approx(2 * idle.trunk_seconds)


# --- estimate: the trunk, matched by its claimant key (R2.5) -----------------------------


def test_estimate_a_node_claiming_other_trunk_keys_does_not_zero_this_trunk() -> None:
    # hostile collapse: near-identical keys that denote other trunks or other images --
    # another init, another arch, a prefix, the image's OWN key (its own install
    # checkpoint), and the old checkpoint-id spelling of the same trunk
    history = FakeHistory(images={"v3-kde-systemd": 100.0}, trunks={DESKTOP_KEY: 1000.0})
    node = _node(
        trunks=(
            "v3-desktop-openrc",
            "znver5-desktop-systemd",
            "v3-desktop",
            "v3-kde-systemd",
            "v3-systemd/install-" + "d" * 24,
        )
    )
    got = schedule.estimate(_image(trunk=True), node, history)
    assert got.trunk_seconds == pytest.approx(1000.0)


def test_estimate_one_claimed_trunk_key_serves_every_image_that_shares_it() -> None:
    # hostile split: kde, gnome and wm all branch from the one desktop trunk the node holds
    history = FakeHistory(
        images={"v3-kde-systemd": 100.0, "v3-gnome-systemd": 120.0, "v3-wm-systemd": 90.0},
        trunks={DESKTOP_KEY: 1000.0},
    )
    node = _node(trunks=(DESKTOP_KEY, "v3-kde-systemd"))
    for name in ("kde", "gnome", "wm"):
        got = schedule.estimate(_image(name, trunk=True), node, history)
        assert got.trunk_seconds == 0, name


def test_estimate_counts_the_trunk_when_the_node_does_not_hold_it() -> None:
    history = FakeHistory(images={"v3-kde-systemd": 100.0}, trunks={DESKTOP_KEY: 1000.0})
    got = schedule.estimate(_image(trunk=True), _node(trunks=()), history)
    assert got.trunk_seconds > 0
    assert got.seconds == pytest.approx(100.0)  # the trunk is apart from the image's own cost


def test_estimate_the_hosts_trunk_does_not_count_for_the_worker() -> None:
    history = FakeHistory(images={"v3-kde-systemd": 100.0}, trunks={DESKTOP_KEY: 1000.0})
    image = _image(trunk=True)
    host = _node(name="dojo", kind="host", trunks=(DESKTOP_KEY,))
    worker = _node(name="bentoo-lab", kind="worker", trunks=())
    assert schedule.estimate(image, host, history).trunk_seconds == 0
    assert schedule.estimate(image, worker, history).trunk_seconds > 0


def test_estimate_an_image_without_a_trunk_has_no_trunk_cost() -> None:
    history = FakeHistory(images={"v3-minimal-systemd": 100.0}, trunks={DESKTOP_KEY: 1000.0})
    got = schedule.estimate(_image("minimal", trunk=False), _node(), history)
    assert got.trunk_seconds == 0


# --- with_history: the node's measured rate (R2.2) ---------------------------------------


def test_with_history_sets_the_nodes_rate_from_its_history_and_nothing_else() -> None:
    node = _node(name="bentoo-lab", kind="worker", threads=16, load1=2.0, trunks=(DESKTOP_KEY,))
    got = schedule.with_history(node, FakeHistory(per_pkg=0.5))
    assert got.sec_per_pkg == pytest.approx(0.5)
    assert (got.name, got.kind, got.threads, got.load1, got.trunks, got.reachable) == (
        node.name,
        node.kind,
        node.threads,
        node.load1,
        node.trunks,
        node.reachable,
    )
    # and the estimate then uses it, not the 1.7 s default (scaled by 16 / (16 - 2) free, R2.4)
    est = schedule.estimate(_image(packages=1000), got, FakeHistory(per_pkg=0.5))
    assert est.source == "packages" and est.seconds == pytest.approx(500.0 * 16 / 14)


def test_with_history_of_a_node_without_measures_leaves_no_rate() -> None:
    assert schedule.with_history(_node(), FakeHistory()).sec_per_pkg is None


# --- edge sources ------------------------------------------------------------------------


def test_estimate_a_zero_or_negative_history_duration_falls_through_to_packages() -> None:
    for bad in (0.0, -5.0):
        history = FakeHistory(images={"v3-kde-systemd": bad})
        got = schedule.estimate(_image(packages=1000), _node(sec_per_pkg=2.0), history)
        assert got.source == "packages" and got.seconds == pytest.approx(2000.0), bad


def test_estimate_a_trunk_without_history_costs_its_packages_at_the_nodes_rate() -> None:
    history = FakeHistory(images={"v3-kde-systemd": 100.0})  # no trunk run anywhere
    got = schedule.estimate(_image(trunk=True, trunk_packages=700), _node(sec_per_pkg=2.0), history)
    assert got.trunk_seconds == pytest.approx(1400.0)


def test_estimate_a_trunk_without_history_or_rate_costs_its_packages_at_1_7_s() -> None:
    got = schedule.estimate(_image(trunk=True, trunk_packages=700), _node(), FakeHistory())
    assert got.trunk_seconds == pytest.approx(1190.0)


def test_estimate_a_trunk_with_its_own_history_uses_that_duration() -> None:
    history = FakeHistory(images={"v3-kde-systemd": 100.0}, trunks={DESKTOP_KEY: 1163.0})
    got = schedule.estimate(_image(trunk=True), _node(sec_per_pkg=2.0), history)
    assert got.trunk_seconds == pytest.approx(1163.0)
