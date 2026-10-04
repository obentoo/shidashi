"""Unit tests of shidashi.progress -- the run's progress on the terminal."""

import datetime
import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from shidashi import audit, progress
from shidashi.container import Container


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _plain(
    clock: _Clock | None = None, *, verbose: bool = False
) -> tuple[progress.View, io.StringIO]:
    """A view without the live footer, writing plain text to a buffer."""
    out = io.StringIO()
    console = Console(file=out, width=200, no_color=True)
    return progress.View(console, live=False, verbose=verbose, clock=clock or _Clock()), out


# --- formatting --------------------------------------------------------------------


def test_duration_grows_its_unit_with_the_time() -> None:
    assert progress.duration(0.42) == "0.4s"
    assert progress.duration(14) == "14s"
    assert progress.duration(192) == "3m12s"
    assert progress.duration(7500) == "2h05m"


def test_elapsed_ticks_like_a_clock() -> None:
    assert progress.elapsed(14.9) == "0:14"
    assert progress.elapsed(723) == "12:03"
    assert progress.elapsed(3733) == "1:02:13"


def test_size_uses_binary_units() -> None:
    assert progress.size(512) == "512 B"
    assert progress.size(1229) == "1.2 KiB"
    assert progress.size(288 * 1024**2) == "288.0 MiB"


def test_command_label_drops_the_env_prefix() -> None:
    argv = ["env", "FEATURES=-ccache", "MAKEOPTS=-j8", "emerge", "--oneshot", "sys-devel/gcc"]
    assert progress.command_label(argv) == "emerge --oneshot sys-devel/gcc"
    assert progress.command_label(["locale-gen"]) == "locale-gen"


# --- emerge's status lines ------------------------------------------------------------


def test_the_tracker_follows_a_package_from_start_to_done() -> None:
    clock = _Clock()
    tracker = progress.EmergeTracker(clock)

    started = tracker.feed(">>> Emerging (1 of 3) sys-libs/zlib-1.3.1::gentoo\n")
    assert started == progress.MergeEvent("start", 1, 3, "sys-libs/zlib-1.3.1")
    assert tracker.total == 3
    assert tracker.active["sys-libs/zlib-1.3.1"].state == "building"

    assert tracker.feed(">>> Installing (1 of 3) sys-libs/zlib-1.3.1::gentoo\n") is None
    assert tracker.active["sys-libs/zlib-1.3.1"].state == "installing"

    clock.now += 45
    done = tracker.feed(">>> Completed (1 of 3) sys-libs/zlib-1.3.1::gentoo\n")
    assert done == progress.MergeEvent("done", 1, 3, "sys-libs/zlib-1.3.1", seconds=45)
    assert tracker.done == 1
    assert not tracker.active


def test_the_tracker_counts_merges_finished_out_of_order_under_jobs() -> None:
    tracker = progress.EmergeTracker(_Clock())
    for line in (
        ">>> Emerging (1 of 3) dev-lang/rust-1.91.0::gentoo",
        ">>> Emerging binary (2 of 3) app-misc/jq-1.8.1::gentoo",
        ">>> Emerging (3 of 3) media-libs/mesa-25.2.3::gentoo",
        ">>> Completed (2 of 3) app-misc/jq-1.8.1::gentoo",
    ):
        tracker.feed(line)
    assert tracker.done == 1
    assert list(tracker.active) == ["dev-lang/rust-1.91.0", "media-libs/mesa-25.2.3"]


def test_the_tracker_marks_binpkgs_and_reads_through_colour() -> None:
    tracker = progress.EmergeTracker(_Clock())
    line = (
        "\x1b[32;01m>>>\x1b[0m Emerging binary (\x1b[33;01m2\x1b[0m of 9) app-misc/jq-1.8::gentoo"
    )
    assert tracker.feed(line) == progress.MergeEvent("start", 2, 9, "app-misc/jq-1.8", binary=True)
    assert tracker.active["app-misc/jq-1.8"].state == "binpkg"


