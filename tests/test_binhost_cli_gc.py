"""``shidashi binhost gc <arch>`` (story 029, tasks 3.2 and 3.3; R2.8, R3.1, R3.3-R3.5,
R3.7-R3.12).

Through the real CLI over the lab of tests/test_binhost_cli_show.py. Only the toolbox is
faked, the way tests/test_assembler.py fakes it (``toolbox.tarball_path``,
``ensure_rootfs``, ``using``, ``Toolbox``): its commands run here, with ``/work/<name>``
mapped back to the host directory mounted there and a fake ``emaint`` first on PATH that
regenerates ``Packages`` as Portage does -- every entry whose file is gone drops out.

``-k worker`` selects task 3.3's tests (a worker-owned arch); no other test name says it.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from shidashi import config, ownership, toolbox, workers
from shidashi import worker as worker_mod
from shidashi.container import CommandResult
from shidashi.workers import WorkerEntry
from tests.test_binhost_cli_show import (
    GEN,
    OTHER_GEN,
    QUARANTINE,
    Lab,
    binhost,
    make_lab,
    per_init_generation,
)
from tests.test_binhost_refs import entry

# --- the fake toolbox ------------------------------------------------------------------------

_FAKE_TOOL = r"""
import json, os, sys
from pathlib import Path

name, args = Path(sys.argv[0]).name, sys.argv[1:]
log = {"tool": name, "argv": args, "pkgdir": os.environ.get("PKGDIR"),
       "env": {k: v for k, v in os.environ.items() if k.startswith("BINPKG_GPG")}}
fail = os.environ.get("FAKE_FAIL", "")


def blocks(text):
    for block in text.split("\n\n"):
        fields = dict(l.split(": ", 1) for l in block.strip().splitlines() if ": " in l)
        if fields:
            yield block.strip("\n"), fields


def record():
    with open(os.environ["FAKE_LOG"], "a") as out:
        out.write(json.dumps(log) + "\n")


if name == "emaint" and "binhost" in args:
    root = Path(os.environ["PKGDIR"])
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*.gpkg.tar"))
    log["files"] = files
    record()
    if fail == "emaint":
        sys.stderr.write("emaint: !!! fake index failure\n")
        sys.exit(1)
    known = {}
    sources = [root / "Packages"]
    if os.environ.get("FAKE_SOURCE_INDEX"):
        sources.append(Path(os.environ["FAKE_SOURCE_INDEX"]))
    for source in sources:
        if source.is_file():
            for block, fields in blocks(source.read_text()):
                if "PATH" in fields:
                    known.setdefault(fields["PATH"], block)
    kept = [known.get(rel, "CPV: unknown/" + Path(rel).name + "\nPATH: " + rel) for rel in files]
    (root / "Packages").write_text("\n\n".join(["ARCH: amd64\nVERSION: 0", *kept]) + "\n")
    sys.exit(0)
if name == "gpkg-sign":
    record()
    if fail == "gpkg-sign" or (fail.startswith("gpkg-sign:") and fail[10:] in " ".join(args)):
        sys.stderr.write("gpkg-sign: !!! fake signing failure\n")
        sys.exit(1)
    for target in args:
        if target.endswith(".gpkg.tar"):
            # rewritten IN PLACE, as a hardlinked copy would see it
            with open(target, "ab") as fh:
                key = os.environ.get("BINPKG_GPG_SIGNING_KEY", "?")
                fh.write(b"\nSIGNED-BY " + key.encode())
    sys.exit(0)
