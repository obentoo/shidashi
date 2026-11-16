"""SIGTERM and SIGHUP as an exit that unwinds -- for every command with something to undo.

The default action of both signals ends the process where it stands: no ``finally``
runs, no ``except`` either. A build then never records how it stopped, and a ``vm run``
leaves its command running in the guest. Routed here, the signal becomes
``SystemExit(128 + signum)`` raised on the main thread: the stack unwinds as it does on
Ctrl+C and the process ends with the status a shell reports for that signal (143, 129).

These lived in ``cli.py`` (``_stopped``, ``_unwinding_on_stop``, ``_stop_under_way``)
until ``vm run`` needed them too (2026-11-15): :mod:`shidashi.vm_exec` cannot import the
CLI. SIGTERM alone, kept ignored once it fired, is
:func:`shidashi.vmrunner.unwinding_sigterm`'s.
"""

import contextlib
import signal
import threading
from collections.abc import Generator

#: What a stop arrives as: ``kill``'s default, and a closed terminal or ssh session.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def exit_on_stop(signum: int, frame: object) -> None:
    """SIGTERM or SIGHUP as an exit that unwinds: every ``finally`` still runs and the
    audit run records its end. Fires once: both signals are ignored from here on, so a
    second one cannot cut the unwinding short."""
    del frame
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def unwinding_on_stop(*, keep_ignored: bool = False) -> Generator[None]:
    """The block with SIGTERM and SIGHUP routed to :func:`exit_on_stop`, the previous
    handlers restored on the way out. Handlers install on the main thread only.

    ``keep_ignored``: a signal ignored on purpose (``nohup``) stays ignored, as
    :func:`parallel.run_children` leaves it."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    wanted = [
        sig
        for sig in STOP_SIGNALS
        if not (keep_ignored and signal.getsignal(sig) is signal.SIG_IGN)
    ]
    previous = {sig: signal.signal(sig, exit_on_stop) for sig in wanted}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_under_way() -> None:
    """Ignore SIGTERM and SIGHUP from here on: a stop is being carried out or reported.

    A hangup is rarely one signal -- ``sudo`` relays the terminal's SIGHUP, then the
    hung-up pty sends its own (2026-10-17). :func:`parallel.run_children` caught the
    first and restored the previous handler as it returned; the next one must not end
    the build before its audit run records what stopped and how to resume it (R4.10).
    The caller's :func:`unwinding_on_stop` restores the handlers on the way out. A child
    started after this inherits both as ignored, so the stop's own helpers (the ssh
    that kills a guest command) outlive a second signal sent to the process group."""
    if threading.current_thread() is threading.main_thread():
        for sig in STOP_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
