"""``build_outcome.recording`` and ``shidashi build --outcome-file`` (story 030, task 2.1;
R3.1-R3.6).

Contract from design.md "shidashi/build_outcome.py" and "``cli.build`` (two additions, no
logic)": ``recording(path, *, arch, init, output_dir)`` wraps the build body. Every failure
reaches it as the ``typer.Exit`` that ``cli._build_errors`` raised, so it classifies by the
exception's ``__cause__``: ``GenerationMismatchError`` -> ``toolchain-refused`` (each ABI
difference, the current PKGDIR, the successor PKGDIR), ``cli._ImagesFailed`` ->
``images-failed`` (its images, the exit code), anything else -> ``error`` (the exit code,
the cause's text); a normal return -> ``ok`` with the ISOs this build made in
``output_dir``. It re-raises unchanged; ``path is None`` is a no-op. ``_build_errors`` prints
the successor PKGDIR on a line of its own (R3.6) and keeps every exit code (R3.5).

The refusals are real: ``generation.check_or_record`` over a PKGDIR whose recorded
fingerprint has another gcc -- the producer the build uses, never a hand-built exception.
The PKGDIR sits under a short ``/tmp`` path so a printed path is never wrapped.
"""

import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from shidashi import cli
from shidashi.generation import GenerationFingerprint, GenerationMismatchError, check_or_record
from shidashi.phases import FactoryError
from shidashi.seed import SeedError
from tests._pending import try_import

recording: Any = try_import("shidashi.build_outcome", "recording")

runner = CliRunner()

THIS = ("v3", "systemd")
MINIMAL = "bentoo-2026.10.25-minimal-systemd-v3.iso"
KDE_XZ = "bentoo-2026.10.25-kde-systemd-v3-xz.iso"  # --compression both: the second ISO
GNOME_NEXT_DAY = "bentoo-2026.10.26-gnome-systemd-v3.iso"  # finished after 00:00 UTC
OTHER_INIT = "bentoo-2026.10.25-kde-openrc-v3.iso"
OTHER_ARCH = "bentoo-2026.10.25-kde-systemd-znver5.iso"


def fingerprint(gcc: str) -> GenerationFingerprint:
    return GenerationFingerprint(
        arch="x86-64-v3",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-O2 -pipe -march=x86-64-v3",
        chost="x86_64-pc-linux-gnu",
        llvm_slot="21",
        gcc=gcc,
        binutils="2.45",
        glibc="2.42",
    )


@pytest.fixture
def pkgdir() -> Iterator[Path]:
    """A generation PKGDIR recorded with gcc 15, under a short path."""
    root = Path(tempfile.mkdtemp(prefix="oc-", dir="/tmp"))
    try:
        where = root / "v3" / "20260823T153057Z"
        check_or_record(where, fingerprint("15.2.1"))
        yield where
    finally:
        shutil.rmtree(root, ignore_errors=True)


def mismatch(pkgdir: Path, *, after_phase: str | None = None) -> GenerationMismatchError:
    """The refusal the build meets when the toolchain moved to gcc 16."""
    try:
        check_or_record(pkgdir, fingerprint("16.1.0"), after_phase=after_phase)
    except GenerationMismatchError as err:
        return err
    raise AssertionError("check_or_record accepted a gcc 15 -> 16 change")


def through_build_errors(err: BaseException) -> Callable[[], None]:
    """A build body that fails the way a real build does: inside ``_build_errors``."""

    def body() -> None:
        with cli._build_errors():
            raise err

    return body


def recorded(path: Path | None, output_dir: Path, body: Callable[[], None]) -> BaseException | None:
    """Run ``body`` under ``recording``; the exception that came out, if any."""
    try:
        with recording(path, arch=THIS[0], init=THIS[1], output_dir=output_dir):
            body()
    except (typer.Exit, KeyboardInterrupt) as err:
        return err
    return None


