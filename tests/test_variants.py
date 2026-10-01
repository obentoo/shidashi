"""INTEGRATION tests of the ``variants/`` tree actually shipped (R8.1–R8.4).

Unlike ``test_config.py``/``test_merge.py`` (which build fixtures), here NO tree
is built at all: ``SHIDASHI_VARIANTS_DIR`` is pointed at the repository's real
``variants/`` (resolved from this test file's location) and we prove that the
shipped fragments parse into the frozen models (``extra="forbid"``) and merge
coherently.

Coverage:
* ``base`` parses; the base make.conf had the compiler flags factored out
  (no ``COMMON_FLAGS``), the ``SYSTEMD=`` group (moved to init) and ``DESKTOPS``
  is empty; the base carries the three trunk cuts.
* the three ``arch`` values parse with coherent flags (arrowlake without avx512,
  znver5 with avx512, v3 without avx512).
* the stages form the ``base → minimal → desktop → <flavor>`` chain (D24);
  kde's USE is in the assembled make.conf, not in a decorative field.
* ``minimal`` is ``base → minimal``, without desktop; every flavor goes through desktop.
* ``systemd``/``openrc`` parse; the systemd profile ends in ``/systemd`` and
  the openrc one does not; the openrc merge prepends the ``seat`` phase.
"""

import itertools
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
from shidashi.resolve import apply_portage, catalog_entry, kit_index

# the binpkg check of a shipped stage extracts the stage3's vdb: stubbed here
pytestmark = pytest.mark.usefixtures("no_stage3_vdb")

# Repo root = parent of tests/; the real variants/ lives at <root>/variants.
_VARIANTS_DIR = Path(__file__).resolve().parent.parent / "variants"


@pytest.fixture(autouse=True)
def _point_at_real_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the path resolver at the shipped variants/ (not a fixture)."""
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(_VARIANTS_DIR))


# --- per-axis loaders (consume the path resolved by shidashi.config) ------------


def _load_base() -> BaseFragment:
    return load_base(config.base_path())


def _load_arch(name: str) -> ArchFragment:
    return load_arch(config.recipe_path("arch", name))


def _load_stage(name: str) -> StageFragment:
    return load_stage(config.stage_path(name))


_INITS = ("systemd", "openrc")


def _recipe(target: str, init: str = "systemd", arch: str = "v3") -> ResolvedRecipe:
    return config.load_recipe(arch, target, init)


def _load_init(name: str) -> InitFragment:
    return load_init(config.recipe_path("init", name))


def _base_make_conf_text() -> str:
    return (_VARIANTS_DIR / "base" / "portage" / "make.conf").read_text(encoding="utf-8")


def _live_text(text: str) -> str:
    """Text without comment lines (ignores the 'FACTORED OUT' documentation)."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# --- the tree exists in the expected places ----------------------------------


def test_variants_dir_resolves_to_shipped_tree() -> None:
    assert config.variants_dir() == _VARIANTS_DIR
    assert config.base_path().is_file()


@pytest.mark.parametrize("axis", ["arch", "flavor", "init"])
def test_axes_are_non_empty(axis: str) -> None:
    assert config.available_names(axis), f"axis {axis!r} has no values"


# --- base: parses, canonical phases, factored make.conf ----------------------


def test_base_parses_with_canonical_profile_and_sets() -> None:
    base = _load_base()
    assert base.profile_base == "default/linux/amd64/23.0/no-multilib"
    assert set(base.sets) == {"base"}


def test_base_declares_only_trunk_cycle_breaks() -> None:
    """The base curates the TRUNK's cycles; the rest is per flavor.

    This test used to require an empty use_break in every base phase, which
    forced the trunk's breaks to live outside the repository -- in a file
    written by hand in the lab.
    """
    base = _load_base()
    atoms = {b.atom for b in base.use_break}
    # no trunk cut since D24 step 3b (2026-09-27): the cycles the lab cut need
    # USE the base no longer has; the audio cut lives on desktop (F72)
    assert atoms == set(), atoms
    for name in config.target_names():
        stage = _load_stage(name)
        assert not stage.use_break, f"{name} declares cuts of its own: {stage.use_break}"


@pytest.mark.parametrize("flavor", ["minimal", "kde"])
def test_the_trunk_phase_carries_no_cut(flavor: str) -> None:
    """Since D24 step 3b the trunk's cycles do not form (2026-09-27): the base
    phase -- the one whose target is @world with --emptytree -- has no cut."""
    recipe = _recipe(flavor)
    trunk = [p for p in recipe.phases if p.emptytree]
    assert len(trunk) == 1, [p.name for p in trunk]
    assert trunk[0].use_break == (), trunk[0].use_break


