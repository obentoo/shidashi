"""UNIT + INTEGRATION of shidashi.container.

UNIT (R4.4): ``_nspawn_argv`` is pure and inspectable — tested without root.
Contract (design.md §container): ``_nspawn_argv(rootfs, argv, *, binds, binds_rw,
ephemeral)`` returns ``["systemd-nspawn", "--directory", str(rootfs),
(optional "--ephemeral"), ("--bind-ro=src:dst" per RO bind), ("--bind=src:dst"
per RW bind, after the RO ones), "--", *argv]``. ``CommandResult(exit_code, stdout,
stderr)`` is a value object.

Story 003 (2.2): ``_nspawn_argv`` gains ``binds_rw`` (emitting ``--bind=`` after
``--bind-ro=``); ``Container`` accepts and propagates ``binds_rw``. Back-compat
(R7.3): without ``binds_rw`` the argv is identical to story 002's.

INTEGRATION (R4.1-R4.3): requires root + systemd-nspawn — gated with
``@pytest.mark.skipif``; on non-Gentoo CI / a non-root sandbox these tests SKIP.
They run on the ``seeded_rootfs`` fixture: a directory under the test's own
``tmp_path`` that ``systemd-nspawn`` accepts as an OS tree (it refuses an empty
one, "doesn't look like it has an OS tree", and exits 1 — the very code
``false`` exits with) and in which ``true`` and ``false`` are runnable. It is
built from what the host already has: no network, no package install.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shidashi.container import CommandResult, Container, _nspawn_argv, _nspawn_shell_argv

_NEEDS_ROOT = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_ROOT, reason="requires root + systemd-nspawn (privileged Gentoo host)"
)


# --- CommandResult value object ----------------------------------------------


def test_command_result_carries_fields() -> None:
    r = CommandResult(exit_code=0, stdout="ok", stderr="")
    assert r.exit_code == 0
    assert r.stdout == "ok"
    assert r.stderr == ""


# --- pure _nspawn_argv (R4.4) ------------------------------------------------


def test_nspawn_argv_basic_shape() -> None:
    argv = _nspawn_argv(
        Path("/scratch/rootfs"),
        ["emerge", "--pretend"],
        binds=[],
        ephemeral=False,
    )
    assert argv[0] == "systemd-nspawn"
    assert "--directory" in argv
    assert argv[argv.index("--directory") + 1] == "/scratch/rootfs"
    # the command's argv comes after the "--" separator
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["emerge", "--pretend"]


def test_nspawn_argv_and_shell_share_the_labs_host_options() -> None:
    """--resolv-conf=copy-host: the stage3's resolv.conf is not trusted to reach
    the mirrors; --register=no: not a --boot container, nothing for machined."""
    run = _nspawn_argv(Path("/r"), ["sh"], binds=[], ephemeral=False)
    shell = _nspawn_shell_argv(Path("/r"))
    for argv in (run, shell):
        assert "--resolv-conf=copy-host" in argv
        assert "--register=no" in argv
    assert run.index("--register=no") < run.index("--")


def test_commands_run_in_pipe_mode_and_never_read_the_callers_terminal() -> None:
    """Without --console, nspawn picks INTERACTIVE when started from a terminal
    (a tmux pane): it would grab the keyboard and put the tty in raw mode for a
    whole build. Commands use pipe mode; only the interactive shell gets a tty."""
    run = _nspawn_argv(Path("/r"), ["sh"], binds=[], ephemeral=False)
    assert "--console=pipe" in run and run.index("--console=pipe") < run.index("--")
    assert not any(a.startswith("--console") for a in _nspawn_shell_argv(Path("/r")))


def test_run_never_passes_the_callers_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shidashi.container as container_mod

    monkeypatch.setattr(
        container_mod, "_nspawn_argv", lambda *_a, **_k: ["sh", "-c", "cat; echo read-done"]
    )
    # cat would block forever on an inherited terminal; /dev/null ends it at once
    for log in (None, tmp_path / "x.log"):
        out = Container(tmp_path, log=log).run(["x"]).stdout
        assert out.strip() == "read-done"


def test_commands_run_as_pid2_under_nspawns_stub_init() -> None:
    """As PID 1, locale-gen aborted ("not all of the selected locales were
    compiled") in the first real run; as a child it installs all 102 (reproduced
    in a user+pid namespace, 2026-09-26). --as-pid2 gives every command a stub
    init that reaps orphans. The interactive shell is unaffected."""
    run = _nspawn_argv(Path("/r"), ["sh"], binds=[], ephemeral=False)
    assert "--as-pid2" in run and run.index("--as-pid2") < run.index("--")
    assert "--as-pid2" not in _nspawn_shell_argv(Path("/r"))


def test_nspawn_argv_ephemeral_flag() -> None:
    with_eph = _nspawn_argv(Path("/r"), ["sh"], binds=[], ephemeral=True)
    without = _nspawn_argv(Path("/r"), ["sh"], binds=[], ephemeral=False)
    assert "--ephemeral" in with_eph
    assert "--ephemeral" not in without


def test_nspawn_argv_emits_ro_binds() -> None:
    binds = [(Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))]
    argv = _nspawn_argv(Path("/r"), ["sh"], binds=binds, ephemeral=False)
    assert "--bind-ro=/var/db/repos/gentoo:/var/db/repos/gentoo" in argv
    # binds come before the command separator
    assert argv.index("--bind-ro=/var/db/repos/gentoo:/var/db/repos/gentoo") < argv.index("--")


# --- read-write binds (story 003 2.2 — R7.1/R7.2/R7.3) -----------------------


def test_nspawn_argv_no_rw_binds_is_story002_backcompat() -> None:
    # without binds_rw the argv is EXACTLY story 002's (empty default)
    ro = [(Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))]
    legacy = _nspawn_argv(Path("/r"), ["emerge", "@world"], binds=ro, ephemeral=False)
    explicit_empty = _nspawn_argv(
        Path("/r"), ["emerge", "@world"], binds=ro, binds_rw=(), ephemeral=False
    )
    assert legacy == explicit_empty
    assert not any(a.startswith("--bind=") for a in legacy)


def test_nspawn_argv_emits_rw_binds_after_ro() -> None:
    ro = [(Path("/var/db/repos/gentoo"), Path("/var/db/repos/gentoo"))]
    rw = [(Path("/var/cache/shidashi/binpkgs/v3"), Path("/var/cache/binpkgs"))]
    argv = _nspawn_argv(Path("/r"), ["sh"], binds=ro, binds_rw=rw, ephemeral=False)
    ro_flag = "--bind-ro=/var/db/repos/gentoo:/var/db/repos/gentoo"
    rw_flag = "--bind=/var/cache/shidashi/binpkgs/v3:/var/cache/binpkgs"
    assert ro_flag in argv
    assert rw_flag in argv
    # RW comes AFTER RO and BEFORE the command separator
    assert argv.index(ro_flag) < argv.index(rw_flag) < argv.index("--")


def test_nspawn_argv_rw_binds_in_declared_order() -> None:
    rw = [
        (Path("/h/pkgdir"), Path("/var/cache/binpkgs")),
        (Path("/h/ccache"), Path("/var/cache/ccache")),
        (Path("/h/distfiles"), Path("/var/cache/distfiles")),
    ]
    argv = _nspawn_argv(Path("/r"), ["sh"], binds=[], binds_rw=rw, ephemeral=False)
    rw_flags = [a for a in argv if a.startswith("--bind=")]
    assert rw_flags == [
        "--bind=/h/pkgdir:/var/cache/binpkgs",
        "--bind=/h/ccache:/var/cache/ccache",
        "--bind=/h/distfiles:/var/cache/distfiles",
    ]


def test_container_threads_binds_rw_into_command() -> None:
    # Container stores binds_rw and passes it on to _nspawn_argv when building the command.
    rw = [(Path("/h/pkgdir"), Path("/var/cache/binpkgs"))]
    container = Container(Path("/r"), ephemeral=False, binds_rw=rw)
    assert tuple(container.binds_rw) == tuple(rw)
    cmd = _nspawn_argv(
        container.rootfs,
        ["sh"],
        binds=container.binds,
        binds_rw=container.binds_rw,
        ephemeral=container.ephemeral,
    )
    assert "--bind=/h/pkgdir:/var/cache/binpkgs" in cmd


# --- host-gated INTEGRATION (R4.1-R4.3) on a seeded OS tree -----------------


def _shared_libs(binary: Path) -> set[Path]:
    """The absolute paths ``ldd`` resolves for ``binary`` (loader included)."""
    out = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=False).stdout
    libs: set[Path] = set()
    for line in out.splitlines():
        for word in line.split():
            if word.startswith("/"):
                libs.add(Path(word))
    return libs


@pytest.fixture
def seeded_rootfs(tmp_path: Path) -> Path:
    """The smallest tree ``systemd-nspawn`` accepts as an OS: merged ``/usr``,
    an ``os-release``, and ``true``/``false`` with the libraries they load, all
    copied from the host running the test."""
    root = tmp_path / "machines" / "rootfs"
    for d in ("usr/bin", "usr/lib", "usr/lib64", "etc", "proc", "sys", "dev", "run", "tmp", "var"):
        (root / d).mkdir(parents=True)
    for link in ("bin", "sbin", "lib", "lib64"):
        target = "usr/bin" if link.endswith("bin") else f"usr/{link}"
        (root / link).symlink_to(target)
    (root / "usr/lib/os-release").write_text('ID=shidashi-test\nNAME="Shidashi test tree"\n')
    (root / "etc/os-release").symlink_to("../usr/lib/os-release")
    for name in ("true", "false"):
        found = shutil.which(name)
        assert found is not None, f"{name} not found on the host"
        binary = Path(found).resolve()
        shutil.copy2(binary, root / "usr/bin" / name)
        for lib in _shared_libs(binary):
            dest = root / lib.relative_to("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(lib.resolve(), dest)
    return root


def _tree(root: Path) -> list[tuple[str, int, int, int]]:
    """Every path in ``root`` (itself included) with mode, size and mtime: what a
    run on a disposable copy must leave exactly as it was."""
    paths = [root, *sorted(root.rglob("*"))]
    return [
        (str(p.relative_to(root)), st.st_mode, st.st_size, st.st_mtime_ns)
        for p in paths
        for st in (p.lstat(),)
    ]


@_skip_privileged
def test_container_runs_command_and_tears_down(seeded_rootfs: Path, tmp_path: Path) -> None:
    # the tree is built for this test, never borrowed from the host
    assert seeded_rootfs.is_relative_to(tmp_path)
    before = _tree(seeded_rootfs)
    neighbours = set(seeded_rootfs.parent.iterdir())

    with Container(seeded_rootfs, ephemeral=True) as c:
        # check=True: an nspawn refusal (exit 1) would raise here
        result = c.run(["true"], check=True)
        assert result.exit_code == 0
        # R4.2: the run went to a disposable copy — nspawn's mount points,
        # resolv.conf and machine-id never reached the seeded tree
        assert _tree(seeded_rootfs) == before

    # the ephemeral copy nspawn made beside the tree is gone with the container
    leftovers = set(seeded_rootfs.parent.iterdir()) - neighbours
    assert leftovers == set()


@_skip_privileged
def test_container_check_true_raises_on_nonzero(seeded_rootfs: Path, tmp_path: Path) -> None:
    assert seeded_rootfs.is_relative_to(tmp_path)
    with Container(seeded_rootfs, ephemeral=True) as c:
        # hostile half: nspawn's own refusal also exits 1, so a raise alone
        # proves nothing — the same container must first run a command cleanly
        assert c.run(["true"], check=True).exit_code == 0
        # converse: check=False hands back the command's own exit, no raise
        assert c.run(["false"], check=False).exit_code == 1
        with pytest.raises(subprocess.CalledProcessError) as err:
            c.run(["false"], check=True)

    assert err.value.returncode == 1
    assert err.value.cmd[-1] == "false"
    streams = f"{err.value.stdout or ''}{err.value.stderr or ''}"
    assert "OS tree" not in streams


# --- streaming log: a 3-hour emerge must be visible while it runs ---------------


def test_run_with_a_log_streams_each_line_and_still_returns_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shidashi.container as container_mod

    monkeypatch.setattr(
        container_mod,
        "_nspawn_argv",
        lambda *_a, **_k: ["sh", "-c", "echo building; echo warned >&2; exit 0"],
    )
    log = tmp_path / "logs" / "v3-minimal-systemd.log"
    result = Container(tmp_path, log=log).run(["emerge", "@world"])

    assert result.exit_code == 0
    assert "building" in result.stdout and "warned" in result.stdout
    text = log.read_text(encoding="utf-8")
    assert "$ emerge @world" in text
    assert "building\n" in text and "warned\n" in text
    assert text.rstrip().endswith("exit 0")


def test_run_with_a_log_raises_on_failure_with_the_streamed_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shidashi.container as container_mod

    monkeypatch.setattr(
        container_mod, "_nspawn_argv", lambda *_a, **_k: ["sh", "-c", "echo boom; exit 3"]
    )
    log = tmp_path / "build.log"
    with pytest.raises(subprocess.CalledProcessError) as err:
        Container(tmp_path, log=log).run(["emerge", "x"])
    assert err.value.returncode == 3
    assert "boom" in err.value.output
    assert "exit 3" in log.read_text(encoding="utf-8")
    # check=False returns instead
    assert Container(tmp_path, log=log).run(["emerge", "x"], check=False).exit_code == 3
