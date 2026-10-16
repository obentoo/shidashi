"""Unit tests of shidashi.schedule.render_plan / render_report -- what the CLI prints.

Contract (design.md, story 014):
- ``render_plan(plan, estimates) -> list[str]``: one line per image (node, estimate,
  source), the search method, and each unreachable worker with its reason (R1.5, R3.1).
- ``render_report(plan, outcomes) -> tuple[int, list[str]]``: exit 1 when any image
  failed, else 0; one line per image with its node, measured and estimated
  durations, its ISO or its failure with the log path (R4.4, R4.5); the plan's
  skipped workers with their reasons (R3.1).

Durations are matched in any of the usual spellings (``754``, ``754s``,
``12m34s``, ``12:34``, ``13m``, ``12.6``) so the test pins the content, not the format.
"""

from pathlib import Path

from shidashi import schedule


def _spellings(seconds: float) -> list[str]:
    whole = int(round(seconds))
    minutes, secs = divmod(whole, 60)
    return [
        str(whole),
        f"{whole}s",
        f"{minutes}m{secs:02d}s",
        f"{minutes}:{secs:02d}",
        f"{round(seconds / 60)}m",
        f"{seconds / 60:.1f}",
    ]


def _mentions_duration(line: str, seconds: float) -> bool:
    return any(s in line for s in _spellings(seconds))


def _lines_naming(lines: list[str], word: str) -> list[str]:
    return [line for line in lines if word in line]


def _plan(
    assignments: dict[str, str],
    *,
    method: str = "exhaustive",
    skipped: dict[str, str] | None = None,
    unplaceable: dict[str, str] | None = None,
) -> schedule.Plan:
    return schedule.Plan(
        assignments=assignments,
        finish={},
        makespan=0.0,
        method=method,  # type: ignore[arg-type]
        skipped_nodes=skipped or {},
        unplaceable=unplaceable or {},
    )


def _est(seconds: float, source: str = "history") -> schedule.Estimate:
    return schedule.Estimate(seconds=seconds, trunk_seconds=0.0, source=source)  # type: ignore[arg-type]


# --- render_plan -------------------------------------------------------------------------


def test_render_plan_two_images_with_the_same_node_and_estimate_stay_distinguishable() -> None:
    # hostile: identical detail on two lines -- each must still say which image it is
    plan = _plan({"v3-kde-systemd": "bentoo-lab", "v3-gnome-systemd": "bentoo-lab"})
    estimates = {
        ("v3-kde-systemd", "bentoo-lab"): _est(754.0),
        ("v3-gnome-systemd", "bentoo-lab"): _est(754.0),
    }
    lines = schedule.render_plan(plan, estimates)
    (kde,) = _lines_naming(lines, "kde")
    (gnome,) = _lines_naming(lines, "gnome")
    assert kde != gnome
    assert "gnome" not in kde and "kde" not in gnome


def test_render_plan_names_each_images_node_estimate_and_source() -> None:
    plan = _plan({"v3-minimal-systemd": "host", "v3-kde-systemd": "bentoo-lab"})
    estimates = {
        ("v3-minimal-systemd", "host"): _est(754.0, "history"),
        ("v3-kde-systemd", "bentoo-lab"): _est(2040.0, "packages"),
        # the image's cost on the node it was NOT given must not be the one shown
        ("v3-minimal-systemd", "bentoo-lab"): _est(9999.0, "default"),
        ("v3-kde-systemd", "host"): _est(8888.0, "default"),
    }
    lines = schedule.render_plan(plan, estimates)
    (minimal,) = _lines_naming(lines, "minimal")
    (kde,) = _lines_naming(lines, "kde")
    assert "host" in minimal and _mentions_duration(minimal, 754.0) and "history" in minimal
    assert "bentoo-lab" in kde and _mentions_duration(kde, 2040.0) and "packages" in kde
    assert not _mentions_duration(minimal, 9999.0) and not _mentions_duration(kde, 8888.0)


def test_render_plan_says_which_search_was_used() -> None:
    estimates = {("v3-kde-systemd", "host"): _est(100.0)}
    for method in ("exhaustive", "greedy"):
        lines = schedule.render_plan(_plan({"v3-kde-systemd": "host"}, method=method), estimates)
        assert any(method in line for line in lines), method


