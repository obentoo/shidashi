"""The contract between the child ``assemble`` and its parent ``build`` (story 006, 7.1).

Every field ``summarize`` and the build's audit step read must be written by the
child, with the same names and types; and the command line the parent builds must
be one the child's CLI accepts with the same options. Here the REAL ``assemble``
CLI writes the result file (in a real child process, its privileged Assembler
faked) and the REAL ``run_children`` reads it.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from shidashi import cli, vm
from shidashi.assembler import AssembleResult
from shidashi.parallel import assemble_argv, run_children, summarize

#: The real ``shidashi assemble`` in a child process; only the privileged Assembler
#: and the build-host check are faked. An image named in $FAIL_IMAGE fails.
_REAL_CHILD = """
import os, sys
from pathlib import Path
from shidashi import cli, doctor
from shidashi.assembler import AssembleResult, AssemblerError
doctor.require_build_host = lambda _w: None

class FakeAssembler:
    def __init__(self, recipe, pkgdir, *, jobs=None):
        self.recipe = recipe
    def assemble(self, output_dir, **kw):
        if self.recipe.flavor == os.environ.get("FAIL_IMAGE"):
            raise AssemblerError("emerge failed in " + self.recipe.flavor)
        iso = output_dir / f"bentoo-x-{self.recipe.flavor}.iso"
        iso.parent.mkdir(parents=True, exist_ok=True)
        iso.write_bytes(b"ISO")
        return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

cli.Assembler = FakeAssembler
sys.argv = ["shidashi", *sys.argv[1:]]
cli.app()
"""

_OPTIONS: dict[str, Any] = {
    "arch": "v3",
    "init": "systemd",
    "jobs": 4,
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


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setenv("FAIL_IMAGE", "worker")
    script = tmp_path / "real_child.py"
    script.write_text(_REAL_CHILD)
    return script


def _child_run_ids(tmp_path: Path) -> dict[str, str]:
    """image -> the run id of its assemble's audit trail."""
    found: dict[str, str] = {}
    for run_dir in (tmp_path / "runs").iterdir():
        manifest = json.loads((run_dir / "manifest.json").read_text())
        found[manifest["inputs"]["recipe"]["flavor"]] = run_dir.name
    return found


def test_contract_a_real_childs_result_is_what_the_parent_reads(tmp_path: Path, env: Path) -> None:
    out = tmp_path / "out"
    logs = tmp_path / "scratch" / "logs" / "build-R"
    argvs = {}
    for image in ("minimal", "worker"):
        argv = assemble_argv(image, output_dir=out, result_file=logs / f"{image}.json", **_OPTIONS)
        argvs[image] = [sys.executable, str(env), *argv[argv.index("assemble") :]]  # same arguments
    results = {r.image: r for r in run_children(argvs, logs=logs)}
    run_ids = _child_run_ids(tmp_path)

    ok, failed = results["minimal"], results["worker"]
    assert ok.exit_code == 0, ok.log.read_text()
    assert ok.run_id == run_ids["minimal"]
    assert tuple(ok.isos) == (out / "bentoo-x-minimal.iso",)
    assert all(isinstance(p, Path) for p in ok.isos)
    assert isinstance(ok.duration_s, int | float)
    assert ok.error is None
    assert failed.exit_code == 1
    assert failed.run_id == run_ids["worker"]
    assert "emerge failed in worker" in failed.log.read_text()

    code, lines = summarize(list(results.values()))
    assert code == 1
    assert any("worker" in line and str(failed.log) in line for line in lines)
    assert any(str(out / "bentoo-x-minimal.iso") in line for line in lines)


_PARTIAL_CHILD = """
import json, sys
args = sys.argv[1:]
result = args[len(args) - 1 - args[::-1].index("--result-file") + 1]
payload = {"image": "kde", "exit_code": 0, "run_id": "r", "isos": ["/out/k.iso"], "duration_s": 1}
del payload[args[0]]
open(result, "w").write(json.dumps(payload))
"""


@pytest.mark.parametrize("missing", ["run_id", "isos"])
def test_contract_a_result_file_missing_a_required_field_fails_the_image(
    tmp_path: Path, missing: str
) -> None:
    logs = tmp_path / "logs"
    argv = [sys.executable, "-c", _PARTIAL_CHILD, missing, "--result-file", str(logs / "kde.json")]
    results = run_children({"kde": argv}, logs=logs)
    code, lines = summarize(results)
    assert code == 1, f"a result file without {missing} counted as success"
    assert any("kde" in line for line in lines)
    (kde,) = results
    # C1: exit_code is the process's own (0); the failure is the error
    assert kde.exit_code == 0 and kde.error == f"result file lacks {missing}"


def test_contract_the_parents_argv_is_accepted_by_the_child_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE", "SCRATCH", "RUNS"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    seen: dict[str, Any] = {}

    class FakeAssembler:
        def __init__(self, recipe: Any, pkgdir: Path, *, jobs: int | None = None) -> None:
            seen["jobs"], seen["flavor"] = jobs, recipe.flavor

        def assemble(self, output_dir: Path, **kw: object) -> AssembleResult:
            seen["output_dir"], seen["kwargs"] = output_dir, kw
            iso = output_dir / "bentoo-x.iso"
            iso.parent.mkdir(parents=True, exist_ok=True)
            iso.write_bytes(b"ISO")
            return AssembleResult(name=iso.stem, isos=(iso,), artifacts=())

    booted: list[object] = []

    def boot_test(iso: Path, **kw: object) -> dict[str, object]:
        booted.append(kw.get("cid"))
        return {"passed": True, "firmwares": {}}

    monkeypatch.setattr(cli, "Assembler", FakeAssembler)
    monkeypatch.setattr(vm, "boot_test", boot_test)
    argv = assemble_argv(
        "minimal",
        **{
            **_OPTIONS,
            "jobs": 8,
            "compressions": ("zstd", "xz"),
            "stage4": True,
            "sbom": False,
            "fresh": True,
            "trunk": False,
            "download": False,
            "keep": True,
            "boot_test": True,
            "cid": 45,
        },
        output_dir=tmp_path / "out",
        result_file=tmp_path / "r.json",
    )
    done = CliRunner().invoke(cli.app, argv[argv.index("assemble") :])
    assert done.exit_code == 0, done.output
    assert seen["flavor"] == "minimal" and seen["jobs"] == 8
    assert seen["output_dir"] == tmp_path / "out"
    assert seen["kwargs"] == {
        "download": False,
        "keep": True,
        "compressions": ("zstd", "xz"),
        "stage4": True,
        "sbom": False,
        "fresh": True,
        "trunk": False,
    }
    assert booted == [45]  # 45 is free in this scratch, so the allocation keeps it
    written = json.loads((tmp_path / "r.json").read_text())
    assert set(written) >= {"image", "exit_code", "run_id", "isos", "duration_s"}


def test_contract_written_and_required_fields() -> None:
    """C1: the child writes RESULT_FIELDS; the reader requires only REQUIRED_FIELDS."""
    from shidashi.parallel import REQUIRED_FIELDS, RESULT_FIELDS

    assert RESULT_FIELDS == ("image", "exit_code", "run_id", "isos", "duration_s")
    assert REQUIRED_FIELDS == ("run_id", "isos")
    assert set(REQUIRED_FIELDS) <= set(RESULT_FIELDS)