@pytest.mark.parametrize("flavor", ["kde", "gnome", "wm"])
def test_the_audio_cycle_is_cut_by_the_desktop_stage(flavor: str) -> None:
    """F72: ffmpeg -> libsdl2 -> pipewire -> ffmpeg forms where the graphical
    USE and the audio server arrive -- the desktop stage. Declared on the base,
    minimal's settle dropped it (pipewire is not in minimal) and the desktop
    resolve over the real minimal vdb met the cycle again (2026-09-27)."""
    recipe = _recipe(flavor)
    desktop = next(p for p in recipe.phases if p.stage == "desktop")
    assert [(b.atom, b.flag, b.enable) for b in desktop.use_break] == [
        ("media-video/pipewire", "ffmpeg", False)
    ]
    base = next(p for p in recipe.phases if p.stage == "base")
    assert "media-video/pipewire" not in {b.atom for b in base.use_break}


@pytest.mark.parametrize("flavor", ["kde", "gnome", "wm"])
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
        assert token not in text, f"{token} should have been factored out to arch"
    # CPU_FLAGS_X86 also belongs to arch (it must not reappear as an assignment)
    assert "CPU_FLAGS_X86=" not in text


def test_base_make_conf_has_no_systemd_group_or_use_reference() -> None:
    text = _live_text(_base_make_conf_text())
    assert "SYSTEMD=" not in text, "the SYSTEMD= group should have gone to init/systemd"
    # the ${SYSTEMD} reference in the USE block must not be left over either
    assert "${SYSTEMD}" not in text


def test_base_make_conf_has_no_desktops_slot() -> None:
    # The base does NOT reserve a ${DESKTOPS} slot in the USE block, and that is
    # not an oversight: apply_portage CONCATENATES the layers' make.conf, so the
    # base's USE block has already been expanded by the shell when the flavor's
    # fragment is read. A slot here would expand empty and the flavor would have
    # no way to fill it — the flavor ADDS (USE="${USE} ${DESKTOPS}") in its own
    # fragment (F28).
    text = _live_text(_base_make_conf_text())
    assert "${DESKTOPS}" not in text
    assert "DESKTOPS=" not in text


def test_kde_flavor_appends_desktops_to_use() -> None:
    # The counterweight of the test above: the flavor defines the group AND adds it
    # to USE. Without the second line the group would be decorative and the
    # graphical layer would vanish from the image.
    text = _live_text(
        (_VARIANTS_DIR / "flavor" / "kde" / "portage" / "make.conf").read_text(encoding="utf-8")
    )
    assert 'DESKTOPS="kde' in text
    assert 'USE="${USE} ${DESKTOPS}"' in text


def test_systemd_init_appends_to_use_instead_of_replacing_it() -> None:
    # Same rule on the init axis. `USE="${SYSTEMD}"` (without ${USE}) would replace
    # the base's whole curation with three flags — which is what F28 measured.
    text = _live_text(
        (_VARIANTS_DIR / "init" / "systemd" / "portage" / "make.conf").read_text(encoding="utf-8")
    )
    assert 'USE="${USE} ${SYSTEMD}"' in text


def test_base_make_conf_keeps_unrelated_groups_verbatim() -> None:
    text = _base_make_conf_text()
    # samples of groups that did NOT move (R8.2: keep verbatim)
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
    # VIDEO_CARDS left make.conf: an assignment there CANNOT clear the profile's
    # defaults (nouveau, vesa, dummy, radeon), only add to them. In package.use
    # the "-*" prefix resets before listing — without it those drivers would be
    # built into every image, and Portage reports nothing.
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
    # the sys-apps/systemd boot ukify line moved to init/systemd
    assert "sys-apps/systemd boot ukify" not in _live_text(system)
    # but neighboring non-init lines remain (e.g. grub mount)
    assert "sys-boot/grub mount" in system


# --- arch: three targets, coherent flags ------------------------------------


def test_all_three_arches_are_present() -> None:
    assert set(config.available_names("arch")) == {"v3", "znver5", "arrowlake"}


@pytest.mark.parametrize("name", ["v3", "znver5", "arrowlake"])
def test_each_arch_recipe_parses(name: str) -> None:
    arch = _load_arch(name)
    assert arch.arch == name
    assert arch.common_flags  # non-empty
    assert arch.cpu_flags_x86  # non-empty


