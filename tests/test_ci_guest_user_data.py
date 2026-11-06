"""The prepare boot's cloud-init documents, the guest's units, hook and prepare check
(story 034, task 1.2; R3.2-R3.4, R4.1, R5.2, R5.5).

Contract (design.md, "shidashi/ci_guest_seed.py", "Guest files"; Implementation Gotchas):

- ``prepare_user_data(pins) -> str``: the ``#cloud-config`` of the prepare boot -- the apt
  snapshot, the pinned packages, user ``runner`` (groups ``docker`` and ``kvm``,
  passwordless sudo), the runner tarball checked against its sha256, the
  ``shidashi-runner.service`` unit (``ImportCredential=``, ``User=runner``, the job-started
  hook, ``ACTIONS_RUNNER_RETURN_JOB_RESULT_FOR_HOSTED=true``, the result line
  ``SHIDASHI-CI-RESULT=<rc>`` on the console), the hook ``shidashi-job-started``, the
  ``shidashi-growroot.service`` oneshot (``growpart /dev/vda 1`` -- its NOCHANGE exit 1 is
  no failure -- then ``resize2fs /dev/vda1``, before the runner: a job's root reaches the
  overlay's ``disk_gib``, R4.1; ``cloud-guest-utils`` installed for it), then the machine
  identity cleaned, cloud-init disabled, and the ``set -euo pipefail`` prepare check
  ``/usr/local/libexec/shidashi-prepare-check`` (every pinned package ``install ok
  installed``, ``docker`` on PATH, ``/opt/actions-runner/run.sh`` executable, both units
  enabled) -- the prepared marker reaches ``/dev/console`` only when it passes (R3.4:
  cloud-init runs ``runcmd:`` without ``set -e`` and goes on past a failed ``packages:``),
  then ``poweroff``.
- ``meta_data(instance_id) -> str``: NoCloud ``meta-data`` carrying the ``instance-id``.

The scripts run for real in a ``GuestSandbox`` (tests/_fake_ci_guest.py): the commands
they are checked against are fakes, never this host's.

Identity rules are written hostile half first: what must stay apart, then what must come
together, then the plain case.
"""

import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._fake_ci_guest import (
    CHECK,
    GROWROOT,
    GuestSandbox,
    complete_guest,
    host_bin_without_fakes,
    run_marker_step,
    run_unit,
    runcmd_lines,
    spoil_guest,
    unit_settings,
    written_file,
)
from tests._fake_ci_toolbox import PREPARED_MARKER, RESULT_PREFIX
from tests._pending import try_import
from tests.test_ci_guest_seed import PACKAGES, RUNNER_SHA, RUNNER_VERSION, SNAPSHOT, pins_in

prepare_user_data: Any = try_import("shidashi.ci_guest_seed", "prepare_user_data")
meta_data: Any = try_import("shidashi.ci_guest_seed", "meta_data")

HOOK = "/usr/local/libexec/shidashi-job-started"
UNIT = "/etc/systemd/system/shidashi-runner.service"


def cloud_config(pins: Any) -> tuple[str, dict[str, Any]]:
    text = prepare_user_data(pins)
    assert isinstance(text, str) and text.startswith("#cloud-config\n"), text[:80]
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "user-data is not a YAML mapping"
    return text, doc


@pytest.fixture(scope="module")
def host_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return host_bin_without_fakes(tmp_path_factory.mktemp("guest-path"))


def guest(tmp_path: Path, host_bin: Path, doc: dict[str, Any]) -> GuestSandbox:
    return complete_guest(tmp_path / "guest", host_bin, doc)


def test_user_data_installs_the_pinned_packages_from_the_pinned_snapshot(tmp_path: Path) -> None:
    pins = pins_in(tmp_path)
    text, doc = cloud_config(pins)
    assert set(PACKAGES) <= {str(p) for p in doc.get("packages") or []}
    assert SNAPSHOT in text, "the apt snapshot is not in the user-data"


