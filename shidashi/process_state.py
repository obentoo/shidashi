"""Whether a process, or a process group, is still running -- a zombie is not.

``kill(pid, 0)`` succeeds on a zombie: a process that has ended and waits for its parent
to collect its exit status. Its parent is normally an init that does that at once, but
not everywhere: in the CI container ``act`` starts, PID 1 is ``tail -f /dev/null``, which
never reaps an orphan, so a QEMU or a guest command that did end stayed "alive" for good
and a stop waited for it until its timeout (2026-11-16, 11 tests). An ended process holds
no memory, no file and no port: it counts as gone here.
"""

import os
from pathlib import Path

#: The state letter ``/proc/<pid>/stat`` gives a process that ended and was not reaped.
_ZOMBIE = "Z"


def _stat_fields(pid: int) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the command name -- state first, then
    ppid and pgrp; ``None`` when there is no such process. I/O.

    The command name sits in parentheses and may hold spaces and ``)``: everything up
    to the LAST ``) `` is it."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _name, _sep, rest = text.rpartition(") ")
    return rest.split()


def process_running(pid: int) -> bool:
    """Whether process ``pid`` exists and has not ended (another user's included). I/O."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # it exists; whose it is does not matter
    fields = _stat_fields(pid)
    return fields is None or fields[0] != _ZOMBIE


def group_running(pgid: int) -> bool:
    """Whether process group ``pgid`` has a member that has not ended. I/O."""
    for stat in Path("/proc").glob("[0-9]*/stat"):
        fields = _stat_fields(int(stat.parent.name))
        if (
            fields is not None
            and len(fields) > 2
            and fields[2] == str(pgid)
            and fields[0] != _ZOMBIE
        ):
            return True
    return False
