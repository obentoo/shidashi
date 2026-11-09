"""The guest's console markers survive a login prompt on the line (story 034, 6.1).

Found in the host proof on 2026-11-08: a job GitHub concluded ``success`` was recorded
rc 1 because the serial console read ``shidashi-ci login: SHIDASHI-CI-RESULT=0``. The
serial getty leaves its prompt without a newline, the runner unit's ``ExecStopPost`` echo
landed after it, and the worker takes a marker only as a line of its own.

Contract: the guest prints each marker -- ``SHIDASHI-CI-RESULT=<rc>`` and the prepared
marker -- with one write that starts with a newline, so whatever sits unterminated on the
console line, the marker is a line of its own. The worker's reading stays strict.
"""

import contextlib
import errno
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._fake_ci_guest import (
    GuestSandbox,
    complete_guest,
    host_bin_without_fakes,
    marker_step,
    unit_settings,
    written_file,
)
from tests._fake_ci_toolbox import PREPARED_MARKER, RESULT_PREFIX
from tests._fake_worker import systemd_expand
from tests._pending import try_import
from tests.test_ci_guest_seed import pins_in

prepare_user_data: Any = try_import("shidashi.ci_guest_seed", "prepare_user_data")
console_value: Any = try_import("shidashi.ci_guest_vm", "console_value")
console_has: Any = try_import("shidashi.ci_guest_vm", "console_has")

UNIT = "/etc/systemd/system/shidashi-runner.service"
PROMPT = "shidashi-ci login: "


@pytest.fixture(scope="module")
def host_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return host_bin_without_fakes(tmp_path_factory.mktemp("guest-path"))


def _guest(tmp_path: Path, host_bin: Path) -> tuple[GuestSandbox, dict[str, Any]]:
    doc = yaml.safe_load(prepare_user_data(pins_in(tmp_path / "pins")))
    sandbox = complete_guest(tmp_path / "guest", host_bin, doc)
    sandbox.install()
    return sandbox, doc


@contextlib.contextmanager
def _serial(sandbox: GuestSandbox, serial: Path) -> Iterator[None]:
    """The sandbox's ``console`` as a FIFO: like a tty, a ``>`` onto it truncates nothing.
    What the guest wrote lands in ``serial`` after the getty's unterminated prompt."""
    fifo = sandbox.base / "console"
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)
    seen: list[bytes] = [PROMPT.encode()]
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            with fifo.open("rb") as stream:
                seen.append(stream.read())

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        yield
    finally:
        stop.set()
        # A blocking open("wb") hangs forever once the reader has seen `stop` and left its
        # loop: no reader will ever open the FIFO again (hung `act` under load, 2026-11-08).
        # A non-blocking writer wakes a reader blocked in open() and gets ENXIO when none is
        # there yet, so it retries until the reader is gone.
        deadline = time.monotonic() + 5
        while reader.is_alive() and time.monotonic() < deadline:
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as err:
                if err.errno != errno.ENXIO:
                    raise
            else:
                os.close(fd)
            reader.join(timeout=0.05)
        serial.write_bytes(b"".join(seen))


#: Some of systemd's specifiers (systemd.unit(5), "Specifiers"), as the guest's root sees
#: them: an Exec line's ``%s`` is the user's shell, never printf's ``%s``.
_SPECIFIERS = {"s": "/bin/bash", "u": "root", "h": "/root", "n": "shidashi-runner.service"}


def _specifiers(text: str) -> str:
    """What systemd makes of ``%`` in an Exec line: ``%%`` is ``%``, ``%x`` a specifier."""

    def one(match: re.Match[str]) -> str:
        key = match.group(1)
        return "%" if key == "%" else _SPECIFIERS.get(key, f"<unknown specifier %{key}>")

    return re.sub(r"%(.)", one, text)


def _stop_post(sandbox: GuestSandbox, doc: dict[str, Any], exit_status: str) -> None:
    settings = unit_settings(written_file(doc, UNIT))
    for line in settings["ExecStopPost"]:
        text = _specifiers(sandbox.sandboxed(line)).lstrip("-@:+!")
        words = systemd_expand(shlex.split(text), {})
        env = {**sandbox.env(), "EXIT_STATUS": exit_status}
        subprocess.run(words, env=env, cwd=sandbox.base, timeout=30, check=True)


@pytest.mark.parametrize(("exit_status", "value"), [("100", "0"), ("102", "102"), ("", "none")])
def test_the_result_is_a_line_of_its_own_after_a_login_prompt(
    tmp_path: Path, host_bin: Path, exit_status: str, value: str
) -> None:
    sandbox, doc = _guest(tmp_path, host_bin)
    serial = tmp_path / "serial.log"
    with _serial(sandbox, serial):
        _stop_post(sandbox, doc, exit_status)
    assert console_value(serial, RESULT_PREFIX) == value, serial.read_text()


def test_the_prepared_marker_is_a_line_of_its_own_after_a_login_prompt(
    tmp_path: Path, host_bin: Path
) -> None:
    sandbox, doc = _guest(tmp_path, host_bin)
    serial = tmp_path / "serial.log"
    with _serial(sandbox, serial):
        sandbox.put_check(doc)
        sandbox.run(["/bin/sh", "-c", sandbox.sandboxed(marker_step(doc))])
    assert console_has(serial, PREPARED_MARKER), serial.read_text()


def test_a_marker_after_a_prompt_on_the_same_line_is_still_not_read(tmp_path: Path) -> None:
    """The worker side stays strict: the fix is the guest's newline, not a looser reader."""
    serial = tmp_path / "serial.log"
    serial.write_text(f"{PROMPT}{RESULT_PREFIX}0\n{PROMPT}{PREPARED_MARKER}\n", encoding="utf-8")
    assert console_value(serial, RESULT_PREFIX) is None
    assert not console_has(serial, PREPARED_MARKER)
