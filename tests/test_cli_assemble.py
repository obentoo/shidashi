"""UNIT tests of the ``shidashi assemble`` CLI via Typer's ``CliRunner`` (Phase 1).

Deterministic on the CI host: ``Assembler.assemble`` (privileged) is monkeypatched in
the ``shidashi.cli`` namespace to return a synthetic ISO ``Path`` or raise
``AssemblerError``/``ImageError``/``SeedError``/``ResolveError``/``CalledProcessError``
— we exercise only the CLI LAYER: ``--help``, pretty/json rendering, exit code
mapping and flag propagation (``--output``/``--no-download``/``--keep``/``--binhost``).
No stage3/nspawn/mksquashfs is touched (mirrors tests/test_cli_factory.py).
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli
from shidashi.assembler import AssemblerError
from shidashi.cli import app
from shidashi.image import ImageError
from shidashi.resolve import ResolveError
from shidashi.seed import SeedError
from tests._variants_tree import write_variants

runner = CliRunner()


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared stage-format tree (tests/_variants_tree.py), pointed at by the env."""
    root = write_variants(tmp_path / "variants")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    # cache/scratch under tmp so --binhost's default (config.pkgdir) never touches /var.
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    return root


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default --output-dir is the current directory: never the repository's."""
    monkeypatch.chdir(tmp_path)


# --- fake Assembler -----------------------------------------------------------


class _FakeInstance:
    def __init__(
        self, assemble_fn: Any, recipe: object, binhost: Path, jobs: int | None = None
    ) -> None:
        self._assemble_fn = assemble_fn
        self.recipe = recipe
        self.binhost = binhost
        self.jobs = jobs

    def assemble(self, output: Path, **kwargs: object) -> Any:
        from shidashi.assembler import AssembleResult

        produced = self._assemble_fn(output, **kwargs)
        if isinstance(produced, Path):  # the tests name the ISO; the CLI gets a result
            return AssembleResult(name=produced.stem, isos=(produced,), artifacts=())
        return produced


def _fake_assembler(assemble_fn: Any, sink: dict[str, Any] | None = None) -> Any:
    def _ctor(recipe: object, binhost: Path, *, jobs: int | None = None) -> _FakeInstance:
        inst = _FakeInstance(assemble_fn, recipe, binhost, jobs)
        if sink is not None:
            sink["instance"] = inst
        return inst

    return _ctor


# --- --help -------------------------------------------------------------------


def test_help_lists_assemble() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "assemble" in result.stdout


