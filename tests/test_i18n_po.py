"""The stdlib .po/.pot/.mo tooling (shidashi.i18n_po)."""

import gettext
import io

import pytest

from shidashi import i18n_po

RU_HEADER = (
    "Language: ru\nContent-Type: text/plain; charset=UTF-8\n"
    "Plural-Forms: nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
    "(n%100<10 || n%100>=20) ? 1 : 2);\n"
)


def _entries() -> list[i18n_po.Entry]:
    return [
        i18n_po.Entry("plain", refs=["shidashi/cli.py"], msgstr="простой"),
        i18n_po.Entry(
            'two\nlines with "quotes"\tand a \\ backslash\n',
            refs=["shidashi/a.py", "shidashi/b.py"],
            msgstr="две\nстроки\n",
        ),
        i18n_po.Entry(
            "{n} file",
            plural="{n} files",
            refs=["shidashi/cli.py"],
            forms=["{n} файл", "{n} файла", "{n} файлов"],
            brace_format=True,
        ),
        i18n_po.Entry("not yet", refs=["shidashi/cli.py"], msgstr="ещё нет", fuzzy=True),
    ]


def test_render_then_parse_gives_the_entries_back() -> None:
    text = i18n_po.render(RU_HEADER, "Russian translation.", _entries(), 3)
    header, entries = i18n_po.parse(text)
    assert header == RU_HEADER
    assert entries == _entries()
    assert text.startswith("# Russian translation.\n")
    assert '#, python-brace-format\nmsgid "{n} file"' in text
    assert '#, fuzzy\nmsgid "not yet"' in text


def test_parse_drops_obsolete_entries_and_rejects_what_it_does_not_know() -> None:
    _, entries = i18n_po.parse('msgid "a"\nmsgstr "b"\n\n#~ msgid "old"\n#~ msgstr "velho"\n')
    assert [e.msgid for e in entries] == ["a"]
    with pytest.raises(ValueError, match="unsupported"):
        i18n_po.parse('msgctxt "menu"\nmsgid "a"\nmsgstr "b"\n')
    with pytest.raises(ValueError, match="out of order"):
        i18n_po.parse('msgid "a"\nmsgid_plural "as"\nmsgstr[1] "b"\n')
    with pytest.raises(ValueError, match="escape"):
        i18n_po.parse('msgid "a\\x41"\nmsgstr ""\n')


def test_merge_keeps_translations_adds_new_messages_and_drops_gone_ones() -> None:
    template = [
        i18n_po.Entry("plain", refs=["shidashi/new.py"]),
        i18n_po.Entry("{n} file", plural="{n} files", brace_format=True),
        i18n_po.Entry("brand new"),
    ]
    old = [*_entries(), i18n_po.Entry("gone", msgstr="ушло")]
    merged = i18n_po.merge(template, old, 3)
    assert [e.msgid for e in merged] == ["plain", "{n} file", "brand new"]
    assert merged[0].msgstr == "простой"
    assert merged[0].refs == ["shidashi/new.py"]
    assert merged[1].forms == ["{n} файл", "{n} файла", "{n} файлов"]
    assert merged[2].msgstr == ""
    assert not merged[2].complete()


def test_a_plural_whose_msgid_plural_changed_starts_over() -> None:
    template = [i18n_po.Entry("{n} file", plural="{n} files here")]
    merged = i18n_po.merge(template, _entries(), 3)
    assert merged[0].forms == ["", "", ""]


def test_a_compiled_catalog_reads_back_with_stdlib_gettext() -> None:
    catalog = gettext.GNUTranslations(io.BytesIO(i18n_po.compile_mo(RU_HEADER, _entries())))
    assert catalog.gettext("plain") == "простой"
    assert catalog.gettext('two\nlines with "quotes"\tand a \\ backslash\n') == "две\nстроки\n"
    assert [catalog.ngettext("{n} file", "{n} files", n) for n in (1, 3, 7)] == [
        "{n} файл",
        "{n} файла",
        "{n} файлов",
    ]
    # fuzzy and untranslated messages are left out, as msgfmt does
    assert catalog.gettext("not yet") == "not yet"


@pytest.mark.parametrize(
    ("header", "expected"),
    [(RU_HEADER, 3), ("Plural-Forms: nplurals=1; plural=0;\n", 1), ("Language: es\n", 2)],
)
def test_nplurals_of(header: str, expected: int) -> None:
    assert i18n_po.nplurals_of(header) == expected
