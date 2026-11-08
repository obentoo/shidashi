"""The guest's runner download and its API fallback (story 034, host proof 6.1; R3.2).

Contract: the prepare user-data writes ``/usr/local/libexec/shidashi-fetch-runner``, a
``set -euo pipefail`` bash script taking the tarball's path. It fetches the pinned
release URL (``github.com/actions/runner/releases/download/...``); when that fails it fetches
the pinned asset through the API (``api.github.com/repos/actions/runner/releases/assets/<id>``
with ``Accept: application/octet-stream``). Whatever served the bytes, ``sha256sum -c``
gates the exit status. The runcmd chain calls the script instead of an inline ``curl``.

Why: on 2026-11-08 every ``releases/download`` URL answered 504 for hours while the API's
asset endpoint served the same bytes, and the prepare boot failed without a runner.

The script runs for real with a fake ``curl`` first on PATH: it records its argv and, per
URL, writes the given bytes or exits 22 (curl's ``-f`` HTTP error).
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._fake_ci_guest import written_file
from tests._pending import try_import
from tests.test_ci_guest_seed import RUNNER_ASSET_ID, RUNNER_VERSION, pins_in

prepare_user_data: Any = try_import("shidashi.ci_guest_seed", "prepare_user_data")

FETCH = "/usr/local/libexec/shidashi-fetch-runner"
PAYLOAD = b"the runner tarball\n"
RELEASE_URL = (
    f"https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/"
    f"actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz"
)
API_URL = f"https://api.github.com/repos/actions/runner/releases/assets/{RUNNER_ASSET_ID}"

_FAKE_CURL = """#!{python} -I
import json, sys
argv = sys.argv[1:]
with open({log!r}, "a") as log:
    log.write(json.dumps(argv) + "\\n")
answers = json.load(open({answers!r}))
url = argv[-1]
out = argv[argv.index("-o") + 1]
if url not in answers:
    sys.exit(22)
open(out, "wb").write(answers[url].encode("latin-1"))
"""


def _doc(tmp_path: Path, payload: bytes = PAYLOAD) -> dict[str, Any]:
    pins = pins_in(tmp_path / "pins", runner_sha=hashlib.sha256(payload).hexdigest())
    doc = yaml.safe_load(prepare_user_data(pins))
    assert isinstance(doc, dict)
    return doc


def _run(tmp_path: Path, doc: dict[str, Any], answers: dict[str, bytes]) -> tuple[int, list[Any]]:
    bin_dir, log = tmp_path / "bin", tmp_path / "curl.log"
    bin_dir.mkdir()
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({u: b.decode("latin-1") for u, b in answers.items()}))
    curl = bin_dir / "curl"
    curl.write_text(
        _FAKE_CURL.format(python=sys.executable, log=str(log), answers=str(answers_file))
    )
    curl.chmod(0o755)
    script = tmp_path / "shidashi-fetch-runner"
    script.write_text(written_file(doc, FETCH))
    script.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    tarball = tmp_path / "actions-runner.tar.gz"
    done = subprocess.run(
        ["bash", str(script), str(tarball)], env=env, capture_output=True, timeout=30, check=False
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return done.returncode, calls


def test_fetch_script_is_strict_bash_and_runcmd_calls_it(tmp_path: Path) -> None:
    doc = _doc(tmp_path)
    text = written_file(doc, FETCH)
    assert text.startswith("#!/bin/bash\n"), text[:40]
    assert "set -euo pipefail" in text
    chain = " ".join(str(step) for step in doc.get("runcmd") or [])
    assert FETCH in chain
    assert "curl" not in chain, "the runner download lives in the fetch script, not runcmd"


def test_fetch_fails_when_both_sources_fail(tmp_path: Path) -> None:
    rc, calls = _run(tmp_path, _doc(tmp_path), {})
    assert rc != 0
    assert [c[-1] for c in calls] == [RELEASE_URL, API_URL]


def test_fetch_refuses_bytes_whose_sha256_is_not_the_pin(tmp_path: Path) -> None:
    rc, _ = _run(tmp_path, _doc(tmp_path), {API_URL: b"tampered"})
    assert rc != 0


def test_fetch_falls_back_to_the_api_asset_when_the_release_url_fails(tmp_path: Path) -> None:
    rc, calls = _run(tmp_path, _doc(tmp_path), {API_URL: PAYLOAD})
    assert rc == 0
    assert [c[-1] for c in calls] == [RELEASE_URL, API_URL]
    assert "Accept: application/octet-stream" in calls[1]


@pytest.mark.parametrize("extra", [{}, {API_URL: b"never asked"}])
def test_fetch_takes_the_release_url_alone_when_it_serves(
    tmp_path: Path, extra: dict[str, bytes]
) -> None:
    rc, calls = _run(tmp_path, _doc(tmp_path), {RELEASE_URL: PAYLOAD, **extra})
    assert rc == 0
    assert [c[-1] for c in calls] == [RELEASE_URL]