def test_the_tracker_reports_a_failed_package() -> None:
    clock = _Clock()
    tracker = progress.EmergeTracker(clock)
    tracker.feed(">>> Emerging (4 of 9) media-video/ffmpeg-8.0::gentoo")
    clock.now += 192
    event = tracker.feed(">>> Failed to emerge media-video/ffmpeg-8.0, Log file:")
    assert event == progress.MergeEvent(
        "failed", 4, 9, "media-video/ffmpeg-8.0", seconds=192, action="emerge"
    )
    assert not tracker.active
    # the elog summary emerge prints at the end is not a second failure
    assert tracker.feed(" * ERROR: media-video/ffmpeg-8.0::gentoo failed (compile phase):") is None


def test_a_download_with_one_job_becomes_the_package_state() -> None:
    tracker = progress.EmergeTracker(_Clock())
    tracker.feed(">>> Emerging (1 of 1) app-misc/hello-2.12::gentoo")
    tracker.feed(">>> Downloading 'https://distfiles.gentoo.org/distfiles/ab/hello-2.12.tar.gz'")
    assert tracker.active["app-misc/hello-2.12"].state == "fetching hello-2.12.tar.gz"


def test_other_lines_are_not_events() -> None:
    tracker = progress.EmergeTracker(_Clock())
    for line in ("Calculating dependencies... done!", ">>> Jobs: 0 of 3 complete", "make -j8", ""):
        assert tracker.feed(line) is None
    assert tracker.total == 0


# --- the plain view (a pipe, a file, CI) -------------------------------------------------


def test_every_finished_step_is_one_line_with_its_path_and_time() -> None:
    clock = _Clock()
    view, out = _plain(clock)
    view.step_started("bootstrap")
    view.step_started("locale")
    clock.now += 14
    view.step_ended("locale", ok=True)
    clock.now += 60
    view.step_ended("bootstrap", ok=True)
    assert out.getvalue().splitlines() == ["✓ bootstrap › locale  14s", "✓ bootstrap  1m14s"]


def test_a_step_that_shows_lines_gets_a_header_first() -> None:
    clock = _Clock()
    view, out = _plain(clock)
    view.step_started("binutils")
    view.command_started(["emerge", "binutils"], log=Path("/var/tmp/shidashi/logs/x.log"))
    view.output(">>> Emerging (1 of 2) sys-kernel/linux-headers-6.12::gentoo\n")
    clock.now += 45
    view.output(">>> Completed (1 of 2) sys-kernel/linux-headers-6.12::gentoo\n")
    view.command_ended()
    view.step_ended("binutils", ok=True)
    assert out.getvalue().splitlines() == [
        "log /var/tmp/shidashi/logs/x.log",
        ":: binutils",
        "   (1/2) sys-kernel/linux-headers-6.12  building…",
        "   (1/2) sys-kernel/linux-headers-6.12  done  45s",
        "✓ binutils  45s",
    ]


def test_the_log_is_named_once() -> None:
    view, out = _plain()
    log = Path("/var/tmp/shidashi/logs/x.log")
    view.command_started(["locale-gen"], log=log)
    view.command_ended()
    view.command_started(["env-update"], log=log)
    assert out.getvalue().count("log /var/tmp/shidashi/logs/x.log") == 1


def test_a_failure_is_shown_once_not_for_every_step_it_unwinds() -> None:
    view, out = _plain()
    view.step_started("stages")
    view.step_started("stage:base")
    view.step_ended("stage:base", ok=False)
    view.step_ended("stages", ok=False)
    assert out.getvalue().splitlines() == ["✗ stages › stage:base  0.0s"]


def test_a_failed_package_is_a_line() -> None:
    view, out = _plain()
    view.step_started("emerge-stage")
    view.command_started(["emerge", "@world"])
    view.output(">>> Emerging (4 of 9) media-video/ffmpeg-8.0::gentoo")
    view.output(">>> Failed to emerge media-video/ffmpeg-8.0, Log file:")
    assert "   (4/9) media-video/ffmpeg-8.0  failed to emerge  0.0s" in out.getvalue()


def test_raw_output_shows_only_when_verbose() -> None:
    quiet, quiet_out = _plain()
    loud, loud_out = _plain(verbose=True)
    for view in (quiet, loud):
        view.step_started("locale")
        view.command_started(["locale-gen"])
        view.output("Generating locales (this might take a while)...\n")
    assert "Generating locales" not in quiet_out.getvalue()
    assert loud_out.getvalue().splitlines() == [
        ":: locale",
        "   $ locale-gen",
        "Generating locales (this might take a while)...",
    ]