def test_render_plan_names_each_worker_that_did_not_answer_and_why() -> None:
    plan = _plan(
        {"v3-kde-systemd": "host"},
        skipped={"bentoo-lab": "status probe timed out after 10 s"},
    )
    lines = schedule.render_plan(plan, {("v3-kde-systemd", "host"): _est(100.0)})
    (skipped,) = _lines_naming(lines, "bentoo-lab")
    assert "timed out after 10 s" in skipped


# --- render_report -----------------------------------------------------------------------


def _outcome(
    image: str,
    node: str,
    *,
    ok: bool = True,
    duration: float = 1500.0,
    estimate: float = 1200.0,
    log: Path | None = None,
    isos: tuple[Path, ...] = (),
    error: str = "",
) -> schedule.Outcome:
    return schedule.Outcome(
        image=image,
        node=node,
        ok=ok,
        duration_s=duration,
        estimate_s=estimate,
        log=log,
        isos=isos,
        error=error,
    )


def test_render_report_blames_the_failed_image_alone_with_its_node_and_log() -> None:
    # hostile: two images, two nodes, two logs -- the failure belongs to one of them
    lab_log = Path("/var/lib/shidashi/logs/build-r1-kde.log")
    host_log = Path("/var/lib/shidashi/logs/build-r1-minimal.log")
    outcomes = [
        _outcome(
            "v3-minimal-systemd", "host", log=host_log, isos=(Path("/out/bentoo-minimal.iso"),)
        ),
        _outcome("v3-kde-systemd", "bentoo-lab", ok=False, log=lab_log, error="job exited 1"),
    ]
    code, lines = schedule.render_report(_plan({}), outcomes)
    assert code == 1
    # a summary line naming the failure is allowed; the blame must be right on every line
    assert any(
        "bentoo-lab" in line and str(lab_log) in line for line in _lines_naming(lines, "kde")
    )
    for line in _lines_naming(lines, "minimal"):
        assert str(lab_log) not in line and "bentoo-lab" not in line


def test_render_report_all_ok_exits_0_with_node_measured_estimate_and_iso() -> None:
    iso = Path("/out/bentoo-v3-kde-systemd.iso")
    outcomes = [
        _outcome("v3-kde-systemd", "bentoo-lab", duration=1500.0, estimate=1200.0, isos=(iso,)),
        _outcome(
            "v3-minimal-systemd",
            "host",
            duration=754.0,
            estimate=700.0,
            isos=(Path("/out/bentoo-v3-minimal-systemd.iso"),),
        ),
    ]
    code, lines = schedule.render_report(_plan({}), outcomes)
    assert code == 0
    (kde,) = _lines_naming(lines, "-kde-")
    assert "bentoo-lab" in kde
    assert _mentions_duration(kde, 1500.0) and _mentions_duration(kde, 1200.0)
    assert iso.name in kde


def test_render_report_an_ok_image_with_a_log_is_not_a_failure() -> None:
    outcomes = [
        _outcome("v3-kde-systemd", "host", log=Path("/logs/kde.log"), isos=(Path("/o/k.iso"),))
    ]
    code, _lines = schedule.render_report(_plan({}), outcomes)
    assert code == 0


def test_render_report_an_image_no_node_runs_is_failed_with_the_reason() -> None:
    outcomes = [
        _outcome(
            "arrowlake-kde-systemd", "", ok=False, duration=0.0, error="no node runs arrowlake"
        )
    ]
    code, lines = schedule.render_report(_plan({}), outcomes)
    assert code == 1
    assert any("no node runs arrowlake" in line for line in _lines_naming(lines, "kde"))


def test_render_plan_names_an_image_no_node_runs() -> None:
    plan = _plan(
        {"v3-kde-systemd": "host"},
        unplaceable={"arrowlake-kde-systemd": "no node runs arrowlake"},
    )
    lines = schedule.render_plan(plan, {("v3-kde-systemd", "host"): _est(100.0)})
    assert any(
        "arrowlake-kde-systemd" in line and "no node runs arrowlake" in line for line in lines
    )


def test_render_report_names_each_worker_that_did_not_answer_and_why() -> None:
    # R3.1: a host-only build because the worker was silent says so in its report too
    plan = _plan({"v3-kde-systemd": "host"}, skipped={"bentoo-lab": "status probe timed out"})
    outcomes = [_outcome("v3-kde-systemd", "host", isos=(Path("/o/bentoo-v3-kde-systemd.iso"),))]
    code, lines = schedule.render_report(plan, outcomes)
    assert code == 0
    assert any("bentoo-lab" in line and "timed out" in line for line in lines)
