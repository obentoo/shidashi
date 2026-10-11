"""A leftover identity directory is refused up front (story 022, task 1.1).

Without ``--replace``, a ``workers_dir/N/`` that already exists made ``provision`` generate
a key and write the whole ISO copy before failing on "Directory not empty". It is now one
of the refusal checks: nothing is run and nothing is written, and the message names the
directory and ``--replace``.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import provision
from shidashi.cli import app

NAME = "bentoo-lab"

needs_tools = pytest.mark.skipif(
    shutil.which("xorriso") is None or shutil.which("ssh-keygen") is None,
    reason="needs xorriso and ssh-keygen",
)


def _pvd_image(path: Path, label: bytes = b"BENTOO_WORKER") -> Path:
    """An ISO 9660 image reduced to its Primary Volume Descriptor: enough for the
    refusal checks, which read the label and run no tool."""
    pvd = bytearray(2048)
    pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
    pvd[40:72] = label.ljust(32, b" ")
    end = bytearray(2048)
    end[0], end[1:6], end[6] = 255, b"CD001", 1
    path.write_bytes(bytes(16 * 2048) + bytes(pvd) + bytes(end))
    return path


def _snapshot(root: Path) -> dict[str, tuple[str, int, bytes]]:
    out: dict[str, tuple[str, int, bytes]] = {}
    for path in sorted([root, *root.rglob("*")]):
        rel = str(path.relative_to(root))
        if path.is_dir():
            out[rel] = ("dir", stat.S_IMODE(path.stat().st_mode), b"")
        else:
            out[rel] = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    return out


def _no_tool(*_a: object, **_k: object) -> Any:
    raise AssertionError("a refused provision ran a tool")


@pytest.fixture
def workers_dir(tmp_path: Path) -> Path:
    wd = tmp_path / "xdg" / "shidashi" / "worker"
    wd.mkdir(parents=True, mode=0o700)
    return wd


@pytest.mark.parametrize("leftover", ["dir", "file"])
def test_a_leftover_identity_is_refused_before_anything_runs(
    tmp_path: Path, workers_dir: Path, leftover: str
) -> None:
    """No registry entry and no pin: only the directory is left (a crash, a hand copy)."""
    target = workers_dir / NAME
    if leftover == "dir":
        (target / "identity").mkdir(parents=True)
        (target / "identity" / "pairing.json").write_text("{}")
    else:
        target.write_text("stray")
    iso = _pvd_image(tmp_path / "worker.iso")
    before = _snapshot(workers_dir)
    with pytest.raises(provision.ProvisionError) as caught:
        provision.provision(NAME, iso, workers_dir=workers_dir, replace=False, runner=_no_tool)
    message = str(caught.value)
    assert str(target) in message
    assert "--replace" in message
    assert _snapshot(workers_dir) == before


def test_without_a_leftover_the_refusal_does_not_fire(tmp_path: Path, workers_dir: Path) -> None:
    """Hostile (the converse): a name with no directory goes past the checks to the tools."""
    iso = _pvd_image(tmp_path / "worker.iso")
    calls: list[list[str]] = []

    def _record(argv: list[str], **_k: object) -> Any:
        calls.append(argv)
        raise OSError("stop here")

    with pytest.raises(provision.ProvisionError) as caught:
        provision.provision(
            NAME,
            iso,
            workers_dir=workers_dir,
            replace=False,
            runner=_record,
            ensure_host_key=lambda: (workers_dir / "id_ed25519.pub").write_text(
                "ssh-ed25519 AAAA x\n"
            ),
        )
    assert "already exists" not in str(caught.value)
    assert calls, "no tool ran: the leftover refusal fired on a fresh name"


@needs_tools
def test_replace_still_replaces_a_leftover_identity(tmp_path: Path, workers_dir: Path) -> None:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(workers_dir / "id_ed25519")],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    tree = tmp_path / "tree"
    (tree / "LiveOS").mkdir(parents=True)
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "LiveOS" / "squashfs.img").write_bytes(b"squash")
    (tree / "boot" / "grub" / "eltorito.img").write_bytes(bytes(2048))
    iso = tmp_path / "worker.iso"
    subprocess.run(
        ["xorriso", "-no_rc", "-as", "mkisofs", "-quiet", "-R", "-V", "BENTOO_WORKER"]
        + ["-b", "boot/grub/eltorito.img", "-no-emul-boot", "-boot-load-size", "4"]
        + ["-boot-info-table", "--protective-msdos-label", "-o", str(iso), str(tree)],
        check=True,
        capture_output=True,
    )
    stale = workers_dir / NAME / "stale-marker"
    stale.parent.mkdir()
    stale.write_text("old")
    done = provision.provision(
        NAME, iso, workers_dir=workers_dir, replace=True, runner=subprocess.run
    )
    assert done.iso_path.is_file()
    assert not stale.exists()
    assert not any(p.name.startswith(f".{NAME}.") for p in workers_dir.iterdir())


def test_the_cli_refuses_a_leftover_with_an_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("COLUMNS", "300")
    target = tmp_path / "xdg" / "shidashi" / "worker" / NAME
    target.mkdir(parents=True)
    iso = _pvd_image(tmp_path / "worker.iso")
    monkeypatch.setattr(subprocess, "run", _no_tool)
    result = CliRunner().invoke(app, ["worker", "provision", NAME, "--iso", str(iso)])
    out = result.stdout + (result.stderr or "")
    assert result.exit_code == 1, out
    assert "error:" in out
    assert "--replace" in out
    assert not (target.parent / "id_ed25519").exists()  # the host key is not even made
    assert os.listdir(target) == []
