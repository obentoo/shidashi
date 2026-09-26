"""Testes de INTEGRAÇÃO da árvore ``variants/`` realmente entregue (R8.1–R8.4).

Diferente de ``test_config.py``/``test_merge.py`` (que montam fixtures), aqui
NÃO se constrói árvore alguma: aponta-se ``SHIDASHI_VARIANTS_DIR`` para o
``variants/`` real do repositório (resolvido a partir da localização deste
arquivo de teste) e prova-se que os fragmentos shipados parseiam nos modelos
frozen (``extra="forbid"``) e fundem-se coerentemente.

Cobertura:
* ``base`` parseia; o make.conf de base teve fatorados os flags de compilador
  (sem ``COMMON_FLAGS``), o grupo ``SYSTEMD=`` (movido para init) e ``DESKTOPS``
  fica vazio; a base carrega os três cortes do tronco.
* os três ``arch`` parseiam com flags coerentes (arrowlake sem avx512, znver5
  com avx512, v3 sem avx512).
* os estágios formam a cadeia ``base → minimal → desktop → <flavor>`` (D24);
  o USE do kde está no make.conf montado, não num campo decorativo.
* ``minimal`` é ``base → minimal``, sem desktop; cada flavor passa pelo desktop.
* ``systemd``/``openrc`` parseiam; o profile de systemd termina em ``/systemd`` e
  o de openrc não; o merge openrc prepende a phase ``seat``.
"""

import subprocess
from pathlib import Path

import pytest

from shidashi import config
from shidashi.phases import phase_target
from shidashi.recipe import (
    ArchFragment,
    BaseFragment,
    InitFragment,
    ResolvedRecipe,
    StageFragment,
    load_arch,
    load_base,
    load_init,
    load_stage,
)
from shidashi.resolve import apply_portage, kit_index

# Raiz do repo = pai de tests/; o variants/ real vive em <raiz>/variants.
_VARIANTS_DIR = Path(__file__).resolve().parent.parent / "variants"


