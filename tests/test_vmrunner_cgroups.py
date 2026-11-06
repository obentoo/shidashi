"""QEMU stays inside the calling unit's cgroup (story 034, task 2.4; R7.1, R7.2, R11.2).

Contract (design.md, Integration Points -> ``shidashi/vmrunner.py``; Implementation
Gotchas, rootful podman and ``--cgroups=split``):

- ``run_argv`` gains the keyword-only ``cgroups: str | None = None``. ``"split"`` adds
  exactly one ``--cgroups=split`` among podman's options (before the image): the container
  stays under the caller's cgroup, so the ``CPUWeight=``/``MemoryMax=`` of the
  ``shidashi-ci-<job>`` unit bind QEMU. Any other value is a ``ValueError``.
- The default leaves the command line byte-identical to story 024's: the boot test's
  podman command does not change (R11.2).
- The container is ``shidashi-vm-<name>``, as today: the CI guest's containers are
  ``shidashi-vm-ci-<job>`` and ``shidashi-vm-ci-prepare-<key>``, the names ``sweep`` probes.

New file on purpose: ``tests/test_vmrunner.py`` (story 024) stays as it is.
"""

from pathlib import Path
from typing import Any

import pytest

from shidashi import vmrunner

TAG = "localhost/shidashi-vm-runner:0123456789abcdef"
INNER = ["ci-guest", "exec", "qemu-system-x86_64", "-name", "shidashi-ci"]
W = Path("/w")

#: ``run_argv`` of story 024 for the arguments of :func:`argv` -- written out word by word,
#: so a change to the default command line shows here, not only in a comparison of two
#: calls of the new code.
STORY_024_ARGV = [
    "podman",
    "run",
    "--rm",
    "--pull=never",
    "--name",
    "shidashi-vm-ci-9001",
    "--network=host",
    "--device",
    "/dev/kvm",
    "--device",
    "/dev/vhost-vsock",
    "--security-opt",
    "seccomp=/w/scratch/runner-seccomp.json",
    "-v",
    "/w/venv:/w/venv:ro",
    "-v",
    "/w/src:/w/src:ro",
    "-v",
    "/w/ci-ro:/w/ci-ro:ro",
    "-v",
    "/w/scratch:/w/scratch",
    "-v",
    "/mnt/work/ci:/mnt/work/ci",
    "-e",
    "SHIDASHI_SCRATCH=/w/scratch",
    "-e",
    "PYTHONPATH=/w/src",
    "-e",
    "SHIDASHI_VARIANTS_DIR=/w/src/variants",
    "-w",
    "/w/src",
    TAG,
    "/w/venv/bin/python",
    "-c",
    "from shidashi.cli import app; app()",
    *INNER,
]


def argv(**extra: Any) -> list[str]:
    args: dict[str, Any] = {
        "name": "ci-9001",
        "inner_args": INNER,
        "venv": W / "venv",
        "tree": W / "src",
        "mounts_ro": [W / "ci-ro"],
        "scratch": W / "scratch",
        "mounts_rw": [Path("/mnt/work/ci")],
        "seccomp": W / "scratch" / "runner-seccomp.json",
    }
    args.update(extra)
    words = vmrunner.run_argv(TAG, **args)
    assert isinstance(words, list) and words, "run_argv returned no command line"
    return words


def cgroup_words(words: list[str]) -> list[str]:
    return [w for w in words if w.startswith(("--cgroups", "--cgroup-parent", "--cgroupns"))]


def test_cgroups_default_is_story_024s_command_line_word_for_word() -> None:
    """R11.2: no ``cgroups`` -- the boot test's caller -- changes nothing."""
    assert argv() == STORY_024_ARGV
    assert argv(cgroups=None) == STORY_024_ARGV


def test_cgroups_split_adds_exactly_one_option_before_the_image_and_nothing_else() -> None:
    """R7.1, R7.2: one ``--cgroups=split``, a podman option (before the image), and the
    rest of the command line unchanged."""
    words = argv(cgroups="split")
    assert cgroup_words(words) == ["--cgroups=split"], words
    assert words.index("--cgroups=split") < words.index(TAG)
    assert [w for w in words if w != "--cgroups=split"] == STORY_024_ARGV


def test_cgroups_split_names_the_container_shidashi_vm_name_once() -> None:
    """The names ``sweep`` probes: ``shidashi-vm-ci-<job>``, ``shidashi-vm-ci-prepare-<key>``."""
    for name, container in (
        ("ci-9001", "shidashi-vm-ci-9001"),
        ("ci-prepare-0123456789abcdef", "shidashi-vm-ci-prepare-0123456789abcdef"),
    ):
        words = argv(name=name, cgroups="split")
        names = [words[i + 1] for i, w in enumerate(words) if w == "--name"]
        names += [w.split("=", 1)[1] for w in words if w.startswith("--name=")]
        assert names == [container], words


@pytest.mark.parametrize(
    "value",
    [
        # hostile collapse: spellings near "split", and an empty string that must not pass
        # for the default
        pytest.param("", id="empty"),
        pytest.param("Split", id="case"),
        pytest.param("split ", id="trailing-space"),
        pytest.param("split,enabled", id="list"),
        # a value that would smuggle another podman option into the command line
        pytest.param("split --privileged", id="extra-option"),
        pytest.param("--privileged", id="option"),
        pytest.param("bogus", id="unknown"),
    ],
)
def test_cgroups_an_unknown_value_is_refused(value: str) -> None:
    assert argv(cgroups="split"), "run_argv returned no command line for split"
    with pytest.raises(ValueError):
        argv(cgroups=value)
