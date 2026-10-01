"""Generation fingerprint -- what makes a PKGDIR's binpkgs safe to reuse (D26).

Portage does not compare CFLAGS, CHOST or the compiler when it picks a binpkg:
only the package, its version and its USE. A PKGDIR that outlives a toolchain
or flags change therefore hands its old binpkgs to the new build silently --
the 9 binpkgs of 2026-08-30, built with -mtune=generic and the old toolchain,
still sat in the lab's PKGDIR for exactly that reason.

A generation is everything built from one verified stage3 with one toolchain.
Its fingerprint is recorded in the PKGDIR the first time a build uses it, and
every later build compares its own before any emerge: a mismatch ends the
generation -- the build is refused, never quietly rebuilt on top.
"""

import json
import re
from pathlib import Path

import pydantic

from shidashi.bootstrap import natural_key
from shidashi.phases import FactoryError
from shidashi.recipe import ResolvedRecipe

#: Beside the binpkgs; Portage ignores dotfiles in PKGDIR.
FINGERPRINT_FILE = ".shidashi-generation.json"

#: category/name of each toolchain package whose installed version counts.
_TOOLCHAIN = {
    "gcc": "sys-devel/gcc",
    "binutils": "sys-devel/binutils",
    "glibc": "sys-libs/glibc",
}

_ASSIGNMENT = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"([^"$]*)"\s*(?:#.*)?$', re.MULTILINE)


class GenerationMismatchError(FactoryError):
    """The PKGDIR belongs to another generation; ``differences`` names the fields."""

    def __init__(self, message: str, *, differences: dict[str, tuple[str, str]]) -> None:
        super().__init__(message, phase="generation")
        self.differences = differences


class GenerationFingerprint(pydantic.BaseModel):
    """The inputs a binpkg's code depends on but Portage does not check (D26)."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")
    arch: str
    profile: str
    common_flags: str
    chost: str
    llvm_slot: str
    gcc: str
    binutils: str
    glibc: str


def installed_version(rootfs: Path, cp: str) -> str:
    """Newest installed version of ``cp`` in the rootfs's vdb; ``""`` if none. Pure I/O.

    Toolchain packages are slotted and the bootstrap leaves the old slot in
    place (gcc-15 next to gcc-16), so "newest" is the one the bootstrap
    switched to.
    """
    category, name = cp.split("/")
    vdb = rootfs / "var" / "db" / "pkg" / category
    if not vdb.is_dir():
        return ""
    prefix = f"{name}-"
    versions = [
        d.name[len(prefix) :]
        for d in vdb.iterdir()
        if d.is_dir() and d.name.startswith(prefix) and d.name[len(prefix) :][:1].isdigit()
    ]
    return max(versions, key=natural_key) if versions else ""


def make_conf_value(make_conf: str, key: str) -> str:
    """The LAST literal assignment of ``key`` -- the shell's winner. Pure.

    Only literal values count: an assignment that expands another variable is
    not a value this module can compare, and the keys read here are literals.
    """
    values = [m.group(2) for m in _ASSIGNMENT.finditer(make_conf) if m.group(1) == key]
    return values[-1] if values else ""


def fingerprint(rootfs: Path, recipe: ResolvedRecipe) -> GenerationFingerprint:
    """The generation fingerprint of a rootfs whose toolchain is bootstrapped. Pure I/O.

    Read from the host side: the assembled ``/etc/portage/make.conf`` and the
    vdb. ``LLVM_SLOT`` comes from the configuration, not the vdb, because LLVM
    is first built by the base stage -- after the fingerprint is taken.
    """
    make_conf_path = rootfs / "etc" / "portage" / "make.conf"
    make_conf = make_conf_path.read_text(encoding="utf-8") if make_conf_path.is_file() else ""
    versions = {field: installed_version(rootfs, cp) for field, cp in _TOOLCHAIN.items()}
    return GenerationFingerprint(
        arch=recipe.arch,
        profile=recipe.profile,
        common_flags=recipe.common_flags,
        chost=make_conf_value(make_conf, "CHOST"),
        llvm_slot=make_conf_value(make_conf, "LLVM_SLOT"),
        **versions,
    )


def check_or_record(pkgdir: Path, current: GenerationFingerprint) -> bool:
    """Record ``current`` in a PKGDIR that has none, or verify it matches. I/O.

    Returns ``True`` when it recorded (the generation starts here). Raises
    :class:`GenerationMismatchError` when the PKGDIR was started by another
    generation. Never overwrites: ending a generation is a decision, taken by
    pointing the build at a new PKGDIR.
    """
    path = pkgdir / FINGERPRINT_FILE
    if not path.is_file():
        pkgdir.mkdir(parents=True, exist_ok=True)
        path.write_text(current.model_dump_json(indent=1) + "\n", encoding="utf-8")
        return True
    recorded = GenerationFingerprint.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if recorded == current:
        return False
    was, now = recorded.model_dump(), current.model_dump()
    differences = {k: (was[k], now[k]) for k in was if was[k] != now[k]}
    detail = "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in differences.items())
    raise GenerationMismatchError(
        f"{pkgdir} belongs to another generation ({detail}). Its binpkgs were built "
        "with a different toolchain or flags, and Portage would reuse them without "
        "checking. Start a new generation: point --pkgdir at an empty directory.",
        differences=differences,
    )