def test_user_data_creates_the_runner_user_in_docker_and_kvm_with_passwordless_sudo(
    tmp_path: Path,
) -> None:
    """R3.2: a ``runner`` user in the ``docker`` and ``kvm`` groups."""
    _, doc = cloud_config(pins_in(tmp_path))
    users = [u for u in doc.get("users") or [] if isinstance(u, dict) and u.get("name") == "runner"]
    assert len(users) == 1, doc.get("users")
    groups = users[0].get("groups")
    names = set(groups.replace(" ", "").split(",") if isinstance(groups, str) else groups or [])
    assert {"docker", "kvm"} <= names
    sudo = users[0].get("sudo")
    assert "NOPASSWD:ALL" in (" ".join(sudo) if isinstance(sudo, list) else str(sudo))


def test_user_data_fetches_the_pinned_runner_and_checks_its_sha256(tmp_path: Path) -> None:
    """R3.2: the pinned runner, verified by its sha256 inside the guest."""
    text, _ = cloud_config(pins_in(tmp_path))
    url = (
        f"https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/"
        f"actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz"
    )
    assert url in text
    assert RUNNER_SHA in text
    assert "sha256sum" in text


def test_user_data_runner_unit_imports_both_credentials_and_reports_its_result(
    tmp_path: Path,
) -> None:
    """R5.2: the JIT config arrives as a systemd credential, never a file on disk or an
    argument from the host; the result goes to the console for the worker to read."""
    _, doc = cloud_config(pins_in(tmp_path))
    unit = written_file(doc, UNIT)
    settings = unit_settings(unit)
    imported = settings.get("ImportCredential", []) + [
        v.split(":", 1)[0] for v in settings.get("LoadCredential", [])
    ]
    for name in ("shidashi.jitconfig", "shidashi.repo"):
        assert any(fnmatch.fnmatchcase(name, glob) for glob in imported), (name, imported)
    assert settings.get("User") == ["runner"]
    assert any("docker.service" in v for v in settings.get("After", []))
    environment = " ".join(settings.get("Environment", []))
    assert "ACTIONS_RUNNER_RETURN_JOB_RESULT_FOR_HOSTED=true" in environment
    assert f"ACTIONS_RUNNER_HOOK_JOB_STARTED={HOOK}" in environment
    start = " ".join(settings.get("ExecStart", []))
    assert "shidashi.jitconfig" in start and "--jitconfig" in start
    assert "CREDENTIALS_DIRECTORY" in start
    assert RESULT_PREFIX in " ".join(settings.get("ExecStopPost", []))


def test_user_data_drops_the_machine_identity_before_the_prepare_check(tmp_path: Path) -> None:
    """R3.3: no machine-id or SSH host key survives into the prepared image; a job boot
    makes its own SSH host keys; cloud-init does not run again on a job boot. The check
    (and with it the marker) comes after all of it, then the power-off."""
    text, doc = cloud_config(pins_in(tmp_path))
    lines = runcmd_lines(doc)

    def first(pattern: str) -> int:
        found = [i for i, line in enumerate(lines) if re.search(pattern, line)]
        assert found, (pattern, lines)
        return found[0]

    clean = first(r"cloud-init\s+clean\b.*--machine-id")
    host_keys = first(r"/etc/ssh/ssh_host_")
    check = first(re.escape(CHECK))
    poweroff = max(i for i, line in enumerate(lines) if re.search(r"\bpoweroff\b", line))
    assert clean < check and host_keys < check, lines
    assert check <= poweroff, lines
    assert "/etc/cloud/cloud-init.disabled" in text
    assert "ssh-keygen -A" in text, "nothing regenerates SSH host keys on a job boot"
    assert PREPARED_MARKER in text


def test_user_data_adds_cloud_guest_utils_once(tmp_path: Path) -> None:
    """R4.1: ``growpart`` comes from ``cloud-guest-utils`` -- added when the pins lack it,
    never twice when they have it."""
    _, doc = cloud_config(pins_in(tmp_path / "without"))
    names = [str(p) for p in doc.get("packages") or []]
    assert names.count("cloud-guest-utils") == 1, names
    _, doc = cloud_config(pins_in(tmp_path / "with", packages=[*PACKAGES, "cloud-guest-utils"]))
    names = [str(p) for p in doc.get("packages") or []]
    assert names.count("cloud-guest-utils") == 1, names
    assert set(PACKAGES) <= set(names)