def test_assemble_help_shows_options() -> None:
    result = runner.invoke(app, ["assemble", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "--format" in out
    assert "--output" in out
    assert "--binhost" in out
    assert "--no-download" in out
    assert "--keep" in out


# --- pretty/json success ------------------------------------------------------


def test_assemble_success_pretty_exit0(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    iso = Path("/dist/bentoo-minimal-systemd-v3.iso")
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda _o, **_k: iso), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    assert "bentoo-minimal-systemd-v3.iso" in result.stdout


def test_assemble_json_shape(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    iso = Path("/dist/x.iso")
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda _o, **_k: iso), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["isos"] == ["/dist/x.iso"] and data["name"] == "x"
    assert data["arch"] == "v3"
    assert data["flavor"] == "minimal"
    assert data["init"] == "systemd"
    assert "binhost" in data


# --- flag propagation --------------------------------------------------------


def test_assemble_passes_output_and_flags(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    captured: dict[str, Any] = {}

    def _capture(output: Path, **kwargs: object) -> Path:
        captured["output"] = output
        captured["kwargs"] = kwargs
        return output

    monkeypatch.setattr(cli, "Assembler", _fake_assembler(_capture), raising=False)
    result = runner.invoke(
        app,
        [
            "assemble",
            "v3",
            "minimal",
            "systemd",
            "-o",
            "/tmp/out",
            "--no-download",
            "--keep",
            "--compression",
            "both",
            "--stage4",
            "--no-sbom",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["output"] == Path("/tmp/out")
    assert captured["kwargs"] == {
        "download": False,
        "keep": True,
        "compressions": ("zstd", "xz"),
        "stage4": True,
        "sbom": False,
    }


def test_assemble_default_output_name(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        cli,
        "Assembler",
        _fake_assembler(lambda o, **_k: captured.update(output=o) or o),
        raising=False,
    )
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    # the directory; the ISO is named bentoo-<date>-minimal-systemd-v3.iso inside it
    assert captured["output"] == Path(".")


def test_assemble_binhost_default_is_per_arch_and_generation(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda o, **_k: o, sink), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    # the default binhost is partitioned per arch and per GENERATION (D26): the same
    # .../binpkgs/v3/<stage3 snapshot> the factory writes to
    from shidashi import config
    from shidashi.seed import load_pointer

    generation = load_pointer("systemd", seeds_dir=config.seeds_dir()).snapshot
    assert sink["instance"].binhost.parts[-3:] == ("binpkgs", "v3", generation)


# --- error mapping → friendly exit 1 -----------------------------------------


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (AssemblerError("no vmlinuz found"), "vmlinuz"),
        (ImageError("mksquashfs missing on the host"), "mksquashfs"),
        (SeedError("sha512 mismatch for stage3 tarball"), "sha512"),
        (ResolveError("repo 'bentoo' missing; run emerge --sync"), "bentoo"),
    ],
)
def test_assemble_known_errors_exit1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path, exc: Exception, needle: str
) -> None:
    def _raise(_o: Path, **_k: object) -> Any:
        raise exc

    monkeypatch.setattr(cli, "Assembler", _fake_assembler(_raise), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert needle.lower() in combined.lower()


def test_assemble_emerge_failure_exit1(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    def _raise(_o: Path, **_k: object) -> Any:
        raise subprocess.CalledProcessError(1, ["emerge"], stderr="!!! no binpkg for @kde")

    monkeypatch.setattr(cli, "Assembler", _fake_assembler(_raise), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined
    assert "emerge/dracut" in combined


def test_assemble_unknown_axis_exit1(variants_tree: Path) -> None:
    # an unknown axis fails in _resolve, before the Assembler is instantiated.
    result = runner.invoke(app, ["assemble", "v3", "doesnotexist", "systemd"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined


def test_assemble_is_not_a_stub(monkeypatch: pytest.MonkeyPatch, variants_tree: Path) -> None:
    # regression: assemble is no longer the exit-2 "Phase 0" stub.
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda o, **_k: o), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0
    assert "Phase 0" not in result.stdout


def test_assemble_jobs_reaches_the_assembler(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda o, **_k: o, sink), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd", "--jobs", "8"])
    assert result.exit_code == 0, result.stdout
    assert sink["instance"].jobs == 8


def test_assemble_without_jobs_leaves_the_defaults(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path
) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda o, **_k: o, sink), raising=False)
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd"])
    assert result.exit_code == 0, result.stdout
    assert sink["instance"].jobs is None


def test_assemble_refuses_zero_jobs(variants_tree: Path) -> None:
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd", "--jobs", "0"])
    assert result.exit_code != 0


def test_assemble_writes_an_audit_trail_with_the_recipe_and_pins(
    monkeypatch: pytest.MonkeyPatch, variants_tree: Path, tmp_path: Path
) -> None:
    import json

    runs = tmp_path / "runs"
    monkeypatch.setenv("SHIDASHI_RUNS", str(runs))
    iso = tmp_path / "out" / "bentoo-x.iso"
    monkeypatch.setattr(cli, "Assembler", _fake_assembler(lambda o, **_k: iso), raising=False)
    result = runner.invoke(
        app, ["assemble", "v3", "minimal", "systemd", "--jobs", "4", "-o", str(iso.parent)]
    )
    assert result.exit_code == 0, result.stdout
    (run_dir,) = runs.iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["command"] == "assemble" and manifest["status"] == "ok"
    inputs = manifest["inputs"]
    assert inputs["recipe"]["flavor"] == "minimal" and inputs["jobs"] == 4
    assert {"repository", "stage3", "gentoo_tree", "overlays"} <= set(inputs)
    assert (run_dir / "report.md").is_file()
    # the closed trail is published beside the ISO, and checksummed
    bundle = iso.parent / "bentoo-x.build.tar.zst"
    assert bundle.is_file()
    assert "bentoo-x.build.tar.zst" in (iso.parent / "SHA256SUMS").read_text()
    assert f"audit: {run_dir}" in result.output.replace("\n", "")


def test_assemble_refuses_an_unknown_compression(variants_tree: Path) -> None:
    result = runner.invoke(app, ["assemble", "v3", "minimal", "systemd", "--compression", "lz4"])
    assert result.exit_code == 1
    assert "zstd, xz or both" in result.output
