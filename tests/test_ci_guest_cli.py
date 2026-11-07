"""``shidashi ci-guest fetch | prepare | run | sweep`` (story 034, task 3.4; R4.3-R4.5, Q4).

Contract (design.md, "shidashi/ci_guest.py", Interface (CLI ``shidashi ci-guest ...``,
worker-only); tasks.md 3.4 Objective): a typer app in ``shidashi/ci_guest_cli.py``,
registered on ``shidashi``'s app as ``ci-guest``.

- every command refuses a caller that is not root: exit 1 naming root, nothing done;
- ``run --job --repo [--memory-gib --disk-gib --timeout-min --min-free-gib]`` calls
  ``ci_guest.run_job`` with those limits (defaults: memory 8 GiB, disk 64 GiB, 360 min,
  20 GiB free) and exits with its code: 0 pass, 1 fail, 3 no room, 4 timeout. There is no
  ``--cpus``: the worker derives the vCPUs from its own CPU count (R7.1);
- ``CiGuestError`` / ``CiGuestSeedError`` -> exit 1 with the message on stderr, no traceback.

``ci_guest``'s functions are replaced per test (the CLI is the unit here); the caller's
euid is stood in by replacing ``os.geteuid``.
"""

import os
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli
from tests._pending import try_import

ci_guest: Any = try_import("shidashi", "ci_guest")
ci_guest_seed: Any = try_import("shidashi", "ci_guest_seed")

ci_guest_runner = CliRunner()
RUN = ["ci-guest", "run", "--job", "9001", "--repo", "obentoo/private-app"]


def invoke(argv: list[str]) -> Result:
    return ci_guest_runner.invoke(cli.app, argv)


def no_traceback(result: Result) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)


@pytest.fixture
def recorded_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """``ci_guest.run_job`` replaced: records its call, returns the rc a test sets."""
    calls: list[dict[str, Any]] = []

    def fake_run_job(job: str, repo: str, **kwargs: Any) -> int:
        calls.append({"job": job, "repo": repo, **kwargs})
        return int(os.environ.get("FAKE_RUN_JOB_RC", "0"))

    monkeypatch.setattr(ci_guest, "run_job", fake_run_job)
    return calls


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["ci-guest", "fetch"], id="fetch"),
        pytest.param(["ci-guest", "prepare"], id="prepare"),
        pytest.param(RUN, id="run"),
        pytest.param(["ci-guest", "sweep"], id="sweep"),
    ],
)
def test_cli_guest_refuses_a_caller_that_is_not_root(
    monkeypatch: pytest.MonkeyPatch, recorded_run: list[dict[str, Any]], argv: list[str]
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    touched: list[str] = []
    for name in ("fetch", "prepare", "sweep"):
        monkeypatch.setattr(ci_guest, name, lambda *a, _n=name, **k: touched.append(_n))
    result = invoke(argv)
    assert result.exit_code == 1, result.output
    assert "root" in result.output.lower()
    assert recorded_run == [] and touched == []
    no_traceback(result)


@pytest.mark.parametrize("rc", [0, 1, 3, 4])
def test_cli_guest_run_exits_with_the_jobs_code(
    as_root: None,
    recorded_run: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    rc: int,
) -> None:
    """R4.3 (4), R4.4 (3), R4.5 (0, 1): the code reaches the systemd unit unchanged."""
    monkeypatch.setenv("FAKE_RUN_JOB_RC", str(rc))
    result = invoke(RUN)
    assert len(recorded_run) == 1, result.output
    assert result.exit_code == rc, result.output
    no_traceback(result)


def test_cli_guest_run_passes_the_job_the_repo_and_every_limit(
    as_root: None, recorded_run: list[dict[str, Any]]
) -> None:
    result = invoke(
        [
            *RUN,
            "--memory-gib",
            "6",
            "--disk-gib",
            "50",
            "--timeout-min",
            "90",
            "--min-free-gib",
            "15",
        ]
    )
    assert result.exit_code == 0, result.output
    assert len(recorded_run) == 1
    call = recorded_run[0]
    assert (call["job"], call["repo"]) == ("9001", "obentoo/private-app")
    got = call["limits"]
    assert (got.memory_gib, got.disk_gib, got.timeout_min, got.min_free_gib) == (6, 50, 90, 15)


def test_cli_guest_run_defaults_are_the_designs_and_cpus_is_no_option(
    as_root: None, recorded_run: list[dict[str, Any]]
) -> None:
    """design.md ``[vm]`` defaults. ``--cpus`` is refused as an unknown option: the vCPUs
    are the worker's to derive (R7.1), never the dispatcher's."""
    result = invoke(RUN)
    assert result.exit_code == 0, result.output
    assert len(recorded_run) == 1
    got = recorded_run[0]["limits"]
    assert (got.memory_gib, got.disk_gib, got.timeout_min, got.min_free_gib) == (8, 64, 360, 20)
    refused = invoke([*RUN, "--cpus", "3"])
    assert refused.exit_code not in (0, 1, 3, 4), refused.output
    assert len(recorded_run) == 1, "run_job ran with a --cpus"


def test_cli_guest_fetch_maps_a_guest_error_to_exit_1_with_its_message(
    as_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_fetch(*_a: Any, **_k: Any) -> Any:
        raise ci_guest.CiGuestError("download of https://cloud-images.example/x.img failed: 404")

    monkeypatch.setattr(ci_guest, "fetch", failing_fetch)
    result = invoke(["ci-guest", "fetch"])
    assert result.exit_code == 1, result.output
    assert "https://cloud-images.example/x.img" in result.output
    no_traceback(result)


def test_cli_guest_prepare_maps_a_pin_error_to_exit_1_with_its_message(
    as_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1.2 through the CLI: a bad pin stops before any download or boot."""
    touched: list[str] = []

    def failing_load(*_a: Any, **_k: Any) -> Any:
        raise ci_guest_seed.CiGuestSeedError(
            "seeds/ci-guest.toml: image.sha256 is not 64 hex digits"
        )

    monkeypatch.setattr(ci_guest_seed, "load_pins", failing_load)
    for name in ("fetch", "prepare"):
        monkeypatch.setattr(ci_guest, name, lambda *a, _n=name, **k: touched.append(_n))
    result = invoke(["ci-guest", "prepare"])
    assert result.exit_code == 1, result.output
    assert "image.sha256" in result.output
    assert touched == []
    no_traceback(result)