record()
sys.exit(0)
"""

_WORK = re.compile(r"/work/([A-Za-z0-9_.+-]+)")


class FakeTools:
    """Stands for ``toolbox.Toolbox``: its constructor, ``path``, ``run`` (stdout) and
    ``container.run`` (a ``CommandResult``) -- but each command runs on this host, every
    ``/work/<name>`` mapped back to the host directory mounted there (any other path is
    already a host path). Each instance is recorded in ``made``."""

    made: ClassVar[list[FakeTools]] = []
    #: the (arch, init) of every recipe ``toolbox.tarball_path`` was asked for
    resolved: ClassVar[list[tuple[str, str]]] = []
    tarball: ClassVar[Path] = Path()
    bindir: ClassVar[Path] = Path()
    log: ClassVar[Path] = Path()
    env: ClassVar[dict[str, str]] = {}
    on_run: ClassVar[list[Callable[[list[str]], None]]] = []

    def __init__(
        self,
        rootfs: Path | None = None,
        *,
        ro: Mapping[str, Path] | None = None,
        rw: Mapping[str, Path] | None = None,
        log: Path | None = None,
        file_binds: Sequence[tuple[Path, Path]] = (),
    ) -> None:
        self.rootfs = rootfs
        self.rw = dict(rw or {})
        self.file_binds = [(Path(src), Path(dst)) for src, dst in file_binds]
        self.mounts = {
            name: Path(host).resolve() for name, host in {**(ro or {}), **(rw or {})}.items()
        }
        self.container = _ContainerView(self)
        FakeTools.made.append(self)

    def path(self, host: Path) -> Path:
        resolved = Path(host).resolve()
        best: tuple[int, Path] | None = None
        for name, root in self.mounts.items():
            inside = resolved == root or root in resolved.parents
            if inside and (best is None or len(root.parts) > best[0]):
                best = (len(root.parts), Path("/work") / name / resolved.relative_to(root))
        return best[1] if best is not None else resolved

    def _host(self, arg: str) -> str:
        def back(match: re.Match[str]) -> str:
            root = self.mounts.get(match.group(1))
            return str(root) if root is not None else match.group(0)

        return _WORK.sub(back, str(arg))

    def invoke(
        self, argv: Sequence[str], env: Mapping[str, str] | None = None, check: bool = True
    ) -> CommandResult:
        for hook in FakeTools.on_run:
            hook([str(a) for a in argv])
        full = {
            **os.environ,
            **FakeTools.env,
            **(env or {}),
            "PATH": f"{FakeTools.bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(FakeTools.log),
        }
        done = subprocess.run(
            [self._host(a) for a in argv], env=full, capture_output=True, text=True, check=False
        )
        if check and done.returncode != 0:
            raise subprocess.CalledProcessError(
                done.returncode, [str(a) for a in argv], output=done.stdout, stderr=done.stderr
            )
        return CommandResult(done.returncode, done.stdout, done.stderr)

    def run(self, argv: Sequence[str]) -> str:
        return self.invoke(argv).stdout

    def versions(self) -> dict[str, str]:
        return {}

    @staticmethod
    def calls(tool: str | None = None) -> list[dict[str, Any]]:
        if not FakeTools.log.is_file():
            return []
        lines = FakeTools.log.read_text(encoding="utf-8").splitlines()
        return [r for r in map(json.loads, lines) if tool is None or r["tool"] == tool]


class _ContainerView:
    """``Toolbox.container``: the same commands, ``Container.run``'s return type."""

    def __init__(self, tools: FakeTools) -> None:
        self._tools = tools

    def run(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, check: bool = True
    ) -> CommandResult:
        return self._tools.invoke(argv, env, check)


