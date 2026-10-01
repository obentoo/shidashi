"""Guarded integration with the Gentoo host's Portage.

This module is Shidashi's *only* entry point to the ``portage`` module
(provided by the system via ``sys-apps/portage``, not by ``pip``). The import is
guarded: on a non-Gentoo host (CI, development laptop) the module loads
without raising and exposes ``PORTAGE_AVAILABLE = False`` (R7.1).

The :class:`PortageUnavailableError` exception is defined **here** — never in
:mod:`shidashi.recipe` — so that the recipe layer stays free of any
coupling to Portage (design §2, §9). The recipe engine and the CLI ``recipe``
subcommands neither import this module nor call :func:`require_portage`.

The read helpers (``portage_version``, ``configured_repos``) call
:func:`require_portage` before touching the ``portage`` module, guaranteeing an
actionable message when it is unavailable (R7.2).
"""

from types import ModuleType

try:
    # ``portage`` is provided by the system and has no type stubs; the ignore is
    # also marked ``unused-ignore`` so it stays valid in both
    # scenarios (Gentoo host, where the import resolves, and a host without Portage).
    import portage as _portage_mod  # type: ignore[import-not-found, unused-ignore]

    _portage: ModuleType | None = _portage_mod
    PORTAGE_AVAILABLE: bool = True
except ImportError:  # non-Gentoo host (CI, development laptop)
    _portage = None
    PORTAGE_AVAILABLE = False


class PortageUnavailableError(Exception):
    """Portage is not available on this host (R7.2).

    Raised by :func:`require_portage` (and, consequently, by any
    helper that depends on it) when ``import portage`` failed while loading
    the module — typically on a non-Gentoo host. The message is actionable,
    stating the requirement of a Gentoo host with ``sys-apps/portage``.
    """


def require_portage() -> ModuleType:
    """Return the ``portage`` module or raise :class:`PortageUnavailableError`.

    It is the single *gate* through which the rest of Shidashi accesses Portage. On a
    non-Gentoo host (``PORTAGE_AVAILABLE is False``) it raises
    :class:`PortageUnavailableError` with an actionable message (R7.2); otherwise
    it returns the already loaded ``portage`` module.
    """
    if not PORTAGE_AVAILABLE or _portage is None:
        raise PortageUnavailableError(
            "This operation requires a Gentoo host with sys-apps/portage."
        )
    return _portage


def portage_version() -> str:
    """Return the Portage version as a ``str`` (R7.2).

    Calls :func:`require_portage` first (hence raises
    :class:`PortageUnavailableError` on a non-Gentoo host). Reads the
    ``VERSION`` attribute of the ``portage`` module defensively — that real access only
    happens on a Gentoo host — and always returns a ``str``.
    """
    portage = require_portage()
    version = getattr(portage, "VERSION", "unknown")
    return str(version)


def configured_repos() -> tuple[str, ...]:
    """Return the names of the configured repositories as a tuple (R7.2).

    Calls :func:`require_portage` first (hence raises
    :class:`PortageUnavailableError` on a non-Gentoo host). Reads the global
    configuration (``portage.settings``) and from it the repository collection; stays
    defensive about the exact shape of the API — this path only runs on a Gentoo
    host — returning a sorted, stable tuple of names (``str``).
    """
    portage = require_portage()
    settings = getattr(portage, "settings", None)
    repositories = getattr(settings, "repositories", None)
    if repositories is None:
        return ()
    prepos = getattr(repositories, "prepos", repositories)
    return tuple(sorted(str(name) for name in prepos))