def test_prepare_check_is_a_strict_bash_script_that_passes_on_a_complete_guest(
    tmp_path: Path, host_bin: Path
) -> None:
    """R3.2, R3.4: ``set -euo pipefail``, and a guest with everything in place passes."""
    _, doc = cloud_config(pins_in(tmp_path / "pins"))
    check = written_file(doc, CHECK)
    lines = [line.strip() for line in check.splitlines() if line.strip()]
    assert re.fullmatch(r"#!\s*(/usr)?/bin/(env\s+)?bash", lines[0]), lines[:1]
    assert "set -euo pipefail" in lines[1:4], lines[:4]
    sandbox = guest(tmp_path, host_bin, doc)
    sandbox.install()
    done = sandbox.run([str(sandbox.put_check(doc))])
    assert done.returncode == 0, done
    queried = {w for call in sandbox.calls() if call[0] in ("dpkg-query", "dpkg") for w in call}
    assert set(PACKAGES) <= queried, ("pinned packages the check never asked dpkg about", queried)


@pytest.mark.parametrize(
    "spoil",
    [
        # dpkg-query exits 0 for a removed package that left its config files: its status,
        # not its exit code, says whether it is installed
        pytest.param({"status": (PACKAGES[0], "deinstall ok config-files")}, id="first-removed"),
        pytest.param({"status": (PACKAGES[-1], "install ok half-configured")}, id="last-half"),
        pytest.param({"status": (PACKAGES[2], None)}, id="unknown-to-dpkg"),
        pytest.param({"docker": False}, id="no-docker"),
        pytest.param({"runner_mode": 0o644}, id="runner-not-executable"),
        pytest.param({"runner_mode": None}, id="no-runner"),
        # systemctl is-enabled A B exits 0 when ONE of them is enabled
        pytest.param({"unit": ("shidashi-growroot.service", "disabled")}, id="growroot-disabled"),
        pytest.param({"unit": ("shidashi-runner.service", "disabled")}, id="runner-disabled"),
    ],
)
def test_prepare_check_fails_on_an_incomplete_guest(
    tmp_path: Path, host_bin: Path, spoil: dict[str, Any]
) -> None:
    """R3.4: whatever cloud-init left half-done, the check exits non-zero."""
    _, doc = cloud_config(pins_in(tmp_path / "pins"))
    sandbox = guest(tmp_path, host_bin, doc)
    spoil_guest(sandbox, spoil)
    sandbox.install()
    done = sandbox.run([str(sandbox.put_check(doc))])
    assert done.returncode != 0, (spoil, done)


@pytest.mark.parametrize(
    "spoil",
    [
        # hostile first: runcmd has no set -e -- a marker on a line of its own after the
        # check would be printed whatever the check said
        pytest.param({"status": (PACKAGES[0], "deinstall ok config-files")}, id="package-removed"),
        pytest.param({"unit": ("shidashi-growroot.service", "disabled")}, id="unit-disabled"),
        pytest.param({"docker": False}, id="no-docker"),
        pytest.param({}, id="complete"),
    ],
)
def test_prepare_check_gates_the_prepared_marker(
    tmp_path: Path, host_bin: Path, spoil: dict[str, Any]
) -> None:
    """R3.4: the marker reaches the console -- as a line of its own -- only when the check
    passes; a failed check prints it nowhere."""
    _, doc = cloud_config(pins_in(tmp_path / "pins"))
    sandbox = guest(tmp_path, host_bin, doc)
    spoil_guest(sandbox, spoil)
    console, stdout = run_marker_step(sandbox, doc)
    if spoil:
        assert PREPARED_MARKER not in console + stdout.splitlines(), (console, stdout)
    else:
        assert PREPARED_MARKER in console, (console, stdout)


