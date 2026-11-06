"""The CI guest's pinned inputs and its cloud-init documents (story 034; R1, R3.2-R3.4).

Pure: reads two small files and renders text. ``load_pins`` validates ``seeds/ci-guest.toml``
and the runner pin shared with story 032 (``lab/vm/runner.env``); ``prepare_user_data``
renders the NoCloud ``user-data`` of the one prepare boot; ``prepared_key`` names the image
that boot produces, so any pin or document change prepares a new one (R1.3).

The guest scripts are rendered here, not shipped as files, because the prepared key must
change whenever one of them does: hashing the rendered user-data covers them all.
"""

import hashlib
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: R1.2's four rules. The package rule is Debian's own name grammar (Policy 5.6.1).
_URL = re.compile(r"https://[^\s]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*")
#: The same rules as lab/vm/guest-runner.sh, so both runners refuse the same pin.
_RUNNER_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

#: Names the guest and the worker agree on (tests/test_ci_contract.py checks both sides).
PREPARED_MARKER = "SHIDASHI-CI-PREPARED"
RESULT_PREFIX = "SHIDASHI-CI-RESULT="
JITCONFIG_CREDENTIAL = "shidashi.jitconfig"
REPO_CREDENTIAL = "shidashi.repo"

RUNNER_DIR = "/opt/actions-runner"
HOOK_PATH = "/usr/local/libexec/shidashi-job-started"
#: What the runner is told: it refuses a hook path not ending in .sh/.ps1/.js
#: (actions/runner HostContextExtension); the same script as HOOK_PATH.
HOOK_RUNNER_PATH = HOOK_PATH + ".sh"
#: Runner.Listener exits 100 + the job's TaskResult (Succeeded 0, Failed 2, ...); run.sh
#: turns that into 0 whatever the result (story 034, tech review of 1.2).
_LISTENER_SUCCEEDED = 100
CHECK_PATH = "/usr/local/libexec/shidashi-prepare-check"
RUNNER_UNIT = "shidashi-runner.service"
GROWROOT_UNIT = "shidashi-growroot.service"
HOSTKEYS_UNIT = "shidashi-hostkeys.service"
#: growpart lives in cloud-guest-utils; the growroot unit needs it on every job boot.
_GROWPART_PACKAGE = "cloud-guest-utils"
#: Where cloud-init's apt module writes ``apt: conf`` (cc_apt_configure, APT_CONFIG_FN).
_APT_CONF = "/etc/apt/apt.conf.d/94cloud-init-config"


class CiGuestSeedError(Exception):
    """A guest pin is missing or malformed (R1.2): names the file and the field."""

    def __init__(self, file: Path | str, field: str | None = None, reason: str = "") -> None:
        # one argument is a whole message (``CiGuestSeedError("<file>: <field> ...")``)
        super().__init__(str(file) if field is None else f"{file}: {field}: {reason}")
        self.file = file
        self.field = field


@dataclass(frozen=True)
class ImagePin:
    url: str
    sha256: str


@dataclass(frozen=True)
class AptPin:
    snapshot: str
    packages: tuple[str, ...]


@dataclass(frozen=True)
class GuestPins:
    image: ImagePin
    apt: AptPin
    runner_version: str
    runner_sha256: str


def _table(doc: dict[str, Any], name: str, file: Path) -> dict[str, Any]:
    value = doc.get(name)
    if not isinstance(value, dict):
        raise CiGuestSeedError(file, name, "missing table")
    return value


def _string(table: dict[str, Any], field: str, rule: re.Pattern[str], file: Path) -> str:
    value = table.get(field)
    if value is None:
        raise CiGuestSeedError(file, field, "missing")
    if not isinstance(value, str) or not rule.fullmatch(value):
        raise CiGuestSeedError(file, field, f"{value!r} does not match {rule.pattern}")
    return value


