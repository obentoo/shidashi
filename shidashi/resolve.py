"""Shidashi's *pretend-resolve* pipeline (OVERVIEW §18) -- the heart of story 002.

Overlays the recipe's portage layers + the pinned repos onto a seeded rootfs,
runs ``emerge --pretend --emptytree @world`` inside a ``systemd-nspawn`` and
parses the output into a :class:`PretendReport` (package list + cycle-break
suggestions that feed the manual curation of ``use_break``, §18.7).

The pure logic (layer mapping, repos.conf parsing, emerge output parsing) is
unit-tested in CI; the privileged orchestration (seed/extract/nspawn) is
host-gated. ``emerge`` runs as a subprocess *inside* the container -- never
through ``import portage``.
"""

import configparser
import os
import re
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shidashi import config, seed
from shidashi.container import CommandResult, Container
from shidashi.recipe import INCLUDE_SET_PREFIX, ResolvedRecipe

_STRICT = ConfigDict(frozen=True, extra="forbid")

# Where the container finds its repos (the pinned trees are bound RO here).
_REPOS_ROOT = Path("/var/db/repos")


class ResolveError(Exception):
    """Failure of the pretend-resolve pipeline (non-root, missing repo, hard-conflict).

    Optionally carries ``raw_output`` -- the raw ``emerge`` output when the error
    is a genuine dependency conflict (hard-conflict, R5.4), so that the CLI can
    surface it to the user. Absent (``None``) in every other case.
    """

    def __init__(self, message: str, *, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class CycleBreak(BaseModel):
    """A cycle-break suggestion extracted from the emerge output (R5.2).

    Frozen pydantic. ``atom`` is the package, ``flag`` the suggested USE flag,
    ``enable`` the sign (``True`` = ``+flag``, ``False`` = ``-flag``) and
    ``raw_line`` the raw source line (traceability for the §18.7 curation).
    """

    model_config = _STRICT
    atom: str
    flag: str
    enable: bool
    raw_line: str


class PretendReport(BaseModel):
    """Typed result of a ``shidashi pretend`` (R1.1/R1.3/R5.2). Frozen pydantic."""

    model_config = _STRICT
    arch: str
    flavor: str
    init: str
    packages: tuple[str, ...]
    cycle_breaks: tuple[CycleBreak, ...]
    raw_output: str


# --- layering (R3.1) ---------------------------------------------------------


def _layer_dirs(
    recipe: ResolvedRecipe, variants_dir: Path, layers: tuple[str, ...] | None = None
) -> list[Path]:
    """Map each ``portage_layers`` entry → ``variants_dir/<entry>/portage``.

    The entries are raw layer values (``"base"``, ``"arch/v3"`` …) **without** a
    ``variants/`` prefix -- no double join. **Pure.**
    """
    chosen = recipe.portage_layers if layers is None else layers
    return [variants_dir / entry / "portage" for entry in chosen]


_MAKE_CONF = "make.conf"


#: Files in the kits library that are documentation, not sets.
_KIT_NON_SETS = frozenset({"README", "README.md"})


def kit_index(kits_dir: Path) -> dict[str, Path]:
    """Map every set name in the kits library to its file (D25).

    The library is ``kits/<category>/<set>``; the category is for people only.
    Portage's set namespace is flat (``/etc/portage/sets/<name>``), so a name
    must be unique across ALL categories -- two files with one name would
    silently install whichever the walk met last. That is an error here, raised
    with both paths.
    """
    index: dict[str, Path] = {}
    for path in sorted(kits_dir.rglob("*")):
        if not path.is_file() or path.name in _KIT_NON_SETS or path.name.startswith("."):
            continue
        if path.name in index:
            raise ResolveError(
                f"set {path.name!r} is defined twice in the kits library: "
                f"{index[path.name].relative_to(kits_dir)} and {path.relative_to(kits_dir)}"
            )
        index[path.name] = path
    return index


def install_sets(rootfs: Path, recipe: ResolvedRecipe) -> None:
    """Install the recipe's sets into ``${rootfs}/etc/portage/sets/`` (R6.4).

    Every set lives in the ``variants/kits/<category>/<name>`` library (D25); the
    layers only declare which ones they install. There is no longer any
    same-name overriding between layers -- a name is unique in the library
    (:func:`kit_index`), and per-flavor tuning is explicit: declare a set, or
    ``exclude:`` atoms.

    Three behaviors that are not obvious:

    **Transitive references.** A set may contain ``@other-set`` and Portage
    expands it recursively (verified 2026-09-09). So installing ``@base``
    requires also installing the sets it references, or ``@base`` resolves to a
    missing target INSIDE the container -- far from the cause. The walk follows
    the ``@refs`` until closed.

    **``recipe.exclude``.** The atoms excluded by the flavor are removed from
    the lists as they are written. The base is the rule; the flavor is the
    exception. Note that this does not stop the atom from coming in as a
    DEPENDENCY of another package -- it is "I do not ask for it", not "I forbid
    it".

    **Fail loudly.** A declared set with no file in the library is a curation
    error and raises :class:`ResolveError`. It used to be silently ignored, and
    the failure only showed up in ``emerge``.
    """
    dest_dir = rootfs / "etc" / "portage" / "sets"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name, lines in set_closure(recipe).items():
        (dest_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_closure(recipe: ResolvedRecipe) -> dict[str, list[str]]:
    """Every set ``recipe`` installs -- its own and each ``@ref`` they reach -- with
    the lines it is written with (``exclude:`` applied). I/O (reads the kits).

    Shared by :func:`install_sets`, which writes them, and :func:`world_atoms`,
    which flattens them: what the image asks for and what its world lists cannot
    drift apart.
    """
    closure: dict[str, list[str]] = {}
    for kit in kit_view(recipe):
        kept = list(kit.lines)
        if kit.catalog:
            kept.insert(
                0, "# shidashi: catalog only (binhost, not this image): " + " ".join(kit.catalog)
            )
        if kit.dropped:
            kept.insert(
                0,
                f"# shidashi: excluded by flavor/{recipe.flavor}: " + " ".join(sorted(kit.dropped)),
            )
        closure[kit.name] = kept
    return closure


#: A catalog-only kit line: ``#`` glued to an atom or ``@ref`` -- ``#dev-lang/rust``,
#: ``#@kde-apps``. The kits are the binhost's whole library; such a line is built for
#: the binhost and installed by no image. A comment with a space (``# text``) is
#: prose. On an ``@ref`` it keeps the whole referenced kit out of the images.
CATALOG_LINE = re.compile(r"^#(@[\w.+-]+|[!<>=~]*[\w.+-]+/\S+)")


def catalog_entry(line: str) -> str | None:
    """The atom or ``@ref`` of a catalog-only line, else ``None``. Pure."""
    match = CATALOG_LINE.match(line)
    return match.group(1) if match else None


@dataclass(frozen=True)
class KitView:
    """One set of an image as the image gets it: what it keeps, what ``exclude:``
    took out, what is catalog-only, and the ``@refs`` it pulls in."""

    name: str
    #: The set's lines, excluded atoms removed (comments and ``@refs`` kept).
    lines: tuple[str, ...]
    #: Atoms of this set that ``exclude:`` removed, in file order.
    dropped: tuple[str, ...]
    #: Sets this one references, without the ``@`` (catalog-only refs left out).
    refs: tuple[str, ...]
    #: Catalog-only atoms and ``@refs`` (``#atom``): in the binhost, in no image.
    catalog: tuple[str, ...] = ()
    #: A stage's ``include:`` (``@include-<stage>``), not a kit of the library.
    included: bool = False

    @property
    def atoms(self) -> tuple[str, ...]:
        """The package atoms this set keeps: no comments, no ``@refs``."""
        tokens = (ln.split("#", 1)[0].split() for ln in self.lines)
        return tuple(t[0] for t in tokens if t and not t[0].startswith("@"))


def _optional_excludes(recipe: ResolvedRecipe) -> frozenset[str]:
    """The excludes that may match nothing in ``recipe``'s chain. Pure.

    The init's excludes cover whole images. A chain that does not end in one
    -- the base or the desktop viewed alone, the toolbox -- may lack what they
    take out: ntp, say, comes in a minimal kit, and the toolbox is base →
    toolbox. Every other exclude must match, image or not.
    """
    if recipe.phases and recipe.phases[-1].ships:
        return frozenset()
    return frozenset(a for a, layer in recipe.exclude_origin.items() if layer.startswith("init/"))


def kit_view(recipe: ResolvedRecipe) -> list[KitView]:
    """Every set ``recipe`` installs, depth first: each set is followed by the
    ``@refs`` it reaches, so ``@base`` reads with its kits under it. I/O (reads
    the kits).

    The one walk of the kits: :func:`set_closure` writes it, ``shidashi world
    <image>`` prints it. An exclude that matches nothing is an error, except
    :func:`_optional_excludes`.
    """
    kits = config.kits_dir()
    index = kit_index(kits)
    clashing = sorted(n for n in index if n.startswith(INCLUDE_SET_PREFIX))
    if clashing:
        raise ResolveError(
            f"kit {clashing[0]!r}: the {INCLUDE_SET_PREFIX}* names belong to stages' include:"
        )
    excluded = frozenset(recipe.exclude)
    seen: dict[str, KitView] = {}
    matched: set[str] = set()
    pending = list(recipe.sets)
    while pending:
        name = pending.pop(0)
        if name in seen:
            continue
        if name in recipe.includes:
            atoms = recipe.includes[name]
            seen[name] = KitView(name, atoms, (), (), included=True)
            continue
        src = index.get(name)
        if src is None:
            raise ResolveError(
                f"set {name!r} declared in the recipe but missing from the library {kits}"
            )
        kept: list[str] = []
        dropped: list[str] = []
        refs: list[str] = []
        catalog: list[str] = []
        for line in src.read_text(encoding="utf-8").splitlines():
            entry = catalog_entry(line)
            if entry is not None:
                if entry in excluded:
                    raise ResolveError(
                        f"exclude: {entry} is already catalog-only in kit {name!r} "
                        f"(`#{entry}`): no image installs it, drop the exclude"
                    )
                catalog.append(entry)
                continue
            token = line.split("#", 1)[0].split()
            if token and token[0].startswith("@"):
                refs.append(token[0][1:])
            if token and token[0] in excluded:
                dropped.append(token[0])
                matched.add(token[0])
                continue
            kept.append(line)
        pending[0:0] = refs
        seen[name] = KitView(name, tuple(kept), tuple(dropped), tuple(refs), tuple(catalog))
    # an exclude that matches nothing is a typo, or a package the chain never
    # had: silently ignored, it would leave in the image what it meant to take out
    unmatched = sorted(excluded - matched - _optional_excludes(recipe))
    if unmatched:
        raise ResolveError(
            f"exclude: {', '.join(unmatched)} is in no set of the {recipe.flavor} chain "
            f"({', '.join(recipe.stages)}): a typo, or a package these stages never had"
        )
    # an include puts back what the chain took out; anything else is a typo, or
    # a package that belongs in a kit (D25), not in a recipe
    taken_out = {a for kit in seen.values() for a in (*kit.dropped, *kit.catalog)}
    for name, atoms in recipe.includes.items():
        stray = [a for a in atoms if a not in taken_out]
        if stray:
            raise ResolveError(
                f"include: {', '.join(stray)} ({name}) is neither excluded by an earlier "
                f"stage nor catalog-only (#atom) in a kit of the {recipe.flavor} chain"
            )
    return list(seen.values())


def world_atoms(recipe: ResolvedRecipe) -> tuple[str, ...]:
    """The packages the image ASKS for: every atom of its set closure, sorted. I/O.

    The other end of the composition (kits -> a stage's sets -> this list): what
    ``/var/lib/portage/world`` holds on the image and ``variants/<stage>/world.<init>``
    in the repository. Explicit choices only -- the ~1300 dependencies stay out,
    or depclean could never remove one that became an orphan.
    """
    atoms: set[str] = set()
    for lines in set_closure(recipe).values():
        for line in lines:
            token = line.split("#", 1)[0].split()
            if token and not token[0].startswith("@"):
                atoms.add(token[0])
    return tuple(sorted(atoms))


def apply_portage(
    rootfs: Path,
    recipe: ResolvedRecipe,
    *,
    variants_dir: Path,
    layers: tuple[str, ...] | None = None,
    host_jobs: bool = True,
) -> None:
    """Compose the layers' ``portage/`` trees into ``${rootfs}/etc/portage`` (R3.1).

    ``host_jobs`` appends this host's ``--jobs`` (:func:`_jobs_override`). The
    assembler turns it off: its rootfs IS the image, and the build host's job
    counts have no place in the ``make.conf`` a user installs.

    The layers are walked in base→arch→flavor→init order, and there are exactly
    **two** regimes, because Portage reads the two kinds of file differently:

    - ``make.conf`` is ONE file, read by the shell. The layers carry *fragments*
      (``arch/v3`` only CPU knobs, ``init/systemd`` only the SYSTEMD group), so
      it is **concatenated** in layer order. Inside the assembled file the shell
      rule applies: the last assignment of a variable wins -- which is exactly
      the specialization effect wanted from the arch axis.
    - Everything else (``package.use/``, ``package.mask/``, ``env/`` …) are
      DIRECTORIES that Portage reads as a UNION. Two layers delivering the same
      path do not combine: one would erase the other. That is always a curation
      error, and here it becomes a :class:`ResolveError` instead of a silent loss.

    The previous version copied everything with overwrite, ``make.conf``
    included. Measured for ``v3 × minimal × systemd``: the base's 133-line
    ``make.conf`` became the 6-line fragment of ``init/systemd``, taking along
    ``FEATURES``, ``PKGDIR``, ``LLVM_SLOT``, ``PYTHON_TARGETS``, ``MAKEOPTS``,
    ``L10N``, ``CFLAGS`` and ``CHOST``; and ``package.use/system`` dropped from
    69 lines to 4 (lab 2026-08-30, F28).

    ``layers`` chooses WHICH layers to compose -- those of a phase
    (``phase.layers``, D24), which grow along the chain; the default is the
    recipe's final list.

    A MISSING layer raises :class:`ResolveError` -- it is a wrong name. A layer
    that exists but has no ``portage/`` is a stage that configures nothing
    (``minimal`` and ``desktop`` today, D24) and simply contributes nothing.
    """
    dest = rootfs / "etc" / "portage"
    dest.mkdir(parents=True, exist_ok=True)

    chosen = recipe.portage_layers if layers is None else layers
    layer_dirs = _layer_dirs(recipe, variants_dir, chosen)
    for layer_dir in layer_dirs:
        if not layer_dir.parent.is_dir():
            raise ResolveError(
                f"missing layer: {layer_dir.parent} (recipe "
                f"{recipe.arch}×{recipe.flavor}×{recipe.init})"
            )

    provider: dict[str, str] = {}
    make_conf_parts: list[tuple[str, str]] = []

    for layer, layer_dir in zip(chosen, layer_dirs, strict=True):
        if not layer_dir.is_dir():
            continue  # a stage with nothing to configure
        for item in sorted(layer_dir.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(layer_dir).as_posix()
            if rel == _MAKE_CONF:
                make_conf_parts.append((layer, item.read_text(encoding="utf-8")))
                continue
            if rel in provider:
                raise ResolveError(
                    f"path collision between layers at etc/portage/{rel}: "
                    f"{provider[rel]!r} and {layer!r} deliver the same file, and the "
                    f"second would erase the first. Portage reads these directories "
                    f"as a union -- rename one of the two (convention: a numeric "
                    f"prefix, e.g. '50-{Path(rel).name}')"
                )
            provider[rel] = layer
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())

    _write_quirks(dest, variants_dir, chosen, provider)

    jobs = _jobs_override() if host_jobs else None
    if jobs is not None:
        make_conf_parts.append((f"runtime ({_JOBS_ENV})", _jobs_make_conf(jobs)))
    if make_conf_parts:
        (dest / _MAKE_CONF).write_text(_assemble_make_conf(make_conf_parts), encoding="utf-8")


def apply_rootfs(
    rootfs: Path,
    recipe: ResolvedRecipe,
    *,
    variants_dir: Path,
    layers: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Copy each layer's ``rootfs/`` tree over ``rootfs``; return what it wrote.

    Files outside ``/etc/portage`` that the image needs from the start -- today
    only ``base/rootfs/etc/locale.gen``, the curated locale list the bootstrap's
    ``locale-gen`` reads (the stage3 ships every entry commented out). The lab
    did this at reseed (``apply-rootfs.sh``); the pipeline never did.

    Unlike ``portage/`` these are whole files, not union directories, so a later
    layer simply wins. A layer without ``rootfs/`` contributes nothing. Modes are
    kept (``copy2``); ownership is the caller's (root) and so stays root's.
    """
    chosen = recipe.portage_layers if layers is None else layers
    written: list[str] = []
    for layer in chosen:
        tree = variants_dir / layer / "rootfs"
        if not tree.is_dir():
            continue
        for item in sorted(tree.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(tree)
            target = rootfs / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            written.append("/" + rel.as_posix())
    return tuple(dict.fromkeys(written))


def _write_quirks(
    dest: Path, variants_dir: Path, layers: tuple[str, ...], provider: dict[str, str]
) -> None:
    """Render every layer's ``quirks.yaml`` into ``dest`` (:mod:`shidashi.quirks`).

    The entries of all layers in force are merged and rendered once. An atom in
    two layers' registries is a curation error, like two layers delivering the
    same file: it raises instead of letting one silently win.
    """
    from shidashi.quirks import Quirk, QuirksError, load_quirks, render_quirks

    merged: list[Quirk] = []
    sources: list[str] = []
    owner: dict[str, str] = {}
    for layer in layers:
        path = variants_dir / layer / "quirks.yaml"
        try:
            quirks = load_quirks(path)
        except QuirksError as err:
            raise ResolveError(str(err)) from err
        for q in quirks:
            if q.atom in owner:
                raise ResolveError(
                    f"quirk {q.atom!r} is declared by both {owner[q.atom]!r} and {layer!r}"
                )
            owner[q.atom] = layer
        if quirks:
            merged.extend(quirks)
            sources.append(f"variants/{layer}/quirks.yaml")
    if not merged:
        return
    for rel, text in render_quirks(tuple(merged), source=", ".join(sources)).items():
        if rel in provider:
            raise ResolveError(
                f"etc/portage/{rel} is rendered from quirks.yaml but layer "
                f"{provider[rel]!r} also delivers it"
            )
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


_JOBS_ENV = "SHIDASHI_JOBS"


def _jobs_override() -> int | None:
    """``SHIDASHI_JOBS`` (``--jobs``) as a positive int; ``None`` when unset.

    How many jobs THIS build host runs is not part of the recipe, so it is not a
    layer: it is appended after every layer, where the shell's last-assignment
    rule makes it win over the base's MAKEOPTS in every phase, and it extends
    EMERGE_DEFAULT_OPTS (:func:`_jobs_make_conf`). Validated because
    the value is written into a shell-sourced file.
    """
    raw = os.environ.get(_JOBS_ENV)
    if raw is None:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ResolveError(f"{_JOBS_ENV} must be a positive integer, got {raw!r}")
    return int(raw)


def _jobs_make_conf(jobs: int) -> str:
    """The ``make.conf`` lines of ``--jobs N``. Pure.

    ``MAKEOPTS`` is the jobs inside one package's build; ``EMERGE_DEFAULT_OPTS``
    the packages built at once. Both carry ``--load-average N``: emerge starts
    another package, and make another job, only while the load is under N, so
    N packages of N make jobs each do not run N² compilers.
    """
    return (
        f'MAKEOPTS="-j{jobs} -l{jobs}"\n'
        f'EMERGE_DEFAULT_OPTS="${{EMERGE_DEFAULT_OPTS}} --jobs={jobs} --load-average={jobs}"\n'
    )


def _assemble_make_conf(parts: list[tuple[str, str]]) -> str:
    """Concatenate the ``make.conf`` fragments, marking where each one came from.

    The per-layer header is not decoration: the assembled file is what ``emerge
    --info`` reflects, and without it there is no way to tell which layer an
    assignment came from -- nor that it overrode another one further up.
    """
    out = [
        "# GENERATED by shidashi.resolve.apply_portage — do not edit here.",
        "# Assembled from one fragment per recipe layer, in layer order. The shell",
        "# rule applies: for a variable assigned twice, the LAST assignment wins.",
        "",
    ]
    for layer, text in parts:
        out.append(f"# {'=' * 74}")
        out.append(f"# layer: {layer}")
        out.append(f"# {'=' * 74}")
        out.append(text.rstrip("\n"))
        out.append("")
    return "\n".join(out) + "\n"


# --- repo binding (R3.2, R3.3, R6.3) -----------------------------------------


def bind_repos(repos_conf_dir: Path, *, pinned: Mapping[str, Path]) -> list[tuple[Path, Path]]:
    """Produce RO pin→container binds for every declared repo (R3.2/R3.3, D26).

    ``repos.conf`` is a **directory** (eselect-repo style): iterate its ``*.conf``
    files and collect the ``[<name>]`` stanzas (stdlib ``configparser``). Each
    declared repo is bound from its pin in ``pinned`` (the ::gentoo snapshot and
    the overlays' commits, :func:`shidashi.tree.pinned_repos`) to
    ``_REPOS_ROOT/<name>`` in the container.

    The host's own ``/var/db/repos`` is never read: a declared repo without a
    pin raises :class:`ResolveError`, so a build does not depend on when (or
    whether) the host synced, nor on the host being Gentoo.
    """
    declared: list[str] = []
    for conf in sorted(repos_conf_dir.glob("*.conf")):
        parser = configparser.ConfigParser()
        parser.read(conf, encoding="utf-8")
        declared.extend(section for section in parser.sections())

    pairs: list[tuple[Path, Path]] = []
    for name in declared:
        if name not in pinned:
            raise ResolveError(
                f"repo {name!r} is declared in repos.conf but not pinned; pin it in "
                "seeds/overlays.toml (::gentoo is pinned by seeds/gentoo.toml)"
            )
        host_path = pinned[name]
        if not host_path.is_dir():
            raise ResolveError(f"pinned repo {name!r} missing at {host_path}")
        pairs.append((host_path, _REPOS_ROOT / name))
    return pairs


# --- emerge output parsing (R5.2) — pure -------------------------------------


def _atom_from_ebuild_line(stripped: str, prefix: str = "[ebuild") -> str | None:
    """Extract ``cat/pkg-version`` from an already-stripped ``[ebuild ...]`` line. Pure.

    Shared core of the ``[ebuild ...]`` matcher (R5.2 / R3.4 / R4.1): requires
    ``stripped`` to start with ``[ebuild``, takes the first token after the ``]``
    and drops the slot/repo suffix (``:slot::repo``). Returns ``None`` when the
    line does not match (does not start with ``[ebuild``, no ``]`` or no token).
    Reused by :func:`_iter_atom_lines` and by :func:`shidashi.phases.parse_emerge_plan`.
    """
    if not stripped.startswith(prefix):  # "[binary" for a binpkg install
        return None
    after = stripped.split("]", 1)
    if len(after) != 2:
        return None
    tokens = after[1].split()
    if not tokens:
        return None
    return tokens[0].split(":", 1)[0]


def _iter_atom_lines(emerge_output: str) -> Iterator[str]:
    """Iterate the ``cat/pkg-version`` atoms of the ``[ebuild ...]`` lines. Pure.

    Shared matcher (R5.2 / R3.4): for each line whose stripped form starts with
    ``[ebuild``, take the first token after the ``]`` and drop the slot/repo
    suffix (``:slot::repo``), yielding ``cat/pkg-version``. Consumed both by
    :func:`parse_packages` (resolve) and by
    :func:`shidashi.phases.parse_built_atoms`. Delegates line matching to
    :func:`_atom_from_ebuild_line`.
    """
    for line in emerge_output.splitlines():
        atom = _atom_from_ebuild_line(line.strip())
        if atom is not None:
            yield atom


def parse_packages(emerge_output: str) -> tuple[str, ...]:
    """Extract the list of resolved atoms from the ``[ebuild ...]`` lines (R5.2). Pure.

    For each line starting with ``[ebuild``, take the first token after the
    ``]`` and drop the slot/repo suffix (``:slot::repo``), yielding
    ``cat/pkg-version``. Output without ``[ebuild ...]`` lines → empty tuple.
    Delegates line matching to :func:`_iter_atom_lines`.
    """
    return tuple(_iter_atom_lines(emerge_output))


def parse_cycle_breaks(emerge_output: str) -> tuple[CycleBreak, ...]:
    """Extract "Change USE" suggestions for circular dependencies (R5.2). Pure.

    Looks for lines of the form ``- <atom> (Change USE: <±flag>)`` and maps each
    one to a :class:`CycleBreak` (atom, flag, sign). Output without suggestions →
    empty tuple. It is the curation instrument for ``use_break`` (§18.7).
    """
    breaks: list[CycleBreak] = []
    for line in emerge_output.splitlines():
        stripped = line.strip()
        marker = "(Change USE:"
        if not stripped.startswith("-") or marker not in stripped:
            continue
        # "- media-libs/libsdl2-2.30.5 (Change USE: -pipewire)"
        head, _, tail = stripped.partition(marker)
        atom = head.lstrip("-").strip()
        change = tail.rstrip(")").strip()  # "-pipewire" / "+sdl"
        if not change or change[0] not in "+-":
            continue
        enable = change[0] == "+"
        flag = change[1:].strip()
        if not atom or not flag:
            continue
        breaks.append(CycleBreak(atom=atom, flag=flag, enable=enable, raw_line=stripped))
    return tuple(breaks)


# --- run + orchestration -----------------------------------------------------


def run_pretend(container: Container) -> CommandResult:
    """Run ``emerge --pretend --emptytree @world`` in the container (R5.1).

    Uses ``check=False`` -- the exit semantics (cycle vs hard-conflict) are
    decided by :func:`pretend_resolve`. Captures stdout+stderr in the result.
    """
    return container.run(["emerge", "--pretend", "--emptytree", "@world"], check=False)


def pretend_resolve(
    arch: str,
    flavor: str,
    init: str,
    *,
    download: bool = True,
    keep: bool = False,
) -> PretendReport:
    """Orchestrate merge→seed→layer→bind→nspawn→parse into a report (R5.1/R5.3/R5.4).

    Privilege guard (R6.1): if not root, raise an actionable
    :class:`ResolveError` **before** any work -- it does not try to escalate
    privileges. A reported cycle is a success (exit 0): it becomes
    ``cycle_breaks`` in the report. A hard-conflict (non-zero exit with no cycle
    suggestions) raises :class:`ResolveError` carrying ``raw_output`` (R5.4).
    """
    if os.geteuid() != 0:
        raise ResolveError(
            "shidashi pretend requires root (systemd-nspawn + stage3 extraction); "
            "run as root -- Shidashi does not escalate privileges by itself"
        )

    # a local import avoids an import cycle (cli imports resolve in group 6).
    from shidashi.cli import _resolve

    recipe = _resolve(arch, flavor, init)

    variants_dir = config.variants_dir()
    scratch = config.scratch_dir()
    rootfs = scratch / f"{arch}-{flavor}-{init}" / "rootfs"

    pointer = seed.load_pointer(init, seeds_dir=config.seeds_dir())
    tarball = seed.fetch_stage3(pointer, cache_dir=config.cache_dir(), download=download)
    seed.extract_stage3(tarball, rootfs)

    from shidashi.tree import pinned_repos  # local: tree imports seed, like this module

    repos = pinned_repos(
        seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
    )
    apply_rootfs(rootfs, recipe, variants_dir=variants_dir)
    apply_portage(rootfs, recipe, variants_dir=variants_dir)
    binds = bind_repos(rootfs / "etc" / "portage" / "repos.conf", pinned=repos)

    with Container(rootfs, ephemeral=not keep, binds=binds) as container:
        result = run_pretend(container)

    combined = result.stdout + result.stderr
    packages = parse_packages(combined)
    cycle_breaks = parse_cycle_breaks(combined)

    # hard-conflict: emerge failed and there are no cycle suggestions → error (R5.4).
    if result.exit_code != 0 and not cycle_breaks:
        raise ResolveError(
            f"unsatisfiable resolution for {arch}×{flavor}×{init} "
            "(a dependency conflict, not a cycle)",
            raw_output=combined,
        )

    return PretendReport(
        arch=arch,
        flavor=flavor,
        init=init,
        packages=packages,
        cycle_breaks=cycle_breaks,
        raw_output=combined,
    )
