"""Generation fingerprint -- what makes a PKGDIR's binpkgs safe to reuse (D26).

Portage does not compare CFLAGS, CHOST or the compiler when it picks a binpkg:
only the package, its version and its USE. A PKGDIR that outlives a toolchain
or flags change therefore hands its old binpkgs to the new build silently --
the 9 binpkgs of 2026-08-30, built with -mtune=generic and the old toolchain,
still sat in the lab's PKGDIR for exactly that reason.

A generation is everything built from one verified stage3 with one toolchain.
Its fingerprint is recorded in the PKGDIR the first time a build uses it, and
every later build compares its own before any emerge and again after every
phase's emerge. The comparison is by ABI, not by exact version (story 016):
gcc counts by its major (its SLOT), glibc may go up but never down, binutils
is recorded but not compared, and the other fields compare exactly. The rules
are derived from the recorded strings, so files written before them still
apply. A mismatch ends the generation -- the build is refused, never quietly
rebuilt on top, and the error names the successor PKGDIR to use instead.
"""

import hashlib
import json
import os
import re
from collections.abc import Mapping
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


#: Fields that compare by exact string (D4).
_EXACT = ("arch", "profile", "common_flags", "chost", "llvm_slot")

_LEADING_DIGITS = re.compile(r"\d+")
_LEADING_RELEASE = re.compile(r"\d+(?:\.\d+)*")


