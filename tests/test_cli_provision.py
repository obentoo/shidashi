"""Tests of ``shidashi worker provision NAME --iso PATH [--replace]`` through Typer's
CliRunner (story 020, task 1.4).

Everything the command writes goes under a temporary ``XDG_DATA_HOME``. Refusals use a
hand-built volume descriptor and run without any tool; the successful runs build a
small real ISO with xorriso and let ``ssh-keygen`` and ``xorriso`` really run (skipped
where they are absent). Paths and fingerprints are looked for in the output with all
whitespace removed: Rich may wrap a long line.

Requirements exercised: R1.4, R1.5, R1.6, R4.3 (the CLI's side: exit 1, ``error:``,
the message, no traceback; the printed name, fingerprint, ISO path and how to write it).
"""

import base64
import hashlib
import json
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import config
from shidashi.cli import app

runner = CliRunner()
NAME = "bentoo-lab"

needs_tools = pytest.mark.skipif(
    shutil.which("xorriso") is None or shutil.which("ssh-keygen") is None,
    reason="needs xorriso and ssh-keygen",
)


def _ed25519_line(seed: str) -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()}"


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


OLD_KEY = _ed25519_line("an-earlier-pairing")


def _out(result: Any) -> str:
    out: str = result.stdout + (getattr(result, "stderr", "") or "")
    return out


def _flat(text: str) -> str:
    return "".join(text.split())


def _pvd_image(path: Path, label: bytes) -> Path:
    pvd = bytearray(2048)
    pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
    pvd[8:40] = b" " * 32
    pvd[40:72] = label.ljust(32, b" ")
    end = bytearray(2048)
    end[0], end[1:6], end[6] = 255, b"CD001", 1
    path.write_bytes(bytes(16 * 2048) + bytes(pvd) + bytes(end))
    return path


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SHIDASHI_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path / "xdg"


@pytest.fixture(scope="module")
def worker_iso(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("xorriso") is None:
        pytest.skip("needs xorriso")
    base = tmp_path_factory.mktemp("cli-iso")
    tree = base / "tree"
    (tree / "LiveOS").mkdir(parents=True)
    (tree / "LiveOS" / "squashfs.img").write_bytes(b"squash" * 512)
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "boot" / "grub" / "eltorito.img").write_bytes(bytes(2048))
    iso = base / "worker.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-quiet", "-R", "-V", "BENTOO_WORKER"]
        + ["-b", "boot/grub/eltorito.img", "-no-emul-boot", "-boot-load-size", "4"]
        + ["--protective-msdos-label", "-o", str(iso), str(tree)],
        check=True,
        capture_output=True,
    )
    return iso


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["worker", "provision", *args])


def _pinned_for(known_hosts: Path, name: str) -> list[str]:
    return [
        " ".join(line.split()[1:3])
        for line in known_hosts.read_text().splitlines()
        if line.split() and name in line.split()[0].split(",")
    ]


def _isos_under(root: Path) -> list[Path]:
    return sorted(root.rglob("*.iso")) if root.exists() else []


# --- the command exists ---------------------------------------------------------------


def test_provision_help_names_the_iso_and_replace_options(xdg: Path) -> None:
    result = _invoke("--help")
    assert result.exit_code == 0, _out(result)
    assert "--iso" in _out(result)
    assert "--replace" in _out(result)


# --- refusals: exit 1, ``error:``, the message, nothing written -----------------------


@pytest.mark.parametrize("label", ["BENTOO_KDE", "BENTOO_WORKERS"])
def test_an_iso_of_another_image_exits_1_naming_its_volume(
    tmp_path: Path, xdg: Path, label: str
) -> None:
    """R1.5. Hostile ``BENTOO_WORKERS``: a label that only starts like the worker's."""
    iso = _pvd_image(tmp_path / "other.iso", label.encode())
    result = _invoke(NAME, "--iso", str(iso))
    out = _out(result)
    assert result.exit_code == 1, out
    assert "error:" in out
    assert "not a worker ISO" in out
    assert label in out
    assert "Traceback" not in out
    wd = config.workers_dir()
    assert not (wd / NAME).exists()
    assert not (wd / "known_hosts").exists()
    assert not (wd / "workers.json").exists()
    assert _isos_under(xdg) == []


