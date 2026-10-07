"""The per-arch owner lock (shidashi/ownership.py): one writer per arch's binhost.

Unit tests on a temporary cache, plus an Integration race between two real
processes (R5.6). The lock lives at ``<cache>/locks/<arch>.owner.json``.
"""

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from shidashi import ownership


@pytest.fixture(autouse=True)
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("SHIDASHI_CACHE", str(root))
    return root


def _owner(
    job: str = "fac-v3",
    *,
    arch: str = "v3",
    worker: str = "bentoo-lab",
    commit: str = "a" * 40,
    since: str = "2026-10-05T14:02:31Z",
) -> ownership.Owner:
    return ownership.Owner(
        arch=arch, worker=worker, job=job, commit=commit, since=since, host_pid=None
    )


# --- hostile halves first: what must stay apart, then what must come together ------


def test_a_lock_on_one_arch_never_refuses_another_arch() -> None:
    ownership.acquire("v3", _owner())
    ownership.require_free("znver5")
    assert ownership.acquire("znver5", _owner(arch="znver5")).arch == "znver5"


def test_require_free_refuses_the_same_worker_running_another_job_or_commit() -> None:
    ownership.acquire("v3", _owner("fac-v3"))
    with pytest.raises(ownership.OwnedElsewhere):
        ownership.require_free("v3", as_owner=_owner("fac-v3-again"))
    with pytest.raises(ownership.OwnedElsewhere):
        ownership.require_free("v3", as_owner=_owner("fac-v3", commit="b" * 40))


def test_require_free_accepts_the_holder_even_as_a_fresh_equal_object() -> None:
    ownership.acquire("v3", _owner())
    ownership.require_free("v3", as_owner=_owner())  # same values, another object
    held = ownership.current("v3")
    assert held is not None
    ownership.require_free("v3", as_owner=held)  # as read back from the file


def test_release_refuses_a_lock_held_by_someone_else() -> None:
    ownership.acquire("v3", _owner("fac-v3"))
    with pytest.raises(Exception):  # noqa: B017 - the contract names no class for this
        ownership.release("v3", expected=_owner("other-job"))
    assert ownership.current("v3") == _owner("fac-v3")


def test_release_by_the_holder_frees_the_arch() -> None:
    ownership.acquire("v3", _owner("fac-v3"))
    ownership.release("v3", expected=ownership.current("v3"))  # type: ignore[arg-type]
    assert ownership.current("v3") is None
    ownership.acquire("v3", _owner("next"))


# --- R5.1 / R5.2 ---------------------------------------------------------------------


def test_acquire_records_worker_job_commit_and_start_in_the_hosts_cache(cache: Path) -> None:
    owner = _owner()
    assert ownership.acquire("v3", owner) == owner
    path = cache / "locks" / "v3.owner.json"
    data = json.loads(path.read_text())
    assert data["worker"] == "bentoo-lab" and data["job"] == "fac-v3"
    assert data["commit"] == "a" * 40 and data["since"] == "2026-10-05T14:02:31Z"
    assert ownership.current("v3") == owner


def test_the_locks_dir_is_shared_by_root_and_the_user(cache: Path) -> None:
    ownership.acquire("v3", _owner())
    assert ownership.locks_dir() == cache / "locks"
    assert stat.S_IMODE((cache / "locks").stat().st_mode) == 0o2775
    assert stat.S_IMODE((cache / "locks" / "v3.owner.json").stat().st_mode) == 0o664


def test_a_second_writer_is_refused_naming_owner_job_since_and_release_command() -> None:
    ownership.acquire("v3", _owner("fac-v3"))
    with pytest.raises(ownership.OwnedElsewhere) as err:
        ownership.acquire("v3", _owner("fac-v3-b", worker="other-box"))
    text = str(err.value)
    for part in ("bentoo-lab", "fac-v3", "14:02", "shidashi worker unlock v3"):
        assert part in text
    assert ownership.current("v3") == _owner("fac-v3")


def test_require_free_without_an_owner_refuses_a_held_arch_and_passes_a_free_one() -> None:
    ownership.require_free("v3")
    ownership.acquire("v3", _owner())
    with pytest.raises(ownership.OwnedElsewhere, match="bentoo-lab"):
        ownership.require_free("v3")


def test_current_is_none_for_a_free_arch() -> None:
    assert ownership.current("v3") is None


def test_owner_is_frozen() -> None:
    owner = _owner()
    with pytest.raises(Exception):  # noqa: B017 - pydantic's ValidationError or TypeError
        owner.job = "changed"  # type: ignore[misc]


# --- R5.6: two real processes (Integration) ------------------------------------------

