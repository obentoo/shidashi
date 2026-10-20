"""The CPU guard over ``binhost gc`` / ``binhost publish`` (story 029, task 7.2; R3.14).

``isaguard.job_target`` names the arch a command runs the binaries of; a job whose arch the
worker's CPU cannot run is refused before anything is shipped (``worker._cpu_guard``). The
``binhost`` sub-app was invisible to it: ``gc`` and ``publish`` run ``emaint`` and
``gpkg-sign`` of an arch's toolbox, ``show`` and ``verify`` run no arch binary at all.

The refusal goes through the real ``worker.job`` against the fake worker of
tests/_fake_worker.py (a Zen 3: no AVX-512, no AVX-VNNI), the way tests/test_worker_job.py
exercises the guard for ``factory``.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import isaguard
from tests._fake_worker import FakeWorker, seed_host_cache
from tests.test_worker_job import _job, _nothing_shipped, _refused

FPR = "0123456789ABCDEF0123456789ABCDEF01234567"
GEN = "20260823T153057Z"

# --- job_target -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "target"),
    [
        # hostile: the first positional is the SUBcommand, so the arch is the second
        (["binhost", "gc", "v3", "--keep", "1"], ("v3", "systemd")),
        (["binhost", "gc", "znver5"], ("znver5", "systemd")),
        # hostile: an option value that looks like an arch is never the arch
        (["binhost", "gc", "--keep", "1", "v3"], ("v3", "systemd")),
        (["binhost", "publish", "--key", "znver5", "v3"], ("v3", "systemd")),
        (["binhost", "publish", "v3", "--key", "znver5"], ("v3", "systemd")),
        (
            ["binhost", "publish", "--generation", "v3", "arrowlake", "--key", FPR],
            ("arrowlake", "systemd"),
        ),
        (["binhost", "publish", "--dest", "znver5", "v3", "--key", FPR], ("v3", "systemd")),
        # hostile: an option value that looks like an init never sets the init
        (["binhost", "publish", "v3", "--key", "openrc"], ("v3", "systemd")),
        (["binhost", "publish", "v3", "--dest", "openrc", "--key", FPR], ("v3", "systemd")),
        # the init is its option, before or after the arch, spaced or with "="
        (["binhost", "gc", "v3", "--init", "openrc"], ("v3", "openrc")),
        (["binhost", "gc", "--init", "openrc", "v3"], ("v3", "openrc")),
        (["binhost", "gc", "v3", "--init=openrc", "--keep=0"], ("v3", "openrc")),
        (["binhost", "gc", "v3", "--dry-run"], ("v3", "systemd")),
        (["binhost", "gc", "--dry-run", "znver5", "--keep", "3"], ("znver5", "systemd")),
        (["binhost", "publish", "v3", "--key", FPR], ("v3", "systemd")),
        (
            ["binhost", "publish", "arrowlake", "--key", FPR, "--init", "openrc"],
            ("arrowlake", "openrc"),
        ),
        (
            ["binhost", "publish", "znver5", "--key", FPR, "--generation", GEN, "--dest", "/x"],
            ("znver5", "systemd"),
        ),
        # an arch given before the subcommand's own options and values, in a mixed order
        (
            ["binhost", "publish", "--dest", "/x", "--key", FPR, "--init", "openrc", "v3"],
            ("v3", "openrc"),
        ),
    ],
)
def test_job_target_reads_the_arch_after_the_gc_or_publish_subcommand(
    args: list[str], target: tuple[str, str]
) -> None:
    assert isaguard.job_target(args) == target


@pytest.mark.parametrize(
    "args",
    [
        ["binhost", "show", "v3"],
        ["binhost", "show", "znver5", "--init", "openrc", "--generation", GEN],
        ["binhost", "verify", "v3", "--pubkey", "key.asc"],
        ["binhost", "verify", "--pubkey", "znver5", "arrowlake"],
        # no arch yet: nothing to guard, and no crash
        ["binhost"],
        ["binhost", "gc"],
        ["binhost", "publish", "--key", FPR],
        ["binhost", "--help"],
        ["binhost", "gc", "--help"],
        # a subcommand this guard does not know is not a CPU command
        ["binhost", "unknown", "v3"],
    ],
)
def test_job_target_is_none_for_show_verify_and_a_command_without_an_arch(
    args: list[str],
) -> None:
    """``show`` reads an index and ``verify`` checks signatures on the host: neither runs an
    arch's binaries, so neither may be refused for the CPU."""
    assert isaguard.job_target(args) is None


def test_the_subcommand_name_is_never_taken_for_the_arch() -> None:
    """Hostile half of the first positional: ``gc`` / ``publish`` would otherwise be the
    arch handed to ``isaguard.missing`` and refused as an unknown arch fragment."""
    for args in (["binhost", "gc", "v3"], ["binhost", "publish", "v3", "--key", FPR]):
        target = isaguard.job_target(args)
        assert target is not None and target[0] not in {"gc", "publish", "binhost"}


@pytest.mark.parametrize(
    ("args", "target"),
    [
        (["assemble", "--jobs", "16", "znver5", "minimal", "systemd"], ("znver5", "systemd")),
        (["factory", "znver5", "kde", "openrc"], ("znver5", "openrc")),
        (["build", "v3", "systemd", "--images", "kde"], ("v3", "systemd")),
        (["recipe", "show", "v3", "minimal", "systemd"], None),
        (["doctor"], None),
    ],
)
def test_the_other_commands_keep_their_parsing(
    args: list[str], target: tuple[str, str] | None
) -> None:
    """Learning the sub-app must not change how the existing commands are read."""
    assert isaguard.job_target(args) == target


# --- the refusal, through the worker job ------------------------------------------------------


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    w = FakeWorker.install(tmp_path, monkeypatch)
    seed_host_cache(w)
    yield w
    w.close()


@pytest.mark.parametrize("tail", [["--keep", "1"], ["--init", "openrc"], ["--dry-run"]])
def test_a_gc_job_for_an_arch_the_workers_cpu_lacks_is_refused_before_anything_ships(
    fw: FakeWorker, capfd: pytest.CaptureFixture[str], tail: list[str]
) -> None:
    """R3.14: a Zen 3 cannot run znver5's ``emaint`` (AVX-512): the refusal names the
    missing flag, and nothing is shipped or started."""
    text = _refused(
        lambda: _job(fw, "gc-z5", ["binhost", "gc", "znver5", *tail]),
        capfd,
    )
    assert "avx512f" in text
    _nothing_shipped(fw)


def test_a_gc_job_for_an_arch_the_workers_cpu_runs_is_not_refused(fw: FakeWorker) -> None:
    """Hostile half: the guard must not refuse what the CPU does run (a Zen 3 runs v3)."""
    assert _job(fw, "gc-v3", ["binhost", "gc", "v3", "--keep", "1"]) == 0
    assert fw.job_invocations()[0]["args"][:3] == ["binhost", "gc", "v3"]


def test_a_value_that_looks_like_an_arch_does_not_refuse_a_runnable_gc(fw: FakeWorker) -> None:
    """Hostile half: ``--init``/``--keep`` values are not archs, so the guard judges v3."""
    assert _job(fw, "gc-v3", ["binhost", "gc", "--init", "openrc", "v3"]) == 0


def test_a_show_job_is_not_cpu_guarded(fw: FakeWorker) -> None:
    """``show`` runs no arch binary: a worker that cannot run znver5 still reads its index."""
    assert _job(fw, "show-z5", ["binhost", "show", "znver5"]) == 0
