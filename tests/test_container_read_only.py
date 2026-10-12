"""The toolbox's container runs read-only (story 006, task 4.2).

Contract (design.md, tasks.md 4.2): the toolbox runs its ``systemd-nspawn`` with
``--read-only`` and a tmpfs on ``/tmp`` -- nspawn then takes a SHARED lock on the
toolbox directory instead of an exclusive one, so two assembles can run
``mksquashfs``/``grub-mkrescue`` in it at once (R3.5); writes go only to the
``/work/*`` binds and the tmpfs. The image's own container stays writable, its
command line byte-identical to before.

v3: ``read_only`` is a parameter beside ``keep_unit`` (spike 012, nested in podman) in
``Container``, ``_nspawn_argv`` and ``_nspawn_shell_argv``. With ``read_only=False`` every
line stays byte-identical to the one before this story, with ``keep_unit`` on and off;
both together give both options. ``Toolbox`` keeps its ``log=``.

The command line is observed where it leaves the process: ``subprocess.run`` (and
``subprocess.Popen`` for a logged container) are faked; no nspawn runs here. Whether
this process runs inside a container (``keep_unit``) is fixed per test, never the
machine's.
"""

import re
import subprocess
from pathlib import Path

import pytest

from shidashi import container
from shidashi.container import Container, _nspawn_argv, _nspawn_shell_argv
from shidashi.toolbox import WORK, Toolbox

_TMPFS = re.compile(r"^--tmpfs=/tmp(:.*)?$")