@pytest.fixture(autouse=True)
def _point_at_real_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aponta o resolvedor de caminhos para o variants/ shipado (não fixture)."""
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(_VARIANTS_DIR))


# --- loaders por eixo (consomem o caminho resolvido por shidashi.config) ---------


def _load_base() -> BaseFragment:
    return load_base(config.base_path())


def _load_arch(name: str) -> ArchFragment:
    return load_arch(config.recipe_path("arch", name))


def _load_stage(name: str) -> StageFragment:
    return load_stage(config.stage_path(name))


def _recipe(target: str, init: str = "systemd", arch: str = "v3") -> ResolvedRecipe:
    return config.load_recipe(arch, target, init)


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
    assert set(base.sets) == {"base"}


def test_base_declares_only_trunk_cycle_breaks() -> None:
    """A base cura os ciclos do TRONCO; o resto é por flavor.

    Antes este teste exigia use_break vazio em toda phase da base, o que
    obrigava as quebras do tronco a viverem fora do repositório -- num ficheiro
    escrito à mão no laboratório.
    """
    base = _load_base()
    atoms = {b.atom for b in base.use_break}
    assert atoms == {"dev-lang/python", "dev-python/pillow", "media-video/pipewire"}, atoms
    assert all(b.enable is False for b in base.use_break)
    for name in config.target_names():
        stage = _load_stage(name)
        assert not stage.use_break, f"{name} declares cuts of its own: {stage.use_break}"



@pytest.mark.parametrize("flavor", ["minimal", "kde"])
def test_trunk_cuts_ride_the_phase_that_builds_the_trunk(flavor: str) -> None:
    """A phase cujo alvo é @world carrega os três cortes do tronco.

    run_phase escreve o use_break DA PRÓPRIA phase antes do emerge, e pula uma
    phase sem alvo antes de escrever. Com os cortes na phase graphics (até
    2026-09-26), o tronco rodava sem corte algum e, sob minimal, os cortes
    nunca eram aplicados. O lab não via: o sync junta os cortes de todas as
    phases num ficheiro só.
    """
    recipe = _recipe(flavor)
    trunk = [p for p in recipe.phases if p.emptytree]
    assert len(trunk) == 1, [p.name for p in trunk]
    atoms = {b.atom for b in trunk[0].use_break}
    assert {"dev-lang/python", "dev-python/pillow", "media-video/pipewire"} <= atoms, atoms


@pytest.mark.parametrize("flavor", ["kde", "gnome", "xfce", "wm"])
def test_every_graphical_flavor_is_built_on_desktop_on_minimal(flavor: str) -> None:
    """D24: base ─► minimal ─► desktop ─► flavor, and only the base rebuilds."""
    recipe = _recipe(flavor)
    assert recipe.stages == ("base", "minimal", "desktop", flavor)
    assert [p.name for p in recipe.phases if p.ships] == ["minimal", "flavor"]
    assert [p.name for p in recipe.phases if p.emptytree] == ["base"]


def test_minimal_is_the_base_plus_the_console_kits() -> None:
    recipe = _recipe("minimal")
    assert recipe.stages == ("base", "minimal")
    assert "desktop" not in {p.name for p in recipe.phases}
    assert recipe.phases[-1].ships


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


def test_base_make_conf_has_no_desktops_slot() -> None:
    # A base NÃO reserva um slot ${DESKTOPS} no bloco USE, e isso não é
    # esquecimento: apply_portage CONCATENA os make.conf dos layers, então o bloco
    # USE da base já foi expandido pelo shell quando o fragmento do flavor é lido.
    # Um slot aqui seria expandido vazio e o flavor não teria como preenchê-lo — o
    # flavor SOMA (USE="${USE} ${DESKTOPS}") no seu próprio fragmento (F28).
    text = _live_text(_base_make_conf_text())
    assert "${DESKTOPS}" not in text
    assert "DESKTOPS=" not in text


def test_kde_flavor_appends_desktops_to_use() -> None:
    # O contrapeso do teste acima: o flavor define o grupo E o soma ao USE. Sem a
    # segunda linha o grupo seria decorativo e a camada gráfica sumiria da imagem.
    text = _live_text(
        (_VARIANTS_DIR / "flavor" / "kde" / "portage" / "make.conf").read_text(encoding="utf-8")
    )
    assert 'DESKTOPS="kde' in text
    assert 'USE="${USE} ${DESKTOPS}"' in text


def test_systemd_init_appends_to_use_instead_of_replacing_it() -> None:
    # Mesma regra no eixo init. `USE="${SYSTEMD}"` (sem ${USE}) trocaria a
    # curadoria inteira da base por três flags — foi o que F28 mediu.
    text = _live_text(
        (_VARIANTS_DIR / "init" / "systemd" / "portage" / "make.conf").read_text(encoding="utf-8")
    )
    assert 'USE="${USE} ${SYSTEMD}"' in text


def test_base_make_conf_keeps_unrelated_groups_verbatim() -> None:
    text = _base_make_conf_text()
    # amostras de grupos que NÃO migraram (R8.2: manter verbatim)
    for token in ('FEATURES="', 'DISTDIR="', 'CORE="', 'L10N="'):
        assert token in text


def test_graphical_use_lives_in_the_desktop_stage_not_the_base() -> None:
    """D24: the base is a console core. The graphical groups belong to the
    desktop stage, and the base forces X off (many ebuilds default +X)."""
    base = _base_make_conf_text()
    desktop = (_VARIANTS_DIR / "desktop" / "portage" / "make.conf").read_text(encoding="utf-8")
    for token in ('GRAPHICS="', 'IMAGE="', 'VIDEO="', 'INPUT_DEVICES="'):
        assert token not in _live_text(base), f"{token} is still in the base"
        assert token in _live_text(desktop), f"{token} missing from the desktop stage"
    assert 'REMOVED="-X ' in base
    assert 'USE="${USE} ' in desktop  # appends; never replaces the base's curation


def test_video_cards_live_in_the_desktop_stage_with_wildcard_reset() -> None:
    # VIDEO_CARDS saiu do make.conf: uma atribuição lá NÃO consegue limpar os
    # defaults do profile (nouveau, vesa, dummy, radeon), só somar a eles. Em
    # package.use o prefixo "-*" zera antes de listar — sem isso esses drivers
    # seriam compilados em toda imagem, e o Portage não reporta nada.
    assert 'VIDEO_CARDS="' not in _base_make_conf_text()
    assert not (_VARIANTS_DIR / "base" / "portage" / "package.use" / "00video_cards").exists()
    entry = (_VARIANTS_DIR / "desktop" / "portage" / "package.use" / "00video_cards").read_text(
        encoding="utf-8"
    )
    line = next(ln for ln in _live_text(entry).splitlines() if "VIDEO_CARDS:" in ln)
    assert line.startswith("*/* VIDEO_CARDS: -*")
    assert "amdgpu" in line and "nvidia" in line


def test_base_package_use_system_drops_init_specific_systemd_line() -> None:
    system = (_VARIANTS_DIR / "base" / "portage" / "package.use" / "22-system").read_text(
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


def test_kde_flavor_parses_as_a_shipped_stage() -> None:
    kde = _load_stage("kde")
    assert (kde.stage, kde.after, kde.ships) == ("kde", "desktop", True)
    assert "kde" in kde.sets


def test_kde_use_lives_in_the_assembled_make_conf(tmp_path: Path) -> None:
    """The USE of an image is its layers' make.conf -- there is no other source.

    Until 2026-09-26 a `use_prefer` field claimed {qt6, kde, wayland} for kde and
    this test checked THAT; the field was only ever displayed by `recipe show`
    and never reached the build.
    """
    text = (_assemble(tmp_path, "v3", "kde", "systemd") / "make.conf").read_text(encoding="utf-8")
    desktops = next(ln for ln in text.splitlines() if ln.startswith("DESKTOPS="))
    assert {"kde", "qt6"} <= set(desktops.split('"')[1].split())
    assert 'USE="${USE} ${DESKTOPS}"' in text


# --- flavors minimal/gnome/xfce/wm: parseiam; minimal omite desktop ----------


@pytest.mark.parametrize("name", ["minimal", "desktop", "gnome", "xfce", "wm"])
def test_every_stage_parses(name: str) -> None:
    assert _load_stage(name).stage == name


def test_no_variant_yaml_still_carries_use_prefer() -> None:
    """use_prefer never reached the build (D24); the models now forbid it, and
    no shipped YAML may carry it as a live key."""
    live = [
        str(p.relative_to(_VARIANTS_DIR))
        for p in _VARIANTS_DIR.rglob("*.yaml")
        if any(ln.startswith(("use_prefer:", "override_ok:"))
               for ln in p.read_text(encoding="utf-8").splitlines())
    ]
    assert live == []


# --- init systemd/openrc: profile e phase de seat ----------------------------


def test_both_inits_parse() -> None:
    assert _load_init("systemd").init == "systemd"
    assert _load_init("openrc").init == "openrc"


def test_systemd_merge_profile_ends_with_systemd_suffix() -> None:
    resolved = _recipe("kde", "systemd")
    assert resolved.profile.endswith("/systemd")
    assert resolved.profile == "default/linux/amd64/23.0/no-multilib/systemd"


def test_openrc_merge_profile_has_no_systemd_suffix() -> None:
    resolved = _recipe("kde", "openrc")
    assert not resolved.profile.endswith("/systemd")
    assert resolved.profile == "default/linux/amd64/23.0/no-multilib"


def test_openrc_merge_prepends_seat_phase() -> None:
    resolved = _recipe("minimal", "openrc")
    assert resolved.phases[0].name == "seat"


# --- apply_portage sobre o variants/ REAL (regressão F28) ---------------------


def _assemble(tmp_path: Path, arch: str, flavor: str, init: str) -> Path:
    resolved = config.load_recipe(arch, flavor, init)
    rootfs = tmp_path / f"{arch}-{flavor}-{init}"
    (rootfs / "etc").mkdir(parents=True)
    apply_portage(rootfs, resolved, variants_dir=_VARIANTS_DIR)
    return rootfs / "etc" / "portage"


@pytest.mark.parametrize("flavor", ["minimal", "kde"])
def test_apply_portage_preserves_the_whole_base_make_conf(tmp_path: Path, flavor: str) -> None:
    # REGRESSÃO F28. apply_portage sobrescrevia arquivos de mesmo caminho, e o
    # make.conf de 133 linhas da base virava o fragmento de 6 linhas do
    # init/systemd — levando junto tudo o que se afere abaixo. O sintoma
    # observável era um make.conf com 6 linhas.
    portage = _assemble(tmp_path, "v3", flavor, "systemd")
    text = (portage / "make.conf").read_text(encoding="utf-8")
    assert len(text.splitlines()) > 100
    for var in ("FEATURES=", "PKGDIR=", "DISTDIR=", "LLVM_SLOT=", "PYTHON_TARGETS=",
                "MAKEOPTS=", "L10N=", "ACCEPT_KEYWORDS="):
        assert var in text, f"{var} perdida na composição"
    # e os fragmentos posteriores continuam presentes
    assert "CPU_FLAGS_X86=" in text          # arch/v3
    assert 'SYSTEMD="boot uki ukify"' in text  # init/systemd


def test_apply_portage_keeps_both_package_use_system_files(tmp_path: Path) -> None:
    # REGRESSÃO F28. base e init/systemd traziam ambos `package.use/system`; o
    # segundo apagava o primeiro, de 69 linhas para 4. O init entrega `50-systemd`
    # e a base `22-system`, e o Portage lê o diretório como união. Desde a
    # numeração (2026-09-13) a colisão nem é mais possível: nenhum layer usa o
    # nome `system` cru.
    portage = _assemble(tmp_path, "v3", "minimal", "systemd")
    base_lines = (_VARIANTS_DIR / "base/portage/package.use/22-system").read_text(
        encoding="utf-8"
    ).splitlines()
    got = (portage / "package.use" / "22-system").read_text(encoding="utf-8").splitlines()
    assert len(got) == len(base_lines)
    assert "sys-apps/systemd boot ukify policykit" in (
        portage / "package.use" / "50-systemd"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("arch", "flavor", "init", "expect", "reject"),
    [
        # minimal is the console core: no graphical flag at all, and X forced off
        ("v3", "minimal", "systemd", {"boot", "uki", "ukify", "-X"},
         {"kde", "qt6", "wayland", "vulkan", "opengl", "X"}),
        ("v3", "kde", "systemd", {"boot", "kde", "qt6", "plymouth", "wayland", "vulkan"}, {"X"}),
        ("znver5", "kde", "openrc", {"kde", "qt6", "wayland"}, {"boot", "uki", "ukify", "X"}),
    ],
)
def test_assembled_make_conf_composes_use_across_axes(
    tmp_path: Path, arch: str, flavor: str, init: str, expect: set[str], reject: set[str]
) -> None:
    # Contar linhas não prova semântica: o make.conf montado é SOURCEADO e o USE
    # resultante conferido. Cada eixo tem de somar o seu, e só o seu.
    portage = _assemble(tmp_path, arch, flavor, init)
    out = subprocess.run(
        ["bash", "-c", f'. "{portage / "make.conf"}"; printf "%s" "$USE"'],
        capture_output=True,
        text=True,
        check=True,
    )
    flags = set(out.stdout.split())
    # As sentinelas TÊM de ser flags que a RECEITA declara, não que o perfil
    # fornece: aqui o make.conf é sourceado isolado, sem perfil algum. `acl` era a
    # sentinela original e passou a falhar no dia em que foi removida da receita
    # por já vir do perfil — o teste acusou "a curadoria sumiu" quando nada tinha
    # sumido. Estas vivem nos grupos de base/portage/make.conf (wayland e vulkan
    # passaram ao estágio desktop em 2026-09-26, D24).
    assert {"btrfs", "cryptsetup"} <= flags, "a curadoria da base sumiu"
    assert expect <= flags
    assert not (reject & flags)


# --- integridade dos sets EMBARCADOS (variants/), não de dados sintéticos -----
#
# Estes testes existem porque a suíte inteira passou verde enquanto
# `bentoo-apps` (então em variants/base/sets/) já tinha sido apagado e a fase `apps` ainda
# apontava para `@bentoo-apps`: todo teste de fase montava uma receita sintética
# e nenhum olhava para o que o repositório realmente embarca.


def _shipped_sets() -> dict[str, Path]:
    """Todo set embarcado, por nome: a biblioteca ``kits/`` (D25)."""
    return kit_index(config.kits_dir())


def test_sets_live_only_in_the_kits_library() -> None:
    """D25: layers configure and choose; no layer carries a set of its own.

    A ``sets/`` directory inside a layer would be dead weight -- install_sets
    only reads the library -- and exactly the "where does this set live?"
    confusion the library was made to end.
    """
    stray = sorted(
        str(p.relative_to(config.variants_dir()))
        for p in config.variants_dir().rglob("sets")
        if p.is_dir() and "kits" not in p.relative_to(config.variants_dir()).parts
    )
    assert stray == [], f"sets outside kits/: {stray}"


def test_set_names_are_unique_across_the_library() -> None:
    """Portage's set namespace is flat; the categories are for people only."""
    names = [
        p.name
        for p in config.kits_dir().rglob("*")
        if p.is_file() and p.name not in {"README", "README.md"}
    ]
    dup = sorted({n for n in names if names.count(n) > 1})
    assert dup == [], f"set names defined more than once: {dup}"
    assert len(_shipped_sets()) == len(names)