def test_a_note_is_a_line_without_the_footer() -> None:
    view, out = _plain()
    view.step_started("seed")
    view.note("extracting stage3-amd64-systemd.tar.xz")
    assert out.getvalue().splitlines() == [":: seed", "   extracting stage3-amd64-systemd.tar.xz"]


def test_a_transfer_shows_its_start_and_its_size_when_done() -> None:
    clock = _Clock()
    view, out = _plain(clock)
    with view.transfer("gentoo-20260926.tar.xz", 2 * 1024**2) as advance:
        advance(1024**2)
        advance(1024**2)
        clock.now += 6
    assert out.getvalue().splitlines() == [
        "   gentoo-20260926.tar.xz  downloading (2.0 MiB)",
        "   gentoo-20260926.tar.xz  2.0 MiB  6.0s",
    ]


def test_a_failed_transfer_says_how_far_it_got() -> None:
    view, out = _plain()
    with pytest.raises(OSError), view.transfer("stage3.tar.xz", None) as advance:
        advance(512)
        raise OSError("connection reset")
    assert "stage3.tar.xz  failed after 512 B" in out.getvalue()


# --- the live footer (a terminal) -------------------------------------------------------


def _live(clock: _Clock) -> tuple[progress.View, io.StringIO]:
    out = io.StringIO()
    console = Console(file=out, width=120, force_terminal=True, no_color=True)
    return progress.View(console, live=True, clock=clock), out


def test_the_footer_shows_the_step_the_transfers_and_the_packages_in_flight() -> None:
    clock = _Clock()
    view, _out = _live(clock)
    view.step_started("emerge-stage")
    view.command_started(["env", "FEATURES=x", "emerge", "@world"])
    view.output(">>> Emerging (1 of 120) dev-lang/rust-1.91.0::gentoo")
    view.output(">>> Emerging (2 of 120) sys-libs/zlib-1.3.1::gentoo")
    view.output(">>> Completed (2 of 120) sys-libs/zlib-1.3.1::gentoo")
    clock.now += 723

    render = io.StringIO()
    Console(file=render, width=120, no_color=True).print(view._footer())
    text = render.getvalue()
    assert "emerge-stage  12:03  $ emerge @world" in text
    assert "emerge 1/120" in text
    assert "(1/120) dev-lang/rust-1.91.0" in text and "building…" in text
    assert "zlib" not in text  # merged: a line above, no longer in flight


def test_on_a_terminal_a_package_start_stays_in_the_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    view, _out = _live(clock)
    kept: list[str] = []
    monkeypatch.setattr(view, "_emit", lambda lines: kept.extend(str(line) for line in lines))
    view.step_started("emerge-stage")
    view.command_started(["emerge", "@world"])
    view.output(">>> Emerging (1 of 1) app-misc/hello-2.12::gentoo")
    assert kept == []
    clock.now += 3
    view.output(">>> Completed (1 of 1) app-misc/hello-2.12::gentoo")
    assert kept == [":: emerge-stage", "   (1/1) app-misc/hello-2.12  done  3.0s"]


def test_paused_stops_the_footer_and_starts_it_again() -> None:
    view, _out = _live(_Clock())
    with view:
        assert view._live is not None and view._live.is_started
        with view.paused():
            assert not view._live.is_started
        assert view._live.is_started


# --- the audit steps, seen ---------------------------------------------------------------


class _Recording(progress.Reporter):
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bool | None]] = []

    def step_started(self, name: str) -> None:
        self.seen.append(("start", name, None))

    def step_ended(self, name: str, *, ok: bool) -> None:
        self.seen.append(("end", name, ok))