_RACER = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from shidashi import ownership
    me, sync, rounds = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    for r in range(rounds):
        (sync / f"ready-{r}-{me}").touch()
        while not (sync / f"go-{r}").exists():
            pass
        owner = ownership.Owner(arch="v3", worker=me, job=f"job-{me}", commit="c" * 40,
                                since="2026-10-05T14:02:31Z", host_pid=None)
        try:
            ownership.acquire("v3", owner)
            result = {"won": True}
        except ownership.OwnedElsewhere as exc:
            result = {"won": False, "message": str(exc)}
        except Exception as exc:
            result = {"won": False, "error": f"{type(exc).__name__}: {exc}"}
        (sync / f"result-{r}-{me}").write_text(json.dumps(result))
    """
)


def test_two_processes_racing_for_one_arch_exactly_one_takes_the_lock(
    cache: Path, tmp_path: Path
) -> None:
    rounds = 8
    sync = tmp_path / "sync"
    sync.mkdir()
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACER, me, str(sync), str(rounds)],
            env={**os.environ, "SHIDASHI_CACHE": str(cache)},
        )
        for me in ("alpha", "beta")
    ]
    try:
        for r in range(rounds):
            deadline = time.monotonic() + 60
            while not all((sync / f"ready-{r}-{m}").exists() for m in ("alpha", "beta")):
                assert time.monotonic() < deadline, "racers never got ready"
                time.sleep(0.005)
            (sync / f"go-{r}").touch()
            while not all((sync / f"result-{r}-{m}").exists() for m in ("alpha", "beta")):
                assert time.monotonic() < deadline, "racers never answered"
                time.sleep(0.005)
            results = {
                m: json.loads((sync / f"result-{r}-{m}").read_text()) for m in ("alpha", "beta")
            }
            winners = [m for m, res in results.items() if res["won"]]
            assert len(winners) == 1, (r, results)
            (loser,) = [m for m in results if m not in winners]
            # refused as a held lock, naming the winner -- never a half-written file
            assert "error" not in results[loser], results[loser]
            assert f"job-{winners[0]}" in results[loser]["message"]
            (cache / "locks" / "v3.owner.json").unlink()
    finally:
        for p in procs:
            p.wait(timeout=60)


# --- corrections at review (contract C5, R5.5, R5.7) ---------------------------------


@pytest.mark.parametrize(
    ("args", "writes"),
    [
        (["assemble", "v3", "kde", "systemd"], False),  # hostile: only reads the binhost
        (["pretend", "v3", "kde", "systemd"], False),  # hostile
        (["build", "v3", "systemd", "--images", "kde", "--skip-factory"], False),  # hostile
        (["doctor"], False),
        (["factory", "v3", "kde", "systemd"], True),
        (["build", "v3", "systemd", "--images", "kde"], True),
        (["build", "--jobs", "8", "v3", "systemd"], True),
    ],
)
def test_writes_pkgdir_names_exactly_the_binhost_writers(args: list[str], writes: bool) -> None:
    assert ownership.writes_pkgdir(args) is writes


def test_the_locks_dir_takes_the_caches_group(cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    ownership.acquire("v3", _owner())
    assert ownership.locks_dir().stat().st_gid == cache.stat().st_gid


def test_acquire_leaves_no_temporary_file_behind() -> None:
    ownership.acquire("v3", _owner())
    assert sorted(p.name for p in ownership.locks_dir().iterdir()) == ["v3.owner.json"]


def test_held_releases_the_lock_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), ownership.held("v3", _owner()):
        assert ownership.current("v3") is not None
        raise RuntimeError("the build failed")
    assert ownership.current("v3") is None


def test_holder_alive_by_pid_for_a_host_holder() -> None:
    alive = _owner(worker="host:bentoo").model_copy(update={"host_pid": os.getpid()})
    assert ownership.holder_alive(alive, probe=lambda _o: False) is True
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead = alive.model_copy(update={"host_pid": int(done.stdout)})
    assert ownership.holder_alive(dead, probe=lambda _o: True) is False


def test_holder_alive_asks_the_probe_for_a_worker_holder() -> None:
    asked: list[str] = []

    def probe(owner: ownership.Owner) -> bool:
        asked.append(owner.job)
        return True

    assert ownership.holder_alive(_owner("fac-v3"), probe=probe) is True
    assert asked == ["fac-v3"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_lock_that_cannot_be_created_raises_lock_error_naming_the_path(cache: Path) -> None:
    ownership.locks_dir()  # created
    locks = cache / "locks"
    locks.chmod(0o555)
    try:
        with pytest.raises(ownership.LockError) as err:
            ownership.acquire("v3", _owner())
    finally:
        locks.chmod(0o2775)
    assert str(locks) in str(err.value)


def test_holder_alive_counts_a_pid_it_cannot_signal_as_alive() -> None:
    other = _owner(worker="host:bentoo").model_copy(update={"host_pid": 1})  # EPERM for a user
    assert ownership.holder_alive(other, probe=lambda _o: False) is True


def test_acquire_refuses_an_owner_whose_arch_is_not_the_locks() -> None:
    with pytest.raises((ValueError, ownership.LockError)):
        ownership.acquire("znver5", _owner(arch="v3"))
    assert ownership.current("znver5") is None