def install_fake_toolbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> type[FakeTools]:
    bindir = tmp_path / "toolbox-bin"
    bindir.mkdir()
    tool = bindir / "fake-tool"
    tool.write_text(f"#!{sys.executable}\n{_FAKE_TOOL}", encoding="utf-8")
    tool.chmod(0o755)
    for name in ("emaint", "gpkg-sign"):
        (bindir / name).symlink_to(tool)
    tarball = tmp_path / "toolbox.tar"
    tarball.write_bytes(b"not a real toolbox\n")
    monkeypatch.setattr(FakeTools, "made", [])
    monkeypatch.setattr(FakeTools, "bindir", bindir)
    monkeypatch.setattr(FakeTools, "log", tmp_path / "toolbox-calls.jsonl")
    monkeypatch.setattr(FakeTools, "env", {})
    monkeypatch.setattr(FakeTools, "on_run", [])
    monkeypatch.setattr(FakeTools, "resolved", [])
    monkeypatch.setattr(FakeTools, "tarball", tarball)

    def tarball_path(recipe: Any, *_a: Any, **_k: Any) -> Path:
        FakeTools.resolved.append((recipe.arch, recipe.init))
        return tarball

    def present(tar: Path, *_a: Any, **_k: Any) -> None:
        if not Path(tar).is_file():  # as toolbox._require_tarball
            raise toolbox.ToolboxError(f"no toolbox at {tar}")

    def ensure_rootfs(tar: Path, *_a: Any, **_k: Any) -> bool:
        present(tar)
        return False

    def using(tar: Path, *_a: Any, **_k: Any) -> Any:
        present(tar)
        return contextlib.nullcontext(False)

    monkeypatch.setattr(toolbox, "tarball_path", tarball_path)
    monkeypatch.setattr(toolbox, "ensure_rootfs", ensure_rootfs)
    monkeypatch.setattr(toolbox, "using", using)
    monkeypatch.setattr(toolbox, "Toolbox", FakeTools)
    return FakeTools


# --- the lab of this file ---------------------------------------------------------------------

#: dev-libs/foo, slot 0: four unavailable CPVs (1.0 has two instances) and one available CPV
#: with two instances; app-misc/bar: one unavailable CPV, alone in its group.
FOO_10_1 = entry("dev-libs/foo-1.0", 1, build_time=100)
FOO_10_2 = entry("dev-libs/foo-1.0", 2, build_time=110)
FOO_11 = entry("dev-libs/foo-1.1", 1, build_time=200)
FOO_12 = entry("dev-libs/foo-1.2", 1, build_time=300)
FOO_13 = entry("dev-libs/foo-1.3", 1, build_time=400)
FOO_20_1 = entry("dev-libs/foo-2.0", 1, build_time=500)
FOO_20_2 = entry("dev-libs/foo-2.0", 2, build_time=50)  # an older instance of an available CPV
BAR = entry("app-misc/bar-1", 1, build_time=10)
ALL = (FOO_10_1, FOO_10_2, FOO_11, FOO_12, FOO_13, FOO_20_1, FOO_20_2, BAR)


def path_of(block: str) -> str:
    return dict(line.split(": ", 1) for line in block.splitlines())["PATH"]


#: keep=2: foo 1.3 and 1.2 stay; 1.1 and both instances of 1.0 go. bar is alone: stays.
PLANNED = {path_of(b) for b in (FOO_10_1, FOO_10_2, FOO_11)}
EVERY = {path_of(b) for b in ALL}


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    lab = make_lab(tmp_path, monkeypatch)
    lab.ebuild("dev-libs/foo-2.0")
    lab.pool(*ALL)
    install_fake_toolbox(tmp_path, monkeypatch)
    return lab


def pool_files(pkgdir: Path) -> set[str]:
    return {str(p.relative_to(pkgdir)) for p in pkgdir.rglob("*.gpkg.tar")}


def quarantined(lab: Lab) -> dict[str, Path]:
    """Every file under the arch's quarantine of this generation, by its name."""
    root = lab.cache / "quarantine" / "binpkgs" / "v3" / GEN
    return {p.name: p for p in root.rglob("*") if p.is_file()}


def listed(pkgdir: Path) -> set[str]:
    path = pkgdir / QUARANTINE
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8"))["paths"])


def _flat(text: str) -> str:
    return "".join(text.split())


# --- dry run ---------------------------------------------------------------------------------


