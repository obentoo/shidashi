"""The host's record of a dev session on a build worker (shidashi/vm_dev_record.py).

``vm run`` and ``vm stop`` decide where to go by this file alone, and what it holds is
spliced into command lines sent to a worker: so its place, its shape, what it refuses
and what it never does to a session of this host are all pinned here.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shidashi import vm, vm_dev, vm_dev_record

_COMMIT = "5" * 40


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's state directory, empty; where the sessions of this host live too."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    return tmp_path / "state" / "shidashi" / "vm"


def _session(name: str = "dev", **changes: str | None) -> vm_dev_record.WorkerSession:
    fields: dict[str, str | None] = {
        "name": name,
        "worker": "bentoo-lab",
        "commit": _COMMIT,
        "started": "2026-11-16T00:10:00Z",
        **changes,
    }
    return vm_dev_record.WorkerSession(**fields)  # type: ignore[arg-type]


def test_the_record_is_worker_json_in_the_users_session_directory(state: Path) -> None:
    path = vm_dev_record.write_record(_session())
    assert path == state / "dev" / "worker.json" == vm_dev_record.record_path("dev")
    assert json.loads(path.read_text()) == {
        "name": "dev",
        "worker": "bentoo-lab",
        "commit": _COMMIT,
        "started": "2026-11-16T00:10:00Z",
        "expires": None,
    }
    assert sorted(p.name for p in path.parent.iterdir()) == ["worker.json"]  # no temp left


def test_a_record_reads_back_as_it_was_written_deadline_included(state: Path) -> None:
    ready = _session().ready("2026-11-16T12:10:23Z")
    vm_dev_record.write_record(ready)
    assert vm_dev_record.read_record("dev") == ready
    assert ready.expires == "2026-11-16T12:10:23Z" and _session().expires is None


def test_no_record_is_none_not_an_error(state: Path) -> None:
    assert vm_dev_record.read_record("dev") is None
    (state / "dev").mkdir(parents=True)  # a local session's directory, no record in it
    assert vm_dev_record.read_record("dev") is None


def test_a_record_is_never_a_local_session(state: Path) -> None:
    # vm.load_session must keep meaning "a VM on this host"
    vm_dev_record.write_record(_session())
    with pytest.raises(vm.VmError, match="no VM session named 'dev'"):
        vm.load_session("dev")


def test_the_sweep_of_dead_dev_sessions_leaves_a_record_alone(state: Path) -> None:
    path = vm_dev_record.write_record(_session())
    assert vm_dev.sweep_dead_dev_sessions(state, keep="other") == []
    assert path.is_file()


@pytest.mark.parametrize(
    "name", ["", ".", "..", "-x", ".hidden", "a/b", "a b", "a;b", "$(id)", "a\nb", "x" * 65, "é"]
)
def test_a_name_that_cannot_cross_a_shell_is_refused(name: str) -> None:
    reason = vm_dev_record.session_name_error(name)
    assert reason is not None and "A-Z a-z 0-9 . _ -" in reason


@pytest.mark.parametrize("name", ["dev", "bentoo-build-4242-1", "h-proof-a", "A.b_c-9", "x" * 64])
def test_the_names_bentoolkit_and_people_use_are_accepted(name: str) -> None:
    assert vm_dev_record.session_name_error(name) is None


def test_a_name_no_start_accepts_has_no_record_and_reads_no_file(state: Path) -> None:
    # "../x" must not be turned into a path and read
    (state / "x").mkdir(parents=True)
    (state / "x" / "worker.json").write_text(json.dumps(_session("x").__dict__))
    assert vm_dev_record.read_record("../vm/x") is None
    assert vm_dev_record.read_record("x") == _session("x")


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        (lambda d: d.pop("commit"), "'commit'"),
        (lambda d: d.update(commit="HEAD; rm -rf /"), "is no commit"),
        (lambda d: d.update(commit=""), "is no commit"),
        (lambda d: d.update(worker="lab; reboot"), "is no worker name"),
        (lambda d: d.update(worker="Bentoo_Lab"), "is no worker name"),
        (lambda d: d.update(worker=7), "strings"),
        (lambda d: d.update(name="other"), "it is the record of 'other'"),
        (lambda d: d.update(expires=12), "expires is a string or null"),
    ],
    ids=[
        "no-commit",
        "shell-commit",
        "empty-commit",
        "shell-worker",
        "bad-worker",
        "typed",
        "renamed",
        "expires",
    ],
)
def test_a_record_naming_what_no_start_wrote_is_an_error(
    state: Path, damage: Callable[[dict[str, Any]], object], said: str
) -> None:
    data = dict(_session().__dict__)
    damage(data)
    path = vm_dev_record.record_path("dev")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))
    with pytest.raises(vm.VmError) as refused:
        vm_dev_record.read_record("dev")
    assert said in str(refused.value) and str(path) in str(refused.value)


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "a b"},
        {"name": "../x"},
        {"worker": "lab; reboot"},
        {"commit": "$(id)"},
        {"commit": "HEAD"},
    ],
    ids=["name-space", "name-path", "worker", "commit-shell", "commit-ref"],
)
def test_a_session_that_could_not_cross_a_shell_cannot_be_made(fields: dict[str, str]) -> None:
    # whoever holds a WorkerSession splices it into command lines: it is safe by construction
    with pytest.raises(ValueError):
        _session(**{"name": "dev", **fields})


@pytest.mark.parametrize("text", ["", "{", "[]", '"dev"', '{"name": "dev"'])
def test_a_record_cut_short_is_an_error_never_no_record(state: Path, text: str) -> None:
    # a session on a worker must not turn into a local one because a file was truncated
    path = vm_dev_record.record_path("dev")
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(vm.VmError, match="does not read as the record"):
        vm_dev_record.read_record("dev")


def test_removing_the_record_removes_its_empty_directory(state: Path) -> None:
    vm_dev_record.write_record(_session())
    vm_dev_record.remove_record("dev")
    assert not (state / "dev").exists()
    vm_dev_record.remove_record("dev")  # again: nothing to do, no error


def test_removing_the_record_keeps_what_else_is_in_the_directory(state: Path) -> None:
    vm_dev_record.write_record(_session())
    (state / "dev" / "session.json").write_text("{}")  # a local session's own file
    vm_dev_record.remove_record("dev")
    assert sorted(p.name for p in (state / "dev").iterdir()) == ["session.json"]


def test_a_second_write_replaces_the_first_whole(state: Path) -> None:
    vm_dev_record.write_record(_session())
    vm_dev_record.write_record(_session(commit="6" * 40, worker="other-lab"))
    read = vm_dev_record.read_record("dev")
    assert read is not None and (read.worker, read.commit) == ("other-lab", "6" * 40)
