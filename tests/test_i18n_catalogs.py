"""``po/`` against the code: in sync, complete, and every translation keeps what must not change.

When one of these fails after a message changed, run
``uv run --frozen python -m shidashi.i18n_catalog update`` and translate what it added.
"""

import collections
import gettext
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from shidashi import i18n, i18n_catalog, i18n_help, i18n_po

PO_DIR = i18n_catalog.REPO_DIR / "po"

# gettext's plural count per language; a header that says otherwise is a typo.
NPLURALS = {"es": 2, "pt_BR": 2, "ru": 3, "zh_CN": 1}

# A rich markup tag ("[bold red]", "[/bold red]", "[green]", "[/]") -- never a bracketed
# word such as "[OPTIONS]" or Typer's "[required]", which a translation may change.
_MARKUP_RE = re.compile(
    r"\[/?(?:(?:bold|dim|italic|underline|red|green|yellow|blue|cyan|magenta|white)"
    r"(?: [a-z ]+)?)?\]"
)
# An inline code span (``recipe``, `shidashi vm`): a command or a name, never translated.
_CODE_RE = re.compile(r"``[^`]+``|`[^`]+`")
# A command-line option: "--format", "--stop-after".
_OPTION_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def _fields(text: str) -> collections.Counter[str]:
    return collections.Counter(m.group(0) for m in i18n_po.FIELD_RE.finditer(text))


def _invariants(text: str) -> dict[str, collections.Counter[str]]:
    return {
        "placeholders": _fields(text),
        "markup tags": collections.Counter(_MARKUP_RE.findall(text)),
        "code spans": collections.Counter(_CODE_RE.findall(text)),
        "options": collections.Counter(_OPTION_RE.findall(text)),
    }


def _po(lang: str) -> tuple[str, list[i18n_po.Entry]]:
    return i18n_po.parse((PO_DIR / f"{lang}.po").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def template() -> list[i18n_po.Entry]:
    return i18n_po.parse((PO_DIR / f"{i18n.DOMAIN}.pot").read_text(encoding="utf-8"))[1]


def test_the_template_holds_exactly_the_messages_of_the_code(
    template: list[i18n_po.Entry],
) -> None:
    code = {(e.msgid, e.plural) for e in i18n_catalog.extract()}
    pot = {(e.msgid, e.plural) for e in template}
    assert sorted(code - pot) == [], "in the code, not in po/shidashi.pot: run update"
    assert sorted(pot - code) == [], "in po/shidashi.pot, gone from the code: run update"


def test_linguas_lists_every_po_file() -> None:
    langs = i18n_catalog.linguas(PO_DIR)
    assert sorted(langs) == sorted(NPLURALS)
    assert sorted(p.stem for p in PO_DIR.glob("*.po")) == sorted(langs)


def test_a_source_message_names_its_placeholders(template: list[i18n_po.Entry]) -> None:
    """``{}`` cannot be reordered by a translator; only Typer's own labels may use it."""
    framework = set(i18n_help.FRAMEWORK_LABELS.values())
    positional = [
        e.msgid
        for e in template
        if e.msgid not in framework
        and "{}" in [m.group(0) for m in i18n_po.FIELD_RE.finditer(e.msgid)]
    ]
    assert positional == []


@pytest.mark.parametrize("lang", sorted(NPLURALS))
def test_every_catalog_is_complete_and_in_sync(lang: str, template: list[i18n_po.Entry]) -> None:
    header, entries = _po(lang)
    assert i18n_po.header_value(header, "Language") == lang
    assert i18n_po.nplurals_of(header) == NPLURALS[lang]
    assert [(e.msgid, e.plural) for e in entries] == [(e.msgid, e.plural) for e in template]
    missing = [e.msgid for e in entries if not e.complete()]
    assert missing == [], f"po/{lang}.po: untranslated or fuzzy"
    for entry in entries:
        if entry.plural:
            assert len(entry.forms) == NPLURALS[lang], entry.msgid


@pytest.mark.parametrize("lang", sorted(NPLURALS))
def test_every_translation_keeps_placeholders_markup_code_and_options(lang: str) -> None:
    problems: list[str] = []
    for entry in _po(lang)[1]:
        want = _invariants(entry.msgid)
        if entry.plural:
            # each form stands for both msgids: it keeps what either of them has
            plural = _invariants(entry.plural)
            want = {kind: want[kind] | plural[kind] for kind in want}
        for form in entry.translations():
            got = _invariants(form)
            for kind, expected in want.items():
                if got[kind] != expected:
                    problems.append(
                        f"{entry.msgid!r}: {kind} {dict(expected)} became {dict(got[kind])}"
                    )
            if entry.msgid.endswith("\n") != form.endswith("\n"):
                problems.append(f"{entry.msgid!r}: the trailing newline differs")
    assert problems == []


@pytest.mark.parametrize(
    ("lang", "counts"),
    [
        ("es", {0: 1, 1: 0, 2: 1, 5: 1}),
        ("pt_BR", {0: 0, 1: 0, 2: 1, 5: 1}),
        ("ru", {1: 0, 2: 1, 4: 1, 5: 2, 11: 2, 21: 0, 22: 1, 25: 2}),
        ("zh_CN", {0: 0, 1: 0, 2: 0, 9: 0}),
    ],
)
def test_the_compiled_catalog_picks_the_plural_form_the_language_needs(
    lang: str, counts: dict[int, int], tmp_path: Path
) -> None:
    i18n_catalog.compile_catalogs(PO_DIR, tmp_path)
    with (tmp_path / lang / "LC_MESSAGES" / f"{i18n.DOMAIN}.mo").open("rb") as handle:
        catalog = gettext.GNUTranslations(handle)
    plurals = [e for e in _po(lang)[1] if e.plural]
    assert plurals, "the catalog has no plural message to check"
    for entry in plurals:
        for count, form in counts.items():
            assert catalog.ngettext(entry.msgid, entry.plural, count) == entry.forms[form]


@pytest.mark.skipif(shutil.which("msgfmt") is None, reason="GNU gettext's msgfmt not installed")
@pytest.mark.parametrize("lang", sorted(NPLURALS))
def test_msgfmt_accepts_every_catalog(lang: str) -> None:
    """``msgfmt --check`` is what the ebuild runs; it also checks python-brace-format."""
    done = subprocess.run(
        ["msgfmt", "--check", "-o", "/dev/null", str(PO_DIR / f"{lang}.po")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
