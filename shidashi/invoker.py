"""Who ran the build when it runs under ``sudo``: the one place that reads ``SUDO_USER``.

Root's data home, keyring and gpg-agent are not the maintainer's, so the code that needs
the maintainer's (worker registry in ``schedule``, signing in ``publish_pool``) asks here
rather than each carrying its own copy of the rule.
"""

import os
import pwd
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InvokingUser:
    """The person behind ``sudo``: who a worker child or a signing agent runs as."""

    name: str
    uid: int
    gid: int
    home: Path
    data_home: Path


def invoking_user() -> InvokingUser | None:
    """``SUDO_USER`` when the build runs under ``sudo`` (effective uid 0 with ``SUDO_USER``
    set), else None. Their data home is ``$XDG_DATA_HOME`` when set, else their
    ``~/.local/share`` from ``pwd.getpwnam``. Raises :class:`LookupError` when
    ``SUDO_USER`` names no user of this system."""
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(user)
    except KeyError as err:
        raise LookupError(f"SUDO_USER={user!r} is not a user of this system") from err
    home = Path(entry.pw_dir)
    data_home = os.environ.get("XDG_DATA_HOME")
    return InvokingUser(
        name=user,
        uid=entry.pw_uid,
        gid=entry.pw_gid,
        home=home,
        data_home=Path(data_home) if data_home else home / ".local" / "share",
    )