class GenerationMismatchError(FactoryError):
    """The PKGDIR belongs to another generation; ``differences`` names the fields.

    ``successor`` is the PKGDIR to pass as ``--pkgdir`` instead (D9);
    ``after_phase`` is the phase whose emerge a re-check followed, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        differences: dict[str, tuple[str, str]],
        successor: Path,
        after_phase: str | None = None,
    ) -> None:
        super().__init__(message, phase="generation")
        self.differences = differences
        self.successor = successor
        self.after_phase = after_phase


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


def gcc_major(version: str) -> str | None:
    """gcc's major version -- its Portage SLOT -- or ``None`` without a leading digit. Pure.

    The whole leading digit run: ``"9.5.0"`` is ``"9"`` and ``"19.1.0"`` is ``"19"``.
    """
    match = _LEADING_DIGITS.match(version)
    return match.group(0) if match else None


def glibc_release(version: str) -> tuple[int, ...] | None:
    """glibc's numeric release as ints, or ``None`` without a leading digit. Pure.

    The suffix is dropped: ``"2.43-r4"`` and ``"2.43_p1"`` are both ``(2, 43)``,
    and ``(2, 9) < (2, 43)`` -- numeric, not lexical.
    """
    match = _LEADING_RELEASE.match(version)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _gcc_differs(was: str, now: str) -> bool:
    """D1: a new major ends the generation, and so does a step down within one (a
    minor release adds ``GLIBCXX_*`` symbols the binpkgs built since may need);
    unparseable versions compare as strings."""
    was_major, now_major = gcc_major(was), gcc_major(now)
    if was_major is None or now_major is None:
        return was != now
    if was_major != now_major:
        return True
    was_release, now_release = glibc_release(was), glibc_release(now)
    return was_release is not None and now_release is not None and now_release < was_release


def _glibc_differs(was: str, now: str) -> bool:
    """D2: a downgrade or a libc change ends the generation; an upgrade does not."""
    if not was and not now:
        return False
    if not was or not now:
        return True
    was_release, now_release = glibc_release(was), glibc_release(now)
    if was_release is None or now_release is None:
        return was != now
    return now_release < was_release


def abi_differences(
    recorded: GenerationFingerprint, current: GenerationFingerprint
) -> dict[str, tuple[str, str]]:
    """The fields that end the generation, with both strings; ``{}`` if it is the same. Pure.

    Exact for arch, profile, common_flags, chost and llvm_slot (D4); gcc by
    major (D1); glibc may go up, never down (D2); binutils never (D3). In
    model field order.
    """
    was, now = recorded.model_dump(), current.model_dump()
    differences: dict[str, tuple[str, str]] = {}
    for field in GenerationFingerprint.model_fields:
        a, b = was[field], now[field]
        if field in _EXACT:
            differs = a != b
        elif field == "gcc":
            differs = _gcc_differs(a, b)
        elif field == "glibc":
            differs = _glibc_differs(a, b)
        else:
            differs = False
        if differs:
            differences[field] = (a, b)
    return differences


def _raised_floor(
    recorded: GenerationFingerprint, current: GenerationFingerprint
) -> GenerationFingerprint:
    """``recorded`` with gcc and glibc raised to ``current``'s where those are higher. Pure.

    Called on an accepted fingerprint: the generation's binpkgs from now on may be
    built against the newer gcc or glibc, so the floor a later build is checked
    against must be the newer one.
    """
    raised: dict[str, str] = {}
    for field in ("gcc", "glibc"):
        was, now = getattr(recorded, field), getattr(current, field)
        was_release, now_release = glibc_release(was), glibc_release(now)
        if was_release is not None and now_release is not None and now_release > was_release:
            raised[field] = now
    return recorded.model_copy(update=raised) if raised else recorded


def generation_key(fp: GenerationFingerprint) -> tuple[str, ...]:
    """The values the comparison actually compares, binutils left out. Pure.

    gcc as its major and glibc as its release joined by ``.``, each falling
    back to the raw string when it does not parse.
    """
    major = gcc_major(fp.gcc)
    release = glibc_release(fp.glibc)
    return (
        fp.arch,
        fp.profile,
        fp.common_flags,
        fp.chost,
        fp.llvm_slot,
        major if major is not None else fp.gcc,
        ".".join(str(part) for part in release) if release is not None else fp.glibc,
    )


def successor_pkgdir(
    pkgdir: Path, current: GenerationFingerprint, differences: Mapping[str, tuple[str, str]]
) -> Path:
    """The new generation's PKGDIR, a sibling of ``pkgdir`` (D9). Pure: creates nothing.

    ``<name>-gcc<major>``, plus ``-<8 hex of the sha256 of generation_key>``
    when a field other than gcc ended the generation.
    """
    name = f"{pkgdir.name}-gcc{gcc_major(current.gcc) or 'none'}"
    if any(field != "gcc" for field in differences):
        payload = json.dumps(list(generation_key(current)))
        name += f"-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:8]}"
    return pkgdir.parent / name


def check_or_record(
    pkgdir: Path, current: GenerationFingerprint, *, after_phase: str | None = None
) -> bool:
    """Record ``current`` in a PKGDIR that has none, or verify it is the same generation. I/O.

    Returns ``True`` when it recorded (the generation starts here) and
    ``False`` when it accepted. Raises :class:`GenerationMismatchError`,
    naming the successor PKGDIR and ``after_phase`` when given, when the
    PKGDIR was started by another generation, and a :class:`FactoryError`
    naming the file when the recorded fingerprint cannot be read. Never
    replaces the generation: ending one is a decision, taken by pointing the
    build at a new PKGDIR. An accept that brings a higher gcc or glibc raises
    the recorded floor (atomically, same format), so going back below it is
    refused; any other accept leaves the file byte for byte.
    """
    path = pkgdir / FINGERPRINT_FILE
    if not path.is_file():
        pkgdir.mkdir(parents=True, exist_ok=True)
        path.write_text(current.model_dump_json(indent=1) + "\n", encoding="utf-8")
        return True
    try:
        recorded = GenerationFingerprint.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, pydantic.ValidationError) as err:
        raise FactoryError(
            f"cannot read the generation fingerprint {path}: {err}", phase="generation"
        ) from err
    differences = abi_differences(recorded, current)
    if not differences:
        raised = _raised_floor(recorded, current)
        if raised != recorded:
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(raised.model_dump_json(indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        return False
    successor = successor_pkgdir(pkgdir, current, differences)
    detail = "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in differences.items())
    where = f", after phase {after_phase!r}" if after_phase is not None else ""
    tail = (
        f". The binpkgs phase {after_phase!r} wrote may already belong to the new generation."
        if after_phase is not None
        else ""
    )
    raise GenerationMismatchError(
        f"{pkgdir} belongs to another generation ({detail}){where}. Its binpkgs were "
        "built with a different toolchain or flags, and Portage would reuse them without "
        f"checking. Start a new generation: --pkgdir {successor}{tail}",
        differences=differences,
        successor=successor,
        after_phase=after_phase,
    )