def test_dry_run_prints_the_planned_paths_and_writes_nothing(lab: Lab) -> None:
    """R3.5."""
    index_before = (lab.pkgdir() / "Packages").read_bytes()
    result = binhost("gc", "v3", "--dry-run")
    assert result.exit_code == 0, result.output
    for rel in PLANNED:
        assert rel in result.output
    for kept in (FOO_12, FOO_13, FOO_20_1, FOO_20_2, BAR):
        assert path_of(kept) not in result.output
    assert pool_files(lab.pkgdir()) == EVERY
    assert quarantined(lab) == {}
    assert not (lab.pkgdir() / QUARANTINE).exists()
    assert FakeTools.calls("emaint") == []
    assert (lab.pkgdir() / "Packages").read_bytes() == index_before


# --- the gc ----------------------------------------------------------------------------------


def test_gc_moves_exactly_the_plan_into_the_arch_quarantine(lab: Lab) -> None:
    """R3.1, R3.2. Hostile halves: foo-2.0's second, older instance stays (available);
    foo-1.0's two instances both go (by PATH, never by CPV alone)."""
    result = binhost("gc", "v3")
    assert result.exit_code == 0, result.output
    assert pool_files(lab.pkgdir()) == EVERY - PLANNED
    moved = quarantined(lab)
    assert set(moved) == {Path(p).name for p in PLANNED}
    for name, where in moved.items():
        assert where.parent.name == "dev-libs", where  # <quarantine>/<arch>/<gen>/<category>/
        assert where.read_bytes().startswith(b"binpkg dev-libs/"), name


def test_gc_lists_what_it_moved_and_reindexes_the_build_pool(lab: Lab) -> None:
    """R3.3, R3.4: the list names the PATHs; the index, regenerated in the toolbox over
    the build pool, no longer names a moved file."""
    result = binhost("gc", "v3")
    assert result.exit_code == 0, result.output
    assert listed(lab.pkgdir()) == PLANNED
    calls = FakeTools.calls("emaint")
    assert len(calls) == 1, calls
    assert Path(calls[0]["pkgdir"]).resolve() == lab.pkgdir().resolve()
    text = (lab.pkgdir() / "Packages").read_text(encoding="utf-8")
    for rel in PLANNED:
        assert rel not in text
    assert path_of(FOO_20_2) in text


def test_a_second_gc_appends_to_the_list(lab: Lab) -> None:
    """R3.4: a later gc with a smaller keep adds its PATHs to the earlier ones."""
    first = binhost("gc", "v3")
    assert first.exit_code == 0, first.output
    result = binhost("gc", "v3", "--keep", "1")
    assert result.exit_code == 0, result.output
    assert listed(lab.pkgdir()) == PLANNED | {path_of(FOO_12)}
    assert path_of(FOO_13) in pool_files(lab.pkgdir())


def test_keep_zero_moves_every_unavailable_cpv_and_never_an_available_one(lab: Lab) -> None:
    result = binhost("gc", "v3", "--keep", "0")
    assert result.exit_code == 0, result.output
    assert pool_files(lab.pkgdir()) == {path_of(FOO_20_1), path_of(FOO_20_2)}


def test_gc_holds_the_owner_lock_while_it_works_and_releases_it(lab: Lab) -> None:
    """R3.9: the reindex (the last write) runs under the arch's lock, held by this host."""
    held: list[Any] = []
    FakeTools.on_run.append(lambda _argv: held.append(ownership.current("v3")))
    result = binhost("gc", "v3")
    assert result.exit_code == 0, result.output
    assert held, "nothing ran in the toolbox"
    assert all(h is not None and h.worker.startswith("host:") for h in held), held
    assert ownership.current("v3") is None


def _hold_elsewhere(arch: str) -> None:
    ownership.acquire(
        arch,
        ownership.Owner(
            arch=arch,
            worker="bentoo-lab",
            job="fac-" + arch,
            commit="a" * 40,
            since="2026-10-20T10:00:00Z",
            host_pid=None,
        ),
    )


