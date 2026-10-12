"""The parallel launcher's pure planning (story 006, task 6.1).

Contract (design.md v2, cross-story contract C1): ``job_share(jobs: int, images: int) =
max(1, jobs // images)`` -- ints only, the caller resolves an absent ``--jobs``;
``assemble_argv(target, *, arch, init, jobs, output_dir, result_file, compressions,
stage4, sbom, fresh, trunk, download, keep, boot_test, cid)`` -- the child ``assemble``
command line (the package has no ``__main__``); ``ChildResult(image, exit_code, run_id,
log, duration_s, isos, error)``; ``summarize(results) -> (exit code, report lines)``.

v3: ``assemble_argv`` also takes ``verbose: bool = False`` (``--verbose``, R1.11) and
``status_file: Path | None = None`` (``--status-file <path>``, R1.9/R1.10), each passed
only when set.
"""

import sys
from pathlib import Path

import pytest

from shidashi.parallel import ChildResult, assemble_argv, job_share, summarize

# --- job_share (R1.3) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("jobs", "images", "share"),
    [
        (24, 3, 8),  # no thread reserved for the parent: 8, not 7
        (24, 5, 4),  # floor, never rounded up
        (11, 2, 5),  # floor, never rounded to nearest
        (2, 3, 1),  # a floor of 1
        (1, 1, 1),
        (24, 1, 24),
    ],
)
def test_job_share_splits_by_integer_division_with_a_floor_of_one(
    jobs: int, images: int, share: int
) -> None:
    assert job_share(jobs, images) == share


def test_job_share_takes_ints_only_the_caller_resolves_an_absent_jobs() -> None:
    """C1: the CPU-count fallback lives in the caller (task 6.4), not in job_share."""
    with pytest.raises(TypeError):
        job_share(None, 3)  # type: ignore[arg-type]


# --- assemble_argv --------------------------------------------------------------------


def _argv(**overrides: object) -> list[str]:
    options: dict[str, object] = {
        "arch": "v3",
        "init": "systemd",
        "jobs": 8,
        "output_dir": Path("/mnt/out/par"),
        "result_file": Path("/scratch/logs/build-R/kde.json"),
        "compressions": ("zstd",),
        "stage4": False,
        "sbom": True,
        "fresh": False,
        "trunk": True,
        "download": True,
        "keep": False,
        "boot_test": False,
        "cid": None,
    }
    options.update(overrides)
    target = str(options.pop("target", "kde"))
    return assemble_argv(target, **options)  # type: ignore[arg-type]


def _value(argv: list[str], option: str) -> str | None:
    return argv[argv.index(option) + 1] if option in argv else None


def test_argv_runs_the_assemble_command_through_the_package() -> None:
    argv = _argv()
    assert argv[:8] == [
        sys.executable,
        "-P",
        "-c",
        "from shidashi.cli import app; app()",
        "assemble",
        "v3",
        "kde",
        "systemd",
    ]
    assert _value(argv, "--jobs") == "8"
    assert _value(argv, "--output-dir") == "/mnt/out/par"


def test_argv_carries_the_result_file_the_parent_will_read() -> None:
    """C1: run_children reads each child's result path from this very argv."""
    argv = _argv(result_file=Path("/scratch/logs/build-R/gnome.json"), target="gnome")
    assert _value(argv, "--result-file") == "/scratch/logs/build-R/gnome.json"


def test_argv_of_two_images_differ_by_the_image() -> None:
    kde, gnome = _argv(target="kde"), _argv(target="gnome")
    assert kde[6] == "kde" and gnome[6] == "gnome"
    assert [a for i, a in enumerate(kde) if i != 6] == [a for i, a in enumerate(gnome) if i != 6]


def test_argv_without_options_passes_no_flag() -> None:
    argv = _argv()
    for flag in (
        "--stage4",
        "--no-sbom",
        "--fresh",
        "--no-trunk",
        "--no-download",
        "--keep",
        "--boot-test",
        "--cid",
        "--verbose",
        "--status-file",
    ):
        assert flag not in argv, flag
    assert _value(argv, "--compression") in (None, "zstd")


@pytest.mark.parametrize(
    ("override", "flag"),
    [
        ({"stage4": True}, "--stage4"),
        ({"sbom": False}, "--no-sbom"),
        ({"fresh": True}, "--fresh"),
        ({"trunk": False}, "--no-trunk"),
        ({"download": False}, "--no-download"),
        ({"keep": True}, "--keep"),
        ({"verbose": True}, "--verbose"),
    ],
)
def test_argv_passes_each_build_option_through(override: dict[str, object], flag: str) -> None:
    assert flag in _argv(**override)


@pytest.mark.parametrize(("compressions", "value"), [(("zstd", "xz"), "both"), (("xz",), "xz")])
def test_argv_passes_the_compression(compressions: tuple[str, ...], value: str) -> None:
    assert _value(_argv(compressions=compressions), "--compression") == value


def test_argv_with_boot_test_and_a_cid_boot_tests_on_it() -> None:
    argv = _argv(boot_test=True, cid=143)
    assert "--boot-test" in argv
    assert _value(argv, "--cid") == "143"


