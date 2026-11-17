"""The worker's shared CPU and memory budget, as Shidashi's units ask for it.

Every consumer of a worker -- Shidashi's jobs, CI VMs and dev VMs, the ``lab`` tool and
Zeo's ``lab.sh`` -- reserves its share of one budget through ``contrib/worker/slot``
before it starts (the contract is that script's header). Before, each took a lock of
its own and none saw the others, so a Zeo build and a CI VM could starve each other.

Shidashi runs the script from the tree it shipped, so the script always matches the
code calling it; the script publishes itself as ``/mnt/work/slots/slot`` for the other
consumers. Everything here but :func:`worker_slot_fd` is pure: argv words, no I/O.
"""

import os
import re
from collections.abc import Mapping

#: Where the script sits inside a shipped tree.
SLOT_IN_TREE = "contrib/worker/slot"
#: A worker job builds with every core (MAKEOPTS follow the worker's threads) and the
#: largest packages (llvm, rust) peak well above 16 GiB.
JOB_MEMORY_GIB = 24
#: Wait for ever: a queued job or CI VM starts when the budget frees up.
WAIT_FOREVER = -1
#: A dev VM is interactive: refuse at once, with the holders named, instead of hanging.
WAIT_REFUSE = 0

_MEMORY = re.compile(r"(?i)^\s*(\d+)\s*(g|gb|gib|m|mb|mib|t|tb|tib)?\s*$")


def memory_gib(spec: str) -> int:
    """Whole GiB for a size as QEMU and Shidashi spell it (``24G``, ``8192M``, ``1T``),
    rounded up: a reservation must never be smaller than what runs. Pure."""
    match = _MEMORY.match(spec)
    if not match:
        raise ValueError(f"memory size {spec!r}: expected a number with G, M or T")
    value = int(match.group(1))
    unit = (match.group(2) or "g").lower()[0]
    if unit == "m":
        return max(1, -(-value // 1024))
    if unit == "t":
        return value * 1024
    return value


def slot_owner(kind: str, name: str) -> str:
    """``shidashi-<kind>:<name>``, within the script's owner charset. Pure."""
    owner = f"shidashi-{kind}:{name}"
    if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,120}", owner):
        raise ValueError(f"slot owner {owner!r} has characters the slot tool refuses")
    return owner


def slot_prefix(tree: str, owner: str, *, cpus: int | str, memory: int, wait_s: int) -> list[str]:
    """The words that run a command inside a reservation: ``bash <tree>/contrib/worker/
    slot run … --``, the command's own words to follow. ``cpus`` is a count, ``"all"``
    or ``"half"``. Pure."""
    if isinstance(cpus, int) and cpus < 1:
        raise ValueError(f"cpus must be positive, got {cpus}")
    if isinstance(cpus, str) and cpus not in ("all", "half"):
        raise ValueError(f"cpus must be a count, 'all' or 'half', got {cpus!r}")
    if memory < 1:
        raise ValueError(f"memory must be at least 1 GiB, got {memory}")
    return [
        "/bin/bash",
        f"{tree}/{SLOT_IN_TREE}",
        "run",
        "--owner",
        owner,
        "--cpus",
        str(cpus),
        "--memory",
        str(memory),
        "--wait",
        str(wait_s),
        "--",
    ]


def worker_slot_fd(env: Mapping[str, str]) -> int | None:
    """The reservation descriptor ``slot run`` left open for this process (``SLOT_FD``),
    or None: not under ``slot``, or the descriptor is not open here. A dev VM passes it
    to QEMU so the reservation lives as long as the VM. I/O: one ``fstat``."""
    raw = env.get("SLOT_FD", "")
    if not raw.isdigit():
        return None
    fd = int(raw)
    try:
        os.fstat(fd)
    except OSError:
        return None
    return fd