def _has_avx512(arch: ArchFragment) -> bool:
    return any("avx512" in flag for flag in arch.cpu_flags_x86)


def test_arrowlake_has_no_avx512_and_is_tier2_buildonly() -> None:
    arch = _load_arch("arrowlake")
    assert not _has_avx512(arch), "Arrow Lake has no AVX-512 (§9.2)"
    assert arch.goamd64 == "v3"
    assert arch.tier == 2
    assert arch.runnable_on_build_host is False
    assert "arrowlake" in arch.common_flags


def test_znver5_has_avx512_and_is_tier1_runnable() -> None:
    arch = _load_arch("znver5")
    assert _has_avx512(arch), "Zen 5 has AVX-512 (§9.1)"
    assert arch.goamd64 == "v4"
    assert arch.tier == 1
    assert arch.runnable_on_build_host is True
    assert "znver5" in arch.common_flags


def test_v3_baseline_has_no_avx512_and_is_tier1_runnable() -> None:
    arch = _load_arch("v3")
    assert not _has_avx512(arch), "baseline x86-64-v3 has no AVX-512 (§9.1)"
    assert arch.goamd64 == "v3"
    assert arch.tier == 1
    assert arch.runnable_on_build_host is True
    assert "x86-64-v3" in arch.common_flags


def test_arch_make_conf_mirrors_recipe_flags() -> None:
    # the CPU knobs go together (§9.3): make.conf mirrors recipe.yaml
    for name in ("v3", "znver5", "arrowlake"):
        arch = _load_arch(name)
        mk = (_VARIANTS_DIR / "arch" / name / "portage" / "make.conf").read_text(encoding="utf-8")
        assert f'COMMON_FLAGS="{arch.common_flags}"' in mk
        assert 'CHOST="x86_64-pc-linux-gnu"' in mk
        for flag in arch.cpu_flags_x86:
            assert flag in mk, f"{flag!r} missing from the make.conf of {name}"


# --- flavor kde: factored, merge enables the KDE layer -----------------------


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


# --- flavors minimal/gnome/wm: parse; minimal omits desktop -------------


@pytest.mark.parametrize("name", ["minimal", "desktop", "gnome", "wm"])
def test_every_stage_parses(name: str) -> None:
    assert _load_stage(name).stage == name


