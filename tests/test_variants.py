"""Testes de INTEGRAÇÃO da árvore ``variants/`` realmente entregue (R8.1–R8.4).

Diferente de ``test_config.py``/``test_merge.py`` (que montam fixtures), aqui
NÃO se constrói árvore alguma: aponta-se ``KAJI_VARIANTS_DIR`` para o
``variants/`` real do repositório (resolvido a partir da localização deste
arquivo de teste) e prova-se que os fragmentos shipados parseiam nos modelos
frozen (``extra="forbid"``) e fundem-se coerentemente.

Cobertura:
* ``base`` parseia; o make.conf de base teve fatorados os flags de compilador
  (sem ``COMMON_FLAGS``), o grupo ``SYSTEMD=`` (movido para init) e ``DESKTOPS``
  fica vazio; toda phase de base tem ``use_break == ()`` e existe a phase
  ``desktop``.
* os três ``arch`` parseiam com flags coerentes (arrowlake sem avx512, znver5
  com avx512, v3 sem avx512).
* ``kde`` parseia; ``merge(base, v3, kde, systemd)`` habilita ⊇ {qt6, kde,
  wayland} e sets ⊇ {kde}.
* ``minimal``/``gnome``/``xfce``/``wm`` parseiam; ``merge(base, v3, minimal,
  systemd)`` OMITE a phase ``desktop``.
* ``systemd``/``openrc`` parseiam; o profile de systemd termina em ``/systemd`` e
  o de openrc não; o merge openrc prepende a phase ``seat``.
"""

from pathlib import Path

import pytest

from kaji import config
from kaji.recipe import (
    ArchFragment,
    BaseFragment,
    FlavorFragment,
    InitFragment,
    load_arch,
    load_base,
    load_flavor,
    load_init,
    merge,
)

# Raiz do repo = pai de tests/; o variants/ real vive em <raiz>/variants.
_VARIANTS_DIR = Path(__file__).resolve().parent.parent / "variants"


@pytest.fixture(autouse=True)
def _point_at_real_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aponta o resolvedor de caminhos para o variants/ shipado (não fixture)."""
    monkeypatch.setenv("KAJI_VARIANTS_DIR", str(_VARIANTS_DIR))


# --- loaders por eixo (consomem o caminho resolvido por kaji.config) ---------


def _load_base() -> BaseFragment:
    return load_base(config.base_path())


def _load_arch(name: str) -> ArchFragment:
    return load_arch(config.recipe_path("arch", name))


def _load_flavor(name: str) -> FlavorFragment:
    return load_flavor(config.recipe_path("flavor", name))


def _load_init(name: str) -> InitFragment:
    return load_init(config.recipe_path("init", name))


def _base_make_conf_text() -> str:
    return (_VARIANTS_DIR / "base" / "portage" / "make.conf").read_text(encoding="utf-8")


def _live_text(text: str) -> str:
    """Texto sem linhas de comentário (ignora a documentação 'FACTORED OUT')."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# --- a árvore existe nos lugares esperados -----------------------------------


def test_variants_dir_resolves_to_shipped_tree() -> None:
    assert config.variants_dir() == _VARIANTS_DIR
    assert config.base_path().is_file()


@pytest.mark.parametrize("axis", ["arch", "flavor", "init"])
def test_axes_are_non_empty(axis: str) -> None:
    assert config.available_names(axis), f"eixo {axis!r} não tem valores"


# --- base: parseia, phases canônicas, make.conf fatorado ---------------------


def test_base_parses_with_canonical_profile_and_sets() -> None:
    base = _load_base()
    assert base.profile_base == "default/linux/amd64/23.0/no-multilib"
    assert set(base.sets) == {"graphics", "bentoo-apps"}


def test_every_base_phase_has_empty_use_break() -> None:
    base = _load_base()
    assert base.phases, "base deve declarar phases"
    for phase in base.phases:
        assert phase.use_break == (), f"phase {phase.name!r} não tem use_break vazio"


def test_base_declares_the_desktop_phase_named_exactly_desktop() -> None:
    # o merge omite a phase de nome literal "desktop"; ela PRECISA existir na base
    base = _load_base()
    assert "desktop" in {p.name for p in base.phases}


def test_base_make_conf_has_no_compiler_flags() -> None:
    text = _live_text(_base_make_conf_text())
    for token in ("COMMON_FLAGS=", "CFLAGS=", "CXXFLAGS=", "CHOST=", "ABI_X86="):
        assert token not in text, f"{token} deveria ter sido fatorado p/ arch"
    # CPU_FLAGS_X86 também é de arch (não pode reaparecer como atribuição)
    assert "CPU_FLAGS_X86=" not in text


def test_base_make_conf_has_no_systemd_group_or_use_reference() -> None:
    text = _live_text(_base_make_conf_text())
    assert "SYSTEMD=" not in text, "grupo SYSTEMD= deveria ter ido p/ init/systemd"
    # a referência ${SYSTEMD} no bloco USE também não pode sobrar
    assert "${SYSTEMD}" not in text


def test_base_make_conf_keeps_desktops_empty() -> None:
    text = _base_make_conf_text()
    # mantém a variável vazia (o flavor a popula); nunca com valor em base
    assert 'DESKTOPS=""' in text
    assert 'DESKTOPS="kde' not in _live_text(text)


def test_base_make_conf_keeps_unrelated_groups_verbatim() -> None:
    text = _base_make_conf_text()
    # amostras de grupos que NÃO migraram (R8.2: manter verbatim)
    for token in ('FEATURES="', 'VIDEO_CARDS="', 'DISTDIR="', 'GRAPHICS="', 'L10N="'):
        assert token in text