def test_argv_boot_test_without_a_cid_leaves_the_cid_to_the_child() -> None:
    argv = _argv(boot_test=True, cid=None)
    assert "--boot-test" in argv and "--cid" not in argv


# --- summarize (R2.2, R2.4) -----------------------------------------------------------


def _result(
    image: str, code: int, *, isos: tuple[Path, ...] = (), error: str | None = None
) -> ChildResult:
    return ChildResult(
        image=image,
        exit_code=code,
        run_id=f"run-{image}",
        log=Path(f"/scratch/logs/build-R/{image}.log"),
        duration_s=12.5,
        isos=isos,
        error=error,
    )


def test_summarize_every_success_exits_0_listing_each_iso() -> None:
    kde_iso, gnome_iso = Path("/out/bentoo-kde.iso"), Path("/out/bentoo-gnome.iso")
    code, lines = summarize(
        [_result("kde", 0, isos=(kde_iso,)), _result("gnome", 0, isos=(gnome_iso,))]
    )
    assert code == 0
    text = "\n".join(lines)
    assert str(kde_iso) in text and str(gnome_iso) in text
    assert not any("failed" in line for line in lines)


def test_summarize_names_each_failed_image_with_its_own_log() -> None:
    """Two failures, each named with ITS log -- not one log twice, not a count."""
    wm_iso = Path("/out/bentoo-wm.iso")
    results = [_result("kde", 1), _result("gnome", 2), _result("wm", 0, isos=(wm_iso,))]
    code, lines = summarize(results)
    assert code == 1
    kde = [line for line in lines if "kde" in line and "failed" in line]
    gnome = [line for line in lines if "gnome" in line and "failed" in line]
    assert len(kde) == 1 and str(results[0].log) in kde[0] and str(results[1].log) not in kde[0]
    assert len(gnome) == 1 and str(results[1].log) in gnome[0]
    assert any(str(wm_iso) in line for line in lines)  # the image that made it is listed


def test_summarize_one_line_per_image() -> None:
    results = [_result(i, 0, isos=(Path(f"/out/{i}.iso"),)) for i in ("kde", "gnome", "wm")]
    _, lines = summarize(results)
    for image in ("kde", "gnome", "wm"):
        assert sum(f"/out/{image}.iso" in line for line in lines) == 1


def test_summarize_reports_the_reason_of_a_failure() -> None:
    """C1: ChildResult.error says why ("no result file", "result file lacks isos")."""
    code, lines = summarize([_result("kde", 1, error="result file lacks isos")])
    assert code == 1
    (kde,) = [line for line in lines if "kde" in line]
    assert "result file lacks isos" in kde and "/scratch/logs/build-R/kde.log" in kde


def test_summarize_a_zero_exit_with_an_error_is_a_failure() -> None:
    """Hostile: an error recorded beside exit code 0 never reads as success."""
    code, lines = summarize([_result("kde", 0, error="no result file")])
    assert code == 1
    assert any("failed" in line and "no result file" in line for line in lines)


def test_argv_needs_only_what_story_014_passes() -> None:
    """Item 8: every keyword story 014 does not pass has a default; cid defaults to None."""
    argv = assemble_argv(
        "gnome",
        arch="v3",
        init="systemd",
        jobs=8,
        output_dir=Path("/mnt/out/par"),
        result_file=Path("/scratch/logs/build-R/gnome.json"),
    )
    assert argv[4:8] == ["assemble", "v3", "gnome", "systemd"]
    for flag in ("--boot-test", "--cid", "--stage4", "--no-sbom", "--fresh", "--no-trunk"):
        assert flag not in argv, flag
    with_boot = assemble_argv(
        "gnome",
        arch="v3",
        init="systemd",
        jobs=8,
        output_dir=Path("/mnt/out/par"),
        result_file=Path("/scratch/logs/build-R/gnome.json"),
        boot_test=True,
    )
    assert "--boot-test" in with_boot and "--cid" not in with_boot


# --- v3: --verbose and --status-file, each only when set ------------------------------------


def test_argv_passes_the_status_file_only_when_given() -> None:
    status = Path("/scratch/logs/build-R/gnome.status")
    argv = _argv(status_file=status)
    assert _value(argv, "--status-file") == str(status)
    assert "--status-file" not in _argv(status_file=None)


def test_status_file_and_result_file_are_never_confused() -> None:
    """Wrongly collapsed: two hidden paths side by side; each keeps its own value."""
    result = Path("/scratch/logs/build-R/gnome.json")
    status = Path("/scratch/logs/build-R/gnome.status")
    argv = _argv(result_file=result, status_file=status)
    assert _value(argv, "--result-file") == str(result)
    assert _value(argv, "--status-file") == str(status)


def test_verbose_false_passes_no_verbose() -> None:
    argv = _argv(verbose=False)
    assert "--verbose" not in argv and "-v" not in argv


def test_story_014s_call_is_neither_verbose_nor_watched() -> None:
    argv = assemble_argv(
        "gnome",
        arch="v3",
        init="systemd",
        jobs=8,
        output_dir=Path("/mnt/out/par"),
        result_file=Path("/scratch/logs/build-R/gnome.json"),
    )
    assert "--verbose" not in argv and "--status-file" not in argv