def _packages(table: dict[str, Any], file: Path) -> tuple[str, ...]:
    value = table.get("packages")
    if value is None:
        raise CiGuestSeedError(file, "packages", "missing")
    if not isinstance(value, list) or not value:
        raise CiGuestSeedError(file, "packages", "must be a non-empty list of package names")
    for name in value:
        if not isinstance(name, str) or not _PACKAGE.fullmatch(name):
            raise CiGuestSeedError(file, "packages", f"{name!r} is not a Debian package name")
    return tuple(value)


def _runner_env(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise CiGuestSeedError(path, "file", f"cannot read: {err}") from err
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            values[key.strip()] = value.strip()
    return values


def load_pins(seeds: Path, runner_env: Path) -> GuestPins:
    """The guest's pins (R1.1), refused whole when one is missing or malformed (R1.2)."""
    try:
        doc = tomllib.loads(seeds.read_text(encoding="utf-8"))
    except OSError as err:
        raise CiGuestSeedError(seeds, "file", f"cannot read: {err}") from err
    except tomllib.TOMLDecodeError as err:
        raise CiGuestSeedError(seeds, "file", f"not TOML: {err}") from err
    if "schema" not in doc:
        raise CiGuestSeedError(seeds, "schema", "missing")
    if doc["schema"] != 1 or isinstance(doc["schema"], bool):
        raise CiGuestSeedError(seeds, "schema", f"{doc['schema']!r} is not 1")
    image = _table(doc, "image", seeds)
    apt = _table(doc, "apt", seeds)
    env = _runner_env(runner_env)
    return GuestPins(
        image=ImagePin(
            url=_string(image, "url", _URL, seeds),
            sha256=_string(image, "sha256", _SHA256, seeds),
        ),
        apt=AptPin(
            snapshot=_string(apt, "snapshot", _SNAPSHOT, seeds),
            packages=_packages(apt, seeds),
        ),
        runner_version=_string(env, "RUNNER_VERSION", _RUNNER_VERSION, runner_env),
        runner_sha256=_string(env, "RUNNER_SHA256", _SHA256, runner_env),
    )


def runner_url(version: str) -> str:
    return (
        f"https://github.com/actions/runner/releases/download/v{version}/"
        f"actions-runner-linux-x64-{version}.tar.gz"
    )


# --- guest files -----------------------------------------------------------------------------

# ExecStart reads the credential into a variable and execs the runner's listener: the JIT
# config is never echoed, and never on the worker's command lines (R5.2; inside the guest it
# is the listener's argument by decision, story Constraints). The listener, not run.sh: with
# ACTIONS_RUNNER_RETURN_JOB_RESULT_FOR_HOSTED it exits 100 + the job's result, and run.sh
# maps every such code to 0. ExecStopPost runs with full privileges ("+") to reach the
# console and power the guest off, printing 0 only for 100 (Succeeded) and "none" when the
# listener reported no job.
_RUNNER_START = (
    "/bin/bash -c 'cfg=$$(cat \"$${CREDENTIALS_DIRECTORY}/"
    + JITCONFIG_CREDENTIAL
    + '"); exec ./bin/Runner.Listener run --jitconfig "$$cfg"\''
)
_RUNNER_STOP_POST = (
    '+/bin/sh -c \'case "$${EXIT_STATUS:-}" in '
    + f'{_LISTENER_SUCCEEDED}) rc=0;; ""|0) rc=none;; *) rc="$$EXIT_STATUS";; esac; '
    + 'echo "'
    + RESULT_PREFIX
    + "$$rc\" > /dev/console; systemctl --no-block poweroff'"
)
_RUNNER_SERVICE = f"""\
[Unit]
Description=Shidashi CI: one GitHub Actions job, then power off
Wants=network-online.target
Wants=docker.service
After=docker.service network-online.target {GROWROOT_UNIT}

[Service]
Type=simple
User=runner
WorkingDirectory={RUNNER_DIR}
ImportCredential=shidashi.*
Environment=ACTIONS_RUNNER_RETURN_JOB_RESULT_FOR_HOSTED=true
Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED={HOOK_RUNNER_PATH}
ExecStart={_RUNNER_START}
ExecStopPost={_RUNNER_STOP_POST}

[Install]
WantedBy=multi-user.target
"""

# growpart exits 1 for NOCHANGE (already at size) and 2 on failure; cloud-init, which grew
# the root at the prepare boot, is disabled on job boots (design.md, gotchas).
_GROWROOT_START = (
    "/bin/sh -c 'growpart /dev/vda 1; rc=$$?; "
    '[ "$$rc" -le 1 ] || exit "$$rc"; resize2fs /dev/vda1\''
)
_GROWROOT_SERVICE = f"""\
[Unit]
Description=Shidashi CI: grow the root filesystem to the job overlay's size
Before={RUNNER_UNIT}

[Service]
Type=oneshot
ExecStart={_GROWROOT_START}

[Install]
WantedBy=multi-user.target
"""

# The prepare boot removes the image's SSH host keys (R3.3); each job boot makes its own.
_HOSTKEYS_SERVICE = """\
[Unit]
Description=Shidashi CI: SSH host keys of this job's own
Before=ssh.service

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=multi-user.target
"""

# Compared whole and case-insensitively (GitHub owner and repository names are); `[ = ]`,
# never `[[ == ]]`, whose right side is a glob. One trailing newline of the credential is
# dropped, not every one: `$(cat)` would strip them all.
_JOB_STARTED_HOOK = f"""\
#!/bin/bash
# shidashi-job-started: ACTIONS_RUNNER_HOOK_JOB_STARTED. Refuses any job of a repository
# other than the one this runner was minted for (story 034, R5.5).
set -euo pipefail
cred="${{CREDENTIALS_DIRECTORY:-}}/{REPO_CREDENTIAL}"
if [ -z "${{CREDENTIALS_DIRECTORY:-}}" ] || [ ! -f "$cred" ]; then
  echo "shidashi-job-started: refusing the job: no {REPO_CREDENTIAL} credential" >&2
  exit 1
fi
want="$(cat -- "$cred"; printf x)"
want="${{want%x}}"
want="${{want%$'\\n'}}"
got="${{GITHUB_REPOSITORY-}}"
if [ -z "$want" ] || [ -z "$got" ] || [ "${{want,,}}" != "${{got,,}}" ]; then
  printf 'shidashi-job-started: refusing the job: GITHUB_REPOSITORY is "%s", minted for "%s"\\n' \\
    "$got" "$want" >&2
  exit 1
fi
"""


def _prepare_check(packages: tuple[str, ...]) -> str:
    """The proof the prepare boot worked: cloud-init's exit code is not (design.md, gotchas)."""
    names = " ".join(shlex.quote(p) for p in packages)
    return f"""\
#!/bin/bash
# shidashi-prepare-check: the prepared marker is printed only when this exits 0 (R3.4).
set -euo pipefail
fail() {{ echo "shidashi-prepare-check: $*" >&2; exit 1; }}
for p in {names}; do
  status="$(dpkg-query -W -f '${{Status}}' "$p" 2>/dev/null)" || fail "package $p is not known"
  [ "$status" = "install ok installed" ] || fail "package $p is '$status'"
done
command -v docker >/dev/null || fail "docker is not on PATH"
[ -f {RUNNER_DIR}/run.sh ] && [ -x {RUNNER_DIR}/run.sh ] ||
  fail "{RUNNER_DIR}/run.sh is not executable"
# one unit per call: is-enabled with several units exits 0 when any one is enabled
for unit in {RUNNER_UNIT} {GROWROOT_UNIT}; do
  systemctl is-enabled --quiet "$unit" || fail "$unit is not enabled"
done
"""


class _LiteralDumper(yaml.SafeDumper):
    """Multi-line strings as ``|`` blocks, so a guest script reads as itself in user-data."""


def _str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_LiteralDumper.add_representer(str, _str_presenter)


def _file(path: str, content: str, mode: str) -> dict[str, str]:
    return {"path": path, "content": content, "permissions": mode, "owner": "root:root"}


def prepare_user_data(pins: GuestPins) -> str:
    """The ``#cloud-config`` of the prepare boot (R3.2-R3.4, R4.1, R5.5)."""
    packages = pins.apt.packages
    if _GROWPART_PACKAGE not in packages:
        packages = (*packages, _GROWPART_PACKAGE)
    tarball = "/var/tmp/actions-runner.tar.gz"
    staging = f"{RUNNER_DIR}.staging"
    # one chain, and RUNNER_DIR appears only at its end: a run.sh exists -- which the
    # prepare check proves -- only when the user, its groups, the runner's digest, its
    # dependencies and its ownership all succeeded
    install_runner = " && ".join(
        [
            # the groups exist only once docker.io is installed, after the users module ran
            "usermod -aG docker,kvm runner",
            f"curl -fsSL --retry 3 -o {tarball} {shlex.quote(runner_url(pins.runner_version))}",
            f"echo {shlex.quote(f'{pins.runner_sha256}  {tarball}')} | sha256sum -c -",
            f"install -d -m 0755 {staging}",
            f"tar -xzf {tarball} -C {staging}",
            f"rm -f {tarball}",
            f"{staging}/bin/installdependencies.sh",
            f"chown -R runner:runner {staging}",
            f"mv {staging} {RUNNER_DIR}",
        ]
    )
    doc: dict[str, Any] = {
        # APT::Snapshot pins every snapshot-capable source (the Ubuntu archive) to one point
        # in time; the form is to be confirmed on 26.04 in the host proof (design.md, gotchas).
        "apt": {"conf": f'APT::Snapshot "{pins.apt.snapshot}";\n'},
        "package_update": True,
        "packages": list(packages),
        "users": [
            {
                "name": "runner",
                "groups": "docker, kvm",
                "shell": "/bin/bash",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "lock_passwd": True,
            }
        ],
        "write_files": [
            _file(f"/etc/systemd/system/{RUNNER_UNIT}", _RUNNER_SERVICE, "0644"),
            _file(f"/etc/systemd/system/{GROWROOT_UNIT}", _GROWROOT_SERVICE, "0644"),
            _file(f"/etc/systemd/system/{HOSTKEYS_UNIT}", _HOSTKEYS_SERVICE, "0644"),
            _file(HOOK_PATH, _JOB_STARTED_HOOK, "0755"),
            # the same script under the name the runner accepts (it wants a .sh)
            _file(HOOK_RUNNER_PATH, _JOB_STARTED_HOOK, "0755"),
            _file(CHECK_PATH, _prepare_check(packages), "0755"),
        ],
        "runcmd": [
            install_runner,
            f"systemctl enable {RUNNER_UNIT} {GROWROOT_UNIT} {HOSTKEYS_UNIT}",
            # the snapshot pins the PREPARE's packages; a job's own apt-get sees today's archive
            f"rm -f {_APT_CONF}",
            "cloud-init clean --machine-id --logs",
            "rm -f /etc/ssh/ssh_host_*",
            "touch /etc/cloud/cloud-init.disabled",
            "apt-get clean",
            # runcmd is one /bin/sh script without set -e: only the check's exit gates the marker
            f"{CHECK_PATH} && echo {PREPARED_MARKER} > /dev/console",
            "poweroff",
        ],
    }
    body = yaml.dump(doc, Dumper=_LiteralDumper, sort_keys=False, width=1000)
    return "#cloud-config\n" + body


def meta_data(instance_id: str) -> str:
    """NoCloud ``meta-data``: without an ``instance-id`` cloud-init may skip the boot."""
    return yaml.safe_dump({"instance-id": instance_id, "local-hostname": "shidashi-ci"})


def prepared_key(pins: GuestPins, user_data: str) -> str:
    """16 hex naming the prepared image (R1.3). NUL separators keep ``a+"2", "2.x"`` and
    ``a, "22.x"`` apart; the runner's sha256 is inside the user-data already."""
    digest = hashlib.sha256()
    for part in (pins.image.sha256, user_data, pins.runner_version):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]
