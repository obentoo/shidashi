"""ADDITIONS to tests/test_phases.py (story 019, task 3.2): ``phase_emerge_argv``'s
``usepkg_exclude``. Self-contained so it runs alone; merge into the existing module
(its ``_recipe`` is the same) when materialized."""

from shidashi.phases import phase_emerge_argv
from shidashi.recipe import Phase, ResolvedRecipe


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="gnome",
        init="systemd",
        profile="default/linux/amd64/23.0/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=("sse4_2",),
        tier=1,
        runnable_on_build_host=True,
        sets=("gnome",),
        phases=(),
        portage_layers=("base", "arch/v3", "flavor/gnome", "init/systemd"),
    )


GNOME = Phase(name="gnome", stage="gnome", sets=("gnome",))


def test_an_exclusion_is_one_usepkg_exclude_per_package_before_the_targets() -> None:
    argv = phase_emerge_argv(
        GNOME, _recipe(), emptytree=False, usepkg_exclude=("x11-libs/vte", "net-libs/nodejs")
    )
    options = [a for a in argv if a.startswith("--usepkg-exclude")]
    assert options == ["--usepkg-exclude=net-libs/nodejs", "--usepkg-exclude=x11-libs/vte"]
    assert argv.index("--usepkg") < argv.index(options[0])
    assert argv.index(options[-1]) < argv.index("@world")  # options before the targets


def test_without_exclusions_the_argv_is_todays_byte_for_byte() -> None:
    today = phase_emerge_argv(GNOME, _recipe(), emptytree=False)
    assert phase_emerge_argv(GNOME, _recipe(), emptytree=False, usepkg_exclude=()) == today
    assert not any(a.startswith("--usepkg-exclude") for a in today)
