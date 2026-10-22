"""Producer -> consumer contracts of the release (story 030, task 6.1; R3.1, R3.2, R5.1,
R5.2, R6.3).

Two pairs, each with both sides real:

- **The outcome file.** What ``shidashi build --outcome-file`` writes (``build_outcome.
  recording`` around the real ``cli.build``) is what ``build_outcome.load`` reads, and what
  ``shidashi release`` puts into ``release.json`` and its summary. Here the release's child
  is the real ``build`` command, run in this process from the argv ``release.build_argv``
  made; only ``cli._build_planned`` (the plan and every node's share) is replaced, by a
  share that drops ISOs or meets a real ``GenerationMismatchError`` from
  ``generation.check_or_record``.
- **The release directory.** What ``release`` writes is what a user checks: every file named
  in ``SHA256SUMS`` exists with that digest (``sha256sum -c`` itself when the host has it),
  no signature and no outcome file is listed, and ``SHA256SUMS.asc`` covers the final
  ``SHA256SUMS``.

The lab, the publisher and the maintainer's gpg are tests/test_release_cli.py's.
"""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, publish
from shidashi.generation import (
    FINGERPRINT_FILE,
    GenerationFingerprint,
    GenerationMismatchError,
    check_or_record,
)
from tests._pending import try_import
from tests.test_release_cli import Lab, all_pairs, line_of, patch_everywhere, release_lab
from tests.test_release_core import FPR, KEY_FILE, fake_signature, listed

load: Any = try_import("shidashi.build_outcome", "load")

child_runner = CliRunner()


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


def iso_names(out: Path, arch: str, init: str) -> list[str]:
    return [
        f"bentoo-{out.name}-minimal-{init}-{arch}.iso",
        f"bentoo-{out.name}-kde-{init}-{arch}-xz.iso",
    ]


class PlannedShare:
    """Stands in for ``cli._build_planned``: the images of every variant are made, except
    ``refused`` variants, whose generation meets a gcc 16 toolchain."""

    def __init__(self, refused: set[tuple[str, str]]) -> None:
        self.refused = refused
        self.errors: dict[tuple[str, str], GenerationMismatchError] = {}

    def __call__(self, options: Any, _recipes: Any, pkgdir: Path, *_a: Any, **_k: Any) -> None:
        variant = (options.arch, options.init)
        if variant in self.refused:
            recorded = GenerationFingerprint.model_validate_json(
                (pkgdir / FINGERPRINT_FILE).read_text(encoding="utf-8")
            )
            with cli._build_errors():
                try:
                    check_or_record(pkgdir, recorded.model_copy(update={"gcc": "16.1.0"}))
                except GenerationMismatchError as err:
                    self.errors[variant] = err
                    raise
            return
        out = Path(options.output_dir)
        sums: dict[str, str] = {}
        for name in iso_names(out, options.arch, options.init):
            iso = out / name
            iso.write_bytes(f"iso {name}".encode())
            digests, sha256 = publish.write_digests(iso)
            sums[name] = sha256
            sums[digests.name] = publish.sha256_of(digests)
        publish.update_sha256sums(out, sums)


def in_process_child(calls: list[list[str]]) -> Callable[..., int]:
    """``release.run_build`` running the real ``shidashi build`` from its argv, here."""

    def run(argv: Any, *_a: Any, **_k: Any) -> int:
        argv = [str(a) for a in argv]
        calls.append(argv)
        result = child_runner.invoke(cli.app, argv[argv.index("build") :])
        return int(result.exit_code)

    return run


# --- the outcome file: build -> load ------------------------------------------------------------


