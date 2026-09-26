"""A minimal, VALID ``variants/`` tree for the CLI tests, in the stage format (D24).

Five test modules used to carry their own copy of this tree, in the pre-D24
format -- a phases list in base.yaml, ``use_prefer``, ``override_ok`` -- and
all five broke together when the model changed. One writer, one format.

The tree::

    base/base.yaml               stage base (emptytree), sets [base]
    arch/v3/recipe.yaml          + portage/make.conf with the compile knobs
    minimal/minimal.yaml         after base, ships, sets [extra-system]
    desktop/desktop.yaml         after minimal                       (kde=True)
    flavor/kde/recipe.yaml       after desktop, ships, sets [kde]    (kde=True)
    flavor/broken/recipe.yaml    declares another stage's name       (broken=True)
    init/systemd/recipe.yaml     profile_suffix systemd
    init/openrc/recipe.yaml      no suffix, a `seat` phase prepended  (openrc=True)
"""

from pathlib import Path

_FILES: dict[str, str] = {
    "base/base.yaml": (
        "profile_base: default/linux/amd64/23.0/no-multilib\n"
        "stage: base\nupdate: emptytree\nsets: [base]\n"
    ),
    "arch/v3/recipe.yaml": "arch: v3\ntier: 1\nrunnable_on_build_host: true\n",
    # The compile knobs live in the arch layer's make.conf, not in recipe.yaml.
    "arch/v3/portage/make.conf": (
        'COMMON_FLAGS="-O2 -march=x86-64-v3 -pipe"\n'
        'GOAMD64="v3"\n'
        'RUSTFLAGS="-C target-cpu=x86-64-v3"\n'
        'CPU_FLAGS_X86="sse4_2 avx2"\n'
    ),
    "minimal/minimal.yaml": "stage: minimal\nafter: base\nships: true\nsets: [extra-system]\n",
    "init/systemd/recipe.yaml": "init: systemd\nprofile_suffix: systemd\n",
}
_KDE: dict[str, str] = {
    "desktop/desktop.yaml": "stage: desktop\nafter: minimal\n",
    "flavor/kde/recipe.yaml": "stage: kde\nafter: desktop\nships: true\nsets: [kde]\n",
}
_OPENRC: dict[str, str] = {
    "init/openrc/recipe.yaml": (
        'init: openrc\nprofile_suffix: ""\nphases_prepend:\n  - name: seat\n'
    ),
}
# A flavor whose file names another stage: load_chain refuses it with a
# RecipeChainError -- the structural error the CLI must report without a traceback.
_BROKEN: dict[str, str] = {
    "flavor/broken/recipe.yaml": "stage: not-broken\nafter: desktop\nships: true\n",
}


def write_variants(
    root: Path, *, kde: bool = False, openrc: bool = False, broken: bool = False
) -> Path:
    """Write the tree under ``root`` and return it."""
    files = dict(_FILES)
    if kde or broken:
        files |= _KDE
    if openrc:
        files |= _OPENRC
    if broken:
        files |= _BROKEN
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