def _set_refs(path: Path) -> list[str]:
    """Nomes referenciados por ``@nome`` dentro de um arquivo de set."""
    refs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.split("#", 1)[0].split()
        if token and token[0].startswith("@"):
            refs.append(token[0][1:])
    return refs


def test_every_set_reference_resolves_to_a_shipped_file() -> None:
    shipped = _shipped_sets()
    for name, path in shipped.items():
        for ref in _set_refs(path):
            assert ref in shipped, f"set {name!r} referencia @{ref}, que não existe"


@pytest.mark.parametrize("flavor", ["minimal", "kde", "gnome", "xfce", "wm"])
def test_every_declared_set_is_shipped(flavor: str) -> None:
    recipe = _recipe(flavor)
    shipped = _shipped_sets()
    for name in recipe.sets:
        assert name in shipped, f"{flavor}: set {name!r} declarado mas não embarcado"


@pytest.mark.parametrize("flavor", ["minimal", "kde", "gnome", "xfce", "wm"])
def test_every_phase_target_is_reachable(flavor: str) -> None:
    """Nenhuma fase pode apontar para um ``@set`` que não será instalado."""
    recipe = _recipe(flavor)
    shipped = _shipped_sets()
    for phase in recipe.phases:
        for target in phase_target(phase, recipe):
            if not target.startswith("@") or target == "@world":
                continue
            name = target[1:]
            assert name in shipped, f"{flavor}/{phase.name}: alvo {target} não existe"
            assert name in recipe.sets, (
                f"{flavor}/{phase.name}: alvo {target} não está em recipe.sets, "
                "logo não seria instalado no rootfs"
            )


