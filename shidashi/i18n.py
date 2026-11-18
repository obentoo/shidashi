"""Translate Shidashi's user-facing text with GNU gettext catalogs (stdlib ``gettext``).

Text is marked in the source in one of three ways, all read by
:mod:`shidashi.i18n_catalog` when it writes ``po/shidashi.pot``:

* ``i18n.t("...")`` translates where the text is shown;
* ``i18n.tn("one ...", "many ...", n)`` picks the plural form the language needs
  (Russian has three);
* ``i18n.mark("...")`` returns its argument: it marks text declared before the
  catalog is loaded, translated later with ``i18n.t(variable)``.

Typer's help (docstrings, ``help=``) is not marked at all: it is read from the built
Click tree by the extractor and translated in place by :mod:`shidashi.i18n_help`.

A placeholder is a named ``str.format`` field (``{name}``), so a translation can
reorder it; ``tests/test_i18n_catalogs.py`` checks every translation keeps the same
fields and the same rich markup tags.

Catalogs are compiled ``.mo`` files, ``<localedir>/<lang>/LC_MESSAGES/shidashi.mo``,
installed by the distribution: Gentoo installs only the languages in ``L10N``.
English is the msgid and the fallback.

Only the console entry point (:func:`shidashi.cli.main`) calls :func:`init`. Library
code, child processes started with ``app()`` (whose output a parent reads) and tests
never do, so they print English whatever the host's locale is.

What stays English on purpose (maintainer decision, as in bentoolkit 0.44): logs,
``--json``/``--format json`` and every other output a program parses, accepted
prompt answers (y/yes), and error and warning messages, so they stay searchable and
can be pasted into an issue.
"""

import gettext
import os
import sys
from collections.abc import Mapping
from pathlib import Path

DOMAIN = "shidashi"

# Overrides the catalog directory at run time, so a checkout's compiled catalogs
# (``python -m shidashi.i18n_catalog compile``) can be tried before they are installed.
LOCALEDIR_ENV = "SHIDASHI_LOCALEDIR"

_NULL = gettext.NullTranslations()
_active: gettext.NullTranslations = _NULL


def default_localedir() -> Path:
    """Where an installed Shidashi's catalogs live: ``<prefix>/share/locale``.

    ``sys.base_prefix`` is ``/usr`` (``${EPREFIX}/usr``) for a Portage install, the
    same place stdlib gettext looks by default.
    """
    return Path(sys.base_prefix) / "share" / "locale"


def mark(message: str) -> str:
    """Mark ``message`` for extraction and return it unchanged."""
    return message


def t(message: str) -> str:
    """Return the loaded catalog's translation of ``message``, or ``message`` itself."""
    return _active.gettext(message)


def tn(singular: str, plural: str, count: int) -> str:
    """Return the form of ``singular``/``plural`` the catalog's plural rule picks for ``count``.

    In English (no catalog): ``singular`` when ``count`` is 1, ``plural`` otherwise. The
    result is a format string; the caller fills its ``{n}`` (or other fields).
    """
    return _active.ngettext(singular, plural, count)


def _strip_locale(value: str) -> str:
    """Drop the codeset and modifier: ``pt_BR.UTF-8@euro`` -> ``pt_BR``."""
    for sep in ".@":
        value = value.split(sep, 1)[0]
    return value


def languages(environ: Mapping[str, str]) -> list[str]:
    """Return the catalog names to try, best first, the way GNU gettext chooses them.

    Stdlib :func:`gettext.find` reads the same variables but differs from GNU (and
    from bentoolkit, whose rules this copies) in two ways that matter here: a C or
    POSIX locale disables translation even when ``LANGUAGE`` is set, and an English
    entry in ``LANGUAGE`` ends the list -- English is the source text, so a user who
    ranks it above another language gets it. Each entry gives its full name, then
    its bare language (``pt_BR`` then ``pt``). Only letters and ``_`` are accepted, so
    a value from the environment can never climb out of the locale directory.
    """
    locale = ""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if environ.get(name):
            locale = environ[name]
            break
    if _strip_locale(locale) in ("", "C", "POSIX"):
        return []
    entries = [*environ.get("LANGUAGE", "").split(":"), locale]
    out: list[str] = []
    for entry in entries:
        full = _strip_locale(entry)
        if not full or not all(c.isascii() and (c.isalpha() or c == "_") for c in full):
            continue
        lang = full.split("_", 1)[0]
        if lang == "en":
            break
        for candidate in (full, lang):
            if candidate not in out:
                out.append(candidate)
    return out


def init(environ: Mapping[str, str] | None = None) -> str:
    """Load the catalog the environment selects; return its language, or ``""`` for English.

    ``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES`` and ``LANG`` choose it (:func:`languages`);
    ``SHIDASHI_LOCALEDIR`` overrides :func:`default_localedir`. No catalog for any of
    them, or a C/POSIX/English locale, leaves the text in English. A catalog that
    cannot be read raises :class:`OSError` (a corrupt one, :class:`ValueError` from
    gettext's parser) after resetting to English.
    """
    global _active
    env = os.environ if environ is None else environ
    _active = _NULL
    localedir = Path(env.get(LOCALEDIR_ENV) or default_localedir())
    for lang in languages(env):
        path = localedir / lang / "LC_MESSAGES" / f"{DOMAIN}.mo"
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            _active = gettext.GNUTranslations(handle)
        return lang
    return ""


def reset() -> None:
    """Unload the catalog: :func:`t` returns English again. Tests that call :func:`init` use it."""
    global _active
    _active = _NULL
