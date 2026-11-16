"""Tests of shidashi.stop_signals -- SIGTERM and SIGHUP as an exit that unwinds.

The signals are real, sent to this process: pytest runs its tests on the main thread,
where the handlers install. What a command does with the unwinding is its own tests'
(tests/test_vm_exec.py, tests/test_cli_build_parallel_hardening.py).
"""

import os
import signal
import threading
from collections.abc import Callable, Generator

import pytest

from shidashi import stop_signals

_BOTH = (signal.SIGTERM, signal.SIGHUP)
_Handler = Callable[[int, object], None]


def _found(signum: int, frame: object) -> None:
    del signum, frame


@pytest.fixture
def handlers() -> Generator[_Handler]:
    """Both signals routed to a handler of this test's, the real ones back afterwards."""
    before = {sig: signal.signal(sig, _found) for sig in _BOTH}
    yield _found
    for sig, handler in before.items():
        signal.signal(sig, handler)


@pytest.mark.parametrize("sig", _BOTH, ids=["SIGTERM", "SIGHUP"])
def test_a_stop_unwinds_the_block_as_128_plus_the_signal(
    handlers: _Handler, sig: signal.Signals
) -> None:
    unwound: list[str] = []
    with pytest.raises(SystemExit) as stopped, stop_signals.unwinding_on_stop():
        try:
            os.kill(os.getpid(), sig)
            signal.pause()  # never reached: the handler ran when kill returned
        finally:
            unwound.append("finally")
    assert stopped.value.code == 128 + sig
    assert unwound == ["finally"]


def test_the_handlers_found_come_back_after_the_block_fired_or_not(handlers: _Handler) -> None:
    with stop_signals.unwinding_on_stop():
        assert [signal.getsignal(sig) for sig in _BOTH] == [stop_signals.exit_on_stop] * 2
    assert [signal.getsignal(sig) for sig in _BOTH] == [handlers, handlers]
    with pytest.raises(SystemExit), stop_signals.unwinding_on_stop():
        os.kill(os.getpid(), signal.SIGTERM)
    assert [signal.getsignal(sig) for sig in _BOTH] == [handlers, handlers]


def test_a_stop_fires_once_the_second_signal_is_ignored(handlers: _Handler) -> None:
    with pytest.raises(SystemExit) as stopped, stop_signals.unwinding_on_stop():
        try:
            os.kill(os.getpid(), signal.SIGHUP)
        finally:
            # the unwinding's own cleanup: neither signal may cut it short
            assert [signal.getsignal(sig) for sig in _BOTH] == [signal.SIG_IGN] * 2
            os.kill(os.getpid(), signal.SIGTERM)
    assert stopped.value.code == 128 + signal.SIGHUP


def test_a_signal_ignored_on_purpose_stays_ignored_when_asked(handlers: _Handler) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with stop_signals.unwinding_on_stop(keep_ignored=True):
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) is stop_signals.exit_on_stop
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    with stop_signals.unwinding_on_stop():
        assert signal.getsignal(signal.SIGHUP) is stop_signals.exit_on_stop
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN


def test_off_the_main_thread_nothing_is_installed(handlers: _Handler) -> None:
    # signal.signal raises ValueError there: the block must run, and change nothing
    seen: list[object] = []

    def block() -> None:
        with stop_signals.unwinding_on_stop():
            seen.extend(signal.getsignal(sig) for sig in _BOTH)
        stop_signals.stop_under_way()
        seen.extend(signal.getsignal(sig) for sig in _BOTH)

    thread = threading.Thread(target=block)
    thread.start()
    thread.join()
    assert seen == [handlers] * 4


def test_stop_under_way_ignores_both_until_the_block_restores_them(handlers: _Handler) -> None:
    with stop_signals.unwinding_on_stop():
        stop_signals.stop_under_way()
        assert [signal.getsignal(sig) for sig in _BOTH] == [signal.SIG_IGN] * 2
        os.kill(os.getpid(), signal.SIGTERM)  # ignored: the block goes on
    assert [signal.getsignal(sig) for sig in _BOTH] == [handlers, handlers]
