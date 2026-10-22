"""A worker job records the commit it shipped (story 030, task 8.4; R5.4).

Contract from design.md "``shidashi/release_cli.py``" (last bullet): a worker job sets
``SHIDASHI_SOURCE_COMMIT=<commit>`` on its unit (``worker.job_unit_argv``), and
``audit.repo_state`` returns that commit with ``dirty: false`` when ``git`` finds no
repository -- the job runs ``git archive HEAD``, which has no ``.git``. Only a 40-hex
commit is taken; inside a real checkout git wins. ``release.json`` and every audit run
read ``repo_state``, so both carry the commit.

``GIT_CEILING_DIRECTORIES`` stops git at the test's directory, so a checkout above
``/tmp`` can never be found by accident.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shidashi import audit, worker

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
ASSEMBLE = ["assemble", "v3", "kde", "systemd"]
VARIABLE = "SHIDASHI_SOURCE_COMMIT"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git on the host")


@pytest.fixture
def no_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shipped tree: the sources without ``.git``; git looks no higher than it."""
    tree = tmp_path / "src" / COMMIT
    tree.mkdir(parents=True)
    (tree / "pyproject.toml").write_text("[project]\nname = 'shidashi'\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return tree


def checkout(where: Path) -> str:
    """A real git checkout with one commit; its HEAD."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(where), *args], capture_output=True, text=True, check=True, env=env
        )
        return done.stdout.strip()

    where.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main")
    (where / "README").write_text("x\n", encoding="utf-8")
    git("add", "README")
    git("commit", "-q", "-m", "init")
    return git("rev-parse", "HEAD")


# --- the job's unit carries the commit -----------------------------------------------------


def test_the_job_unit_sets_the_shipped_commit_as_a_systemd_run_option() -> None:
    argv = worker.job_unit_argv("kde", COMMIT, ASSEMBLE)
    option = f"--setenv={VARIABLE}={COMMIT}"
    assert option in argv, argv
    # a systemd-run option, before the unit's command -- never a word of that command
    assert argv.index(option) < argv.index("/bin/bash"), argv


# --- hostile halves first: when the variable must NOT be taken ----------------------------


@needs_git
def test_a_real_checkout_ignores_the_variable_and_keeps_its_own_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git wins inside a checkout: the variable must not replace HEAD, nor hide that the
    tree is dirty."""
    repo = tmp_path / "repo"
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    head = checkout(repo)
    (repo / "README").write_text("changed\n", encoding="utf-8")
    monkeypatch.setenv(VARIABLE, OTHER)

    state = audit.repo_state(repo)

    assert state["commit"] == head != OTHER
    assert state["branch"] == "main"
    assert state["dirty"] is True
    assert state["dirty_files"] != []


@pytest.mark.parametrize(
    "value",
    [
        "31770bb",  # a short commit: not the full 40-hex object name
        "g" * 40,  # 40 characters, not hex
        COMMIT + "0",  # 41 hex
        f"{COMMIT}\n",  # a trailing newline
        "$(id)",
        "",
    ],
    ids=["short", "not-hex", "41-hex", "newline", "shell", "empty"],
)
def test_without_git_a_malformed_variable_is_ignored(
    no_repo: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(VARIABLE, value)
    state = audit.repo_state(no_repo)
    assert state["commit"] is None
    assert state["dirty"] is None


def test_without_git_and_without_the_variable_the_state_is_unknown_as_today(
    no_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)
    assert audit.repo_state(no_repo) == {
        "commit": None,
        "branch": None,
        "dirty": None,
        "dirty_files": [],
    }


# --- a worker job: no .git, the variable set -----------------------------------------------


def test_without_git_the_variable_is_the_commit_and_the_tree_is_clean(
    no_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VARIABLE, COMMIT)
    assert audit.repo_state(no_repo) == {
        "commit": COMMIT,
        "branch": None,
        "dirty": False,
        "dirty_files": [],
    }
