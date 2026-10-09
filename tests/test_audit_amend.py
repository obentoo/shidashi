"""``Recorder.amend``: results added to a step after it ended (story 019, 3.3)."""

from pathlib import Path

from shidashi import audit


def _manifest(tmp: Path, body: object) -> dict[str, object]:
    with audit.run(tmp / "runs", command="factory", argv=[]) as trail:
        assert callable(body)
        body(audit.current())
    return audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))


def test_an_amend_reaches_the_ended_step_and_extends_its_lists(tmp_path: Path) -> None:
    def body(run: audit.Recorder) -> None:
        with run.step("stage:gnome"), run.step("stale-binpkgs") as step:
            step.add(quarantined=["a.gpkg.tar"], excluded=["x11-libs/vte"])
            path = step.path
        run.amend(path, quarantined=["b.gpkg.tar"])

    steps = _manifest(tmp_path, body)["steps"]
    assert isinstance(steps, list)
    (stale,) = [s for s in steps if s["step"] == "stage:gnome/stale-binpkgs"]
    assert stale["quarantined"] == ["a.gpkg.tar", "b.gpkg.tar"]
    assert stale["excluded"] == ["x11-libs/vte"]


def test_an_amend_of_a_step_that_never_ended_is_kept_apart(tmp_path: Path) -> None:
    def body(run: audit.Recorder) -> None:
        run.amend("stage:gnome/nowhere", quarantined=["c.gpkg.tar"])

    manifest = _manifest(tmp_path, body)
    assert manifest["amendments"] == [
        {"target": "stage:gnome/nowhere", "quarantined": ["c.gpkg.tar"]}
    ]


def test_outside_a_run_an_amend_records_nothing() -> None:
    audit.Recorder().amend("anything", quarantined=["d.gpkg.tar"])