def test_a_held_lock_refuses_naming_the_holder_and_moves_nothing(lab: Lab) -> None:
    """R3.8."""
    _hold_elsewhere("v3")
    result = binhost("gc", "v3")
    assert result.exit_code == 1
    assert "bentoo-lab" in result.output and "fac-v3" in result.output
    assert pool_files(lab.pkgdir()) == EVERY
    assert FakeTools.calls("emaint") == []
    held = ownership.current("v3")
    assert held is not None and held.worker == "bentoo-lab"  # never taken over


def test_a_lock_on_another_arch_does_not_refuse(lab: Lab) -> None:
    """Hostile half: the lock guards its own arch only."""
    _hold_elsewhere("znver5")
    result = binhost("gc", "v3")
    assert result.exit_code == 0, result.output
    assert pool_files(lab.pkgdir()) == EVERY - PLANNED


def test_a_running_pull_refuses_and_moves_nothing(lab: Lab) -> None:
    """R3.8: a live pull of the arch is writing its binhost."""
    marker = ownership.locks_dir() / "v3.pull"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = binhost("gc", "v3")
    assert result.exit_code == 1
    assert str(os.getpid()) in result.output or "pull" in result.output
    assert pool_files(lab.pkgdir()) == EVERY
    assert FakeTools.calls("emaint") == []


def test_a_failed_move_stops_still_reindexes_lists_what_moved_and_names_the_file(
    lab: Lab,
) -> None:
    """R3.7. ``<quarantine>/dev-libs`` is a FILE: every dev-libs move fails, whoever runs
    it; app-arch's planned file can move. Nothing is lost: each planned file is in the
    pool or in the quarantine, the list names exactly what left, and the index no longer
    names it."""
    zz = [entry(f"app-arch/zz-1.{n}", 1, build_time=n + 1) for n in range(3)]
    lab.pool(*ALL, *zz)
    planned = PLANNED | {path_of(zz[0])}
    root = lab.cache / "quarantine" / "binpkgs" / "v3" / GEN
    root.mkdir(parents=True)
    (root / "dev-libs").write_text("in the way\n", encoding="utf-8")
    result = binhost("gc", "v3")
    assert result.exit_code == 1
    flat = _flat(result.output)
    assert any(Path(p).name in flat for p in PLANNED), result.output
    left = planned - pool_files(lab.pkgdir())
    assert left <= {path_of(zz[0])}  # no dev-libs file can have left
    assert listed(lab.pkgdir()) == left
    assert {p.name for p in root.rglob("*.gpkg.tar")} == {Path(p).name for p in left}
    assert len(FakeTools.calls("emaint")) == 1
    text = (lab.pkgdir() / "Packages").read_text(encoding="utf-8")
    assert not any(rel in text for rel in left)


def test_a_failed_reindex_exits_1_with_its_output(lab: Lab) -> None:
    FakeTools.env["FAKE_FAIL"] = "emaint"
    result = binhost("gc", "v3")
    assert result.exit_code == 1
    assert "fake index failure" in result.output


@pytest.mark.parametrize("where", ["climbing", "absolute"])
def test_an_unsafe_path_anywhere_in_the_plan_refuses_before_the_first_move(
    lab: Lab, where: str
) -> None:
    """R3.11 (an index is input). Hostile half: the unsafe PATH sorts in the MIDDLE of the
    plan (after app-misc/bar, among dev-libs/foo) -- a check made move by move would have
    moved bar first. Nothing moves, no list is written, nothing outside is touched."""
    outside = lab.root / "outside" / "mid-1.gpkg.tar"
    outside.parent.mkdir()
    outside.write_bytes(b"not the pool's\n")
    climbing = "dev-libs/foo/../../../../../../outside/mid-1.gpkg.tar"
    assert (lab.pkgdir() / climbing).resolve() == outside.resolve()
    unsafe = str(outside) if where == "absolute" else climbing
    if where == "climbing":  # sorts between bar and foo's own files
        assert path_of(BAR) < unsafe < path_of(FOO_10_1)
    lab.pool(*ALL, entry("dev-libs/foo-0.9", 1, path=unsafe, build_time=1))
    result = binhost("gc", "v3", "--keep", "0")
    assert result.exit_code == 1, result.output
    assert "mid-1.gpkg.tar" in _flat(result.output)
    assert pool_files(lab.pkgdir()) == EVERY
    assert quarantined(lab) == {}
    assert not (lab.pkgdir() / QUARANTINE).exists()
    assert outside.read_bytes() == b"not the pool's\n"
    assert FakeTools.calls("emaint") == []


