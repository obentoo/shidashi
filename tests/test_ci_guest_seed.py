"""The CI guest's pins and its prepared-image key (story 034, tasks 1.1 and 1.3; R1.1-R1.3).

Contract (design.md, "seeds/ci-guest.toml" and "shidashi/ci_guest_seed.py"):

- ``load_pins(seeds, runner_env) -> GuestPins`` reads ``[image] url, sha256`` and
  ``[apt] snapshot, packages`` (``schema = 1``) from the TOML file and ``RUNNER_VERSION``,
  ``RUNNER_SHA256`` from ``runner.env``. ``GuestPins`` mirrors those tables:
  ``pins.image.url``, ``pins.image.sha256``, ``pins.apt.snapshot``, ``pins.apt.packages``,
  ``pins.runner_version``, ``pins.runner_sha256``. A missing or malformed value raises
  ``CiGuestSeedError`` naming the file and the field (R1.2): url not ``https://``, a digest
  not 64 hex digits, a snapshot not ``YYYYMMDDTHHMMSSZ``, a package name outside
  ``[a-z0-9][a-z0-9+.-]*``.
- ``prepared_key(pins, user_data) -> str``: 16 hex; any pin or user-data change -> a new
  key, nothing changed -> the same key (R1.3).

The prepare boot's user-data, units, hook and check: tests/test_ci_guest_user_data.py.

Identity rules are written hostile half first: what must stay apart, then what must come
together, then the plain case.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import

load_pins: Any = try_import("shidashi.ci_guest_seed", "load_pins")
prepare_user_data: Any = try_import("shidashi.ci_guest_seed", "prepare_user_data")
prepared_key: Any = try_import("shidashi.ci_guest_seed", "prepared_key")
CiGuestSeedError: Any = try_import("shidashi.ci_guest_seed", "CiGuestSeedError")

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE_URL = (
    "https://cloud-images.ubuntu.com/releases/26.04/release-20261020/"
    "ubuntu-26.04-server-cloudimg-amd64.img"
)
IMAGE_SHA = "a1" * 32
SNAPSHOT = "20261020T000000Z"
PACKAGES = ["docker.io", "docker-buildx", "docker-compose-v2", "git", "qemu-system-x86"]
RUNNER_VERSION = "2.338.0"
RUNNER_SHA = "b2" * 32


def write_pins(
    directory: Path,
    *,
    schema: object = 1,
    url: object = IMAGE_URL,
    sha256: object = IMAGE_SHA,
    snapshot: object = SNAPSHOT,
    packages: object = PACKAGES,
    runner_version: str | None = RUNNER_VERSION,
    runner_sha: str | None = RUNNER_SHA,
    name: str = "ci-guest.toml",
) -> tuple[Path, Path]:
    """A ``ci-guest.toml`` and a ``runner.env`` in ``directory``; ``None`` omits a value."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [] if schema is None else [f"schema = {json.dumps(schema)}"]
    lines.append("[image]")
    lines += [] if url is None else [f"url = {json.dumps(url)}"]
    lines += [] if sha256 is None else [f"sha256 = {json.dumps(sha256)}"]
    lines.append("[apt]")
    lines += [] if snapshot is None else [f"snapshot = {json.dumps(snapshot)}"]
    lines += [] if packages is None else [f"packages = {json.dumps(packages)}"]
    seeds = directory / name
    seeds.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_lines = ["# runner.env as story 032 writes it", "RUNNER_GROUP_ID=3"]
    env_lines += [] if runner_version is None else [f"RUNNER_VERSION={runner_version}"]
    env_lines += [] if runner_sha is None else [f"RUNNER_SHA256={runner_sha}"]
    runner_env = directory / "runner.env"
    runner_env.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return seeds, runner_env


def pins_in(directory: Path, **changes: Any) -> Any:
    return load_pins(*write_pins(directory, **changes))


# --- 1.1 pins ------------------------------------------------------------------------------


def test_pins_load_every_field_from_the_two_files(tmp_path: Path) -> None:
    """R1.1: image URL and sha256, apt snapshot and packages, runner version and sha256."""
    pins = pins_in(tmp_path)
    assert pins.image.url == IMAGE_URL
    assert pins.image.sha256 == IMAGE_SHA
    assert pins.apt.snapshot == SNAPSHOT
    assert list(pins.apt.packages) == PACKAGES
    assert pins.runner_version == RUNNER_VERSION
    assert pins.runner_sha256 == RUNNER_SHA


