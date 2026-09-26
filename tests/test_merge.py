"""Testes do motor de merge (shidashi.recipe.merge / load_chain) — D24.

A receita é uma CADEIA DE ESTÁGIOS ``base → minimal → desktop → <flavor>``,
cada um declarando ``after:``, fundida com os eixos ``arch`` e ``init``. Os
fragmentos são construídos programaticamente; só os testes de ``load_chain``
escrevem YAML num diretório temporário.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shidashi.recipe import (
    ArchFragment,
    BaseFragment,
    InitFragment,
    Phase,
    RecipeChainError,
    StageFragment,
    UseBreak,
    load_chain,
    merge,
    stage_layer,
    stage_phase_name,
)
from shidashi.state import recipe_hash

# --- builders de fragmentos ---------------------------------------------------

_TRUNK_CUT = UseBreak(atom="dev-lang/python", flag="bluetooth")


def make_base(
    *,
    profile_base: str = "default/linux/amd64/23.0",
    sets: tuple[str, ...] = ("base",),
    use_break: tuple[UseBreak, ...] = (_TRUNK_CUT,),
) -> BaseFragment:
    return BaseFragment(profile_base=profile_base, sets=sets, use_break=use_break)


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
    # seed_source omitido (None) exercita o default do modelo; quando fornecido
    # é passado cru, para que valores inválidos ("metro") fiquem com o pydantic.
    extra: dict[str, Any] = {} if seed_source is None else {"seed_source": seed_source}
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


def make_stage(stage: str, after: str, **kw: Any) -> StageFragment:
    return StageFragment(stage=stage, after=after, **kw)


def make_chain(flavor: str = "kde") -> tuple[StageFragment, ...]:
    """The canonical graphical chain after the base: minimal → desktop → flavor."""
    return (
        make_stage("minimal", "base", ships=True, sets=("extra-system",)),
        make_stage("desktop", "minimal", sets=("gpu",)),
        make_stage(flavor, "desktop", ships=True, sets=(flavor, "extra-desktop")),
    )


def make_init(
    *,
    init: str = "systemd",
    profile_suffix: str = "systemd",
    phases_prepend: tuple[Phase, ...] = (),
) -> InitFragment:
    return InitFragment(init=init, profile_suffix=profile_suffix, phases_prepend=phases_prepend)


# --- profile (R2.2) -----------------------------------------------------------


def test_profile_systemd_suffix_appended() -> None:
    r = merge(make_base(), make_arch(), make_chain(), make_init(profile_suffix="systemd"))
    assert r.profile == "default/linux/amd64/23.0/systemd"


def test_profile_empty_suffix_no_change() -> None:
    # openrc usa sufixo vazio -> profile permanece o profile_base, sem barra final
    r = merge(make_base(), make_arch(), make_chain(), make_init(init="openrc", profile_suffix=""))
    assert r.profile == "default/linux/amd64/23.0"
    assert not r.profile.endswith("/")


def test_arch_and_stages_never_touch_profile() -> None:
    r = merge(
        make_base(profile_base="default/linux/arm64/23.0"),
        make_arch(arch="arm64"),
        make_chain(),
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
    r = merge(make_base(), arch, make_chain(), make_init())
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
    r = merge(make_base(), make_arch(), make_chain(), make_init())
    assert r.seed_source == "download"


def test_seed_source_catalyst_surfaces_on_resolved() -> None:
    r = merge(make_base(), make_arch(seed_source="catalyst"), make_chain(), make_init())
    assert r.seed_source == "catalyst"


def test_seed_source_invalid_value_rejected() -> None:
    # qualquer valor fora de {download, catalyst} falha no load do fragmento.
    with pytest.raises(ValidationError):
        make_arch(seed_source="metro")


def test_seed_source_changes_recipe_hash() -> None:
    # incluir seed_source na receita resolvida sensibiliza o recipe_hash, de modo
    # que estado persistido anterior é detectado como obsoleto (is_stale).
    base, chain, init = make_base(), make_chain(), make_init()
    h_download = recipe_hash(merge(base, make_arch(seed_source="download"), chain, init))
    h_catalyst = recipe_hash(merge(base, make_arch(seed_source="catalyst"), chain, init))
    assert h_download != h_catalyst


# --- stages: names, layers, phases (D24) --------------------------------------


def test_core_stages_name_their_phase_and_layer_flavors_do_not() -> None:
    assert [stage_phase_name(s) for s in ("base", "minimal", "desktop", "kde")] == [
        "base", "minimal", "desktop", "flavor",
    ]
    assert [stage_layer(s) for s in ("minimal", "desktop", "kde")] == [
        "minimal", "desktop", "flavor/kde",
    ]


def test_target_is_the_last_stage_and_the_chain_is_recorded() -> None:
    r = merge(make_base(), make_arch(), make_chain("gnome"), make_init())
    assert r.flavor == "gnome"
    assert r.stages == ("base", "minimal", "desktop", "gnome")


def test_empty_chain_is_the_base_alone() -> None:
    r = merge(make_base(), make_arch(), (), make_init())
    assert r.flavor == "base"
    assert [p.name for p in r.phases] == ["base"]


def test_portage_layers_base_arch_each_stage_then_init() -> None:
    r = merge(make_base(), make_arch(arch="amd64"), make_chain(), make_init(init="systemd"))
    assert r.portage_layers == (
        "base", "arch/amd64", "minimal", "desktop", "flavor/kde", "init/systemd",
    )


def test_each_phase_carries_the_layers_in_effect_up_to_its_stage() -> None:
    """The configuration GROWS along the chain -- that is how desktop can switch
    the graphical USE on in the middle of it. init applies from the seed on."""
    r = merge(make_base(), make_arch(arch="amd64"), make_chain(), make_init(init="systemd"))
    by = {p.name: p.layers for p in r.phases}
    assert by["base"] == ("base", "arch/amd64", "init/systemd")
    assert by["minimal"] == ("base", "arch/amd64", "minimal", "init/systemd")
    assert by["desktop"] == ("base", "arch/amd64", "minimal", "desktop", "init/systemd")
    assert by["flavor"] == r.portage_layers


def test_one_phase_per_stage_with_its_mode_after_the_init_prepends() -> None:
    init = make_init(phases_prepend=(Phase(name="seat", packages=("sys-auth/seatd",)),))
    r = merge(make_base(), make_arch(), make_chain(), init)
    assert [(p.name, p.stage) for p in r.phases] == [
        ("seat", ""), ("base", "base"), ("minimal", "minimal"),
        ("desktop", "desktop"), ("flavor", "kde"),
    ]
    assert [p.name for p in r.phases if p.emptytree] == ["base"]
    assert [p.name for p in r.phases if p.ships] == ["minimal", "flavor"]
    assert r.phases[0].layers == ("base", "arch/amd64", "init/systemd")


def test_sets_and_exclude_are_ordered_unions_over_the_chain() -> None:
    chain = (
        make_stage("minimal", "base", sets=("extra-system", "base"), exclude=("a/x",)),
        make_stage("kde", "minimal", sets=("kde",), exclude=("a/x", "b/y")),
    )
    r = merge(make_base(), make_arch(), chain, make_init())
    assert r.sets == ("base", "extra-system", "kde")
    assert r.exclude == ("a/x", "b/y")


def test_init_sets_add_the_current_inits_sets_to_their_stage_only() -> None:
    """A stage may install a set only under one init -- kde's display manager:
    plasma-login-manager needs systemd, openrc gets sddm. The init's sets join
    the stage's own, on that stage's phase; another init's are ignored."""
    chain = (
        make_stage("minimal", "base", sets=("extra-system",)),
        make_stage(
            "kde", "minimal", sets=("kde",),
            init_sets={"systemd": ("kde-dm-plasma",), "openrc": ("kde-dm-sddm",)},
        ),
    )
    sd = merge(make_base(), make_arch(), chain, make_init())
    rc = merge(make_base(), make_arch(), chain, make_init(init="openrc", profile_suffix=""))
    assert {p.name: p.sets for p in sd.phases}["flavor"] == ("kde", "kde-dm-plasma")
    assert {p.name: p.sets for p in rc.phases}["flavor"] == ("kde", "kde-dm-sddm")
    assert sd.sets == ("base", "extra-system", "kde", "kde-dm-plasma")
    assert "kde-dm-plasma" not in rc.sets