@pytest.mark.parametrize("init", ["systemd", "openrc"])
def test_a_missing_toolbox_refuses_before_the_first_move_naming_how_to_build_it(
    lab: Lab, init: str
) -> None:
    """R3.12: without the toolbox the reindex cannot run, so nothing may move (an index
    naming moved files fails the next emerge)."""
    FakeTools.tarball.unlink()
    argv = ["gc", "v3"] if init == "systemd" else ["gc", "v3", "--init", init]
    result = binhost(*argv)
    assert result.exit_code == 1, result.output
    assert f"shidashi factory v3 toolbox {init}" in " ".join(result.output.split())
    assert pool_files(lab.pkgdir()) == EVERY
    assert quarantined(lab) == {}
    assert not (lab.pkgdir() / QUARANTINE).exists()


def test_gc_works_on_the_pkgdir_and_toolbox_of_its_init(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.8: openrc's PKGDIR (another generation here) and openrc's toolbox; systemd's
    pool is not touched."""
    per_init_generation(monkeypatch)
    other = lab.pool(*ALL, generation=OTHER_GEN)
    result = binhost("gc", "v3", "--init", "openrc")
    assert result.exit_code == 0, result.output
    assert pool_files(other) == EVERY - PLANNED
    assert pool_files(lab.pkgdir()) == EVERY
    assert listed(other) == PLANNED
    aside = lab.cache / "quarantine" / "binpkgs" / "v3" / OTHER_GEN
    assert {p.name for p in aside.rglob("*.gpkg.tar")} == {Path(p).name for p in PLANNED}
    assert FakeTools.resolved and {init for _arch, init in FakeTools.resolved} == {"openrc"}


def test_a_corrupt_owner_lock_refuses_with_its_message(lab: Lab) -> None:
    """CorruptLock is exit 1 with its message, never a traceback; nothing moves."""
    lock = ownership.locks_dir() / "v3.owner.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("{truncated", encoding="utf-8")
    result = binhost("gc", "v3")
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "shidashi worker unlock v3 --force" in " ".join(result.output.split())
    assert pool_files(lab.pkgdir()) == EVERY


@pytest.mark.parametrize("raised", ["owned-elsewhere", "lock-error"])
def test_a_lock_lost_between_the_check_and_the_hold_refuses_and_moves_nothing(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, raised: str
) -> None:
    """A worker takes the lock after the check: ``held``'s own refusal is exit 1 with its
    message too, before any move."""
    holder = ownership.Owner(
        arch="v3",
        worker="bentoo-lab",
        job="race-v3",
        commit="b" * 40,
        since="2026-10-20T10:00:00Z",
        host_pid=None,
    )
    error: Exception = (
        ownership.OwnedElsewhere(holder)
        if raised == "owned-elsewhere"
        else ownership.LockError(Path("/locks/v3.owner.json"), "root", "fix the locks dir")
    )

    def acquire(*_a: Any, **_k: Any) -> Any:
        raise error

    monkeypatch.setattr(ownership, "acquire", acquire)
    result = binhost("gc", "v3")
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert ("race-v3" if raised == "owned-elsewhere" else "fix the locks dir") in " ".join(
        result.output.split()
    )
    assert pool_files(lab.pkgdir()) == EVERY
    assert FakeTools.calls("emaint") == []


def test_a_damaged_quarantine_list_stops_the_gc_before_any_move(lab: Lab) -> None:
    """R7.3, R7.4: never replaced by a fresh list, nothing moved."""
    path = lab.pkgdir() / QUARANTINE
    path.write_text('{"version": 1, "paths": "x"}', encoding="utf-8")
    result = binhost("gc", "v3")
    assert result.exit_code == 1
    assert QUARANTINE in _flat(result.output)
    assert path.read_text(encoding="utf-8") == '{"version": 1, "paths": "x"}'
    assert pool_files(lab.pkgdir()) == EVERY


def test_gc_without_an_index_names_the_pkgdir_and_the_factory(lab: Lab) -> None:
    (lab.pkgdir() / "Packages").unlink()
    result = binhost("gc", "v3")
    assert result.exit_code == 1
    assert "shidashifactory" in _flat(result.output)
    assert pool_files(lab.pkgdir()) == EVERY


def test_a_negative_keep_is_refused(lab: Lab) -> None:
    result = binhost("gc", "v3", "--keep", "-1")
    assert result.exit_code != 0
    assert pool_files(lab.pkgdir()) == EVERY
    assert quarantined(lab) == {}
    assert "keep" in result.output.lower()


# --- task 3.3: a worker-owned arch ---------------------------------------------------------


@pytest.fixture
def owned(lab: Lab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """bentoo-lab owns v3's cache (story 028); ``worker.job`` is recorded, never run."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = config.workers_dir() / "workers.json"
    path.parent.mkdir(parents=True)
    workers.save_registry(
        path,
        {
            "bentoo-lab": WorkerEntry(
                name="bentoo-lab",
                address="192.0.2.10",
                host_key="ssh-ed25519 AAAA",
                host_key_fingerprint="SHA256:x",
                paired_at="2026-10-17T00:00:00Z",
                cache_archs=("v3",),
            )
        },
    )
    jobs: list[dict[str, Any]] = []

    def job(_remote: Any, entry: Any, name: str, args: Sequence[str], **kw: Any) -> Any:
        jobs.append({"worker": entry.name, "job": name, "args": list(args), **kw})
        code = 0 if "--dry-run" in args else 3
        return worker_mod.JobResult(
            exit_code=code, log=None, isos=(), run_ids=(), duration_s=0.0, followed=True
        )

    monkeypatch.setattr(worker_mod, "job", job)
    return jobs


def test_gc_of_a_worker_owned_arch_runs_as_a_job_on_that_worker(
    lab: Lab, owned: list[dict[str, Any]]
) -> None:
    """R3.10: the same gc, its options forwarded, on the owner; nothing here moves and no
    lock is taken on this host."""
    result = binhost("gc", "v3", "--keep", "3", "--dry-run")
    assert len(owned) == 1, (owned, result.output)
    assert result.exit_code == 0, result.output
    job = owned[0]
    assert job["worker"] == "bentoo-lab"
    args = job["args"]
    assert args[:3] == ["binhost", "gc", "v3"], args
    assert "--dry-run" in args
    assert "--keep" in args and "3" in args[args.index("--keep") :][:2], args
    assert pool_files(lab.pkgdir()) == EVERY
    assert FakeTools.calls("emaint") == []
    assert ownership.current("v3") is None


def test_the_worker_jobs_failure_is_the_commands_failure(
    lab: Lab, owned: list[dict[str, Any]]
) -> None:
    result = binhost("gc", "v3")
    assert len(owned) == 1, (owned, result.output)
    assert result.exit_code != 0
    assert pool_files(lab.pkgdir()) == EVERY


def test_an_arch_the_worker_does_not_own_is_collected_on_this_host_not_by_the_worker(
    lab: Lab, owned: list[dict[str, Any]]
) -> None:
    """Hostile half: owning v3 says nothing about znver5."""
    pkgdir = lab.pool(*ALL, arch="znver5")
    result = binhost("gc", "znver5")
    assert result.exit_code == 0, result.output
    assert owned == []
    assert pool_files(pkgdir) == EVERY - PLANNED