def test_pins_of_the_repository_load_and_carry_docker_buildx_and_compose() -> None:
    """R1.1, R3.2: the shipped pin file and the shared runner pin of story 032 load; the
    guest gets Docker's engine, buildx and compose; the runner pin is runner.env's."""
    seeds = REPO_ROOT / "seeds" / "ci-guest.toml"
    runner_env = REPO_ROOT / "lab" / "vm" / "runner.env"
    pins = load_pins(seeds, runner_env)
    env = dict(
        line.split("=", 1)
        for line in runner_env.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert pins.runner_version == env["RUNNER_VERSION"]
    assert pins.runner_sha256 == env["RUNNER_SHA256"]
    assert pins.image.url.startswith("https://")
    assert re.fullmatch(r"[0-9a-f]{64}", pins.image.sha256)
    assert {"docker.io", "docker-buildx", "docker-compose-v2"} <= set(pins.apt.packages)


@pytest.mark.parametrize(
    ("changes", "field", "where"),
    [
        # hostile: values one character away from valid
        pytest.param(
            {"url": "http://cloud-images.ubuntu.com/x.img"}, "url", "seeds", id="url-http"
        ),
        pytest.param({"url": "file:///etc/passwd"}, "url", "seeds", id="url-file"),
        pytest.param({"sha256": "a1" * 31 + "a"}, "sha256", "seeds", id="sha-63"),
        pytest.param({"sha256": "a1" * 32 + "0"}, "sha256", "seeds", id="sha-65"),
        pytest.param({"sha256": "g1" * 32}, "sha256", "seeds", id="sha-not-hex"),
        pytest.param({"snapshot": "20261020"}, "snapshot", "seeds", id="snapshot-date"),
        pytest.param({"snapshot": "2026-10-20T00:00:00Z"}, "snapshot", "seeds", id="snapshot-iso"),
        pytest.param({"snapshot": "20261020T000000"}, "snapshot", "seeds", id="snapshot-no-z"),
        pytest.param({"snapshot": SNAPSHOT + "x"}, "snapshot", "seeds", id="snapshot-trailing"),
        pytest.param({"packages": ["docker.io;rm"]}, "packages", "seeds", id="pkg-semicolon"),
        pytest.param({"packages": ["Docker.io"]}, "packages", "seeds", id="pkg-upper"),
        pytest.param({"packages": ["-o"]}, "packages", "seeds", id="pkg-option"),
        pytest.param({"packages": [".docker"]}, "packages", "seeds", id="pkg-dot"),
        pytest.param({"packages": ["docker io"]}, "packages", "seeds", id="pkg-space"),
        pytest.param({"packages": ["docker.io\n"]}, "packages", "seeds", id="pkg-newline"),
        pytest.param({"packages": [""]}, "packages", "seeds", id="pkg-empty"),
        pytest.param({"packages": "docker.io"}, "packages", "seeds", id="pkg-not-list"),
        pytest.param({"schema": 2}, "schema", "seeds", id="schema-2"),
        # missing values
        pytest.param({"schema": None}, "schema", "seeds", id="schema-missing"),
        pytest.param({"url": None}, "url", "seeds", id="url-missing"),
        pytest.param({"sha256": None}, "sha256", "seeds", id="sha-missing"),
        pytest.param({"snapshot": None}, "snapshot", "seeds", id="snapshot-missing"),
        pytest.param({"packages": None}, "packages", "seeds", id="packages-missing"),
        pytest.param({"runner_version": "v2.338.0"}, "RUNNER_VERSION", "env", id="runner-v"),
        pytest.param({"runner_version": "2.338"}, "RUNNER_VERSION", "env", id="runner-xy"),
        pytest.param({"runner_version": "2.338.0-rc1"}, "RUNNER_VERSION", "env", id="runner-rc"),
        pytest.param({"runner_version": None}, "RUNNER_VERSION", "env", id="runner-missing"),
        pytest.param({"runner_sha": "b2" * 31}, "RUNNER_SHA256", "env", id="runner-sha-62"),
        pytest.param({"runner_sha": None}, "RUNNER_SHA256", "env", id="runner-sha-missing"),
    ],
)
def test_pins_a_malformed_or_missing_value_names_the_file_and_the_field(
    tmp_path: Path, changes: dict[str, Any], field: str, where: str
) -> None:
    """R1.2: refused before any download or boot, naming the file and the field."""
    seeds, runner_env = write_pins(tmp_path, **changes)
    with pytest.raises(CiGuestSeedError) as caught:
        load_pins(seeds, runner_env)
    message = str(caught.value)
    assert str(seeds if where == "seeds" else runner_env) in message, message
    assert field in message, message


@pytest.mark.parametrize("which", ["seeds", "env"])
def test_pins_a_missing_file_names_it(tmp_path: Path, which: str) -> None:
    seeds, runner_env = write_pins(tmp_path)
    gone = seeds if which == "seeds" else runner_env
    gone.unlink()
    with pytest.raises(CiGuestSeedError) as caught:
        load_pins(seeds, runner_env)
    assert str(gone) in str(caught.value)


def test_pins_a_file_that_is_not_toml_names_it(tmp_path: Path) -> None:
    seeds, runner_env = write_pins(tmp_path)
    seeds.write_text("schema = 1\n[image\nurl = \n", encoding="utf-8")
    with pytest.raises(CiGuestSeedError) as caught:
        load_pins(seeds, runner_env)
    assert str(seeds) in str(caught.value)


def test_pins_valid_package_names_at_the_edges_of_the_alphabet_load(tmp_path: Path) -> None:
    """Hostile split of R1.2: names Debian really uses -- ``+``, a leading digit, a dot --
    are valid and must not be refused with the bad ones."""
    edge = ["g++", "libstdc++6", "0ad", "python3.14", "docker-compose-v2"]
    pins = pins_in(tmp_path, packages=edge)
    assert list(pins.apt.packages) == edge


# --- 1.3 prepared key -----------------------------------------------------------------------


def test_key_is_16_hex_and_stable(tmp_path: Path) -> None:
    pins = pins_in(tmp_path)
    data = prepare_user_data(pins)
    key = prepared_key(pins, data)
    assert re.fullmatch(r"[0-9a-f]{16}", key), key
    assert prepared_key(pins, data) == key


def test_key_keeps_apart_inputs_whose_concatenation_is_equal(tmp_path: Path) -> None:
    """Hostile collapse of R1.3: user-data ``U+"2"`` with runner ``2.338.0`` and user-data
    ``U`` with runner ``22.338.0`` concatenate to the same text; they are two guests."""
    one = pins_in(tmp_path / "one", runner_version="2.338.0")
    two = pins_in(tmp_path / "two", runner_version="22.338.0")
    text = "#cloud-config\npackages: [git]\n# 2"
    assert prepared_key(one, text + "2") != prepared_key(two, text)
    assert re.fullmatch(r"[0-9a-f]{16}", prepared_key(one, text + "2"))


def test_key_of_equivalent_pin_files_is_the_same(tmp_path: Path) -> None:
    """Hostile split of R1.3: comments, blank lines and table order are not pins."""
    plain = pins_in(tmp_path / "plain")
    spelled = tmp_path / "spelled"
    _, runner_env = write_pins(spelled)
    seeds = spelled / "ci-guest.toml"
    seeds.write_text(
        "# the same pins, written otherwise\n\n"
        f"[apt]\npackages = {json.dumps(PACKAGES)}\nsnapshot = {json.dumps(SNAPSHOT)}\n\n"
        f"[image]\nsha256 = {json.dumps(IMAGE_SHA)}\nurl = {json.dumps(IMAGE_URL)}\n",
        encoding="utf-8",
    )
    # schema must stay first: a TOML key after a table belongs to that table
    seeds.write_text("schema = 1\n" + seeds.read_text(encoding="utf-8"), encoding="utf-8")
    other = load_pins(seeds, runner_env)
    key = prepared_key(plain, prepare_user_data(plain))
    assert re.fullmatch(r"[0-9a-f]{16}", key)
    assert prepared_key(other, prepare_user_data(other)) == key


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"sha256": "c3" * 32}, id="image-sha256"),
        pytest.param({"snapshot": "20261027T000000Z"}, id="apt-snapshot"),
        pytest.param({"packages": [*PACKAGES, "jq"]}, id="packages"),
        pytest.param({"runner_version": "2.339.0"}, id="runner-version"),
        pytest.param({"runner_sha": "d4" * 32}, id="runner-sha256"),
    ],
)
def test_key_changes_with_every_pin(tmp_path: Path, changes: dict[str, Any]) -> None:
    """R1.3: a bumped pin is a new prepared image (one re-prepare on the next job)."""
    base = pins_in(tmp_path / "base")
    bumped = pins_in(tmp_path / "bumped", **changes)
    key = prepared_key(base, prepare_user_data(base))
    assert re.fullmatch(r"[0-9a-f]{16}", key)
    assert prepared_key(bumped, prepare_user_data(bumped)) != key


def test_key_changes_with_the_user_data_alone(tmp_path: Path) -> None:
    pins = pins_in(tmp_path)
    data = prepare_user_data(pins)
    key = prepared_key(pins, data)
    assert re.fullmatch(r"[0-9a-f]{16}", key)
    assert prepared_key(pins, data + "# one more line\n") != key