class _FakePopen:
    """Enough of ``subprocess.Popen`` for a logged container run."""

    def __init__(self, cmd: list[str], **_kw: object) -> None:
        self.cmd = cmd
        self.stdout = iter(["ok\n"])

    def __enter__(self) -> _FakePopen:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def wait(self) -> int:
        return 0


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every command line a container hands to the OS, instead of running it."""
    cmds: list[list[str]] = []

    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "mksquashfs version 4.6.1\n", "")

    def popen(cmd: list[str], **kw: object) -> _FakePopen:
        cmds.append(list(cmd))
        return _FakePopen(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(container, "inside_container", lambda: False)
    return cmds


def _options(cmd: list[str]) -> list[str]:
    """The nspawn options: everything before the ``--`` that starts the command."""
    return cmd[: cmd.index("--")]


@pytest.mark.parametrize("logged", [False, True])
def test_the_toolbox_runs_its_tools_read_only_with_a_tmpfs(
    tmp_path: Path, launched: list[list[str]], logged: bool
) -> None:
    tools = Toolbox(
        tmp_path / "toolbox" / "v3-systemd",
        ro={"rootfs": tmp_path / "scratch" / "img"},
        rw={"scratch": tmp_path / "scratch", "out": tmp_path / "out"},
        log=tmp_path / "logs" / "toolbox.log" if logged else None,
    )
    tools.run(["mksquashfs", "/work/rootfs", "/work/scratch/img.squashfs"])
    (cmd,) = launched
    options = _options(cmd)
    assert cmd[0] == "systemd-nspawn"
    assert "--read-only" in options
    assert any(_TMPFS.match(o) for o in options), options
    # the writable places are the /work binds, as before
    assert f"--bind={(tmp_path / 'out').resolve()}:{WORK / 'out'}" in options
    assert f"--bind-ro={(tmp_path / 'scratch' / 'img').resolve()}:{WORK / 'rootfs'}" in options
    assert cmd[cmd.index("--") + 1 :] == [
        "mksquashfs",
        "/work/rootfs",
        "/work/scratch/img.squashfs",
    ]


def test_the_version_probe_runs_read_only_too(tmp_path: Path, launched: list[list[str]]) -> None:
    tools = Toolbox(tmp_path / "toolbox" / "v3-systemd")
    tools.versions()
    assert launched and all("--read-only" in _options(cmd) for cmd in launched)


def test_the_image_container_stays_writable_and_unchanged(
    tmp_path: Path, launched: list[list[str]]
) -> None:
    """Wrongly collapsed: only the toolbox becomes read-only. The image's container
    -- where emerge installs -- keeps the exact line it had before this story."""
    rootfs = tmp_path / "scratch" / "assemble" / "v3-kde-systemd"
    repo, binhost = tmp_path / "repo", tmp_path / "binhost"
    Container(
        rootfs,
        ephemeral=False,
        binds=[(repo, Path("/var/db/repos/gentoo")), (binhost, Path("/var/cache/binpkgs"))],
        binds_rw=[],
    ).run(["emerge", "--depclean"])
    (cmd,) = launched
    assert cmd == [
        "systemd-nspawn",
        "--directory",
        str(rootfs),
        "--register=no",
        "--resolv-conf=copy-host",
        "--timezone=off",
        "--console=pipe",
        "--as-pid2",
        f"--bind-ro={repo}:/var/db/repos/gentoo",
        f"--bind-ro={binhost}:/var/cache/binpkgs",
        "--",
        "emerge",
        "--depclean",
    ]


def test_a_default_container_is_not_read_only(tmp_path: Path, launched: list[list[str]]) -> None:
    Container(tmp_path / "r").run(["true"])
    (cmd,) = launched
    assert "--read-only" not in cmd
    assert not any(_TMPFS.match(o) for o in cmd)


# --- v3: read_only beside keep_unit -------------------------------------------------------

_ROOT = Path("/var/tmp/shidashi/assemble/v3-kde-systemd")
_BINDS = [(Path("/srv/repo"), Path("/var/db/repos/gentoo"))]
_BINDS_RW = [(Path("/srv/out"), Path("/work/out"))]
_HOST = ["--register=no", "--resolv-conf=copy-host", "--timezone=off"]


@pytest.mark.parametrize("keep_unit", [False, True])
def test_read_only_false_keeps_every_line_byte_identical(keep_unit: bool) -> None:
    """Hostile half: an explicit ``read_only=False`` must change nothing of today's lines,
    in a container (``--keep-unit``) or not -- the image container is one of them."""
    unit = ["--keep-unit"] if keep_unit else []
    assert _nspawn_argv(
        _ROOT,
        ["emerge", "--depclean"],
        binds=_BINDS,
        binds_rw=_BINDS_RW,
        ephemeral=False,
        keep_unit=keep_unit,
        read_only=False,
    ) == [
        "systemd-nspawn",
        "--directory",
        str(_ROOT),
        *_HOST,
        *unit,
        "--console=pipe",
        "--as-pid2",
        "--bind-ro=/srv/repo:/var/db/repos/gentoo",
        "--bind=/srv/out:/work/out",
        "--",
        "emerge",
        "--depclean",
    ]
    assert _nspawn_shell_argv(
        _ROOT, binds=_BINDS, binds_rw=_BINDS_RW, keep_unit=keep_unit, read_only=False
    ) == [
        "systemd-nspawn",
        "--directory",
        str(_ROOT),
        *_HOST,
        *unit,
        "--bind-ro=/srv/repo:/var/db/repos/gentoo",
        "--bind=/srv/out:/work/out",
    ]


def test_read_only_and_keep_unit_together_give_both_options() -> None:
    """Wrongly collapsed: one must not replace the other -- a toolbox nested in podman
    needs both ``--keep-unit`` and ``--read-only``."""
    both = _nspawn_argv(
        _ROOT, ["true"], binds=_BINDS, ephemeral=False, keep_unit=True, read_only=True
    )
    options = _options(both)
    assert "--keep-unit" in options and "--read-only" in options
    assert any(_TMPFS.match(o) for o in options), options
    # nothing else moves: without the read-only pair it is the keep_unit line
    plain = _nspawn_argv(_ROOT, ["true"], binds=_BINDS, ephemeral=False, keep_unit=True)
    assert [a for a in both if a != "--read-only" and not _TMPFS.match(a)] == plain
    shell = _nspawn_shell_argv(_ROOT, binds=_BINDS, keep_unit=True, read_only=True)
    assert "--keep-unit" in shell and "--read-only" in shell
    assert any(_TMPFS.match(o) for o in shell), shell


def test_a_read_only_container_nested_in_a_container_runs_with_both(
    tmp_path: Path, launched: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container, "inside_container", lambda: True)
    Container(tmp_path / "toolbox", read_only=True).run(["mksquashfs", "-version"])
    (cmd,) = launched
    options = _options(cmd)
    assert "--keep-unit" in options and "--read-only" in options


def test_the_image_container_nested_in_a_container_stays_writable(
    tmp_path: Path, launched: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrongly collapsed: being nested (``--keep-unit``) never makes the image read-only."""
    monkeypatch.setattr(container, "inside_container", lambda: True)
    Container(tmp_path / "img").run(["emerge", "--info"])
    (cmd,) = launched
    assert "--keep-unit" in _options(cmd)
    assert "--read-only" not in cmd
    assert not any(_TMPFS.match(o) for o in cmd)


def test_the_toolbox_still_writes_its_log(tmp_path: Path, launched: list[list[str]]) -> None:
    """``Toolbox(log=...)`` keeps streaming its tools' output into the log, read-only."""
    log = tmp_path / "logs" / "assemble-v3-kde-systemd.log"
    tools = Toolbox(tmp_path / "toolbox" / "v3-systemd", log=log)
    tools.run(["mksquashfs", "-version"])
    (cmd,) = launched
    assert "--read-only" in _options(cmd)
    assert "$ mksquashfs -version" in log.read_text()