def test_a_name_already_paired_exits_1_naming_its_key_and_the_replace_flag(
    tmp_path: Path, xdg: Path
) -> None:
    """R1.6: nothing changes -- not the pin, not the registry."""
    wd = config.workers_dir()
    wd.mkdir(parents=True, mode=0o700)
    entry = {
        "name": NAME,
        "address": "192.168.15.7",
        "host_key": OLD_KEY,
        "host_key_fingerprint": _fingerprint(OLD_KEY),
        "paired_at": "2026-10-05T12:00:00+00:00",
        "cpu_flags": ["avx2"],
        "image": "20261005T1200",
    }
    (wd / "workers.json").write_text(json.dumps({NAME: entry}, indent=1) + "\n")
    (wd / "known_hosts").write_text(f"{NAME} {OLD_KEY}\n")
    before = {p.name: p.read_bytes() for p in (wd / "workers.json", wd / "known_hosts")}
    iso = _pvd_image(tmp_path / "worker.iso", b"BENTOO_WORKER")

    result = _invoke(NAME, "--iso", str(iso))
    out = _out(result)
    assert result.exit_code == 1, out
    assert "error:" in out
    assert _fingerprint(OLD_KEY) in _flat(out)
    assert "--replace" in out
    assert "Traceback" not in out
    assert {p.name: p.read_bytes() for p in (wd / "workers.json", wd / "known_hosts")} == before
    assert not (wd / NAME).exists()


def test_a_workers_dir_inside_a_git_work_tree_exits_1_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: Path
) -> None:
    """R4.3: the data home lives in a checkout -- one ``git add`` from publishing it."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "repo" / "xdg"))
    iso = _pvd_image(tmp_path / "worker.iso", b"BENTOO_WORKER")
    result = _invoke(NAME, "--iso", str(iso))
    out = _out(result)
    assert result.exit_code == 1, out
    assert "error:" in out
    assert "inside a git work tree" in " ".join(out.split())
    assert _flat(str(config.workers_dir())) in _flat(out)
    assert "Traceback" not in out
    assert not (config.workers_dir() / NAME).exists()
    assert _isos_under(tmp_path / "repo") == []


def test_a_missing_iso_is_refused_writing_nothing(tmp_path: Path, xdg: Path) -> None:
    result = _invoke(NAME, "--iso", str(tmp_path / "absent.iso"))
    out = _out(result)
    assert result.exit_code != 0
    assert "No such command" not in out  # refused by the command, not for lack of one
    assert "absent.iso" in _flat(out)
    assert "Traceback" not in out
    assert not (config.workers_dir() / NAME).exists()


@pytest.mark.parametrize("name", ["Bentoo_Lab", "bentoo.lab", "lab-"])
def test_a_name_that_is_not_a_worker_name_is_refused_writing_nothing(
    tmp_path: Path, xdg: Path, name: str
) -> None:
    """The name becomes the worker's hostname and its known_hosts name."""
    iso = _pvd_image(tmp_path / "worker.iso", b"BENTOO_WORKER")
    result = _invoke(name, "--iso", str(iso))
    out = _out(result)
    assert result.exit_code != 0
    assert "No such command" not in out  # refused by the command, not for lack of one
    assert "worker name" in " ".join(out.split())
    assert "Traceback" not in out
    assert _isos_under(xdg) == []


# --- success --------------------------------------------------------------------------


@needs_tools
def test_provision_prints_the_name_fingerprint_iso_and_how_to_write_it(
    xdg: Path, worker_iso: Path
) -> None:
    """R1.4; the host key is made on first use, and it is the one the identity grants."""
    wd = config.workers_dir()
    assert not wd.exists()
    result = _invoke(NAME, "--iso", str(worker_iso))
    out = _out(result)
    assert result.exit_code == 0, out

    pinned = _pinned_for(wd / "known_hosts", NAME)
    assert len(pinned) == 1
    flat = _flat(out)
    assert NAME in out
    assert _fingerprint(pinned[0]) in flat
    assert _flat(str(wd / NAME / f"{NAME}.iso")) in flat
    assert "dd" in out.split() or "dd " in out
    assert "of=" in out
    assert "private key" in " ".join(out.lower().split())

    key = wd / "id_ed25519"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    record = json.loads((wd / NAME / "identity" / "pairing.json").read_text())
    host_pub = Path(f"{key}.pub").read_text()
    assert record["granting_key_fingerprint"] == _fingerprint(host_pub)
    registry = json.loads((wd / "workers.json").read_text())
    assert registry[NAME]["provisioned"] is True
    assert registry[NAME]["address"] is None


@needs_tools
def test_replace_through_the_cli_is_refused_without_the_flag_and_rekeys_with_it(
    xdg: Path, worker_iso: Path
) -> None:
    """R1.6 then R1.7 from the command line."""
    wd = config.workers_dir()
    first = _invoke(NAME, "--iso", str(worker_iso))
    assert first.exit_code == 0, _out(first)
    old = _pinned_for(wd / "known_hosts", NAME)

    again = _invoke(NAME, "--iso", str(worker_iso))
    assert again.exit_code == 1, _out(again)
    assert "--replace" in _out(again)
    assert _pinned_for(wd / "known_hosts", NAME) == old

    replaced = _invoke(NAME, "--iso", str(worker_iso), "--replace")
    assert replaced.exit_code == 0, _out(replaced)
    new = _pinned_for(wd / "known_hosts", NAME)
    assert len(new) == 1
    assert new != old
    assert _fingerprint(new[0]) in _flat(_out(replaced))
    assert _fingerprint(old[0]) not in _flat(_out(replaced))
