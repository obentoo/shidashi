"""Testes do motor de merge (kaji.recipe.merge) — R2.1–R2.7, R3.1, R3.2.

Os fragmentos são construídos programaticamente via os modelos; não se lê o
diretório ``variants/``.
"""

import pytest

from kaji.recipe import (
    ArchFragment,
    BaseFragment,
    FlavorFragment,
    InitFragment,
    Phase,
    RecipeConflictError,
    UsePrefer,
    merge,
)

# --- builders/fixtures de fragmentos -----------------------------------------

_NO_USE = UsePrefer()  # singleton imutável p/ default de argumento (evita B008)


def make_base(
    *,
    profile_base: str = "default/linux/amd64/23.0",
    sets: tuple[str, ...] = ("@system",),
    phases: tuple[Phase, ...] = (
        Phase(name="system"),
        Phase(name="desktop"),
        Phase(name="late"),
    ),
) -> BaseFragment:
    return BaseFragment(profile_base=profile_base, sets=sets, phases=phases)


def make_arch(
    *,
    arch: str = "amd64",
    common_flags: str = "-O2 -pipe",
    goamd64: str = "v2",
    rustflags: str = "-C target-cpu=x86-64-v2",
    cpu_flags_x86: tuple[str, ...] = ("sse2", "avx"),
    runnable_on_build_host: bool = True,
    tier: int = 1,
) -> ArchFragment:
    return ArchFragment(
        arch=arch,
        common_flags=common_flags,
        goamd64=goamd64,
        rustflags=rustflags,
        cpu_flags_x86=cpu_flags_x86,
        runnable_on_build_host=runnable_on_build_host,
        tier=tier,
    )


def make_flavor(
    *,
    flavor: str = "desktop",
    use_prefer: UsePrefer = _NO_USE,
    sets: tuple[str, ...] = ("@desktop",),
    override_ok: bool = False,
) -> FlavorFragment:
    return FlavorFragment(flavor=flavor, use_prefer=use_prefer, sets=sets, override_ok=override_ok)


def make_init(
    *,
    init: str = "systemd",
    profile_suffix: str = "systemd",
    use_prefer: UsePrefer = _NO_USE,
    phases_prepend: tuple[Phase, ...] = (Phase(name="early"),),
) -> InitFragment:
    return InitFragment(
        init=init,
        profile_suffix=profile_suffix,
        use_prefer=use_prefer,
        phases_prepend=phases_prepend,
    )


# --- profile (R2.2) -----------------------------------------------------------


def test_profile_systemd_suffix_appended() -> None:
    r = merge(make_base(), make_arch(), make_flavor(), make_init(profile_suffix="systemd"))
    assert r.profile == "default/linux/amd64/23.0/systemd"


def test_profile_empty_suffix_no_change() -> None:
    # openrc usa sufixo vazio -> profile permanece o profile_base, sem barra final
    r = merge(make_base(), make_arch(), make_flavor(), make_init(init="openrc", profile_suffix=""))
    assert r.profile == "default/linux/amd64/23.0"
    assert not r.profile.endswith("/")


def test_arch_and_flavor_never_touch_profile() -> None:
    r = merge(
        make_base(profile_base="default/linux/arm64/23.0"),
        make_arch(arch="arm64"),
        make_flavor(flavor="minimal"),
        make_init(profile_suffix=""),
    )
    assert r.profile == "default/linux/arm64/23.0"


# --- arch knobs (R2.6) --------------------------------------------------------


def test_arch_knobs_copied_through() -> None:
    arch = make_arch(
        arch="amd64",
        common_flags="-O3 -march=native",
        goamd64="v3",
        rustflags="-C target-cpu=x86-64-v3",
        cpu_flags_x86=("sse2", "avx", "avx2"),
        runnable_on_build_host=False,
        tier=2,
    )
    r = merge(make_base(), arch, make_flavor(), make_init())
    assert r.arch == "amd64"
    assert r.common_flags == "-O3 -march=native"
    assert r.goamd64 == "v3"
    assert r.rustflags == "-C target-cpu=x86-64-v3"
    assert r.cpu_flags_x86 == ("sse2", "avx", "avx2")
    assert r.runnable_on_build_host is False
    assert r.tier == 2


# --- portage_layers (R2.7) ----------------------------------------------------


def test_portage_layers_ordered_base_arch_flavor_init() -> None:
    r = merge(
        make_base(),
        make_arch(arch="amd64"),
        make_flavor(flavor="desktop"),
        make_init(init="systemd"),
    )
    assert r.portage_layers == (
        "base",
        "arch/amd64",
        "flavor/desktop",
        "init/systemd",
    )


# --- USE accumulation (R2.3) --------------------------------------------------


