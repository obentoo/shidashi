"""The pairing code prompt on a real terminal (story 009, found in task 6.3).

A hidden prompt that shows nothing left the person unsure the typing landed: on a
terminal ``_read_code`` echoes one ``*`` per character and lets Backspace correct, and
the code itself never reaches the screen. Driven through a pseudo-terminal, so the
termios path runs as it does for a person.

Requirements exercised: R1.6, R2.5.
"""

import os
import pty
import select
import signal
import sys
import time

import pytest

from shidashi import cli

CODE = "K7M4Q2XP"


def _drive(keys: bytes, *, early: bool = False) -> tuple[str, str]:
    """Run ``_read_code`` in a child on a pty, type ``keys``; (what it returned, screen).

    ``early`` types the keys right after the fork, before the prompt appears (a paste or
    a fast typist). A child still blocked at the deadline is killed, never waited on
    forever: a stuck prompt fails its test instead of hanging the suite."""
    read_end, write_end = os.pipe()
    pid, master = pty.fork()
    if pid == 0:  # the child: the pty is its terminal
        os.close(read_end)
        try:
            # pytest's capture objects came across the fork: talk to the pty itself
            sys.stdin = open(0, closefd=False)  # noqa: SIM115
            sys.stdout = open(1, "w", closefd=False)  # noqa: SIM115
            result = cli._read_code("Code")
            os.write(write_end, result.encode())
        finally:
            os._exit(0)
    os.close(write_end)
    screen = b""
    deadline = time.monotonic() + 5
    if early:
        os.write(master, keys)
    while b"Code:" not in screen and time.monotonic() < deadline:  # wait for the prompt
        if select.select([master], [], [], 0.1)[0]:
            screen += os.read(master, 1024)
    if not early:
        os.write(master, keys)
    while time.monotonic() < deadline:
        ready = select.select([master], [], [], 0.1)[0]
        if not ready:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
            continue
        try:
            chunk = os.read(master, 1024)
        except OSError:
            break
        if not chunk:
            break
        screen += chunk
    if not os.waitpid(pid, os.WNOHANG)[0]:
        os.kill(pid, signal.SIGKILL)  # still waiting for input: fail, never hang
        os.waitpid(pid, 0)
    returned = os.read(read_end, 64).decode()
    os.close(read_end)
    os.close(master)
    return returned, screen.decode(errors="replace")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pty and termios")
def test_the_code_prompt_shows_one_star_per_character_and_never_the_code() -> None:
    returned, screen = _drive(b"k7m4-q2xp\r")
    assert returned == "k7m4-q2xp"
    assert "*" * 9 in screen
    for secret in (CODE, "k7m4", "q2xp", "K7M4", "Q2XP"):
        assert secret not in screen, screen


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pty and termios")
def test_backspace_corrects_the_last_character() -> None:
    returned, screen = _drive(b"k7m4q2xQ\x7fp\r")
    assert returned == "k7m4q2xp"
    assert "\b \b" in screen


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pty and termios")
def test_keys_typed_before_the_prompt_appears_are_kept() -> None:
    """A code pasted (or typed fast) before the prompt shows must not be discarded."""
    returned, screen = _drive(b"k7m4-q2xp\r", early=True)
    assert returned == "k7m4-q2xp", screen
    assert "*" * 9 in screen


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pty and termios")
def test_a_prompt_that_never_gets_its_enter_is_killed_within_the_deadline() -> None:
    started = time.monotonic()
    returned, _screen = _drive(b"k7m4")  # no Enter: the child would wait forever
    assert returned == ""
    assert time.monotonic() - started < 7