def test_base_package_use_system_drops_init_specific_systemd_line() -> None:
    system = (_VARIANTS_DIR / "base" / "portage" / "package.use" / "system").read_text(
        encoding="utf-8"
    )
    # a linha sys-apps/systemd boot ukify migrou p/ init/systemd
    assert "sys-apps/systemd boot ukify" not in _live_text(system)
    # mas linhas vizinhas não-init continuam (ex.: grub mount)
    assert "sys-boot/grub mount" in system


# --- arch: três alvos, flags coerentes ---------------------------------------


def test_all_three_arches_are_present() -> None:
    assert set(config.available_names("arch")) == {"v3", "znver5", "arrowlake"}


@pytest.mark.parametrize("name", ["v3", "znver5", "arrowlake"])
def test_each_arch_recipe_parses(name: str) -> None:
    arch = _load_arch(name)
    assert arch.arch == name
    assert arch.common_flags  # não-vazio
    assert arch.cpu_flags_x86  # não-vazio


def _has_avx512(arch: ArchFragment) -> bool:
    return any("avx512" in flag for flag in arch.cpu_flags_x86)


def test_arrowlake_has_no_avx512_and_is_tier2_buildonly() -> None:
    arch = _load_arch("arrowlake")
    assert not _has_avx512(arch), "Arrow Lake não tem AVX-512 (§9.2)"
    assert arch.goamd64 == "v3"
    assert arch.tier == 2
    assert arch.runnable_on_build_host is False
    assert "arrowlake" in arch.common_flags


def test_znver5_has_avx512_and_is_tier1_runnable() -> None:
    arch = _load_arch("znver5")
    assert _has_avx512(arch), "Zen 5 tem AVX-512 (§9.1)"
    assert arch.goamd64 == "v4"
    assert arch.tier == 1
    assert arch.runnable_on_build_host is True
    assert "znver5" in arch.common_flags


def test_v3_baseline_has_no_avx512_and_is_tier1_runnable() -> None:
    arch = _load_arch("v3")
    assert not _has_avx512(arch), "baseline x86-64-v3 não tem AVX-512 (§9.1)"
    assert arch.goamd64 == "v3"
    assert arch.tier == 1
    assert arch.runnable_on_build_host is True
    assert "x86-64-v3" in arch.common_flags


def test_arch_make_conf_mirrors_recipe_flags() -> None:
    # os knobs de CPU andam juntos (§9.3): make.conf espelha o recipe.yaml
    for name in ("v3", "znver5", "arrowlake"):
        arch = _load_arch(name)
        mk = (_VARIANTS_DIR / "arch" / name / "portage" / "make.conf").read_text(encoding="utf-8")
        assert f'COMMON_FLAGS="{arch.common_flags}"' in mk
        assert 'CHOST="x86_64-pc-linux-gnu"' in mk
        for flag in arch.cpu_flags_x86:
            assert flag in mk, f"{flag!r} ausente no make.conf de {name}"


# --- flavor kde: factored, merge habilita a camada KDE -----------------------


def test_kde_flavor_parses_and_is_curated() -> None:
    kde = _load_flavor("kde")
    assert kde.flavor == "kde"
    assert kde.override_ok is False  # curado/autoritativo
    assert "kde" in kde.sets


def test_merge_kde_v3_systemd_enables_kde_layer_and_set() -> None:
    resolved = merge(_load_base(), _load_arch("v3"), _load_flavor("kde"), _load_init("systemd"))
    assert {"qt6", "kde", "wayland"} <= set(resolved.use.enabled)
    assert {"kde"} <= set(resolved.sets)


# --- flavors minimal/gnome/xfce/wm: parseiam; minimal omite desktop ----------


@pytest.mark.parametrize("name", ["minimal", "gnome", "xfce", "wm"])
def test_other_flavors_parse(name: str) -> None:
    flavor = _load_flavor(name)
    assert flavor.flavor == name


def test_minimal_and_wm_are_free_flavors() -> None:
    assert _load_flavor("minimal").override_ok is True
    assert _load_flavor("wm").override_ok is True


def test_gnome_and_xfce_are_curated_flavors() -> None:
    assert _load_flavor("gnome").override_ok is False
    assert _load_flavor("xfce").override_ok is False


def test_merge_minimal_omits_desktop_phase() -> None:
    resolved = merge(_load_base(), _load_arch("v3"), _load_flavor("minimal"), _load_init("systemd"))
    names = [p.name for p in resolved.phases]
    assert "desktop" not in names
    # as demais phases de base permanecem
    assert "rebuild" in names and "apps" in names


# --- init systemd/openrc: profile e phase de seat ----------------------------


def test_both_inits_parse() -> None:
    assert _load_init("systemd").init == "systemd"
    assert _load_init("openrc").init == "openrc"


def test_systemd_merge_profile_ends_with_systemd_suffix() -> None:
    resolved = merge(_load_base(), _load_arch("v3"), _load_flavor("kde"), _load_init("systemd"))
    assert resolved.profile.endswith("/systemd")
    assert resolved.profile == "default/linux/amd64/23.0/no-multilib/systemd"


def test_openrc_merge_profile_has_no_systemd_suffix() -> None:
    resolved = merge(_load_base(), _load_arch("v3"), _load_flavor("kde"), _load_init("openrc"))
    assert not resolved.profile.endswith("/systemd")
    assert resolved.profile == "default/linux/amd64/23.0/no-multilib"


def test_openrc_merge_prepends_seat_phase() -> None:
    resolved = merge(_load_base(), _load_arch("v3"), _load_flavor("minimal"), _load_init("openrc"))
    assert resolved.phases[0].name == "seat"