def test_use_same_sign_dedup_is_idempotent() -> None:
    # qt6 repetido em add (flavor) e add (init) não duplica nem levanta erro
    flavor = make_flavor(use_prefer=UsePrefer(add=("qt6", "qt6")))
    init = make_init(use_prefer=UsePrefer(add=("qt6",)))
    r = merge(make_base(), make_arch(), flavor, init)
    assert r.use.enabled == ("qt6",)
    assert r.use.disabled == ()


def test_use_enabled_and_disabled_both_sorted() -> None:
    flavor = make_flavor(
        use_prefer=UsePrefer(add=("x264", "qt6", "alsa"), drop=("-gtk", "-pulseaudio"))
    )
    r = merge(make_base(), make_arch(), flavor, make_init())
    assert r.use.enabled == ("alsa", "qt6", "x264")
    assert r.use.disabled == ("gtk", "pulseaudio")
    assert list(r.use.enabled) == sorted(r.use.enabled)
    assert list(r.use.disabled) == sorted(r.use.disabled)


def test_use_flavor_init_union() -> None:
    flavor = make_flavor(use_prefer=UsePrefer(add=("qt6",), drop=("-gtk",)))
    init = make_init(use_prefer=UsePrefer(add=("systemd",), drop=("-elogind",)))
    r = merge(make_base(), make_arch(), flavor, init)
    assert r.use.enabled == ("qt6", "systemd")
    assert r.use.disabled == ("elogind", "gtk")


def test_use_negated_token_lands_in_disabled_without_dash() -> None:
    flavor = make_flavor(use_prefer=UsePrefer(drop=("-gtk",)))
    r = merge(make_base(), make_arch(), flavor, make_init())
    assert "gtk" in r.use.disabled
    assert "-gtk" not in r.use.disabled
    assert "gtk" not in r.use.enabled


def test_use_arch_and_base_contribute_nothing() -> None:
    # nenhum USE em flavor/init => resolved use vazio (arch/base não contribuem)
    r = merge(make_base(), make_arch(), make_flavor(), make_init())
    assert r.use.enabled == ()
    assert r.use.disabled == ()


# --- override_ok conflict rule (R3.1/R3.2) ------------------------------------


def test_conflict_raises_when_flavor_not_overridable() -> None:
    # flavor habilita gtk; init tenta desabilitar; override_ok=False -> conflito
    flavor = make_flavor(use_prefer=UsePrefer(add=("gtk",)), override_ok=False)
    init = make_init(use_prefer=UsePrefer(drop=("-gtk",)))
    with pytest.raises(RecipeConflictError) as excinfo:
        merge(make_base(), make_arch(), flavor, init)
    err = excinfo.value
    assert err.flag == "gtk"
    # ambas as camadas nomeadas: a anterior (flavor) e a posterior (init)
    assert err.layer_a == "flavor"
    assert err.layer_b == "init"
    msg = str(err)
    assert "gtk" in msg
    assert "flavor" in msg
    assert "init" in msg


def test_override_ok_true_later_layer_wins_silently() -> None:
    # mesmo cenário, override_ok=True -> init vence (desabilita), sem exceção
    flavor = make_flavor(use_prefer=UsePrefer(add=("gtk",)), override_ok=True)
    init = make_init(use_prefer=UsePrefer(drop=("-gtk",)))
    r = merge(make_base(), make_arch(), flavor, init)
    assert "gtk" in r.use.disabled
    assert "gtk" not in r.use.enabled


# --- sets (R2.4) --------------------------------------------------------------


def test_sets_ordered_unique_union() -> None:
    base = make_base(sets=("@system", "@core"))
    flavor = make_flavor(sets=("@desktop", "@core", "@media"))
    r = merge(base, make_arch(), flavor, make_init())
    # primeira ocorrência preservada, duplicata (@core) descartada
    assert r.sets == ("@system", "@core", "@desktop", "@media")


# --- phases (R2.5) ------------------------------------------------------------


def test_phases_prepend_then_base_with_desktop_kept() -> None:
    base = make_base(phases=(Phase(name="system"), Phase(name="desktop"), Phase(name="late")))
    init = make_init(phases_prepend=(Phase(name="early"),))
    flavor = make_flavor(sets=("@desktop",))  # sets não vazio -> mantém desktop
    r = merge(base, make_arch(), flavor, init)
    assert tuple(p.name for p in r.phases) == ("early", "system", "desktop", "late")
    assert r.phases == init.phases_prepend + base.phases


def test_phases_empty_flavor_sets_omits_desktop() -> None:
    base = make_base(phases=(Phase(name="system"), Phase(name="desktop"), Phase(name="late")))
    init = make_init(phases_prepend=(Phase(name="early"),))
    flavor = make_flavor(flavor="minimal", sets=())  # sets vazio -> omite desktop
    r = merge(base, make_arch(), flavor, init)
    assert tuple(p.name for p in r.phases) == ("early", "system", "late")
    assert all(p.name != "desktop" for p in r.phases)