def test_each_stage_keeps_its_own_cuts_on_its_own_phase() -> None:
    """The trunk's cuts ride the base phase; a later stage adds its own on its
    phase and never replaces the base's."""
    own = UseBreak(atom="media-video/ffmpeg", flag="sdl")
    chain = (make_stage("minimal", "base"), make_stage("kde", "minimal", use_break=(own,)))
    r = merge(make_base(), make_arch(), chain, make_init())
    by = {p.name: p.use_break for p in r.phases}
    assert by["base"] == (_TRUNK_CUT,)
    assert by["minimal"] == ()
    assert by["flavor"] == (own,)


def test_a_chain_out_of_order_is_refused() -> None:
    chain = (make_stage("desktop", "minimal"),)  # skips minimal
    with pytest.raises(RecipeChainError, match="follows 'minimal'"):
        merge(make_base(), make_arch(), chain, make_init())


# --- load_chain: walking after: on disk ---------------------------------------


def _write_stages(root: Path, stages: dict[str, str]) -> dict[str, Path]:
    paths = {}
    for name, body in stages.items():
        path = root / f"{name}.yaml"
        path.write_text(body, encoding="utf-8")
        paths[name] = path
    return paths


def test_load_chain_walks_after_down_to_the_base(tmp_path: Path) -> None:
    paths = _write_stages(tmp_path, {
        "minimal": "stage: minimal\nafter: base\nships: true\n",
        "desktop": "stage: desktop\nafter: minimal\n",
        "kde": "stage: kde\nafter: desktop\nships: true\nsets: [kde]\n",
    })
    chain = load_chain("kde", paths.__getitem__)
    assert [s.stage for s in chain] == ["minimal", "desktop", "kde"]
    assert load_chain("minimal", paths.__getitem__)[0].ships is True
    assert load_chain("base", paths.__getitem__) == ()


def test_load_chain_refuses_a_stage_without_after(tmp_path: Path) -> None:
    paths = _write_stages(tmp_path, {"kde": "stage: kde\n"})
    with pytest.raises(RecipeChainError, match="no `after:`"):
        load_chain("kde", paths.__getitem__)


def test_load_chain_refuses_a_loop(tmp_path: Path) -> None:
    paths = _write_stages(tmp_path, {
        "a": "stage: a\nafter: b\n",
        "b": "stage: b\nafter: a\n",
    })
    with pytest.raises(RecipeChainError, match="loops"):
        load_chain("a", paths.__getitem__)


def test_load_chain_refuses_a_file_that_names_another_stage(tmp_path: Path) -> None:
    paths = _write_stages(tmp_path, {"kde": "stage: gnome\nafter: desktop\n"})
    with pytest.raises(RecipeChainError, match="declares stage 'gnome'"):
        load_chain("kde", paths.__getitem__)


def test_stage_fragment_forbids_the_old_use_prefer() -> None:
    """use_prefer never reached the build; a YAML still carrying it must fail
    loudly instead of being ignored."""
    with pytest.raises(ValidationError):
        StageFragment(stage="kde", after="desktop", use_prefer={"add": ["qt6"]})
