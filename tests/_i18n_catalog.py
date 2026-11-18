"""Build compiled catalogs for tests, so a test loads translations without msgfmt."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest
import typer.main

from shidashi import i18n, i18n_catalog, i18n_help, i18n_po

PLURAL_FORMS = {
    "es": "nplurals=2; plural=(n != 1);",
    "pt_BR": "nplurals=2; plural=(n > 1);",
    "ru": (
        "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
        "(n%100<10 || n%100>=20) ? 1 : 2);"
    ),
    "zh_CN": "nplurals=1; plural=0;",
}


def write_test_catalog(
    localedir: Path,
    lang: str,
    messages: Mapping[str, str],
    plurals: Mapping[tuple[str, str], list[str]] | None = None,
) -> Path:
    """Write ``<localedir>/<lang>/LC_MESSAGES/shidashi.mo`` holding ``messages`` and ``plurals``."""
    header = (
        f"Language: {lang}\nContent-Type: text/plain; charset=UTF-8\n"
        f"Plural-Forms: {PLURAL_FORMS.get(lang, PLURAL_FORMS['es'])}\n"
    )
    entries = [i18n_po.Entry(msgid=k, msgstr=v) for k, v in messages.items()]
    entries += [
        i18n_po.Entry(msgid=one, plural=many, forms=list(forms))
        for (one, many), forms in (plurals or {}).items()
    ]
    target = localedir / lang / "LC_MESSAGES" / f"{i18n.DOMAIN}.mo"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(i18n_po.compile_mo(header, entries))
    return localedir


def compile_repo_catalogs(out_dir: Path) -> Path:
    """Compile the repository's own ``po/*.po`` into ``out_dir``; return ``out_dir``."""
    i18n_catalog.compile_catalogs(i18n_catalog.REPO_DIR / "po", out_dir)
    return out_dir


def english_again() -> None:
    """Unload the catalog and put Typer's labels back to English."""
    from shidashi.cli import app

    i18n.reset()
    i18n_help.localize_command_tree(typer.main.get_command(app))


@contextmanager
def loaded_catalog(monkeypatch: pytest.MonkeyPatch, localedir: Path, locale: str) -> Iterator[str]:
    """Load ``locale``'s catalog from ``localedir`` as the entry point would; English after."""
    monkeypatch.setenv(i18n.LOCALEDIR_ENV, str(localedir))
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LC_ALL", locale)
    monkeypatch.setenv("LANG", locale)
    try:
        yield i18n.init()
    finally:
        english_again()
