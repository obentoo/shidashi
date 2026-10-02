"""UNIT + INTEGRATION tests of shidashi.resolve — the heart of the pretend flow.

UNIT (deterministic, non-Gentoo CI):
* ``_layer_dirs`` maps ``portage_layers`` → ``variants/<layer>/portage/`` (R3.1);
* ``apply_portage`` overlays files layer by layer in a tmp dir, later layers
  overriding earlier ones; missing layer → ResolveError (R3.1);
* ``bind_repos`` takes a ``repos.conf/`` DIRECTORY (eselect-repo style),
  collects its ``[<name>]`` stanzas (stdlib ``configparser``) and binds each
  declared repo from its pin to ``/var/db/repos/<name>`` in the container (RO),
  raising ResolveError naming a repo without a pin (R3.2, R3.3, R6.3, D26);
* ``parse_cycle_breaks`` extracts atom+flag+sign from captured emerge output,
  incl. the ``libsdl2 ↔ pipewire ↔ ffmpeg`` cycle (§18.2) (R5.2);
* ``parse_packages`` extracts the list of resolved atoms from the
  ``emerge --pretend`` output, independently of cycle parsing (R5.2);
* ``CycleBreak``/``PretendReport`` are frozen pydantic;
* ``ResolveError`` carries an optional ``raw_output`` (raw emerge on hard-conflict);
* ``pretend_resolve(..., keep=False)`` raises ResolveError BEFORE any work when
  not root (R5.1, R6.1) — ``os.geteuid`` is monkeypatched.

INTEGRATION (host-gated, R5.1/R5.3/R5.4): the real pipeline on ``v3 × minimal ×
systemd`` requires root+Gentoo → SKIPS outside the privileged host (deferred Red).

Contract (design.md §resolve, refined): Frozen pydantic
``CycleBreak(atom, flag, enable, raw_line)`` and ``PretendReport(arch, flavor,
init, packages, cycle_breaks, raw_output)``. ``bind_repos(repos_conf_dir: Path)``
(a DIRECTORY of ``*.conf``). ``parse_packages(output: str) -> tuple[str, ...]``.
``ResolveError(msg, *, raw_output: str | None = None)`` exposes ``.raw_output``.
``pretend_resolve(arch, flavor, init, *, download=True, keep=False)``.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shidashi.recipe import ResolvedRecipe
from shidashi.resolve import (
    CycleBreak,
    PretendReport,
    ResolveError,
    _layer_dirs,
    apply_portage,
    apply_rootfs,
    bind_repos,
    parse_cycle_breaks,
    parse_packages,
    pretend_resolve,
)

_NEEDS_HOST = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST, reason="requires root + systemd-nspawn + a seeded stage3 (Gentoo host)"
)

_LAYERS = ("base", "arch/v3", "flavor/minimal", "init/systemd")


def _recipe() -> ResolvedRecipe:
    # builds a minimal ResolvedRecipe with known portage_layers; only that
    # field matters for _layer_dirs/apply_portage.

    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(),
        portage_layers=_LAYERS,
    )


# --- ResolveError ------------------------------------------------------------


def test_resolve_error_is_exception_subclass() -> None:
    assert issubclass(ResolveError, Exception)


def test_resolve_error_carries_optional_raw_output() -> None:
    # raw_output is optional: absent by default (None), present when the error
    # carries the raw emerge output (hard-conflict, §Error Handling).
    plain = ResolveError("nope")
    assert getattr(plain, "raw_output", None) is None
    with_raw = ResolveError("hard conflict", raw_output="!!! conflict\n...emerge...")
    assert with_raw.raw_output == "!!! conflict\n...emerge..."


# --- _layer_dirs (R3.1) ------------------------------------------------------


def test_layer_dirs_maps_each_layer(tmp_path: Path) -> None:
    dirs = _layer_dirs(_recipe(), tmp_path)
    expected = [tmp_path / layer / "portage" for layer in _LAYERS]
    assert dirs == expected


# --- apply_portage layering (R3.1) -------------------------------------------


def _seed_layer(variants: Path, layer: str, rel: str, content: str) -> None:
    target = variants / layer / "portage" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_apply_portage_concatenates_make_conf_and_keeps_unique_files(tmp_path: Path) -> None:
    # make.conf is ONE file read by the shell and the layers carry FRAGMENTS, so
    # it is concatenated — not overwritten. Overwriting turned the base's 133-line
    # make.conf into the init's 6-line fragment (F28).
    variants = tmp_path / "variants"
    _seed_layer(variants, "base", "make.conf", "FROM_BASE")
    _seed_layer(variants, "arch/v3", "package.use/arch", "ARCH")
    _seed_layer(variants, "flavor/minimal", "package.use/flavor", "FLAVOR")
    _seed_layer(variants, "init/systemd", "make.conf", "FROM_INIT")

    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)

    apply_portage(rootfs, _recipe(), variants_dir=variants)

    portage = rootfs / "etc" / "portage"
    make_conf = (portage / "make.conf").read_text(encoding="utf-8")
    assert "FROM_BASE" in make_conf
    assert "FROM_INIT" in make_conf
    # and in layer ORDER, which is what gives the shell's "last one wins" its meaning
    assert make_conf.index("FROM_BASE") < make_conf.index("FROM_INIT")
    # the assembled file names the origin of each fragment
    assert "layer: base" in make_conf
    assert "layer: init/systemd" in make_conf

    # files unique to intermediate layers are still kept
    assert (portage / "package.use" / "arch").read_text(encoding="utf-8") == "ARCH"
    assert (portage / "package.use" / "flavor").read_text(encoding="utf-8") == "FLAVOR"


def test_apply_portage_assembled_make_conf_gives_the_last_assignment(tmp_path: Path) -> None:
    # Inside the assembled file the shell rule applies: the last assignment wins.
    # That is the specialization effect of the arch axis over the base.
    variants = tmp_path / "variants"
    _seed_layer(variants, "base", "make.conf", 'COMMON_FLAGS="-O2"\nUSE="a b"\n')
    _seed_layer(variants, "arch/v3", "make.conf", 'COMMON_FLAGS="-march=x86-64-v3 -O2"\n')
    _seed_layer(variants, "flavor/minimal", "package.use/keep", "KEEP")
    _seed_layer(variants, "init/systemd", "make.conf", 'USE="${USE} systemd"\n')

    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    apply_portage(rootfs, _recipe(), variants_dir=variants)

    make_conf = rootfs / "etc" / "portage" / "make.conf"
    out = subprocess.run(
        ["bash", "-c", f'. "{make_conf}"; printf "%s|%s" "$COMMON_FLAGS" "$USE"'],
        capture_output=True,
        text=True,
        check=True,
    )
    flags, use = out.stdout.split("|")
    assert flags == "-march=x86-64-v3 -O2"  # arch beat the base
    assert sorted(use.split()) == ["a", "b", "systemd"]  # init ADDED, did not replace


def test_apply_portage_jobs_override_is_the_last_makeopts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SHIDASHI_JOBS (factory --jobs) sets MAKEOPTS and emerge's jobs for THIS
    host, after every layer, so it wins over the base's -j32 in every phase that
    re-applies it -- and keeps whatever EMERGE_DEFAULT_OPTS a layer set."""
    variants = tmp_path / "variants"
    _seed_layer(
        variants,
        "base",
        "make.conf",
        'MAKEOPTS="-j32 -l32"\nEMERGE_DEFAULT_OPTS="--with-bdeps=y"\n',
    )
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    monkeypatch.setenv("SHIDASHI_JOBS", "16")

    apply_portage(rootfs, _recipe(), variants_dir=variants, layers=("base",))

    make_conf = rootfs / "etc" / "portage" / "make.conf"
    out = subprocess.run(
        ["bash", "-c", f'. "{make_conf}"; printf "%s|%s" "$MAKEOPTS" "$EMERGE_DEFAULT_OPTS"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == "-j16 -l16|--with-bdeps=y --jobs=16 --load-average=16"
    assert "layer: runtime (SHIDASHI_JOBS)" in make_conf.read_text(encoding="utf-8")


def test_apply_portage_leaves_the_host_jobs_out_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assembler's rootfs is the image: the build host's jobs stay out of it."""
    variants = tmp_path / "variants"
    _seed_layer(variants, "base", "make.conf", 'MAKEOPTS="-j32 -l32"\n')
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    monkeypatch.setenv("SHIDASHI_JOBS", "16")

    apply_portage(rootfs, _recipe(), variants_dir=variants, layers=("base",), host_jobs=False)

    text = (rootfs / "etc" / "portage" / "make.conf").read_text(encoding="utf-8")
    assert "SHIDASHI_JOBS" not in text and "EMERGE_DEFAULT_OPTS" not in text
    assert 'MAKEOPTS="-j32 -l32"' in text


@pytest.mark.parametrize("bad", ["0", "-3", "sixteen", "16; rm -rf /"])
def test_apply_portage_refuses_a_jobs_value_that_is_not_a_positive_integer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    variants = tmp_path / "variants"
    _seed_layer(variants, "base", "make.conf", 'MAKEOPTS="-j32"\n')
    (tmp_path / "rootfs" / "etc").mkdir(parents=True)
    monkeypatch.setenv("SHIDASHI_JOBS", bad)
    with pytest.raises(ResolveError, match="SHIDASHI_JOBS"):
        apply_portage(tmp_path / "rootfs", _recipe(), variants_dir=variants, layers=("base",))


def test_apply_portage_raises_when_two_layers_provide_the_same_file(tmp_path: Path) -> None:
    # Outside make.conf, these paths are DIRECTORIES that Portage reads as a
    # union: two layers at the same path do not combine, one erases the other. It
    # was a silent loss — package.use/system dropped from 69 lines to 4 (F28).
    variants = tmp_path / "variants"
    _seed_layer(variants, "base", "make.conf", "BASE")
    _seed_layer(variants, "base", "package.use/system", "SIXTY-NINE LINES")
    _seed_layer(variants, "arch/v3", "package.use/arch", "ARCH")
    _seed_layer(variants, "flavor/minimal", "package.use/flavor", "FLAVOR")
    _seed_layer(variants, "init/systemd", "package.use/system", "FOUR LINES")

    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)

    with pytest.raises(ResolveError) as excinfo:
        apply_portage(rootfs, _recipe(), variants_dir=variants)
    msg = str(excinfo.value)
    # the error must name BOTH layers and the path, otherwise it is not actionable
    assert "package.use/system" in msg
    assert "base" in msg
    assert "init/systemd" in msg


# --- apply_rootfs ------------------------------------------------------------


def _seed_rootfs_file(variants: Path, layer: str, rel: str, content: str) -> None:
    target = variants / layer / "rootfs" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_apply_rootfs_copies_each_layers_tree_over_the_root(tmp_path: Path) -> None:
    """A layer's rootfs/ lands at / -- the stage3's locale.gen (~500 commented
    entries) is replaced by the curated one before the bootstrap's locale-gen."""
    variants = tmp_path / "variants"
    _seed_rootfs_file(variants, "base", "etc/locale.gen", "en_US.UTF-8 UTF-8\n")
    _seed_rootfs_file(variants, "init/systemd", "etc/systemd/x.conf", "X")
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "etc" / "locale.gen").write_text("# stage3 default\n", encoding="utf-8")

    copied = apply_rootfs(rootfs, _recipe(), variants_dir=variants)

    assert (rootfs / "etc/locale.gen").read_text(encoding="utf-8") == "en_US.UTF-8 UTF-8\n"
    assert (rootfs / "etc/systemd/x.conf").read_text(encoding="utf-8") == "X"
    assert copied == ("/etc/locale.gen", "/etc/systemd/x.conf")


def test_apply_rootfs_later_layer_wins_and_missing_trees_are_skipped(tmp_path: Path) -> None:
    variants = tmp_path / "variants"
    _seed_rootfs_file(variants, "base", "etc/motd", "base")
    _seed_rootfs_file(variants, "init/systemd", "etc/motd", "init")
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()

    apply_rootfs(rootfs, _recipe(), variants_dir=variants)  # arch/flavor have no rootfs/

    assert (rootfs / "etc/motd").read_text(encoding="utf-8") == "init"


def test_apply_portage_missing_layer_raises(tmp_path: Path) -> None:
    variants = tmp_path / "variants"
    # only base exists; arch/v3 missing → ResolveError
    _seed_layer(variants, "base", "make.conf", "X")
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    with pytest.raises(ResolveError):
        apply_portage(rootfs, _recipe(), variants_dir=variants)


# --- bind_repos (R3.2, R3.3, R6.3, D26) -------------------------------------
#
# bind_repos takes a repos.conf/ DIRECTORY (eselect-repo style), collects the
# [<name>] stanzas and binds each declared repo from its PIN to
# /var/db/repos/<name> in the container. The host's /var/db/repos is never read.

_ESELECT_REPO_CONF = """\
[gentoo]
location = /var/db/repos/gentoo

[bentoo]
location = /var/db/repos/bentoo
"""


def _write_repos_conf_dir(tmp_path: Path) -> Path:
    """Create a repos.conf/ directory with a two-stanza eselect-repo.conf."""
    repos_conf_dir = tmp_path / "repos.conf"
    repos_conf_dir.mkdir()
    (repos_conf_dir / "eselect-repo.conf").write_text(_ESELECT_REPO_CONF, encoding="utf-8")
    return repos_conf_dir


def _pins(tmp_path: Path, *names: str) -> dict[str, Path]:
    pins = {name: tmp_path / "cache" / "repos" / name for name in names}
    for path in pins.values():
        path.mkdir(parents=True)
    return pins


def test_bind_repos_binds_each_pin_at_the_repos_usual_path(tmp_path: Path) -> None:
    pins = _pins(tmp_path, "gentoo", "bentoo")

    pairs = bind_repos(_write_repos_conf_dir(tmp_path), pinned=pins)

    assert pairs == [
        (pins["gentoo"], Path("/var/db/repos/gentoo")),
        (pins["bentoo"], Path("/var/db/repos/bentoo")),
    ]


def test_bind_repos_unpinned_repo_raises_naming_it(tmp_path: Path) -> None:
    # bentoo is declared but has no pin: refused, never taken from the host
    with pytest.raises(ResolveError) as excinfo:
        bind_repos(_write_repos_conf_dir(tmp_path), pinned=_pins(tmp_path, "gentoo"))
    assert "bentoo" in str(excinfo.value)
    assert "seeds/overlays.toml" in str(excinfo.value)


def test_bind_repos_missing_pin_dir_raises(tmp_path: Path) -> None:
    pins = _pins(tmp_path, "gentoo")
    pins["bentoo"] = tmp_path / "nowhere"
    with pytest.raises(ResolveError, match="bentoo"):
        bind_repos(_write_repos_conf_dir(tmp_path), pinned=pins)


# --- parse_cycle_breaks (R5.2) — core of the §18.2 curation ------------------

# Fixture inspired by real emerge output reporting circular dependencies with
# "change USE" suggestions. Contains the libsdl2 ↔ pipewire ↔ ffmpeg cycle.
_EMERGE_CYCLE = """\
These are the packages that would be merged, in order:

Calculating dependencies... done!

!!! Multiple package instances within a single package slot have been pulled
!!! into the dependency graph, resulting in a slot conflict:

  circular dependencies:

    (media-libs/libsdl2-2.30.5:0/0::gentoo, ebuild scheduled for merge) depends on
     (media-video/pipewire-1.2.1:0/0.4::gentoo, ebuild scheduled for merge) (buildtime)
      (media-video/ffmpeg-6.1.1:0/58.60.60::gentoo, ebuild scheduled for merge) (buildtime)
       (media-libs/libsdl2-2.30.5:0/0::gentoo, ebuild scheduled for merge) (buildtime)

   It might be possible to break this cycle
   by applying any of the following changes:
   - media-libs/libsdl2-2.30.5 (Change USE: -pipewire)
   - media-video/ffmpeg-6.1.1 (Change USE: +sdl)
"""

# Fixture of "clean" emerge --pretend output: the list of resolved packages,
# one [ebuild ...] line per atom, no cycles. Captures the real format of
# emerge --pretend --emptytree @world.
_EMERGE_PACKAGES = """\
These are the packages that would be merged, in order:

Calculating dependencies... done!

[ebuild  N     ] sys-libs/zlib-1.3.1:0/1::gentoo  USE="minizip" 0 KiB
[ebuild  N     ] dev-libs/openssl-3.3.1:0/3::gentoo  USE="asm" 15123 KiB
[ebuild  N     ] sys-apps/portage-3.0.66.1::gentoo  0 KiB

Total: 3 packages (3 new), Size of downloads: 15138 KiB
"""


def test_parse_cycle_breaks_extracts_atom_flag_sign() -> None:
    breaks = parse_cycle_breaks(_EMERGE_CYCLE)
    assert isinstance(breaks, tuple)
    assert len(breaks) == 2
    by_atom = {b.atom: b for b in breaks}
    # "-pipewire" → disable
    sdl = by_atom["media-libs/libsdl2-2.30.5"]
    assert sdl.flag == "pipewire"
    assert sdl.enable is False
    # "+sdl" → enable
    ff = by_atom["media-video/ffmpeg-6.1.1"]
    assert ff.flag == "sdl"
    assert ff.enable is True


def test_parse_cycle_breaks_empty_on_clean_output() -> None:
    assert parse_cycle_breaks(_EMERGE_PACKAGES) == ()


# --- parse_packages (R5.2) — list of resolved atoms, independent of cycles


def test_parse_packages_extracts_resolved_atom_list() -> None:
    pkgs = parse_packages(_EMERGE_PACKAGES)
    assert isinstance(pkgs, tuple)
    # extracts the atoms of the [ebuild ...] lines, independently of cycle parsing
    assert pkgs == (
        "sys-libs/zlib-1.3.1",
        "dev-libs/openssl-3.3.1",
        "sys-apps/portage-3.0.66.1",
    )


def test_parse_packages_empty_when_no_ebuild_lines() -> None:
    # output without [ebuild ...] lines → empty list (does not raise)
    assert parse_packages("Calculating dependencies... done!\n") == ()


# --- frozen models -----------------------------------------------------------


def test_cycle_break_is_frozen() -> None:
    cb = CycleBreak(atom="x/y-1", flag="foo", enable=True, raw_line="raw")
    with pytest.raises(Exception):  # noqa: B017
        cb.flag = "bar"


def test_pretend_report_holds_packages_and_breaks() -> None:
    cb = CycleBreak(atom="x/y-1", flag="foo", enable=False, raw_line="raw")
    rep = PretendReport(
        arch="v3",
        flavor="minimal",
        init="systemd",
        packages=("x/y-1", "a/b-2"),
        cycle_breaks=(cb,),
        raw_output="...",
    )
    assert rep.packages == ("x/y-1", "a/b-2")
    assert rep.cycle_breaks[0].flag == "foo"


# --- non-root guard (R5.1, R6.1) — fails before any work --------------------


def test_pretend_resolve_non_root_raises_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(ResolveError) as excinfo:
        pretend_resolve("v3", "minimal", "systemd", keep=False)
    # the actionable message mentions root
    assert "root" in str(excinfo.value).lower()


# --- host-gated INTEGRATION (R5.1, R5.3, R5.4) -------------------------------


@_skip_privileged
def test_pretend_resolve_returns_nonempty_package_list() -> None:
    report = pretend_resolve("v3", "minimal", "systemd")
    assert isinstance(report, PretendReport)
    assert len(report.packages) > 0