def test_what_build_writes_is_what_load_reads_for_an_ok_and_a_refused_build(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.1, R3.2: both through the real ``shidashi build --outcome-file``."""
    share = PlannedShare(refused={("v3", "openrc")})
    monkeypatch.setattr(cli, "_build_planned", share)
    out = lab.root / "out"
    out.mkdir()
    ok_file, refused_file = out / "ok.json", out / "refused.json"
    ok = child_runner.invoke(
        cli.app, ["build", "v3", "systemd", "-o", str(out), "--outcome-file", str(ok_file)]
    )
    refused = child_runner.invoke(
        cli.app, ["build", "v3", "openrc", "-o", str(out), "--outcome-file", str(refused_file)]
    )
    assert ok.exit_code == 0, ok.output
    assert refused.exit_code == 1, refused.output
    assert ok_file.is_file() and refused_file.is_file(), "no outcome file"

    built = load(ok_file)
    assert (built.arch, built.init, built.status, built.exit_code) == ("v3", "systemd", "ok", 0)
    assert sorted(built.isos) == sorted(iso_names(out, "v3", "systemd"))

    stopped = load(refused_file)
    error = share.errors[("v3", "openrc")]
    assert (stopped.arch, stopped.init, stopped.status) == ("v3", "openrc", "toolchain-refused")
    assert stopped.exit_code == 1
    assert stopped.toolchain is not None
    assert stopped.toolchain.successor == str(error.successor)
    assert stopped.toolchain.pkgdir == str(lab.pkgdir("v3"))
    assert any("gcc" in d for d in stopped.toolchain.differences)


# --- the outcome file: build -> release.json and the summary ----------------------------------


def test_the_files_real_child_builds_write_reach_release_json_and_the_summary(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.1, R3.2, R5.1: the release's children are real builds; what each wrote reaches
    its variant's entry and its summary line."""
    refused = ("v3", "openrc")
    share = PlannedShare(refused={refused})
    monkeypatch.setattr(cli, "_build_planned", share)
    calls: list[list[str]] = []
    patch_everywhere(monkeypatch, "shidashi.release", "run_build", in_process_child(calls))

    result = lab.release("--all", "--key", FPR)
    assert len(calls) == len(all_pairs()), result.output
    assert result.exit_code == 1, result.output
    directory = lab.the_release()

    for arch, init in all_pairs():
        entry = lab.variant(arch, init)
        if (arch, init) == refused:
            continue
        assert entry["status"] == "ok", entry
        assert sorted(entry["isos"]) == sorted(iso_names(directory, arch, init))
    entry = lab.variant(*refused)
    error = share.errors[refused]
    assert entry["status"] == "toolchain-refused"
    assert entry["toolchain_refusal"]["successor"] == str(error.successor)
    assert entry["toolchain_refusal"]["pkgdir"] == str(lab.pkgdir("v3"))
    assert str(error.successor) in line_of(result.output, *refused)


# --- the release directory: release -> the user's checks ----------------------------------------


def test_every_file_sha256sums_names_exists_with_its_digest_and_nothing_else_is_listed(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.2, R6.3: what a user runs -- ``sha256sum -c SHA256SUMS`` -- passes; signatures and
    outcome files are not listed; ``SHA256SUMS.asc`` signs the SHA256SUMS on disk."""
    monkeypatch.setattr(cli, "_build_planned", PlannedShare(refused=set()))
    calls: list[list[str]] = []
    patch_everywhere(monkeypatch, "shidashi.release", "run_build", in_process_child(calls))
    result = lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    directory = lab.the_release()

    names = listed(directory)
    assert {"release.json", KEY_FILE} <= set(names)
    assert set(iso_names(directory, "v3", "systemd")) <= set(names)
    assert not [n for n in names if n.endswith(".asc") and n != KEY_FILE], names
    assert not [n for n in names if n.startswith(".") or "/" in n], names
    for name, digest in names.items():
        assert (directory / name).is_file(), name
        assert publish.sha256_of(directory / name) == digest, name
    if shutil.which("sha256sum") is not None:
        checked = subprocess.run(
            ["sha256sum", "--quiet", "-c", "SHA256SUMS"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
    sums = (directory / "SHA256SUMS").read_bytes()
    assert (directory / "SHA256SUMS.asc").read_text(encoding="utf-8") == fake_signature(FPR, sums)