#: The one set nobody reaches on purpose: rar/unrar are non-free, so no
#: aggregator references `archive-nonfree` and no flavor declares it.
_INTENTIONALLY_UNREACHABLE = {"archive-nonfree"}


def test_no_orphan_sets() -> None:
    """Every shipped set must be reachable from some flavor.

    The mirror image of test_every_declared_set_is_shipped, and the gap that let
    `p2p` sit unused after the base sets were split: proving the atoms are in the
    FILES says nothing about the files being USED.
    """
    files: dict[str, Path] = _shipped_sets()

    def refs(name: str) -> list[str]:
        path = files.get(name)
        if path is None:
            return []
        return [
            line.split("#")[0].strip()[1:]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#")[0].strip().startswith("@")
        ]

    reachable: set[str] = set()
    for flavor in ("minimal", "kde", "gnome", "xfce", "wm"):
        recipe = _recipe(flavor)
        pending = list(recipe.sets)
        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            reachable.add(name)
            pending += refs(name)

    orphans = set(files) - reachable - _INTENTIONALLY_UNREACHABLE
    assert not orphans, f"sets curados que ninguém instala: {sorted(orphans)}"


# --- D24: the configuration grows stage by stage (the shipped tree, for real) ---


class _MakeConfWitness:
    """A container that records the make.conf in force at every emerge."""

    def __init__(self, rootfs: Path) -> None:
        self.rootfs = rootfs
        self.seen: list[tuple[list[str], str, frozenset[str]]] = []

    def run(self, argv: object, **_k: object) -> object:
        from shidashi.container import CommandResult

        portage = self.rootfs / "etc" / "portage"
        package_use = frozenset(p.name for p in (portage / "package.use").iterdir())
        mc = (portage / "make.conf").read_text(encoding="utf-8")
        self.seen.append((list(argv), mc, package_use))  # type: ignore[call-overload]
        return CommandResult(0, "", "")


