"""Testes do motor de merge (shidashi.recipe.merge) — R2.1–R2.7, R3.1, R3.2.

Os fragmentos são construídos programaticamente via os modelos; não se lê o
diretório ``variants/``.

Story 003 (1.2): ``merge`` injeta o mapa ``flavor.use_break`` (phase-name →
breaks) nas phases montadas — cada phase cujo nome é chave no mapa é
substituída por ``phase.model_copy(update={"use_break": ...})``; phases sem
entrada ficam com ``use_break`` vazio. A injeção respeita a ordem das phases e
a regra de omitir ``desktop`` quando ``flavor.sets == ()``. ``UseBreak`` é
importado de forma tolerante para não abortar a coleção enquanto não existe.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from shidashi.recipe import (
    ArchFragment,
    BaseFragment,
    FlavorFragment,
    InitFragment,
    Phase,
    RecipeConflictError,
    UsePrefer,
    merge,
)
from shidashi.state import recipe_hash
from tests._pending import try_import

UseBreak: Any = try_import("shidashi.recipe", "UseBreak")

# --- builders/fixtures de fragmentos -----------------------------------------

_NO_USE = UsePrefer()  # singleton imutável p/ default de argumento (evita B008)
_NO_BREAKS: dict[str, Any] = {}  # default imutável (evita B006)


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
    seed_source: str | None = None,
) -> ArchFragment:
    # seed_source omitido (None) exercita o default do modelo (R1.1); quando
    # fornecido, é passado para validar valores explícitos/ inválidos.
    extra = {} if seed_source is None else {"seed_source": seed_source}
    return ArchFragment(
        arch=arch,
        common_flags=common_flags,
        goamd64=goamd64,
        rustflags=rustflags,
        cpu_flags_x86=cpu_flags_x86,
        runnable_on_build_host=runnable_on_build_host,
        tier=tier,
        **extra,
    )


def make_flavor(
    *,
    flavor: str = "desktop",
    use_prefer: UsePrefer = _NO_USE,
    sets: tuple[str, ...] = ("@desktop",),
    override_ok: bool = False,
    use_break: dict[str, Any] = _NO_BREAKS,
) -> FlavorFragment:
    return FlavorFragment(
        flavor=flavor,
        use_prefer=use_prefer,
        sets=sets,
        override_ok=override_ok,
        use_break=use_break,
    )


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


# --- seed_source (R1.1–R1.4, story 005) ---------------------------------------


def test_seed_source_defaults_to_download() -> None:
    # arch sem seed_source -> default "download" propagado à receita resolvida.
    r = merge(make_base(), make_arch(), make_flavor(), make_init())
    assert r.seed_source == "download"


def test_seed_source_catalyst_surfaces_on_resolved() -> None:
    r = merge(make_base(), make_arch(seed_source="catalyst"), make_flavor(), make_init())
    assert r.seed_source == "catalyst"


def test_seed_source_invalid_value_rejected() -> None:
    # qualquer valor fora de {download, catalyst} falha no load do fragmento.
    with pytest.raises(ValidationError):
        make_arch(seed_source="metro")


def test_seed_source_changes_recipe_hash() -> None:
    # incluir seed_source na receita resolvida sensibiliza o recipe_hash, de modo
    # que estado persistido anterior é detectado como obsoleto (is_stale).
    base, flavor, init = make_base(), make_flavor(), make_init()
    h_download = recipe_hash(merge(base, make_arch(seed_source="download"), flavor, init))
    h_catalyst = recipe_hash(merge(base, make_arch(seed_source="catalyst"), flavor, init))
    assert h_download != h_catalyst


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
    r = merge(make_base(), make_arch(), make_flavor(), make_init())
    assert r.use.enabled == ()
    assert r.use.disabled == ()


# --- override_ok conflict rule (R3.1/R3.2) ------------------------------------


def test_conflict_raises_when_flavor_not_overridable() -> None:
    flavor = make_flavor(use_prefer=UsePrefer(add=("gtk",)), override_ok=False)
    init = make_init(use_prefer=UsePrefer(drop=("-gtk",)))
    with pytest.raises(RecipeConflictError) as excinfo:
        merge(make_base(), make_arch(), flavor, init)
    err = excinfo.value
    assert err.flag == "gtk"
    assert err.layer_a == "flavor"
    assert err.layer_b == "init"
    msg = str(err)
    assert "gtk" in msg
    assert "flavor" in msg
    assert "init" in msg


def test_override_ok_true_later_layer_wins_silently() -> None:
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


# --- use_break injection (story 003 1.2 — R4.1, R4.5, R4.6) -------------------


def _ffmpeg_break() -> Any:
    return UseBreak(atom="media-video/ffmpeg", flag="sdl", enable=False)


def test_merge_injects_flavor_use_break_onto_matching_phase() -> None:
    brk = _ffmpeg_break()
    base = make_base(
        phases=(Phase(name="rebuild"), Phase(name="graphics"), Phase(name="desktop"))
    )
    flavor = make_flavor(flavor="kde", sets=("@kde",), use_break={"graphics": (brk,)})
    r = merge(base, make_arch(), flavor, make_init(phases_prepend=()))
    by_name = {p.name: p for p in r.phases}
    assert by_name["graphics"].use_break == (brk,)
    # phases sem entrada no mapa ficam com use_break vazio
    assert by_name["rebuild"].use_break == ()
    assert by_name["desktop"].use_break == ()


def test_merge_minimal_empty_map_leaves_all_phases_without_breaks() -> None:
    base = make_base(phases=(Phase(name="rebuild"), Phase(name="graphics"), Phase(name="late")))
    flavor = make_flavor(flavor="minimal", sets=(), use_break={})
    r = merge(base, make_arch(), flavor, make_init(phases_prepend=()))
    assert all(p.use_break == () for p in r.phases)


def test_merge_injection_respects_desktop_omit_and_phase_order() -> None:
    brk = _ffmpeg_break()
    base = make_base(
        phases=(Phase(name="rebuild"), Phase(name="graphics"), Phase(name="desktop"))
    )
    flavor = make_flavor(flavor="minimal", sets=(), use_break={"desktop": (brk,)})
    r = merge(base, make_arch(), flavor, make_init(phases_prepend=(Phase(name="early"),)))
    assert tuple(p.name for p in r.phases) == ("early", "rebuild", "graphics")
    assert all(p.name != "desktop" for p in r.phases)
    assert all(p.use_break == () for p in r.phases)


def test_merge_injection_preserves_phase_order_with_break() -> None:
    brk = _ffmpeg_break()
    base = make_base(
        phases=(Phase(name="rebuild"), Phase(name="graphics"), Phase(name="desktop"))
    )
    flavor = make_flavor(flavor="kde", sets=("@kde",), use_break={"graphics": (brk,)})
    r = merge(base, make_arch(), flavor, make_init(phases_prepend=(Phase(name="early"),)))
    assert tuple(p.name for p in r.phases) == ("early", "rebuild", "graphics", "desktop")
