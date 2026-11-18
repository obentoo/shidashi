"""The runtime side of translation (shidashi.i18n): which catalog loads, and what it returns."""

from pathlib import Path

import pytest

from shidashi import i18n
from tests._i18n_catalog import loaded_catalog, write_test_catalog


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, []),
        ({"LANG": "C"}, []),
        ({"LANG": "POSIX"}, []),
        ({"LANG": "C.UTF-8"}, []),
        # GNU, not stdlib gettext.find: a C locale disables LANGUAGE too
        ({"LANG": "C", "LANGUAGE": "pt_BR"}, []),
        ({"LANG": "pt_BR.UTF-8"}, ["pt_BR", "pt"]),
        ({"LANG": "ru_RU.UTF-8@euro"}, ["ru_RU", "ru"]),
        ({"LANG": "es_ES.UTF-8", "LC_ALL": "zh_CN.UTF-8"}, ["zh_CN", "zh"]),
        ({"LANG": "es_ES.UTF-8", "LC_MESSAGES": "ru_RU.UTF-8"}, ["ru_RU", "ru"]),
        (
            {"LANG": "es_ES.UTF-8", "LANGUAGE": "pt_BR:ru"},
            ["pt_BR", "pt", "ru", "es_ES", "es"],
        ),
        # English is the source text: ranked first, it wins over every later entry
        ({"LANG": "pt_BR.UTF-8", "LANGUAGE": "en_US:pt_BR"}, []),
        ({"LANG": "en_US.UTF-8"}, []),
        ({"LANG": "es_ES.UTF-8", "LANGUAGE": "ru:en:pt_BR"}, ["ru"]),
        # a name that could climb out of the locale directory is skipped
        ({"LANG": "es_ES.UTF-8", "LANGUAGE": "../../etc:pt_BR"}, ["pt_BR", "pt", "es_ES", "es"]),
    ],
)
def test_languages_follow_gnu_gettext(environ: dict[str, str], expected: list[str]) -> None:
    assert i18n.languages(environ) == expected


def test_without_a_catalog_every_message_is_its_own_english() -> None:
    assert i18n.t("paired {name} at {address}") == "paired {name} at {address}"
    assert i18n.tn("{n} problem", "{n} problems", 1) == "{n} problem"
    assert i18n.tn("{n} problem", "{n} problems", 0) == "{n} problems"
    assert i18n.tn("{n} problem", "{n} problems", 2) == "{n} problems"
    assert i18n.mark("left as it is") == "left as it is"


def test_init_loads_the_catalog_of_the_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    localedir = write_test_catalog(tmp_path, "pt_BR", {"none": "nenhum"})
    with loaded_catalog(monkeypatch, localedir, "pt_BR.UTF-8") as lang:
        assert lang == "pt_BR"
        assert i18n.t("none") == "nenhum"
        # a message the catalog lacks falls back to English
        assert i18n.t("no answer") == "no answer"
    assert i18n.t("none") == "none"


def test_a_bare_language_catalog_serves_a_regional_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    localedir = write_test_catalog(tmp_path, "es", {"none": "ninguno"})
    with loaded_catalog(monkeypatch, localedir, "es_MX.UTF-8") as lang:
        assert lang == "es"
        assert i18n.t("none") == "ninguno"


def test_no_installed_catalog_stays_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with loaded_catalog(monkeypatch, tmp_path, "ru_RU.UTF-8") as lang:
        assert lang == ""
        assert i18n.t("none") == "none"


def test_a_c_locale_ignores_an_installed_catalog(tmp_path: Path) -> None:
    localedir = write_test_catalog(tmp_path, "pt_BR", {"none": "nenhum"})
    assert i18n.init({"LANG": "C", "LANGUAGE": "pt_BR", i18n.LOCALEDIR_ENV: str(localedir)}) == ""
    assert i18n.t("none") == "none"


@pytest.mark.parametrize(
    ("count", "form"),
    [(1, 0), (21, 0), (101, 0), (2, 1), (4, 1), (22, 1), (5, 2), (11, 2), (12, 2), (0, 2)],
)
def test_russian_takes_three_plural_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, form: int
) -> None:
    forms = ["{n} проблема", "{n} проблемы", "{n} проблем"]
    localedir = write_test_catalog(
        tmp_path, "ru", {}, plurals={("{n} problem", "{n} problems"): forms}
    )
    with loaded_catalog(monkeypatch, localedir, "ru_RU.UTF-8"):
        assert i18n.tn("{n} problem", "{n} problems", count) == forms[form]


def test_a_corrupt_catalog_raises_and_leaves_english(tmp_path: Path) -> None:
    target = tmp_path / "pt_BR" / "LC_MESSAGES" / "shidashi.mo"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not a catalog at all, far too short")
    with pytest.raises((OSError, ValueError)):
        i18n.init({"LANG": "pt_BR.UTF-8", i18n.LOCALEDIR_ENV: str(tmp_path)})
    assert i18n.t("none") == "none"


def test_the_default_catalog_directory_is_the_prefix_share_locale() -> None:
    assert i18n.default_localedir().parts[-2:] == ("share", "locale")