def test_the_kde_layer_is_not_in_force_until_the_kde_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs the real kde chain against a recording container: the base, minimal
    and desktop stages must be built WITHOUT the kde layer's make.conf, and the
    flavor stage with it. Applying every layer up front -- what the pipeline
    did before D24 -- would have built the trunk with DESKTOPS="kde qt6 …"."""
    from shidashi import phases

    monkeypatch.setattr(phases, "snapshot_fork_point", lambda _root, dest: dest)
    recipe = _recipe("kde")
    witness = _MakeConfWitness(tmp_path / "rootfs")
    phases.run_phases(
        witness, recipe, emptytree=True, snapshot="S", fork_points_dir=tmp_path  # type: ignore[arg-type]
    )
    stage_emerges = [s for s in witness.seen if "--oneshot" not in s[0]]
    assert len(stage_emerges) == 4  # base, minimal, desktop, flavor
    # make.conf is REWRITTEN per stage, package.use only ever ADDS -- check both:
    # a kde package.use file present while the base builds is the regression
    # that applying every layer up front would bring back.
    kde_package_use = _VARIANTS_DIR / "flavor" / "kde" / "portage" / "package.use"
    kde_files = {p.name for p in kde_package_use.iterdir()}
    assert ["DESKTOPS=" in mc for _a, mc, _pu in stage_emerges] == [False, False, False, True]
    assert [bool(kde_files & pu) for _a, _mc, pu in stage_emerges] == [False, False, False, True]
    assert stage_emerges[0][0][2] == "--emptytree"