@pytest.mark.parametrize(
    "growpart_rc", [pytest.param(1, id="nochange"), pytest.param(0, id="grown")]
)
def test_growroot_unit_grows_the_root_before_the_runner_starts(
    tmp_path: Path, host_bin: Path, growpart_rc: int
) -> None:
    """R4.1: a job's overlay is ``disk_gib`` but cloud-init, which grew the root at the
    prepare boot, is off on a job boot: this oneshot grows it, before the runner. Exit 1 of
    ``growpart`` is NOCHANGE -- the disk already at size -- and no failure."""
    _, doc = cloud_config(pins_in(tmp_path / "pins"))
    unit = written_file(doc, GROWROOT)
    settings = unit_settings(unit)
    assert settings.get("Type") == ["oneshot"], settings
    assert "shidashi-runner.service" in " ".join(settings.get("Before", [])).split(), settings
    sandbox = GuestSandbox(tmp_path / "guest", host_bin, packages=())
    sandbox.rcs["growpart"] = growpart_rc
    assert run_unit(sandbox, unit), (unit, sandbox.calls())
    calls = sandbox.calls()
    assert ["growpart", "/dev/vda", "1"] in calls, calls
    if growpart_rc == 0:
        assert ["resize2fs", "/dev/vda1"] in calls, calls


def test_user_data_meta_data_carries_the_instance_id(tmp_path: Path) -> None:
    one = yaml.safe_load(meta_data("prepare-0123456789abcdef"))
    two = yaml.safe_load(meta_data("prepare-fedcba9876543210"))
    assert isinstance(one, dict) and isinstance(two, dict), (one, two)
    assert one["instance-id"] == "prepare-0123456789abcdef"
    assert two["instance-id"] == "prepare-fedcba9876543210"


def _run_hook(tmp_path: Path, script: str, *, credential: str | None, repo: str | None) -> Any:
    hook = tmp_path / "shidashi-job-started"
    hook.write_text(script, encoding="utf-8")
    hook.chmod(0o755)
    creds = tmp_path / "credentials"
    creds.mkdir(exist_ok=True)
    (creds / "shidashi.repo").unlink(missing_ok=True)
    if credential is not None:
        (creds / "shidashi.repo").write_text(credential, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CREDENTIALS_DIRECTORY": str(creds)}
    if repo is not None:
        env["GITHUB_REPOSITORY"] = repo
    return subprocess.run(
        ["/bin/bash", str(hook)], env=env, capture_output=True, text=True, timeout=30, check=False
    )


@pytest.mark.parametrize(
    ("credential", "repo"),
    [
        # hostile collapse: near-identical names of ANOTHER repository
        pytest.param("obentoo/app", "obentoo/app-evil", id="longer"),
        pytest.param("obentoo/app", "obentoo/ap", id="shorter"),
        pytest.param("obentoo/app", "evil/obentoo/app", id="suffix"),
        pytest.param("obentoo/app", "obentoo/app/x", id="extra-segment"),
        pytest.param("obentoo/app", "obentoo/*", id="glob"),
        pytest.param("obentoo/app", "", id="empty"),
        pytest.param("obentoo/app", None, id="unset"),
        pytest.param(None, "obentoo/app", id="no-credential"),
    ],
)
def test_hook_refuses_a_job_of_another_repository(
    tmp_path: Path, credential: str | None, repo: str | None
) -> None:
    """R5.5: a job of any other repository fails before its first step, naming both."""
    _, doc = cloud_config(pins_in(tmp_path / "pins"))
    done = _run_hook(tmp_path, written_file(doc, HOOK), credential=credential, repo=repo)
    assert done.returncode != 0, done
    if credential and repo:
        assert credential in done.stderr and repo in done.stderr, done.stderr


@pytest.mark.parametrize(
    ("credential", "repo"),
    [
        # hostile split: the SAME repository, spelled differently, must not be refused
        pytest.param("obentoo/app\n", "obentoo/app", id="credential-newline"),
        # GitHub owner and repository names are case-insensitive: ci.toml may spell it
        # otherwise than GITHUB_REPOSITORY, which carries GitHub's own spelling
        pytest.param("Obentoo/App", "obentoo/app", id="case"),
        # the plain case
        pytest.param("obentoo/app", "obentoo/app", id="equal"),
    ],
)
def test_hook_accepts_a_job_of_the_minted_repository(
    tmp_path: Path, credential: str, repo: str
) -> None:
    _, doc = cloud_config(pins_in(tmp_path / "pins"))
    done = _run_hook(tmp_path, written_file(doc, HOOK), credential=credential, repo=repo)
    assert done.returncode == 0, done
