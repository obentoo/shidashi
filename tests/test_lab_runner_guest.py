"""``lab/vm/guest-runner.sh`` -- one JIT runner in the builder guest (story 032, task 7.1;
R6.2, R6.3).

Contract (story.md R6.2, R6.3; design.md "lab/vm/builder.sh weekly + runner-hook.sh +
runner.env", "Implementation Gotchas"): ``lab/vm/runner.env`` pins ``RUNNER_VERSION`` and
``RUNNER_SHA256`` of ``actions-runner-linux-x64``; the guest downloads that tarball and
refuses -- exit 1 (the script, or the guest unit it starts), before running anything --
one whose SHA-256 is not the pinned one. The
runner starts with ``./run.sh --jitconfig <the config>`` read from ``/run/shidashi/jitconfig``
(tmpfs, 0600, written by the host over ssh stdin), never through ``config.sh``, as root
(``RUNNER_ALLOW_RUNASROOT`` set), with ``ACTIONS_RUNNER_HOOK_JOB_STARTED`` naming the
job-started hook. The config never lands under a shared directory (``/mnt``).

Runs the script as root in ``FakeBuilderGuest`` (tests/_fake_builder_guest.py) with
``FakeActionsRunner``'s tarball served in place of GitHub's. The config is both in
``/run/shidashi/jitconfig`` and on stdin, so either way of handing it over works.
"""

import re
import shlex
from pathlib import Path

import pytest

from tests._fake_builder_guest import (
    CHECKOUT,
    SANDBOX_REASON,
    FakeActionsRunner,
    FakeBuilderGuest,
    copy_checkout,
    guest_sandbox_works,
    pin_runner,
)
from tests._fake_github_api import mint_jit_config

RUNNER_ENV = CHECKOUT / "lab" / "vm" / "runner.env"


def runner_env_of(path: Path) -> dict[str, str]:
    assert path.is_file(), f"{path} is missing"
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.removeprefix("export ").partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def test_guest_runner_env_pins_a_runner_version_and_its_sha256() -> None:
    pins = runner_env_of(RUNNER_ENV)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.get("RUNNER_VERSION", "")), pins
    assert re.fullmatch(r"[0-9a-f]{64}", pins.get("RUNNER_SHA256", "")), pins


needs_guest = pytest.mark.skipif(not guest_sandbox_works(), reason=SANDBOX_REASON)


class GuestRun:
    def __init__(self, tmp_path: Path) -> None:
        self.checkout = copy_checkout(tmp_path / "checkout")
        self.runner = FakeActionsRunner(tmp_path / "runner")
        self.guest = FakeBuilderGuest(
            tmp_path / "guest",
            out=tmp_path / "out",
            cache=tmp_path / "cache",
            runner=self.runner,
        )
        self.out = tmp_path / "out"
        self.jit = mint_jit_config()
        self.script = self.checkout / "lab" / "vm" / "guest-runner.sh"

    def pin(self, sha256: str) -> None:
        pin_runner(self.checkout, version=self.runner.version, sha256=sha256)

    def start(self) -> tuple[int, str]:
        assert self.script.is_file(), f"{self.script} is missing"
        self.guest.provision_jitconfig(self.jit)
        done = self.guest.run(f"bash {shlex.quote(str(self.script))}", stdin=self.jit.encode())
        return done.returncode, done.stdout + done.stderr


@pytest.fixture
def guest_run(tmp_path: Path) -> GuestRun:
    return GuestRun(tmp_path)


def jitconfig_argument(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "--jitconfig" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--jitconfig="):
            return arg.split("=", 1)[1]
    return None


@needs_guest
def test_guest_runner_starts_one_jit_runner_as_root_with_the_hook_and_no_registration(
    guest_run: GuestRun,
) -> None:
    guest_run.pin(guest_run.runner.sha256)
    code, output = guest_run.start()
    assert code == 0, output
    runs = guest_run.runner.runs()
    assert len(runs) == 1, output
    run = runs[0]
    assert jitconfig_argument(run["argv"]) == guest_run.jit, run["argv"]
    assert guest_run.runner.config_calls() == [], "a JIT runner must not run config.sh"
    assert run["uid"] == 0
    assert run["env"].get("RUNNER_ALLOW_RUNASROOT"), "the runner refuses root without it"
    hook = run["env"].get("ACTIONS_RUNNER_HOOK_JOB_STARTED")
    if not hook and run["dotenv"]:
        found = re.search(r"^ACTIONS_RUNNER_HOOK_JOB_STARTED=(.+)$", run["dotenv"], re.M)
        hook = found.group(1).strip().strip("\"'") if found else None
    assert hook, "no job-started hook is set for the runner"
    assert Path(hook).name == "runner-hook.sh" and Path(hook).is_file(), hook
    urls = [d["url"] for d in guest_run.guest.downloads() if d["url"]]
    assert any(
        guest_run.runner.version in url and "actions-runner-linux-x64" in url for url in urls
    ), urls


@needs_guest
def test_guest_runner_never_writes_the_config_under_a_shared_directory(
    guest_run: GuestRun,
) -> None:
    guest_run.pin(guest_run.runner.sha256)
    code, output = guest_run.start()
    assert len(guest_run.runner.runs()) == 1, output
    for path in [*guest_run.guest.files_under("mnt"), *guest_run.out.rglob("*")]:
        if path.is_file():
            assert guest_run.jit not in path.read_text(errors="replace"), path
    assert guest_run.jit not in output


@needs_guest
@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param("other-bytes", id="another-tarballs-digest"),
        # Hostile halves: a digest one hex digit away, and an empty pin, must not pass.
        pytest.param("last-digit", id="one-digit-off"),
        pytest.param("empty", id="no-pin"),
    ],
)
def test_guest_runner_refuses_a_tarball_whose_sha256_is_not_the_pinned_one(
    guest_run: GuestRun, spoil: str
) -> None:
    real = guest_run.runner.sha256
    pinned = {
        "other-bytes": "0" * 64,
        "last-digit": real[:-1] + ("0" if real[-1] != "0" else "1"),
        "empty": "",
    }[spoil]
    guest_run.pin(pinned)
    code, output = guest_run.start()
    # design.md: "the guest unit exits 1" -- the script itself, or the unit it started
    units = guest_run.guest.unit_results()
    assert code == 1 or 1 in units.values(), (code, units, output)
    assert guest_run.runner.runs() == [], "the runner ran from an unverified tarball"
    assert guest_run.runner.config_calls() == []
    assert "sha256" in output.lower() or real in output, output