def test_observed_shows_each_step_and_still_records_it(tmp_path: Path) -> None:
    run = audit.Run(
        tmp_path / "run1",
        command="factory",
        argv=["shidashi", "factory"],
        wall=lambda: datetime.datetime(2026, 10, 4, tzinfo=datetime.UTC),
    )
    seen = _Recording()
    observed = progress.Observed(run, seen)
    with observed.step("seed") as step:
        step.add(fork_point_reused=False)
    with pytest.raises(RuntimeError), observed.step("bootstrap"):
        raise RuntimeError("gcc failed")
    observed.metric("packages.built", 3)
    run.close("error")

    assert seen.seen == [
        ("start", "seed", None),
        ("end", "seed", True),
        ("start", "bootstrap", None),
        ("end", "bootstrap", False),
    ]
    events = audit.read_events(tmp_path / "run1" / "events.jsonl")
    ends = [e for e in events if e["kind"] == "step.end"]
    assert [(e["step"], e["status"]) for e in ends] == [("seed", "ok"), ("bootstrap", "error")]
    assert ends[0]["fork_point_reused"] is False
    assert any(e["kind"] == "metric" and e["name"] == "packages.built" for e in events)


def test_reporting_installs_the_view_and_shows_the_audit_steps() -> None:
    out = io.StringIO()
    console = Console(file=out, width=200, no_color=True)
    assert type(progress.current()) is progress.Reporter
    with progress.reporting(console) as view:
        assert progress.current() is view
        assert isinstance(audit.current(), progress.Observed)
        with audit.current().step("seed"):
            pass
    assert type(progress.current()) is progress.Reporter
    assert not isinstance(audit.current(), progress.Observed)
    assert out.getvalue().startswith("✓ seed  ")


# --- downloads ---------------------------------------------------------------------------


def test_copy_moves_every_byte_and_shows_the_transfer() -> None:
    out = io.StringIO()
    console = Console(file=out, width=200, no_color=True)
    data = b"x" * (3 * 1024 + 5)
    dest = io.BytesIO()
    with progress.reporting(console):
        copied = progress.copy(
            io.BytesIO(data), dest, label="file.tar.xz", total=len(data), chunk=1024
        )
    assert copied == len(data)
    assert dest.getvalue() == data
    assert "file.tar.xz  3.0 KiB" in out.getvalue()


def test_copy_outside_a_reporting_block_just_copies() -> None:
    dest = io.BytesIO()
    assert progress.copy(io.BytesIO(b"abc"), dest, label="x") == 3
    assert dest.getvalue() == b"abc"


# --- the container feeds its lines ---------------------------------------------------------


def test_a_logged_container_command_reaches_the_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shidashi.container as container_mod

    script = (
        "echo '>>> Emerging (1 of 1) app-misc/hello-2.12::gentoo'; "
        "echo 'make: building'; "
        "echo '>>> Completed (1 of 1) app-misc/hello-2.12::gentoo'"
    )
    monkeypatch.setattr(container_mod, "_nspawn_argv", lambda *_a, **_k: ["sh", "-c", script])
    out = io.StringIO()
    console = Console(file=out, width=200, no_color=True)
    log = tmp_path / "logs" / "v3-minimal-systemd.log"
    with progress.reporting(console), audit.current().step("emerge-stage"):
        result = Container(tmp_path, log=log).run(["emerge", "@world"])

    assert "make: building" in result.stdout  # the result and the log are unchanged
    assert "make: building" in log.read_text(encoding="utf-8")
    lines = out.getvalue().splitlines()
    assert lines[:3] == [
        f"log {log}",
        ":: emerge-stage",
        "   (1/1) app-misc/hello-2.12  building…",
    ]
    assert lines[3].startswith("   (1/1) app-misc/hello-2.12  done  ")
    assert lines[4].startswith("✓ emerge-stage  ")
    assert len(lines) == 5  # the build's own output stays in the log


def test_a_failing_logged_command_still_ends_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shidashi.container as container_mod

    monkeypatch.setattr(
        container_mod, "_nspawn_argv", lambda *_a, **_k: ["sh", "-c", "echo boom; exit 3"]
    )
    console = Console(file=io.StringIO(), width=200, no_color=True)
    with progress.reporting(console) as reporter:
        with pytest.raises(subprocess.CalledProcessError):
            Container(tmp_path, log=tmp_path / "x.log").run(["emerge", "x"])
        assert isinstance(reporter, progress.View)
        assert reporter._command == "" and reporter._emerge is None