def test_no_variant_yaml_still_carries_use_prefer() -> None:
    """use_prefer never reached the build (D24); the models now forbid it, and
    no shipped YAML may carry it as a live key."""
    live = [
        str(p.relative_to(_VARIANTS_DIR))
        for p in _VARIANTS_DIR.rglob("*.yaml")
        if any(
            ln.startswith(("use_prefer:", "override_ok:"))
            for ln in p.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert live == []


# --- init systemd/openrc: profile and seat phase ----------------------------


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


# --- apply_portage over the REAL variants/ (F28 regression) ------------------


def _assemble(tmp_path: Path, arch: str, flavor: str, init: str) -> Path:
    resolved = config.load_recipe(arch, flavor, init)
    rootfs = tmp_path / f"{arch}-{flavor}-{init}"
    (rootfs / "etc").mkdir(parents=True)
    apply_portage(rootfs, resolved, variants_dir=_VARIANTS_DIR)
    return rootfs / "etc" / "portage"


@pytest.mark.parametrize("flavor", ["minimal", "kde"])
def test_apply_portage_preserves_the_whole_base_make_conf(tmp_path: Path, flavor: str) -> None:
    # F28 REGRESSION. apply_portage overwrote files with the same path, and the
    # base's 133-line make.conf became the 6-line fragment of init/systemd —
    # taking along everything checked below. The observable symptom was a
    # 6-line make.conf.
    portage = _assemble(tmp_path, "v3", flavor, "systemd")
    text = (portage / "make.conf").read_text(encoding="utf-8")
    assert len(text.splitlines()) > 100
    for var in (
        "FEATURES=",
        "PKGDIR=",
        "DISTDIR=",
        "LLVM_SLOT=",
        "PYTHON_TARGETS=",
        "MAKEOPTS=",
        "L10N=",
        "ACCEPT_KEYWORDS=",
    ):
        assert var in text, f"{var} lost in the composition"
    # and the later fragments are still present
    assert "CPU_FLAGS_X86=" in text  # arch/v3
    assert 'SYSTEMD="boot uki ukify"' in text  # init/systemd


def test_apply_portage_keeps_both_package_use_system_files(tmp_path: Path) -> None:
    # F28 REGRESSION. base and init/systemd both carried `package.use/system`; the
    # second erased the first, from 69 lines to 4. The init delivers `50-systemd`
    # and the base `22-system`, and Portage reads the directory as a union. Since
    # the numbering (2026-09-13) the collision is not even possible anymore: no
    # layer uses the bare `system` name.
    portage = _assemble(tmp_path, "v3", "minimal", "systemd")
    base_lines = (
        (_VARIANTS_DIR / "base/portage/package.use/22-system")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    got = (portage / "package.use" / "22-system").read_text(encoding="utf-8").splitlines()
    assert len(got) == len(base_lines)
    assert "sys-apps/systemd boot ukify policykit" in (
        portage / "package.use" / "50-systemd"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("arch", "flavor", "init", "expect", "reject"),
    [
        # minimal is the console core: no graphical flag at all, and X forced off
        (
            "v3",
            "minimal",
            "systemd",
            {"boot", "uki", "ukify", "-X"},
            {"kde", "qt6", "wayland", "vulkan", "opengl", "X"},
        ),
        ("v3", "kde", "systemd", {"boot", "kde", "qt6", "plymouth", "wayland", "vulkan"}, {"X"}),
        (
            "znver5",
            "kde",
            "openrc",
            {"kde", "qt6", "wayland", "elogind", "udev"},
            {"boot", "uki", "ukify", "X"},
        ),
        ("v3", "gnome", "systemd", {"gtk", "gnome", "wayland"}, {"kde", "qt6", "X", "elogind"}),
    ],
)
def test_assembled_make_conf_composes_use_across_axes(
    tmp_path: Path, arch: str, flavor: str, init: str, expect: set[str], reject: set[str]
) -> None:
    # Counting lines does not prove semantics: the assembled make.conf is SOURCED
    # and the resulting USE checked. Each axis must add its own, and only its own.
    portage = _assemble(tmp_path, arch, flavor, init)
    out = subprocess.run(
        ["bash", "-c", f'. "{portage / "make.conf"}"; printf "%s" "$USE"'],
        capture_output=True,
        text=True,
        check=True,
    )
    flags = set(out.stdout.split())
    # The sentinels MUST be flags that the RECIPE declares, not that the profile
    # provides: here make.conf is sourced in isolation, with no profile at all.
    # `acl` was the original sentinel and started failing the day it was removed
    # from the recipe because it already came from the profile — the test claimed
    # "the curation vanished" when nothing had vanished. These live in the groups
    # of base/portage/make.conf (wayland and vulkan moved to the desktop stage on
    # 2026-09-26, D24).
    assert {"btrfs", "cryptsetup"} <= flags, "the base's curation vanished"
    assert expect <= flags
    assert not (reject & flags)


# --- integrity of the SHIPPED sets (variants/), not of synthetic data --------
#
# These tests exist because the whole suite passed green while
# `bentoo-apps` (then in variants/base/sets/) had already been deleted and the `apps`
# phase still pointed at `@bentoo-apps`: every phase test built a synthetic recipe
# and none looked at what the repository actually ships.


def _shipped_sets() -> dict[str, Path]:
    """Every shipped set, by name: the ``kits/`` library (D25)."""
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
    """Names referenced by ``@name`` inside a set file."""
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
            assert ref in shipped, f"set {name!r} references @{ref}, which does not exist"


@pytest.mark.parametrize("flavor", ["minimal", "kde", "gnome", "wm"])
def test_every_declared_set_is_shipped(flavor: str) -> None:
    recipe = _recipe(flavor)
    shipped = set(_shipped_sets()) | set(recipe.includes)  # a stage's include: is its own set
    for name in recipe.sets:
        assert name in shipped, f"{flavor}: set {name!r} declared but not shipped"


@pytest.mark.parametrize("flavor", ["minimal", "kde", "gnome", "wm"])
def test_every_phase_target_is_reachable(flavor: str) -> None:
    """No phase may point at an ``@set`` that will not be installed."""
    recipe = _recipe(flavor)
    shipped = set(_shipped_sets()) | set(recipe.includes)
    for phase in recipe.phases:
        for target in phase_target(phase, recipe):
            if not target.startswith("@") or target == "@world":
                continue
            name = target[1:]
            assert name in shipped, f"{flavor}/{phase.name}: target {target} does not exist"
            assert name in recipe.sets, (
                f"{flavor}/{phase.name}: target {target} is not in recipe.sets, "
                "so it would not be installed in the rootfs"
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
    # Every init: a stage's init_sets reach a set only under their own init.
    for flavor, init in itertools.product(("minimal", "kde", "gnome", "wm", "toolbox"), _INITS):
        recipe = _recipe(flavor, init)
        pending = list(recipe.sets)
        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            reachable.add(name)
            pending += refs(name)

    # A kit whose every line is catalog-only (#atom) is the binhost's on purpose
    # (kits/README): no image is meant to reach it. One with plain atoms that
    # nobody reaches is still a forgotten kit.
    def binhost_only(name: str) -> bool:
        lines = files[name].read_text(encoding="utf-8").splitlines()
        entries = [
            ln for ln in lines if ln.strip() and not ln.startswith("# ") and ln.strip() != "#"
        ]
        return bool(entries) and all(catalog_entry(ln) is not None for ln in entries)

    orphans = {
        name
        for name in set(files) - reachable - _INTENTIONALLY_UNREACHABLE
        if not binhost_only(name)
    }
    assert not orphans, f"curated sets that nobody installs: {sorted(orphans)}"


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
        witness,  # type: ignore[arg-type]
        recipe,
        emptytree=True,
        snapshot="S",
        fork_points_dir=tmp_path,
    )
    # not the settles (--oneshot) nor the binpkg checks (--pretend)
    stage_emerges = [s for s in witness.seen if "--oneshot" not in s[0] and "--pretend" not in s[0]]
    assert len(stage_emerges) == 4  # base, minimal, desktop, flavor
    # make.conf is REWRITTEN per stage, package.use only ever ADDS -- check both:
    # a kde package.use file present while the base builds is the regression
    # that applying every layer up front would bring back.
    kde_package_use = _VARIANTS_DIR / "flavor" / "kde" / "portage" / "package.use"
    kde_files = {p.name for p in kde_package_use.iterdir()}
    assert ["DESKTOPS=" in mc for _a, mc, _pu in stage_emerges] == [False, False, False, True]
    assert [bool(kde_files & pu) for _a, _mc, pu in stage_emerges] == [False, False, False, True]
    assert stage_emerges[0][0][3] == "--emptytree"


def test_app_alternatives_alone_trade_collision_protect_for_protect_owned(
    tmp_path: Path,
) -> None:
    """F71: app-arch/cpio's pkg_postinst leaves /bin/cpio UNOWNED for
    app-alternatives/cpio to take over, which collision-protect refuses. The
    exception is scoped to app-alternatives/*; the base stays strict (F43)."""
    portage = _assemble(tmp_path, "v3", "minimal", "systemd")
    out = subprocess.run(
        ["bash", "-c", f'. "{portage / "make.conf"}"; printf "%s" "$FEATURES"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "collision-protect" in out.stdout.split()
    mapping = [
        line.split()
        for f in (portage / "package.env").iterdir()
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    # rendered from variants/base/quirks.yaml
    assert ["app-alternatives/*", "quirk-app-alternatives.conf"] in mapping
    env = (portage / "env" / "quirk-app-alternatives.conf").read_text(encoding="utf-8")
    assert 'FEATURES="-collision-protect protect-owned"' in env


def test_seabios_is_taken_prebuilt(tmp_path: Path) -> None:
    """qemu's || ( seabios seabios-bin ) picked the source build, whose
    PYTHON_COMPAT stops at 3.13 and pulled python:3.13 into kde (D15). The
    source package is masked so the prebuilt one satisfies qemu."""
    portage = _assemble(tmp_path, "v3", "kde", "systemd")
    masked = [
        line.strip()
        for f in (portage / "package.mask").iterdir()
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "sys-firmware/seabios" in masked


def test_openjdk_builds_without_ccache(tmp_path: Path) -> None:
    """F73: openjdk's pkg_pretend dies under FEATURES=ccache, and pkg_pretend
    runs for every package before the first build -- it stopped the whole kde
    flavor. It gets its own package.env; the base keeps ccache on."""
    portage = _assemble(tmp_path, "v3", "kde", "systemd")
    mapping = [
        line.split()
        for f in (portage / "package.env").iterdir()
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    # rendered from variants/base/quirks.yaml; F74: its javac server also talks
    # over loopback, which network-sandbox leaves down inside nspawn
    assert ["dev-java/openjdk", "quirk-dev-java_openjdk.conf"] in mapping
    env = (portage / "env" / "quirk-dev-java_openjdk.conf").read_text(encoding="utf-8")
    assert 'FEATURES="-ccache -network-sandbox"' in env
