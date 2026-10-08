"""Regression (review of 2026-10-08): no unlock while a pull still writes the binhost.

``worker unlock`` without --force treated a worker job as ended once its ``.rc``
existed and its unit had stopped, but the host could still be pulling that job's
results into the arch's binhost -- an emaint, then gigabytes of rsync. Releasing
then let a host build write the same PKGDIR, and the pull's ``os.replace`` of the
index dropped the build's binpkgs from it; a ``sync pull`` racing the job's own pull
collided on the same temp index. A pull as the owner now marks itself.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from shidashi import cli, ownership, worker


@pytest.fixture(autouse=True)
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("SHIDASHI_CACHE", str(root))
    return root


def _dead_pid() -> int:
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(done.stdout)


def _holder() -> ownership.Owner:
    return ownership.Owner(
        arch="v3",
        worker="bentoo-lab",
        job="fac-v3",
        commit="c" * 40,
        since="2026-10-05T14:02:31Z",
        host_pid=None,
    )


def test_a_pull_marks_itself_for_the_block_and_removes_the_mark() -> None:
    assert ownership.pulling_pid("v3") is None
    with ownership.pulling("v3"):
        assert ownership.pulling_pid("v3") == os.getpid()
    assert ownership.pulling_pid("v3") is None
    assert not (ownership.locks_dir() / "v3.pull").exists()


def test_the_mark_is_removed_when_the_pull_fails() -> None:
    with pytest.raises(RuntimeError), ownership.pulling("v3"):
        raise RuntimeError("rsync failed")
    assert not (ownership.locks_dir() / "v3.pull").exists()


def test_a_second_pull_of_the_arch_is_refused_while_the_first_runs() -> None:
    with ownership.pulling("v3"):
        with pytest.raises(ownership.PullRunning) as caught, ownership.pulling("v3"):
            pass
        assert caught.value.pid == os.getpid()
        assert ownership.pulling_pid("v3") == os.getpid()  # the first one's mark stays


def test_another_arch_is_not_refused() -> None:
    with ownership.pulling("v3"), ownership.pulling("znver5"):
        assert ownership.pulling_pid("znver5") == os.getpid()


@pytest.mark.parametrize("content", ["{pid}\n", "not a pid\n", ""])
def test_a_dead_or_unreadable_mark_counts_as_none_and_is_replaced(content: str) -> None:
    mark = ownership.locks_dir() / "v3.pull"
    mark.write_text(content.format(pid=_dead_pid()), encoding="utf-8")
    assert ownership.pulling_pid("v3") is None
    with ownership.pulling("v3"):
        assert ownership.pulling_pid("v3") == os.getpid()


def test_unlock_refuses_while_the_holders_results_are_being_pulled() -> None:
    with ownership.pulling("v3"):
        reason = cli._unlock_refusal(_holder(), "v3")
    assert reason is not None
    assert "still being pulled" in reason
    assert str(os.getpid()) in reason
    assert "--force" in reason


def test_a_pull_as_the_owner_holds_the_mark_while_it_transfers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[int | None] = []

    def transfer(*_a: Any, **_k: Any) -> worker.PullResult:
        seen.append(ownership.pulling_pid("v3"))
        raise RuntimeError("stop here")

    monkeypatch.setattr(worker, "_pull_unmarked", transfer)
    with pytest.raises(RuntimeError):
        worker.pull(object(), "v3", "fac-v3", results=tmp_path, owner=_holder())  # type: ignore[arg-type]
    assert seen == [os.getpid()]
    assert ownership.pulling_pid("v3") is None


def test_a_pull_without_the_lock_does_not_mark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[int | None] = []

    def transfer(*_a: Any, **_k: Any) -> worker.PullResult:
        seen.append(ownership.pulling_pid("v3"))
        raise RuntimeError("stop here")

    monkeypatch.setattr(worker, "_pull_unmarked", transfer)
    with pytest.raises(RuntimeError):
        worker.pull(object(), "v3", "fac-v3", results=tmp_path)  # type: ignore[arg-type]
    assert seen == [None]