def outcome_of(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"no outcome file at {path}"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def drop(output_dir: Path, *names: str) -> None:
    for name in names:
        (output_dir / name).write_bytes(b"iso")


# --- recording: a build that returns (R3.1) ---------------------------------------------------


def test_ok_never_lists_another_variants_iso_from_the_same_directory(tmp_path: Path) -> None:
    """Hostile half 1 (collapse): the release directory holds every variant's ISOs; one
    that differs only by init or by arch is not this build's."""
    out = tmp_path / "release"
    out.mkdir()
    drop(out, OTHER_INIT, OTHER_ARCH)
    path = tmp_path / "v3-systemd.json"
    assert recorded(path, out, lambda: drop(out, MINIMAL)) is None
    outcome = outcome_of(path)
    assert outcome["status"] == "ok"
    assert outcome["isos"] == [MINIMAL]


def test_ok_lists_every_iso_of_this_variant_its_xz_twin_and_a_next_day_name(
    tmp_path: Path,
) -> None:
    """Hostile half 2 (split): ``--compression both`` names its second ISO ``...-v3-xz.iso``,
    and an image finished after midnight carries the next day. Both are this build's."""
    out = tmp_path / "release"
    out.mkdir()
    path = tmp_path / "v3-systemd.json"
    assert recorded(path, out, lambda: drop(out, MINIMAL, KDE_XZ, GNOME_NEXT_DAY)) is None
    assert sorted(outcome_of(path)["isos"]) == sorted([MINIMAL, KDE_XZ, GNOME_NEXT_DAY])


def test_ok_records_arch_init_status_exit_zero_and_no_failure(tmp_path: Path) -> None:
    out = tmp_path / "release"
    out.mkdir()
    path = tmp_path / "v3-systemd.json"
    recorded(path, out, lambda: drop(out, MINIMAL, f"{MINIMAL}.DIGESTS", f"{MINIMAL}.packages"))
    outcome = outcome_of(path)
    assert (outcome["arch"], outcome["init"]) == THIS
    assert outcome["status"] == "ok" and outcome["exit_code"] == 0
    assert outcome["isos"] == [MINIMAL]
    assert outcome["failed_images"] == [] and outcome["toolchain"] is None


def test_without_a_path_recording_writes_nothing_and_with_one_it_writes_the_outcome(
    tmp_path: Path,
) -> None:
    """``path is None`` is a no-op (R3.5); the same body with a path records it."""
    out = tmp_path / "release"
    out.mkdir()
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert recorded(None, out, lambda: None) is None
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
    path = tmp_path / "v3-systemd.json"
    recorded(path, out, lambda: None)
    assert outcome_of(path)["status"] == "ok"


# --- recording: a toolchain refusal (R3.2) ----------------------------------------------------


def test_a_plain_factory_error_is_an_error_never_a_toolchain_refusal(tmp_path: Path) -> None:
    """Hostile half 1 (collapse): ``GenerationMismatchError`` IS a ``FactoryError`` and both
    exit 1 -- a phase that failed must not be reported as a toolchain change."""
    path = tmp_path / "v3-systemd.json"
    err = recorded(
        path, tmp_path, through_build_errors(FactoryError("emerge failed", phase="@world"))
    )
    assert isinstance(err, typer.Exit) and err.exit_code == 1
    outcome = outcome_of(path)
    assert outcome["status"] == "error"
    assert outcome["toolchain"] is None
    assert "emerge failed" in outcome["message"]


def test_a_refusal_after_a_phase_is_still_a_toolchain_refusal(tmp_path: Path, pkgdir: Path) -> None:
    """Hostile half 2 (split): the re-check after a phase raises the same refusal with
    ``after_phase`` set; it is the same kind of outcome."""
    path = tmp_path / "v3-systemd.json"
    refusal = mismatch(pkgdir, after_phase="@system")
    recorded(path, tmp_path, through_build_errors(refusal))
    outcome = outcome_of(path)
    assert outcome["status"] == "toolchain-refused"
    assert outcome["toolchain"]["successor"] == str(refusal.successor)


def test_a_toolchain_refusal_records_each_difference_the_pkgdir_and_the_successor(
    tmp_path: Path, pkgdir: Path
) -> None:
    path = tmp_path / "v3-systemd.json"
    refusal = mismatch(pkgdir)
    err = recorded(path, tmp_path, through_build_errors(refusal))
    assert isinstance(err, typer.Exit) and err.exit_code == 1, "the exit code changed"
    outcome = outcome_of(path)
    assert outcome["status"] == "toolchain-refused"
    assert outcome["exit_code"] == 1
    toolchain = outcome["toolchain"]
    assert toolchain["pkgdir"] == str(pkgdir)
    assert toolchain["successor"] == str(refusal.successor)
    (difference,) = toolchain["differences"]  # gcc alone moved: glibc and binutils did not
    assert "gcc" in difference and "15" in difference and "16" in difference


# --- recording: failed images and other errors (R3.3, R3.4) ----------------------------------


def test_failed_images_record_their_names_and_the_builds_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "v3-systemd.json"
    failed = cli._ImagesFailed(1, ["gnome: failed", "kde: failed"], ["gnome", "kde"])
    err = recorded(path, tmp_path, through_build_errors(failed))
    assert isinstance(err, typer.Exit) and err.exit_code == 1
    outcome = outcome_of(path)
    assert outcome["status"] == "images-failed"
    assert outcome["failed_images"] == ["gnome", "kde"]
    assert outcome["exit_code"] == 1
    assert outcome["toolchain"] is None


def test_any_other_error_records_the_exit_code_and_the_errors_message(tmp_path: Path) -> None:
    path = tmp_path / "v3-systemd.json"
    err = recorded(
        path, tmp_path, through_build_errors(SeedError("sha512 mismatch for stage3.tar.xz"))
    )
    assert isinstance(err, typer.Exit) and err.exit_code == 1
    outcome = outcome_of(path)
    assert outcome["status"] == "error"
    assert outcome["exit_code"] == 1
    assert "sha512 mismatch for stage3.tar.xz" in outcome["message"]


def test_the_exit_is_re_raised_unchanged_and_its_own_code_recorded(tmp_path: Path) -> None:
    """A bare ``typer.Exit(2)`` (a usage error): the same object comes out, and the
    outcome carries 2 -- never a hard-coded 1."""
    path = tmp_path / "v3-systemd.json"
    raised = typer.Exit(2)

    def body() -> None:
        raise raised

    assert recorded(path, tmp_path, body) is raised
    outcome = outcome_of(path)
    assert outcome["status"] == "error" and outcome["exit_code"] == 2


# --- shidashi build --outcome-file (R3.1-R3.6) -----------------------------------------------


class FakePlannedBuild:
    """Stands in for ``cli._build_planned``: runs ``body(options)`` and records the call."""

    def __init__(self, body: Callable[[Any], None]) -> None:
        self.body = body
        self.calls = 0

    def __call__(self, options: Any, *_a: Any, **_k: Any) -> None:
        self.calls += 1
        self.body(options)


@pytest.fixture
def build_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pkgdir: Path) -> Path:
    """Cache, scratch and output under tmp; the generation PKGDIR is ``pkgdir``; a wide
    error console, so a wrapped message can never pass for a line of its own."""
    for name in ("CACHE", "SCRATCH"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setattr(cli, "_generation_pkgdir", lambda _arch, _init: pkgdir)
    monkeypatch.setattr(cli._err_console, "_width", 10_000, raising=False)
    (tmp_path / "out").mkdir()
    return tmp_path


def build(lab: Path, *extra: str) -> Any:
    return runner.invoke(
        cli.app, ["build", *THIS, "--images", "minimal", "-o", str(lab / "out"), *extra]
    )


def files_under(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


def successor_alone(output: str, successor: Path) -> bool:
    """Some line holds the successor PKGDIR with at most a short label -- not as the tail
    of the long refusal message."""
    for line in output.splitlines():
        text = line.strip()
        if (
            text.endswith(str(successor))
            and "another generation" not in text
            and len(text) - len(str(successor)) < 40
        ):
            return True
    return False


def test_build_with_an_outcome_file_records_ok_and_the_isos_it_made(
    build_lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePlannedBuild(lambda options: drop(options.output_dir, MINIMAL))
    monkeypatch.setattr(cli, "_build_planned", fake)
    path = build_lab / "v3-systemd.json"
    result = build(build_lab, "--outcome-file", str(path))
    assert result.exit_code == 0, result.output
    assert fake.calls == 1
    outcome = outcome_of(path)
    assert outcome["status"] == "ok" and outcome["isos"] == [MINIMAL]


def test_build_refused_for_its_toolchain_records_it_and_prints_the_successor_alone(
    build_lab: Path, pkgdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusal = mismatch(pkgdir)
    monkeypatch.setattr(
        cli, "_build_planned", FakePlannedBuild(lambda _o: through_build_errors(refusal)())
    )
    path = build_lab / "v3-systemd.json"
    result = build(build_lab, "--outcome-file", str(path))
    assert result.exit_code == 1, result.output
    assert successor_alone(result.output, refusal.successor), result.output
    outcome = outcome_of(path)
    assert outcome["status"] == "toolchain-refused"
    assert outcome["toolchain"]["successor"] == str(refusal.successor)
    assert outcome["toolchain"]["pkgdir"] == str(pkgdir)


def test_build_without_an_outcome_file_also_prints_the_successor_on_its_own_line(
    build_lab: Path, pkgdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.6 holds for every build, not only the release's children."""
    refusal = mismatch(pkgdir)
    monkeypatch.setattr(
        cli, "_build_planned", FakePlannedBuild(lambda _o: through_build_errors(refusal)())
    )
    result = build(build_lab)
    assert result.exit_code == 1, result.output
    assert successor_alone(result.output, refusal.successor), result.output


def test_the_outcome_file_adds_a_file_and_changes_neither_the_exit_code_nor_the_output(
    build_lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.5: the same failing build, without then with ``--outcome-file``: one exit code,
    one output; only the second leaves a file, and it is the outcome."""
    failed = cli._ImagesFailed(1, ["kde: failed"], ["kde"])
    monkeypatch.setattr(
        cli, "_build_planned", FakePlannedBuild(lambda _o: through_build_errors(failed)())
    )
    before = files_under(build_lab)
    plain = build(build_lab)
    assert files_under(build_lab) == before
    path = build_lab / "v3-systemd.json"
    recorded_run = build(build_lab, "--outcome-file", str(path))
    assert files_under(build_lab) - before == {path}
    assert plain.exit_code == recorded_run.exit_code == 1
    assert plain.output == recorded_run.output
    outcome = outcome_of(path)
    assert outcome["status"] == "images-failed" and outcome["failed_images"] == ["kde"]


def test_a_build_refused_before_planning_records_an_error_with_its_message(
    build_lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.4: ``--images desktop`` stops before any plan; the outcome still says why."""
    fake = FakePlannedBuild(lambda _o: None)
    monkeypatch.setattr(cli, "_build_planned", fake)
    path = build_lab / "v3-systemd.json"
    result = runner.invoke(
        cli.app,
        [
            "build",
            *THIS,
            "--images",
            "desktop",
            "-o",
            str(build_lab / "out"),
            "--outcome-file",
            str(path),
        ],
    )
    assert result.exit_code == 1, result.output
    assert fake.calls == 0
    outcome = outcome_of(path)
    assert outcome["status"] == "error" and outcome["exit_code"] == 1
    assert "desktop" in outcome["message"]
