"""Unit tests of the factory builds keyed by the pins (story 016, task 3.4).

R8.2, R8.5, R8.7, R8.8, R8.12, R6.11, R6.12: both build paths derive the pin id
from ``seeds/`` before any work, write the bootstrap checkpoint and hand the
runners the current pin id, save it in every state, and restore nothing of
another pin id (or of the pre-fix key), which stays on disk untouched.

:func:`wire` runs ``Factory.build`` / ``build_stepwise`` unprivileged: the
container, the stage3, the bootstrap and the runners are faked, the pin files,
the fingerprint check and the state files are real. ``tests/test_factory_recheck.py``
reuses it.
"""

import dataclasses
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit, config, factory, isacheck, phases, state, tree
from shidashi.bootstrap import BootstrapResult
from shidashi.recipe import Phase, ResolvedRecipe
from shidashi.seed import Stage3Pointer

SNAP = "20260823T153057Z"
OLD = "p20260901.0a1b2c3d"
COMMIT = "e2891460957c2ef4175056790d10509a2611c36d"


def recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="kde",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(
            Phase(name="base", stage="base", emptytree=True),
            Phase(name="flavor", stage="kde", ships=True),
        ),
        portage_layers=(),
        stages=("base", "kde"),
    )


def pointer() -> Stage3Pointer:
    return Stage3Pointer(
        init="systemd",
        base_url="https://distfiles.gentoo.org/x",
        snapshot=SNAP,
        filename=f"stage3-amd64-nomultilib-systemd-{SNAP}.tar.xz",
        sha512="0" * 128,
    )


def write_seeds(seeds: Path, *, gentoo: str | None = None) -> Path:
    seeds.mkdir(parents=True, exist_ok=True)
    (seeds / "gentoo.toml").write_text(
        gentoo
        if gentoo is not None
        else f'date = "20260928"\nbase_url = "https://m/s"\nsha512 = "{"1" * 128}"\n',
        encoding="utf-8",
    )
    (seeds / "overlays.toml").write_text(
        f'[bentoo]\nurl = "https://github.com/obentoo/bentoo.git"\ncommit = "{COMMIT}"\n',
        encoding="utf-8",
    )
    return seeds


def current_pins(seeds: Path) -> str:
    return str(tree.pin_id(tree.load_tree_pin(seeds), tree.load_overlay_pins(seeds)))


def toolchain(rootfs: Path, gcc: str) -> None:
    """The vdb entries the fingerprint reads."""
    for cpv in (f"sys-devel/gcc-{gcc}", "sys-devel/binutils-2.46.1", "sys-libs/glibc-2.43-r4"):
        (rootfs / "var/db/pkg" / cpv).mkdir(parents=True, exist_ok=True)


class _Container:
    def __init__(self, rootfs: Path, **_k: Any) -> None:
        self.rootfs = rootfs

    def __enter__(self) -> _Container:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@dataclasses.dataclass
class Wired:
    pkgdir: Path
    rootfs: Path
    seeds: Path
    fork_points: Path
    seeded: list[str]
    snapshots: list[Path]
    runner_kwargs: dict[str, Any]


Runner = Callable[[Wired, dict[str, Any]], tuple[Any, ...]]


def wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, runner: Runner | None = None) -> Wired:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    seeds = write_seeds(tmp_path / "seeds")
    monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(seeds))
    r = recipe()
    w = Wired(
        pkgdir=tmp_path / "cache" / "binpkgs" / "v3" / SNAP,
        rootfs=config.build_root() / f"{r.arch}-{r.flavor}-{r.init}",
        seeds=seeds,
        fork_points=config.fork_points_dir(),
        seeded=[],
        snapshots=[],
        runner_kwargs={},
    )

    def fresh_seed(rootfs: Path, *_a: object, **_k: object) -> None:
        w.seeded.append("fresh")
        toolchain(rootfs, "16.2.0")

    def snapshot(_rootfs: Path, dest: Path) -> Path:
        w.snapshots.append(dest)
        return dest

    def run(*_a: Any, **kwargs: Any) -> tuple[Any, ...]:
        w.runner_kwargs.update(kwargs)
        return runner(w, kwargs) if runner is not None else ()

    monkeypatch.setattr(factory, "load_pointer", lambda init, *, seeds_dir: pointer())
    monkeypatch.setattr(factory, "pinned_repos", lambda **_k: {"gentoo": tmp_path / "tree"})
    monkeypatch.setattr(factory, "_fresh_seed", fresh_seed)
    monkeypatch.setattr(factory, "_prepare_portage", lambda *_a, **_k: None)
    monkeypatch.setattr(factory, "_build_binds", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(factory, "_ensure_bind_dirs", lambda *_a, **_k: None)
    monkeypatch.setattr(factory, "Container", _Container)
    monkeypatch.setattr(
        factory,
        "run_bootstrap",
        lambda *_a, **_k: BootstrapResult(
            steps=(), binutils="", gcc="", locales_before=0, locales_after=0, output=""
        ),
    )
    monkeypatch.setattr(factory, "snapshot_fork_point", snapshot)
    monkeypatch.setattr(factory, "run_phases", run)
    monkeypatch.setattr(factory, "run_phases_stepwise", run)
    monkeypatch.setattr(isacheck, "check_rootfs", lambda *_a, **_k: [])
    return w


def tarball(tmp_path: Path, dest: Path, marker: str) -> Path:
    tree_dir = tmp_path / f"tree-{dest.name}"
    (tree_dir / "etc").mkdir(parents=True)
    (tree_dir / "etc" / "marker").write_text(marker, encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    return phases.snapshot_fork_point(tree_dir, dest)


def _no_fresh_seed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seeded: list[str] = []
    monkeypatch.setattr(factory, "_fresh_seed", lambda *_a, **_k: seeded.append("fresh"))
    return seeded


NEW = "p20260928.3fa9c2d1"


# --- the key -----------------------------------------------------------------------------


def test_the_bootstrap_checkpoint_is_keyed_by_the_pins(tmp_path: Path) -> None:
    path = factory.bootstrap_fork_point_path(
        recipe(), snapshot=SNAP, pins=NEW, fork_points_dir=tmp_path
    )
    assert path == tmp_path / f"v3-systemd-{SNAP}-{NEW}-bootstrap.tar"


# --- _seed_or_restore (one-shot) -----------------------------------------------------------


def test_restore_points_of_an_older_pin_or_the_pre_fix_key_are_never_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeded = _no_fresh_seed(monkeypatch)
    fps = tmp_path / "fp"
    old = [
        tarball(tmp_path, fps / f"v3-systemd-{SNAP}-{OLD}-bootstrap.tar", "old pin"),
        tarball(tmp_path, fps / f"v3-systemd-{SNAP}-{OLD}-base.tar", "old pin"),
        tarball(tmp_path, fps / f"v3-systemd-{SNAP}-bootstrap.tar", "pre-fix"),
        tarball(tmp_path, fps / f"v3-systemd-{SNAP}-base.tar", "pre-fix"),
    ]
    before = {p: p.read_bytes() for p in old}
    resume, first_stage, reused, bootstrapped = factory._seed_or_restore(
        recipe(),
        tmp_path / "rootfs",
        pointer(),
        snapshot=SNAP,
        pins=NEW,
        fork_points_dir=fps,
        download=False,
    )
    assert (resume, reused, bootstrapped) == (None, False, False)
    assert seeded == ["fresh"]
    assert first_stage.name == f"v3-systemd-{SNAP}-{NEW}-base.tar"
    assert {p: p.read_bytes() for p in old} == before  # R8.8


def test_the_current_pins_checkpoint_wins_over_a_deeper_stage_of_an_older_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeded = _no_fresh_seed(monkeypatch)
    fps = tmp_path / "fp"
    tarball(tmp_path, fps / f"v3-systemd-{SNAP}-{OLD}-base.tar", "old pin base")
    tarball(tmp_path, fps / f"v3-systemd-{SNAP}-{NEW}-bootstrap.tar", "current bootstrap")
    rootfs = tmp_path / "rootfs"
    resume, _fp, reused, bootstrapped = factory._seed_or_restore(
        recipe(), rootfs, pointer(), snapshot=SNAP, pins=NEW, fork_points_dir=fps, download=False
    )
    assert (resume, reused, bootstrapped) == (None, False, True)
    assert seeded == []
    assert (rootfs / "etc/marker").read_text(encoding="utf-8") == "current bootstrap"


def test_the_current_pins_stage_fork_point_is_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R6.11: unchanged pins and snapshot restore as before."""
    _no_fresh_seed(monkeypatch)
    fps = tmp_path / "fp"
    tarball(tmp_path, fps / f"v3-systemd-{SNAP}-{NEW}-base.tar", "current base")
    rootfs = tmp_path / "rootfs"
    resume, _fp, reused, _b = factory._seed_or_restore(
        recipe(), rootfs, pointer(), snapshot=SNAP, pins=NEW, fork_points_dir=fps, download=False
    )
    assert (resume, reused) == ("base", True)
    assert (rootfs / "etc/marker").read_text(encoding="utf-8") == "current base"


# --- _seed_or_restore_stepwise, case (c) -----------------------------------------------------


def _stepwise_seed(tmp_path: Path, fps: Path) -> Path:
    path = tmp_path / "state.json"
    factory.Factory(recipe(), tmp_path / "pkgdir")._seed_or_restore_stepwise(
        recipe(),
        tmp_path / "rootfs",
        pointer(),
        snapshot=SNAP,
        pins=NEW,
        recipe_hash="H",
        fork_points_dir=fps,
        state_path=path,
        completed=(),
        seed_done=False,
        interactive=False,
        download=False,
        on_checkpoint=None,
    )
    return path


def test_stepwise_seeds_fresh_over_an_older_pins_checkpoint_and_saves_the_pins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeded = _no_fresh_seed(monkeypatch)
    fps = tmp_path / "fp"
    old = tarball(tmp_path, fps / f"v3-systemd-{SNAP}-{OLD}-bootstrap.tar", "old")
    saved = state.load_state(_stepwise_seed(tmp_path, fps))
    assert seeded == ["fresh"]
    assert saved is not None and saved.pins == NEW and saved.bootstrap_done is False
    assert old.is_file()


def test_stepwise_restores_the_current_pins_checkpoint_and_saves_the_pins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeded = _no_fresh_seed(monkeypatch)
    fps = tmp_path / "fp"
    tarball(tmp_path, fps / f"v3-systemd-{SNAP}-{NEW}-bootstrap.tar", "current")
    saved = state.load_state(_stepwise_seed(tmp_path, fps))
    assert seeded == []
    assert saved is not None and saved.pins == NEW and saved.bootstrap_done is True


# --- the builds: pins derived once, handed everywhere ----------------------------------------


def _step_end(trail: Any, name: str) -> dict[str, Any]:
    events = audit.read_events(trail.path / "events.jsonl")
    return next(e for e in events if e["kind"] == "step.end" and e["step"] == name)


def test_build_hands_the_pins_to_the_bootstrap_the_runner_and_the_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    w = wire(monkeypatch, tmp_path)
    pins = current_pins(w.seeds)
    with audit.run(tmp_path / "runs", command="factory", argv=[]) as trail:
        factory.Factory(recipe(), w.pkgdir).build(download=False)
    assert w.runner_kwargs["pins"] == pins
    assert w.snapshots == [w.fork_points / f"v3-systemd-{SNAP}-{pins}-bootstrap.tar"]
    assert _step_end(trail, "seed")["pins"] == pins


def test_build_stepwise_hands_the_pins_to_the_runner_and_the_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    w = wire(monkeypatch, tmp_path)
    pins = current_pins(w.seeds)
    factory.Factory(recipe(), w.pkgdir).build_stepwise(download=False)
    assert w.runner_kwargs["pins"] == pins
    assert w.snapshots == [w.fork_points / f"v3-systemd-{SNAP}-{pins}-bootstrap.tar"]
    saved = state.load_state(config.build_state_path(recipe()))
    assert saved is not None and saved.pins == pins


@pytest.mark.parametrize("saved_pins", [OLD, None])
def test_a_state_saved_under_another_pin_id_is_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, saved_pins: str | None
) -> None:
    """R8.7: another pin id, or a pre-fix state (no pins), refuses before any seed."""
    w = wire(monkeypatch, tmp_path)
    extra: dict[str, Any] = {} if saved_pins is None else {"pins": saved_pins}
    state.save_state(
        config.build_state_path(recipe()),
        state.BuildState(
            arch="v3",
            flavor="kde",
            init="systemd",
            snapshot=SNAP,
            recipe_hash=state.recipe_hash(recipe()),
            seed_done=True,
            bootstrap_done=True,
            completed_phases=("base",),
            **extra,
        ),
    )
    with pytest.raises(factory.StaleStateError):
        factory.Factory(recipe(), w.pkgdir).build_stepwise(download=False)
    assert w.seeded == []


def test_a_state_saved_under_the_same_pins_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R6.12: not stale; the run goes on to the runner after the completed phase."""
    w = wire(monkeypatch, tmp_path)
    toolchain(w.rootfs, "16.2.0")
    state.save_state(
        config.build_state_path(recipe()),
        state.BuildState(
            arch="v3",
            flavor="kde",
            init="systemd",
            snapshot=SNAP,
            recipe_hash=state.recipe_hash(recipe()),
            seed_done=True,
            bootstrap_done=True,
            completed_phases=("base",),
            pins=current_pins(w.seeds),
        ),
    )
    factory.Factory(recipe(), w.pkgdir).build_stepwise(download=False)
    assert w.runner_kwargs["completed"] == ("base",)


@pytest.mark.parametrize("method", ["build", "build_stepwise", "update"])
def test_an_unreadable_pin_file_fails_before_any_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str
) -> None:
    """R8.12."""
    w = wire(monkeypatch, tmp_path)
    write_seeds(w.seeds, gentoo="date = \n")
    fetched: list[str] = []
    monkeypatch.setattr(factory, "pinned_repos", lambda **_k: fetched.append("repos") or {})
    monkeypatch.setattr(factory, "_restore_into", lambda *_a: fetched.append("restore"))
    with pytest.raises(tree.TreeError) as err:
        getattr(factory.Factory(recipe(), w.pkgdir), method)(download=False)
    assert "gentoo.toml" in str(err.value)
    assert fetched == []  # not even the pinned repos were fetched
    assert w.seeded == [] and w.snapshots == [] and w.runner_kwargs == {}
    assert not config.build_state_path(recipe()).exists()
